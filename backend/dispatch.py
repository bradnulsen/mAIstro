"""Dispatch engine — prompt assembly, dispatch lifecycle, watch triggers."""

import fnmatch
import glob as globmod
import logging
import os
from datetime import datetime, timezone
from typing import AsyncIterator

from backend import cli, database as db, git

log = logging.getLogger("maistro.dispatch")


def utcnow() -> str:
    """UTC timestamp string for database storage."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── Dispatch lifecycle ──────────────────────────────────────

async def run_dispatch(
    dispatch_id: int,
    agent: dict,
    project_dir: str,
    context: str | None = None,
) -> AsyncIterator[dict]:
    """Execute a dispatch: build prompt, invoke Claude CLI, yield SSE events."""
    props = agent["properties"]
    agent_id = agent["id"]

    log.info(f"[dispatch:{dispatch_id}] Starting agent={agent_id}")

    await db.update_dispatch(dispatch_id, started_at=utcnow())
    await db.set_agent_running(agent_id, True)

    yield {"type": "dispatch", "status": "started", "agent_id": agent_id, "dispatch_id": dispatch_id}

    try:
        queue_context = await _build_queue_context(dispatch_id, project_dir)
        manifest = await build_agent_manifest()

        system_prompt = build_system_prompt(agent, project_dir)
        user_prompt = build_user_prompt(agent, project_dir, context, queue_context, manifest)

        log.info(f"[dispatch:{dispatch_id}] System: {len(system_prompt)} chars, User: {len(user_prompt)} chars")
        log.debug(f"[dispatch:{dispatch_id}] System prompt:\n{system_prompt[:500]}")
        log.debug(f"[dispatch:{dispatch_id}] User prompt:\n{user_prompt[:500]}")

        allowed = props.get("base_tools") or None
        disallowed = props.get("disallowed_tools") or None

        async for event in cli.invoke(
            prompt=user_prompt,
            system_prompt=system_prompt,
            cwd=project_dir,
            model=props.get("model"),
            allowed_tools=allowed,
            disallowed_tools=disallowed,
        ):
            if event["type"] == "error":
                log.error(f"[dispatch:{dispatch_id}] CLI error: {event.get('message', '')[:200]}")
            elif event["type"] == "tool_use":
                log.info(f"[dispatch:{dispatch_id}] Tool use: {event.get('tool', '?')}")
            yield event

        commit_hash = commit_agent_changes(agent, project_dir)
        if commit_hash:
            log.info(f"[dispatch:{dispatch_id}] Committed: {commit_hash[:8]}")
            await db.update_dispatch(dispatch_id, completed_at=utcnow(), result_commit=commit_hash)
        else:
            log.info(f"[dispatch:{dispatch_id}] No file changes to commit")
            await db.update_dispatch(dispatch_id, completed_at=utcnow())

        yield {
            "type": "result",
            "status": "completed",
            "agent_id": agent_id,
            "dispatch_id": dispatch_id,
            "commit": commit_hash,
        }
        log.info(f"[dispatch:{dispatch_id}] Completed")

    except Exception as e:
        log.exception(f"[dispatch:{dispatch_id}] Failed: {e}")
        await db.update_dispatch(dispatch_id, completed_at=utcnow(), error=str(e))
        yield {"type": "error", "message": str(e), "agent_id": agent_id, "dispatch_id": dispatch_id}
    finally:
        await db.set_agent_running(agent_id, False)


# ── Prompt assembly ─────────────────────────────────────────

PLATFORM_PREAMBLE = (
    "Documentation is the source of truth. Your files should be first-principle, "
    "as-is representations of current project state — not task lists, not work-in-progress notes. "
    "Any reasoning, context, or work management belongs in commit messages, not in documentation. "
    "Keep your documentation current, accurate, and useful to anyone reading it cold."
)


def build_system_prompt(agent: dict, project_dir: str) -> str:
    props = agent["properties"]
    name = agent["name"]
    persona = props.get("persona")

    sections = [
        f"You are {name}, an agent in mAistro.",
        "## Execution Mode\n"
        "You are running in HEADLESS DISPATCH mode. There is no human in the loop.\n"
        "- Do NOT ask questions, request clarification, or wait for confirmation.\n"
        "- Make decisions autonomously based on available context.\n"
        "- Read files, analyze the codebase, and do your work.\n"
        "- If instructions are ambiguous, use your best judgment and document your reasoning.\n"
        "- Produce concrete output: write files, update artifacts, commit results.",
        f"## Documentation Principle\n{PLATFORM_PREAMBLE}",
    ]
    if persona:
        sections.append(f"## Your Role\n{persona}")

    sections.append(f"## Working Directory\n{project_dir}")

    return "\n\n".join(sections)


def build_user_prompt(agent: dict, project_dir: str, context: str | None = None,
                      queue_context: str | None = None, manifest: str | None = None) -> str:
    props = agent["properties"]
    sections = []

    if queue_context:
        sections.append(queue_context)

    if manifest:
        sections.append(f"## Agent Registry\n{manifest}")

    # Subscription contents — files this agent watches and receives as context
    sub_globs = props.get("subscriptions")
    if sub_globs:
        sub_contents = read_glob_contents(project_dir, sub_globs)
        if sub_contents:
            sections.append(f"## Subscriptions (context)\n{sub_contents}")

    recent = git.log_oneline(project_dir)
    if recent:
        sections.append(f"## Recent Activity\n```\n{recent}\n```")

    if context:
        sections.append(f"## Context\n{context}")
    else:
        sections.append(
            "## Task\n"
            "Review your subscriptions and the recent activity. "
            "Do your work and update your documentation."
        )

    return "\n\n".join(sections)


# ── Agent manifest ─────────────────────────────────────────

async def build_agent_manifest() -> str:
    """Build the agent registry — all agents with personas and subscriptions."""
    agents = await db.list_agents()
    if not agents:
        return ""
    lines = ["Agents in this project:"]
    for a in agents:
        persona = a["properties"].get("persona") or ""
        subs = a["properties"].get("subscriptions") or []
        summary = persona[:100] + "..." if len(persona) > 100 else persona
        sub_str = ", ".join(subs) if subs else "(none)"
        lines.append(f"- **{a['name']}** — {summary or '(no persona)'}")
        lines.append(f"  Subscriptions: {sub_str}")
    return "\n".join(lines)


# ── Queue context ───────────────────────────────────────────

async def _build_queue_context(dispatch_id: int, project_dir: str) -> str | None:
    """Build context from the queuing agent for agent-queued dispatches."""
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        return None

    trigger = dispatch.get("trigger")
    trigger_detail = dispatch.get("trigger_detail")
    context = dispatch.get("context")
    sections = []

    if trigger == "agent_queue" and trigger_detail:
        queuing_agent = await db.get_agent(trigger_detail)
        if queuing_agent:
            sections.append(
                f"## Queued by: {queuing_agent['name']}\n"
                f"Agent `{queuing_agent['name']}` has queued you to run."
            )
            if context:
                sections.append(f"**Reason:** {context}")

            # Read the queuing agent's subscribed files for summary context
            q_subs = queuing_agent["properties"].get("subscriptions") or []
            for pattern in q_subs:
                for fpath in globmod.glob(os.path.join(project_dir, pattern), recursive=True):
                    if fpath.endswith("summary.md") and os.path.isfile(fpath):
                        content = git.read_file(project_dir, os.path.relpath(fpath, project_dir))
                        if content:
                            rel = os.path.relpath(fpath, project_dir)
                            sections.append(
                                f"### {queuing_agent['name']}'s Current Summary\n"
                                f"(from `{rel}`):\n```\n{content[:4000]}\n```"
                            )

    elif trigger == "commit" and trigger_detail:
        info = git.show(project_dir, trigger_detail, stat=True)
        if info:
            sections.append(f"## Triggered by commit\n```\n{info.strip()}\n```")

    return "\n\n".join(sections) if sections else None


# ── Agent commit ────────────────────────────────────────────

def commit_agent_changes(agent: dict, project_dir: str) -> str | None:
    """Stage and commit any changes the agent made. Returns commit hash or None."""
    author = f"{agent['name']} <{agent['id']}@maistro.local>"
    message = f"[{agent['name']}] automated update"
    return git.commit_all(project_dir, message, author=author)


# ── Watch pattern matching ──────────────────────────────────

async def check_watch_triggers(commit_hash: str, project_dir: str) -> list[dict]:
    """Check which watch-mode agents should be triggered by a commit."""
    changed_files = git.changed_files_in_commit(project_dir, commit_hash)
    if not changed_files:
        return []

    agents = await db.list_agents()
    triggered = []

    for agent in agents:
        props = agent["properties"]
        if props.get("mode") != "watch" or props.get("running"):
            continue

        patterns = props.get("subscriptions")
        if not patterns:
            continue

        last_dispatch = await db.get_last_dispatch_time(agent["id"])
        cooldown = props.get("cooldown_seconds", 30)
        if last_dispatch:
            elapsed = (datetime.now(timezone.utc) - last_dispatch).total_seconds()
            if elapsed < cooldown:
                continue

        if _any_file_matches(changed_files, patterns):
            triggered.append(agent)

    return triggered


def _any_file_matches(files: list[str], patterns: list[str]) -> bool:
    """Check if any file matches any glob pattern."""
    for pattern in patterns:
        for f in files:
            if fnmatch.fnmatch(f, pattern):
                return True
    return False


# ── File helpers ────────────────────────────────────────────

def read_glob_contents(project_dir: str, patterns: list[str], max_lines: int = 500) -> str:
    """Read files matching glob patterns, return concatenated contents."""
    seen = set()
    sections = []
    for pattern in patterns:
        for fpath in globmod.glob(os.path.join(project_dir, pattern), recursive=True):
            if fpath in seen or not os.path.isfile(fpath):
                continue
            seen.add(fpath)
            rel = os.path.relpath(fpath, project_dir)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[:max_lines]
                    content = "".join(lines)
                sections.append(f"### {rel}\n```\n{content}\n```")
            except (OSError, UnicodeDecodeError):
                continue
    return "\n\n".join(sections) if sections else ""


def resolve_glob_files(project_dir: str, patterns: list[str]) -> list[dict]:
    """Resolve glob patterns to file metadata dicts."""
    files = []
    seen = set()
    for pattern in patterns:
        for fpath in globmod.glob(os.path.join(project_dir, pattern), recursive=True):
            if fpath in seen or not os.path.isfile(fpath):
                continue
            seen.add(fpath)
            rel = os.path.relpath(fpath, project_dir).replace("\\", "/")
            stat = os.stat(fpath)
            files.append({
                "path": rel,
                "pattern": pattern,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    return files
