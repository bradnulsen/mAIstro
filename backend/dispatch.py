"""Dispatch engine — builds prompts, invokes Claude CLI, streams results."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from backend import database as db

log = logging.getLogger("maistro.dispatch")


async def run_dispatch(
    dispatch_id: int,
    agent: dict,
    project_dir: str,
    instructions: str | None = None,
) -> AsyncIterator[dict]:
    """Execute a dispatch: build prompt, invoke Claude CLI, yield SSE events."""
    props = agent["properties"]
    agent_id = agent["id"]

    log.info(f"[dispatch:{dispatch_id}] Starting agent={agent_id} instructions={instructions!r:.80}")

    # Mark running
    await db.update_dispatch(dispatch_id, started_at=_now())
    await db.set_agent_running(agent_id, True)

    yield _sse("dispatch", {"status": "started", "agent_id": agent_id, "dispatch_id": dispatch_id})

    try:
        # Build queue context for agent-queued dispatches
        queue_context = await _build_queue_context(dispatch_id, project_dir)

        system_prompt = build_system_prompt(agent, project_dir)
        user_prompt = build_user_prompt(agent, project_dir, instructions, queue_context)

        log.info(f"[dispatch:{dispatch_id}] System prompt: {len(system_prompt)} chars, User prompt: {len(user_prompt)} chars")
        log.debug(f"[dispatch:{dispatch_id}] System prompt:\n{system_prompt[:500]}")
        log.debug(f"[dispatch:{dispatch_id}] User prompt:\n{user_prompt[:500]}")

        full_text = []
        async for event in invoke_claude_cli(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            project_dir=project_dir,
            model=props.get("model", "sonnet"),
            allowed_tools=props.get("base_tools", []),
        ):
            if event["type"] == "text":
                full_text.append(event["content"])
            elif event["type"] == "error":
                log.error(f"[dispatch:{dispatch_id}] CLI error: {event.get('message', '')[:200]}")
            elif event["type"] == "tool_use":
                log.info(f"[dispatch:{dispatch_id}] Tool use: {event.get('tool', '?')}")
            yield event

        # Commit any file changes the agent made
        commit_hash = await commit_agent_changes(agent, project_dir)
        if commit_hash:
            log.info(f"[dispatch:{dispatch_id}] Committed: {commit_hash[:8]}")
            await db.update_dispatch(dispatch_id, completed_at=_now(), result_commit=commit_hash)
        else:
            log.info(f"[dispatch:{dispatch_id}] No file changes to commit")
            await db.update_dispatch(dispatch_id, completed_at=_now())

        yield _sse("result", {
            "status": "completed",
            "agent_id": agent_id,
            "dispatch_id": dispatch_id,
            "commit": commit_hash,
        })
        log.info(f"[dispatch:{dispatch_id}] Completed")

    except Exception as e:
        log.exception(f"[dispatch:{dispatch_id}] Failed: {e}")
        await db.update_dispatch(dispatch_id, completed_at=_now(), error=str(e))
        yield _sse("error", {"message": str(e), "agent_id": agent_id, "dispatch_id": dispatch_id})
    finally:
        await db.set_agent_running(agent_id, False)


def build_system_prompt(agent: dict, project_dir: str) -> str:
    props = agent["properties"]
    name = agent["name"]
    persona = props.get("persona", "")

    sections = [
        f"You are {name}, an agent in mAistro.",
    ]
    if persona:
        sections.append(f"Your role:\n{persona}")

    # Artifact manifest — tell agent about all agents' artifacts
    # This is built dynamically at dispatch time
    sections.append("## Artifact Manifest")
    sections.append("Other agents and their output artifacts are listed in the user prompt context.")

    # Output artifact ownership
    outputs = props.get("output_artifacts", [])
    if outputs:
        sections.append(f"## Your Output Artifacts\nYou own and maintain files matching: {', '.join(outputs)}")
        sections.append("Update these files to reflect your current understanding. Keep them current.")

    # Summary requirement
    sections.append(
        "## Summary Requirement\n"
        "You must update your summary.md with your current perspective on the project. "
        "This is your executive summary — other agents and humans read it to understand your view."
    )

    # Working directory
    sections.append(f"## Working Directory\n{project_dir}")

    return "\n\n".join(sections)


def build_user_prompt(agent: dict, project_dir: str, instructions: str | None = None,
                      queue_context: str | None = None) -> str:
    props = agent["properties"]
    sections = []

    # Queue context — bootstrapped from the agent that queued this one
    if queue_context:
        sections.append(queue_context)

    # Artifact manifest (all agents' declarations)
    manifest = _build_artifact_manifest_sync(project_dir)
    if manifest:
        sections.append(f"## Artifact Manifest\n{manifest}")

    # Output artifacts — agent's own state
    output_globs = props.get("output_artifacts", [])
    if output_globs:
        output_contents = _read_glob_contents(project_dir, output_globs)
        if output_contents:
            sections.append(f"## Your Output Artifacts (current state)\n{output_contents}")

    # Input artifacts — context from other agents
    input_globs = props.get("input_artifacts", [])
    if input_globs:
        input_contents = _read_glob_contents(project_dir, input_globs)
        if input_contents:
            sections.append(f"## Input Artifacts (context)\n{input_contents}")

    # Recent git activity
    recent = _get_recent_git_log(project_dir, limit=20)
    if recent:
        sections.append(f"## Recent Activity\n```\n{recent}\n```")

    # Instructions
    if instructions:
        sections.append(f"## Instructions\n{instructions}")
    else:
        sections.append(
            "## Task\n"
            "Review your input artifacts and the recent activity. "
            "Do your work, update your output artifacts, and update your summary.md."
        )

    return "\n\n".join(sections)


async def invoke_claude_cli(
    system_prompt: str,
    user_prompt: str,
    project_dir: str,
    model: str = "sonnet",
    allowed_tools: list[str] | None = None,
) -> AsyncIterator[dict]:
    """Invoke Claude CLI as subprocess, yield NDJSON events as SSE-shaped dicts.

    Uses subprocess.Popen + thread reader instead of asyncio subprocess
    to avoid Windows ProactorEventLoop NotImplementedError.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        log.error("Claude CLI not found in PATH")
        yield _sse("error", {"message": "Claude CLI not found in PATH"})
        return

    cmd = [
        claude_bin,
        "-p", user_prompt,
        "--output-format", "stream-json",
        "--model", model,
        "--system-prompt", system_prompt,
        "--max-turns", "50",
    ]

    if allowed_tools:
        for tool in allowed_tools:
            cmd.extend(["--allowedTools", tool])

    log.info(f"[cli] Spawning: {claude_bin} -p <{len(user_prompt)} chars> --model {model} --max-turns 50")
    log.info(f"[cli] Tools: {allowed_tools}")
    log.info(f"[cli] CWD: {project_dir}")

    # Use Popen + thread to avoid Windows asyncio subprocess issues
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=project_dir,
    )

    # Read stdout lines in a thread, push to an asyncio queue
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
            loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

    import threading
    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    try:
        line_count = 0
        while True:
            line = await queue.get()
            if line is None:
                break  # stdout closed
            if line.startswith("__ERROR__:"):
                log.error(f"[cli] Reader error: {line}")
                yield _sse("error", {"message": line[10:]})
                break

            line_count += 1
            try:
                event = json.loads(line)
                etype = event.get("type", "unknown")
                log.debug(f"[cli] NDJSON #{line_count}: type={etype} keys={list(event.keys())}")

                if etype == "assistant":
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            yield _sse("text", {"content": block["text"]})
                        elif block.get("type") == "tool_use":
                            yield _sse("tool_use", {
                                "tool": block.get("name", ""),
                                "input": block.get("input", {}),
                            })
                elif etype == "result":
                    text = ""
                    for block in event.get("content", []):
                        if block.get("type") == "text":
                            text += block["text"]
                    if text:
                        yield _sse("text", {"content": text})
                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield _sse("text", {"content": delta["text"]})
                else:
                    log.info(f"[cli] Unhandled event type: {etype} — {str(event)[:200]}")
            except json.JSONDecodeError:
                log.warning(f"[cli] Non-JSON line: {line[:200]}")
                yield _sse("text", {"content": line})

        process.wait()
        log.info(f"[cli] Process exited: code={process.returncode} lines={line_count}")
        if process.returncode != 0:
            stderr = process.stderr.read().decode("utf-8").strip()
            err_msg = stderr or f"Claude CLI exited with code {process.returncode}"
            log.error(f"[cli] stderr: {err_msg[:500]}")
            yield _sse("error", {"message": err_msg})
    except Exception as e:
        log.exception(f"[cli] Exception during streaming: {e}")
        process.kill()
        yield _sse("error", {"message": str(e)})


async def _build_queue_context(dispatch_id: int, project_dir: str) -> str | None:
    """Build context from the queuing agent for agent-queued dispatches.

    When agent A queues agent B, B gets:
    - Who queued it and why (the reason/instructions)
    - Agent A's summary.md (its current perspective)
    - The trigger commit context if it was commit-triggered
    """
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        return None

    trigger = dispatch.get("trigger")
    trigger_detail = dispatch.get("trigger_detail")
    instructions = dispatch.get("instructions")

    sections = []

    if trigger == "agent_queue" and trigger_detail:
        # trigger_detail is the queuing agent's ID
        queuing_agent = await db.get_agent(trigger_detail)
        if queuing_agent:
            sections.append(
                f"## Queued by: {queuing_agent['name']}\n"
                f"Agent `{queuing_agent['name']}` has queued you to run."
            )
            if instructions:
                sections.append(f"**Reason:** {instructions}")

            # Read the queuing agent's summary.md for context
            q_props = queuing_agent.get("properties", {})
            q_outputs = q_props.get("output_artifacts", [])
            for pattern in q_outputs:
                # Look for summary.md specifically
                import glob as globmod
                full = os.path.join(project_dir, pattern)
                for fpath in globmod.glob(full, recursive=True):
                    if fpath.endswith("summary.md") and os.path.isfile(fpath):
                        try:
                            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                                content = f.read(4000)  # cap at 4K
                            rel = os.path.relpath(fpath, project_dir)
                            sections.append(
                                f"### {queuing_agent['name']}'s Current Summary\n"
                                f"(from `{rel}`):\n```\n{content}\n```"
                            )
                        except OSError:
                            pass

    elif trigger == "commit" and trigger_detail:
        # Commit-triggered: include commit info
        result = subprocess.run(
            ["git", "show", "--stat", "--format=%H %an: %s", trigger_detail],
            capture_output=True, text=True, cwd=project_dir
        )
        if result.returncode == 0:
            sections.append(
                f"## Triggered by commit\n```\n{result.stdout.strip()}\n```"
            )

    return "\n\n".join(sections) if sections else None


async def commit_agent_changes(agent: dict, project_dir: str) -> str | None:
    """Stage and commit any changes the agent made. Returns commit hash or None."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=project_dir
    )
    if not result.stdout.strip():
        return None

    # Stage all changes
    subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)

    # Commit with agent as author
    agent_name = agent["name"]
    agent_id = agent["id"]
    author = f"{agent_name} <{agent_id}@maistro.local>"
    message = f"[{agent_name}] automated update"

    result = subprocess.run(
        ["git", "commit", "-m", message, "--author", author],
        capture_output=True, text=True, cwd=project_dir
    )
    if result.returncode != 0:
        return None

    # Get commit hash
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=project_dir
    )
    return result.stdout.strip() if result.returncode == 0 else None


async def invoke_chat_message(
    agent: dict,
    message: str,
    project_dir: str,
    session_id: str | None = None,
    cli_session_id: str | None = None,
) -> AsyncIterator[dict]:
    """Invoke Claude CLI for a chat message, yield SSE events."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        yield _sse("error", {"message": "Claude CLI not found in PATH"})
        return

    props = agent["properties"]
    system_prompt = build_system_prompt(agent, project_dir)

    cmd = [
        claude_bin,
        "-p", message,
        "--output-format", "stream-json",
        "--model", props.get("model", "sonnet"),
        "--system-prompt", system_prompt,
    ]

    if cli_session_id:
        cmd.extend(["--resume", cli_session_id])

    if props.get("base_tools"):
        for tool in props["base_tools"]:
            cmd.extend(["--allowedTools", tool])

    log.info(f"[chat] Spawning Claude CLI for chat, model={props.get('model', 'sonnet')}")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=project_dir,
    )

    chat_queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _reader():
        try:
            for raw_line in process.stdout:
                line = raw_line.decode("utf-8").strip()
                if line:
                    loop.call_soon_threadsafe(chat_queue.put_nowait, line)
        except Exception as e:
            loop.call_soon_threadsafe(chat_queue.put_nowait, f"__ERROR__:{e}")
        finally:
            loop.call_soon_threadsafe(chat_queue.put_nowait, None)

    import threading
    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    try:
        while True:
            line = await chat_queue.get()
            if line is None:
                break
            if line.startswith("__ERROR__:"):
                yield _sse("error", {"message": line[10:]})
                break

            try:
                event = json.loads(line)
                if event.get("type") == "assistant":
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            yield _sse("text", {"content": block["text"]})
                elif event.get("type") == "result":
                    sid = event.get("session_id")
                    if sid:
                        yield _sse("session_id", {"cli_session_id": sid})
                    text = ""
                    for block in event.get("content", []):
                        if block.get("type") == "text":
                            text += block["text"]
                    if text:
                        yield _sse("text", {"content": text})
                elif event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield _sse("text", {"content": delta["text"]})
            except json.JSONDecodeError:
                yield _sse("text", {"content": line})

        process.wait()
        if process.returncode != 0:
            stderr = process.stderr.read().decode("utf-8").strip()
            err_msg = stderr or f"CLI exited with code {process.returncode}"
            log.error(f"[chat] stderr: {err_msg[:500]}")
            yield _sse("error", {"message": err_msg})
    except Exception as e:
        log.exception(f"[chat] Exception: {e}")
        process.kill()
        yield _sse("error", {"message": str(e)})


# ── Watch pattern matching ──────────────────────────────────

async def check_watch_triggers(commit_hash: str, project_dir: str) -> list[dict]:
    """Check which watch-mode agents should be triggered by a commit."""
    import fnmatch

    # Get changed files from commit
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash],
        capture_output=True, text=True, cwd=project_dir
    )
    if result.returncode != 0:
        return []
    changed_files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    if not changed_files:
        return []

    agents = await db.list_agents()
    triggered = []

    for agent in agents:
        props = agent["properties"]
        if props.get("mode") != "watch":
            continue
        if props.get("running"):
            continue

        input_patterns = props.get("input_artifacts", [])
        if not input_patterns:
            continue

        # Check cooldown
        last_dispatch = await _get_last_dispatch_time(agent["id"])
        cooldown = props.get("cooldown_seconds", 30)
        if last_dispatch:
            elapsed = (datetime.now(timezone.utc) - last_dispatch).total_seconds()
            if elapsed < cooldown:
                continue

        # Check if any changed file matches input patterns
        matched = False
        for pattern in input_patterns:
            for changed in changed_files:
                if fnmatch.fnmatch(changed, pattern):
                    matched = True
                    break
            if matched:
                break

        if matched:
            triggered.append(agent)

    return triggered


async def _get_last_dispatch_time(agent_id: str) -> datetime | None:
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT created_at FROM dispatch_queue WHERE agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (agent_id,)
        )
        if rows:
            return datetime.fromisoformat(rows[0]["created_at"]).replace(tzinfo=timezone.utc)
        return None
    finally:
        await conn.close()


# ── Git helpers ─────────────────────────────────────────────

def _get_recent_git_log(project_dir: str, limit: int = 20) -> str:
    result = subprocess.run(
        ["git", "log", f"--max-count={limit}", "--oneline", "--no-decorate"],
        capture_output=True, text=True, cwd=project_dir
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _read_glob_contents(project_dir: str, patterns: list[str], max_per_file: int = 500) -> str:
    """Read files matching glob patterns, return concatenated contents."""
    import glob as globmod
    seen = set()
    sections = []
    for pattern in patterns:
        full_pattern = os.path.join(project_dir, pattern)
        for filepath in globmod.glob(full_pattern, recursive=True):
            if filepath in seen or not os.path.isfile(filepath):
                continue
            seen.add(filepath)
            rel = os.path.relpath(filepath, project_dir)
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[:max_per_file]
                    content = "".join(lines)
                sections.append(f"### {rel}\n```\n{content}\n```")
            except (OSError, UnicodeDecodeError):
                continue
    return "\n\n".join(sections) if sections else ""


def _build_artifact_manifest_sync(project_dir: str) -> str:
    """Build artifact manifest — requires sync context (called from build_user_prompt)."""
    # This is a simplified version — in a full implementation,
    # we'd query the DB. For now, we read from a cached manifest.
    # The real implementation will be async and called before prompt building.
    return ""


# ── Utilities ───────────────────────────────────────────────

def _sse(event_type: str, data: dict) -> dict:
    return {"type": event_type, **data}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
