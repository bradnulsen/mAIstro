"""Claude CLI invocation — Popen + thread reader for Windows compatibility.

Pipes prompt and system prompt via stdin to avoid Windows .cmd arg
quoting issues with multi-line strings. Reads both stdout and stderr
(CLI writes NDJSON to stderr on --resume).

Uses subprocess.Popen + thread readers because asyncio.create_subprocess_exec
raises NotImplementedError on Windows ProactorEventLoop.

Event schema (yielded dicts):
    {"type": "text",       "content": "..."}
    {"type": "tool_use",   "tool": "...", "input": {...}}
    {"type": "session_id", "cli_session_id": "..."}
    {"type": "error",      "message": "..."}
"""

import asyncio
import json
import logging
import shutil
import subprocess
import threading
from typing import AsyncIterator

log = logging.getLogger("maistro.cli")


async def invoke(
    prompt: str,
    system_prompt: str,
    cwd: str,
    model: str = "sonnet",
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    max_turns: int = 50,
    resume_session: str | None = None,
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

    log.info("[cli] Spawning: model=%s max_turns=%d prompt=<%d chars>", model, max_turns, len(prompt))
    log.info("[cli] Tools: allowed=%s disallowed=%s", allowed_tools or "all", disallowed_tools or "none")
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
    loop = asyncio.get_event_loop()

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
            line = await queue.get()
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

            event = _translate_event(data)
            if event:
                yield event

        process.wait()
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
        yield {"type": "error", "message": str(e)}


def _translate_event(data: dict) -> dict | None:
    """Translate a single NDJSON event from Claude CLI into our event schema."""
    msg_type = data.get("type", "")

    # Streaming deltas (real-time text chunks)
    if msg_type == "stream_event":
        event = data.get("event", {})
        if event.get("type") == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if text:
                    return {"type": "text", "content": text}
        return None

    # Bare content_block_delta (some CLI versions)
    if msg_type == "content_block_delta":
        delta = data.get("delta", {})
        if delta.get("type") == "text_delta":
            return {"type": "text", "content": delta.get("text", "")}
        return None

    # Full assistant turn
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
            return {"type": "text", "content": "\n\n".join(text_parts)}
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
