"""Claude CLI invocation — single function for dispatch and chat."""

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
    """Invoke Claude CLI as subprocess, yield events as dicts.

    Uses subprocess.Popen + thread reader to avoid Windows
    ProactorEventLoop NotImplementedError with asyncio subprocess.

    Yields dicts with 'type' key:
        - text: {"type": "text", "content": "..."}
        - tool_use: {"type": "tool_use", "tool": "...", "input": {...}}
        - session_id: {"type": "session_id", "cli_session_id": "..."}
        - error: {"type": "error", "message": "..."}
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        log.error("Claude CLI not found in PATH")
        yield {"type": "error", "message": "Claude CLI not found in PATH"}
        return

    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", "stream-json",
        "--model", model,
        "--system-prompt", system_prompt,
        "--max-turns", str(max_turns),
        "--permission-mode", "bypassPermissions",
    ]

    if resume_session:
        cmd.extend(["--resume", resume_session])

    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    if disallowed_tools:
        cmd.extend(["--disallowedTools", ",".join(disallowed_tools)])

    log.info(f"[cli] Spawning: model={model} max_turns={max_turns} prompt=<{len(prompt)} chars>")
    log.info(f"[cli] Tools: allowed={allowed_tools or 'all'} disallowed={disallowed_tools or 'none'}")
    log.info(f"[cli] CWD: {cwd}")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )

    queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _reader():
        try:
            for raw_line in process.stdout:
                line = raw_line.decode("utf-8").strip()
                if line:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, f"__ERROR__:{e}")
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    try:
        line_count = 0
        while True:
            line = await queue.get()
            if line is None:
                break
            if line.startswith("__ERROR__:"):
                log.error(f"[cli] Reader error: {line}")
                yield {"type": "error", "message": line[10:]}
                break

            line_count += 1
            for event in _parse_ndjson_line(line, line_count):
                yield event

        process.wait()
        log.info(f"[cli] Process exited: code={process.returncode} lines={line_count}")
        if process.returncode != 0:
            stderr = process.stderr.read().decode("utf-8").strip()
            err_msg = stderr or f"Claude CLI exited with code {process.returncode}"
            log.error(f"[cli] stderr: {err_msg[:500]}")
            yield {"type": "error", "message": err_msg}
    except Exception as e:
        log.exception(f"[cli] Exception during streaming: {e}")
        process.kill()
        yield {"type": "error", "message": str(e)}


def _parse_ndjson_line(line: str, line_num: int) -> list[dict]:
    """Parse a single NDJSON line from Claude CLI into event dicts."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        log.warning(f"[cli] Non-JSON line #{line_num}: {line[:200]}")
        return [{"type": "text", "content": line}]

    etype = event.get("type", "unknown")
    log.debug(f"[cli] NDJSON #{line_num}: type={etype}")
    results = []

    if etype == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text":
                results.append({"type": "text", "content": block["text"]})
            elif block.get("type") == "tool_use":
                results.append({
                    "type": "tool_use",
                    "tool": block.get("name", ""),
                    "input": block.get("input", {}),
                })

    elif etype == "result":
        # Extract session ID for resumption
        sid = event.get("session_id")
        if sid:
            results.append({"type": "session_id", "cli_session_id": sid})
        # Extract text content
        for block in event.get("content", []):
            if block.get("type") == "text":
                results.append({"type": "text", "content": block["text"]})

    elif etype == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            results.append({"type": "text", "content": delta["text"]})

    else:
        log.info(f"[cli] Unhandled event type: {etype} — {str(event)[:200]}")

    return results
