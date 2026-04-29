"""Claude CLI invocation — Popen + thread reader for Windows compatibility.

Pipes prompt and system prompt via stdin to avoid Windows .cmd arg
quoting issues with multi-line strings. Reads both stdout and stderr
(CLI writes NDJSON to stderr on --resume).

Uses subprocess.Popen + thread readers because asyncio.create_subprocess_exec
raises NotImplementedError on Windows ProactorEventLoop.

Event schema: see backend/events.py for the canonical definition of all
event types, their shapes, and constructor functions.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import AsyncIterator

from backend import events

log = logging.getLogger("maistro.cli")



def _terminate_tree(proc: subprocess.Popen):
    """Terminate a process and its children, then wait for exit.

    On Windows, Popen.terminate() only kills the immediate process — child
    processes (MCP servers, node workers) survive as orphans.  Use taskkill /T
    to kill the entire tree.  Falls back to terminate+kill if taskkill isn't
    available.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
            proc.wait(timeout=5)
            return
        except Exception:
            pass
    # Fallback: terminate, grace period, kill
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("[cli] Process %d resisted kill — giving up", proc.pid)

# Known Claude CLI native tools — used by the dispatch layer to compute the
# complement of a job's allowed_tools (i.e. what to pass as --disallowedTools).
CLI_NATIVE_TOOLS = frozenset([
    "Read", "Edit", "Write", "Bash", "Glob", "Grep",
    "Agent", "TodoWrite", "NotebookEdit",
    "WebFetch", "WebSearch",
])


def _resolve_claude_cmd() -> list[str] | None:
    """Resolve the Claude CLI to a direct executable, bypassing .CMD wrappers.

    On Windows, npm installs a .CMD batch wrapper that spawns cmd.exe as an
    intermediary.  This breaks process lifecycle: terminate() kills cmd.exe
    but orphans the real node.exe process (and its MCP server children),
    leaving pipes open and the task stuck.  We parse the .CMD to extract the
    underlying node + script path and invoke that directly.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return None

    if not claude_bin.lower().endswith(".cmd"):
        return [claude_bin]

    # Parse npm .CMD wrapper to find the script path
    import re
    try:
        with open(claude_bin, "r") as f:
            content = f.read()
    except OSError:
        return [claude_bin]

    # npm wrappers end with: "%_prog%" "%dp0%\node_modules\...\cli.js" %*
    # Extract the script path relative to the .CMD's directory
    match = re.search(r'"%dp0%\\([^"]+\.js)"', content)
    if not match:
        return [claude_bin]

    cmd_dir = os.path.dirname(claude_bin)
    script_path = os.path.join(cmd_dir, match.group(1))
    if not os.path.isfile(script_path):
        return [claude_bin]

    # Resolve node: prefer local node.exe next to .CMD, fall back to PATH
    local_node = os.path.join(cmd_dir, "node.exe")
    node = local_node if os.path.isfile(local_node) else (shutil.which("node") or "node")

    log.info("[cli] Bypassing .CMD wrapper: %s %s", node, script_path)
    return [node, script_path]


async def invoke(
    prompt: str,
    system_prompt: str,
    cwd: str,
    model: str = "sonnet",
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    mcp_config_path: str | None = None,
    max_turns: int = 50,
    resume_session: str | None = None,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[dict]:
    """Invoke Claude CLI as subprocess, yield events as dicts."""
    claude_cmd = _resolve_claude_cmd()
    if not claude_cmd:
        log.error("Claude CLI not found in PATH")
        yield events.error("Claude CLI not found in PATH")
        return

    # Prompt and system prompt piped via stdin —
    # only bare flags and simple string args on the command line.
    cmd = [
        *claude_cmd,
        "-p",
        "--output-format", "stream-json",
        "--model", model,
        "--max-turns", str(max_turns),
        "--verbose",
    ]

    if resume_session:
        cmd.extend(["--resume", resume_session])

    if allowed_tools:
        cmd.append("--allowedTools")
        cmd.extend(allowed_tools)
    if disallowed_tools:
        cmd.append("--disallowedTools")
        cmd.extend(disallowed_tools)
    if mcp_config_path:
        cmd.extend(["--mcp-config", mcp_config_path])

    log.info("[cli] Spawning: model=%s max_turns=%d prompt=<%d chars>", model, max_turns, len(prompt))
    log.info("[cli] Tools: allowed=%s disallowed=%s mcp=%s", allowed_tools or "all", disallowed_tools or "none", mcp_config_path or "none")
    log.info("[cli] CWD: %s", cwd)

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )

    # Build stdin: system prompt (for fresh sessions) + user prompt
    stdin_parts = []
    if system_prompt and not resume_session:
        stdin_parts.append(
            f"<system-instructions>\n{system_prompt}\n</system-instructions>\n\n"
        )
    stdin_parts.append(prompt)

    process.stdin.write("".join(stdin_parts).encode("utf-8"))
    process.stdin.close()

    # Read stdout and stderr via threads, feed into async queue
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _reader(pipe, label):
        try:
            for raw_line in pipe:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, f"__ERROR__:{label}:{e}")
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    t_out = threading.Thread(target=_reader, args=(process.stdout, "stdout"), daemon=True)
    t_err = threading.Thread(target=_reader, args=(process.stderr, "stderr"), daemon=True)
    t_out.start()
    t_err.start()

    try:
        non_json_lines = []
        pipes_done = 0
        _got_result = False
        _process_dead_since = None  # monotonic time when we first saw process exit

        while pipes_done < 2:
            # Check for cancellation
            if cancel_event and cancel_event.is_set():
                log.info("[cli] Cancellation requested — terminating process")
                _terminate_tree(process)
                yield events.error("cancelled")
                return

            # Detect process exit independently of pipe EOF.
            # Child processes (MCP servers) can hold pipe handles open long
            # after the CLI exits, preventing reader threads from seeing EOF.
            if _process_dead_since is None and process.poll() is not None:
                _process_dead_since = asyncio.get_running_loop().time()
                log.info("[cli] Process exited (code=%d) — draining", process.returncode)

            # Three drain strategies, from fastest to slowest:
            #  1. Got result event → non-blocking drain, break immediately
            #  2. Process dead → drain with deadline (5s grace period)
            #  3. Normal → block with 1s timeout
            if _got_result:
                try:
                    line = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            elif _process_dead_since is not None:
                elapsed = asyncio.get_running_loop().time() - _process_dead_since
                if elapsed > 5.0:
                    log.warning("[cli] Drain deadline exceeded — breaking out")
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
            else:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    log.debug("[cli] Tick: poll=%s pipes_done=%d got_result=%s qsize=%d",
                              process.poll(), pipes_done, _got_result, queue.qsize())
                    continue

            if line is None:
                pipes_done += 1
                continue
            if isinstance(line, str) and line.startswith("__ERROR__:"):
                log.error("[cli] Reader error: %s", line)
                yield events.error(line[10:])
                continue

            log.debug("[cli] output: %s", line[:200])

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                non_json_lines.append(line)
                continue

            raw_type = data.get("type", "")
            log.debug("[cli] Event: %s", raw_type)
            yield events.raw(line, raw_type)

            # The `result` event is the CLI's "I'm done" signal — always
            # the last semantic event.  Mark it so we drain and exit.
            if raw_type == "result":
                _got_result = True
                log.info("[cli] Received result event — draining")

            for event in _translate_event(data):
                yield event

        log.info("[cli] Loop exited: pipes_done=%d got_result=%s",
                 pipes_done, _got_result)
        # Clean up the process — give it a moment, then force-terminate.
        if process.poll() is None:
            try:
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, process.wait),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                log.warning("[cli] Process still alive after result — terminating")
                _terminate_tree(process)
        log.info("[cli] Process exited: code=%d", process.returncode)

        if process.returncode not in (0, None) and not _got_result:
            error_detail = "\n".join(non_json_lines)
            log.error("[cli] exit=%d detail=%s", process.returncode, error_detail[:500])
            yield events.error(
                f"Process exited with code {process.returncode}"
                + (f": {error_detail}" if error_detail else "")
            )

    except Exception as e:
        log.exception("[cli] Exception during streaming: %s", e)
        yield events.error(str(e))

    finally:
        # Ensure process is always reaped — covers GeneratorExit, CancelledError,
        # and any BaseException subclass that bypasses the except clause above.
        if process.poll() is None:
            log.warning("[cli] Process still alive in finally — terminating tree")
            _terminate_tree(process)
        # Join reader threads to prevent call_soon_threadsafe on a closed loop.
        t_out.join(timeout=5)
        t_err.join(timeout=5)


def _translate_event(data: dict) -> list[dict]:
    """Translate a single NDJSON event from Claude CLI into our event schema.

    Returns a list of events — an assistant turn can contain multiple content
    blocks (thinking, text, tool_use) and we surface all of them.
    """
    msg_type = data.get("type", "")

    # Streaming deltas (real-time text chunks, thinking, and tool use starts)
    # These fire when --include-partial-messages is enabled.
    if msg_type == "stream_event":
        event = data.get("event", {})
        etype = event.get("type", "")
        if etype == "content_block_delta":
            delta = event.get("delta", {})
            dtype = delta.get("type", "")
            if dtype == "text_delta":
                t = delta.get("text", "")
                if t:
                    return [events.text(t)]
            elif dtype == "thinking_delta":
                t = delta.get("thinking", "")
                if t:
                    return [events.thinking(t)]
        elif etype == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                return [events.tool_use(block.get("name", ""), {})]
        return []

    # Bare content_block_delta (some CLI versions)
    if msg_type == "content_block_delta":
        delta = data.get("delta", {})
        dtype = delta.get("type", "")
        if dtype == "text_delta":
            return [events.text(delta.get("text", ""))]
        elif dtype == "thinking_delta":
            return [events.thinking(delta.get("thinking", ""))]
        return []

    # Full assistant turn — extract ALL content blocks: thinking, text, tool_use.
    # Text that precedes a tool_use is "preamble" — emitted for live display only
    # (not for DB accumulation).  Only trailing text (after all tool_uses, or in a
    # text-only turn) becomes assistant_complete for storage.
    if msg_type == "assistant":
        blocks = (
            data.get("message", {}).get("content", [])
            or data.get("content", [])
        )
        has_tool_use = any(b.get("type") == "tool_use" for b in blocks)
        result = []
        text_parts = []
        for block in blocks:
            btype = block.get("type")
            if btype == "thinking":
                t = block.get("thinking", "")
                if t:
                    result.append(events.thinking(t))
            elif btype == "text":
                text_parts.append(block["text"])
            elif btype == "tool_use":
                if text_parts:
                    result.append(events.text("\n\n".join(text_parts)))
                    text_parts = []
                result.append(events.tool_use(block.get("name", ""), block.get("input", {})))
        if text_parts:
            joined = "\n\n".join(text_parts)
            if has_tool_use:
                result.append(events.text(joined))
            else:
                result.append(events.assistant_complete(joined))
        return result

    # Result (final summary) — may carry session_id AND execution metadata.
    if msg_type == "result":
        content = data.get("result", "")
        is_error = data.get("is_error", False)
        sid = data.get("session_id", "")

        if is_error:
            subtype = data.get("subtype", "")
            if not content:
                content = subtype or "unknown error"
            # Derive stop_reason from subtype so the worker can route max-turns
            # exhaustion to the `exhausted` terminal state instead of silently
            # treating it as `completed`. Other error subtypes pass through for
            # diagnostics.
            stop_reason = "max_turns" if subtype == "error_max_turns" else (subtype or "error")
            return [
                events.error(content),
                events.result_meta(
                    cli_session_id=sid or None,
                    stop_reason=stop_reason,
                    num_turns=data.get("num_turns"),
                    cost_usd=data.get("total_cost_usd"),
                ),
            ]

        return [events.result_meta(
            cli_session_id=sid or None,
            stop_reason=data.get("stop_reason"),
            num_turns=data.get("num_turns"),
            cost_usd=data.get("total_cost_usd"),
        )]

    # System/init events — surface session_id early
    if msg_type in ("system", "init"):
        sid = data.get("session_id", "")
        if sid:
            return [events.session_id(sid)]
        return []

    log.debug("[cli] Unhandled event type: %s — %s", msg_type, str(data)[:200])
    return []
