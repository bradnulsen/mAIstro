"""Claude CLI invocation — Popen + thread reader for Windows compatibility.

Pipes prompt and system prompt via stdin to avoid Windows .cmd arg
quoting issues with multi-line strings. Reads both stdout and stderr
(CLI writes NDJSON to stderr on --resume).

Uses subprocess.Popen + thread readers because asyncio.create_subprocess_exec
raises NotImplementedError on Windows ProactorEventLoop.

Event schema (yielded dicts):
    {"type": "_raw",              "raw_json": "...", "event_type": "..."}
    {"type": "text",              "content": "..."}
    {"type": "thinking",          "content": "..."}
    {"type": "tool_use",          "tool": "...", "input": {...}}
    {"type": "assistant_complete","content": "..."}
    {"type": "session_id",        "cli_session_id": "..."}
    {"type": "error",             "message": "..."}
"""

import asyncio
import json
import logging
import shutil
import subprocess
import threading
from typing import AsyncIterator

log = logging.getLogger("maistro.cli")

# Known Claude CLI native tools — used by the dispatch layer to compute the
# complement of a job's allowed_tools (i.e. what to pass as --disallowedTools).
CLI_NATIVE_TOOLS = frozenset([
    "Read", "Edit", "Write", "Bash", "Glob", "Grep",
    "Agent", "TodoWrite", "NotebookEdit",
    "WebFetch", "WebSearch",
])


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
    claude_bin = shutil.which("claude")
    if not claude_bin:
        log.error("Claude CLI not found in PATH")
        yield {"type": "error", "message": "Claude CLI not found in PATH"}
        return

    # Prompt and system prompt piped via stdin —
    # only bare flags and simple string args on the command line.
    cmd = [
        claude_bin,
        "-p",
        "--output-format", "stream-json",
        "--model", model,
        "--max-turns", str(max_turns),
        "--verbose",
        "--dangerously-skip-permissions",
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

        while pipes_done < 2:
            # Check for cancellation
            if cancel_event and cancel_event.is_set():
                log.info("[cli] Cancellation requested — terminating process")
                process.terminate()
                # Grace period: wait up to 5s for clean exit, then force kill
                try:
                    await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(None, process.wait),
                        timeout=5.0
                    )
                    log.info("[cli] Process terminated gracefully (code=%d)", process.returncode)
                except asyncio.TimeoutError:
                    log.warning("[cli] Process did not exit after terminate — killing")
                    process.kill()
                    await asyncio.get_running_loop().run_in_executor(None, process.wait)
                yield {"type": "error", "message": "cancelled"}
                return

            try:
                line = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if line is None:
                pipes_done += 1
                continue
            if isinstance(line, str) and line.startswith("__ERROR__:"):
                log.error("[cli] Reader error: %s", line)
                yield {"type": "error", "message": line[10:]}
                continue

            log.debug("[cli] output: %s", line[:200])

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                non_json_lines.append(line)
                continue

            # Always yield raw event for storage
            raw_type = data.get("type", "")
            yield {"type": "_raw", "raw_json": line, "event_type": raw_type}

            event = _translate_event(data)
            if event:
                yield event

        await asyncio.get_running_loop().run_in_executor(None, process.wait)
        log.info("[cli] Process exited: code=%d", process.returncode)

        if process.returncode != 0:
            error_detail = "\n".join(non_json_lines)
            log.error("[cli] exit=%d detail=%s", process.returncode, error_detail[:500])
            yield {
                "type": "error",
                "message": f"Process exited with code {process.returncode}"
                + (f": {error_detail}" if error_detail else ""),
            }

    except Exception as e:
        log.exception("[cli] Exception during streaming: %s", e)
        process.kill()
        process.wait()
        yield {"type": "error", "message": str(e)}


def _translate_event(data: dict) -> dict | None:
    """Translate a single NDJSON event from Claude CLI into our event schema."""
    msg_type = data.get("type", "")

    # Streaming deltas (real-time text chunks, thinking, and tool use starts)
    if msg_type == "stream_event":
        event = data.get("event", {})
        etype = event.get("type", "")
        if etype == "content_block_delta":
            delta = event.get("delta", {})
            dtype = delta.get("type", "")
            if dtype == "text_delta":
                text = delta.get("text", "")
                if text:
                    return {"type": "text", "content": text}
            elif dtype == "thinking_delta":
                thinking = delta.get("thinking", "")
                if thinking:
                    return {"type": "thinking", "content": thinking}
        elif etype == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                return {"type": "tool_use", "tool": block.get("name", ""), "input": {}}
            elif block.get("type") == "thinking":
                return {"type": "thinking", "content": ""}
        return None

    # Bare content_block_delta (some CLI versions)
    if msg_type == "content_block_delta":
        delta = data.get("delta", {})
        dtype = delta.get("type", "")
        if dtype == "text_delta":
            return {"type": "text", "content": delta.get("text", "")}
        elif dtype == "thinking_delta":
            return {"type": "thinking", "content": delta.get("thinking", "")}
        return None

    # Full assistant turn — used for DB storage, not streaming
    if msg_type == "assistant":
        blocks = (
            data.get("message", {}).get("content", [])
            or data.get("content", [])
        )
        text_parts = []
        for block in blocks:
            if block.get("type") == "text":
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                return {
                    "type": "tool_use",
                    "tool": block.get("name", ""),
                    "input": block.get("input", {}),
                }
        if text_parts:
            return {"type": "assistant_complete", "content": "\n\n".join(text_parts)}
        return None

    # Result (final summary)
    if msg_type == "result":
        content = data.get("result", "")
        is_error = data.get("is_error", False)
        sid = data.get("session_id", "")

        if is_error:
            if not content:
                subtype = data.get("subtype", "unknown error")
                content = subtype
            return {"type": "error", "message": content}

        if sid:
            return {"type": "session_id", "cli_session_id": sid}
        return None

    # System/init events — surface session_id early
    if msg_type in ("system", "init"):
        sid = data.get("session_id", "")
        if sid:
            return {"type": "session_id", "cli_session_id": sid}
        return None

    log.debug("[cli] Unhandled event type: %s — %s", msg_type, str(data)[:200])
    return None
