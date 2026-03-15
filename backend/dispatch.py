"""Dispatch engine — prompt assembly, dispatch lifecycle, watch triggers."""

import logging
import os
from typing import AsyncIterator

from backend import cli, database as db, git, mcp_config as mcp_cfg

log = logging.getLogger("maistro.dispatch")


# ── Dispatch lifecycle ──────────────────────────────────────

async def run_dispatch(
    dispatch_id: int,
    task: dict,
    project_dir: str,
    cancel_event=None,
) -> AsyncIterator[dict]:
    """Build prompts, invoke Claude CLI, yield events.

    Only handles prompt assembly and CLI invocation.
    Lifecycle management (timestamps, chat storage) belongs to the worker.
    """
    props = task["properties"]

    log.info("[dispatch:%d] Starting task=%s", dispatch_id, task["id"])

    dispatch_record = await db.get_dispatch(dispatch_id)
    queue_context = _build_queue_context(dispatch_record)
    manifest = await build_task_manifest()
    resume_session_id = dispatch_record.get("resume_session_id") if dispatch_record else None

    # Build dispatch metadata for prompt injection
    dispatch_meta = None
    if dispatch_record:
        triggers = dispatch_record.get("triggers") or []
        primary_trigger = triggers[0]["trigger"] if triggers else "manual"
        dispatch_meta = {
            "trigger": primary_trigger,
        }

    system_prompt = build_dispatch_system_prompt(task, project_dir)
    user_prompt = build_user_prompt(task, project_dir, queue_context, manifest, dispatch_meta=dispatch_meta)

    log.info("[dispatch:%d] System: %d chars, User: %d chars%s",
             dispatch_id, len(system_prompt), len(user_prompt),
             f", resuming session {resume_session_id}" if resume_session_id else "")

    # Build MCP config connecting the agent to the internal server and any
    # enabled external servers. Config is written to a temp file and cleaned up
    # after the dispatch completes.
    session_id = dispatch_record.get("session_id") or ""
    external_servers = await db.list_mcp_servers()
    mcp_config_path = mcp_cfg.write_mcp_config(
        task=task,
        project_dir=project_dir,
        session_id=session_id,
        external_servers=external_servers,
    )
    log.info("[dispatch:%d] MCP config written to %s", dispatch_id, mcp_config_path)

    try:
        async for event in cli.invoke(
            prompt=user_prompt,
            system_prompt=system_prompt,
            cwd=project_dir,
            model=props.get("model"),
            allowed_tools=props.get("base_tools") or None,
            disallowed_tools=props.get("disallowed_tools") or None,
            mcp_config_path=mcp_config_path,
            cancel_event=cancel_event,
            resume_session=resume_session_id,
        ):
            if event["type"] == "error":
                log.error("[dispatch:%d] CLI error: %s", dispatch_id, event.get("message", "")[:200])
            elif event["type"] == "tool_use":
                log.info("[dispatch:%d] Tool use: %s", dispatch_id, event.get("tool", "?"))
            yield event
    finally:
        if mcp_config_path and os.path.exists(mcp_config_path):
            try:
                os.unlink(mcp_config_path)
            except OSError:
                pass


# ── Prompt assembly ─────────────────────────────────────────

DISPATCH_SYSTEM_PROMPT = """\
You are an autonomous agent in mAistro, a development engine where tasks coordinate through git.

## Execution Mode
You are running in HEADLESS DISPATCH mode. There is no human in the loop.
- Act autonomously — do not ask questions or wait for confirmation.
- If instructions are ambiguous, use your best judgment and document your reasoning in commit messages.

## Documentation Principle
Documentation is the source of truth. Your files should be first-principle, \
as-is representations of current project state — not task lists, not work-in-progress notes. \
Any reasoning, context, or work management belongs in commit messages, not in documentation. \
Keep your documentation current, accurate, and useful to anyone reading it cold.

## Git Workflow
- Commit your changes with descriptive messages explaining what changed and why.
- Use the task name as a commit tag prefix: [{task_name}] description
- Stage and commit related changes together as logical units.

## Working Directory
{project_dir}"""


def build_dispatch_system_prompt(task: dict, project_dir: str) -> str:
    return DISPATCH_SYSTEM_PROMPT.format(
        task_name=task["name"],
        project_dir=project_dir,
    )


def build_user_prompt(task: dict, project_dir: str,
                      queue_context: str | None = None, manifest: str | None = None,
                      dispatch_meta: dict | None = None) -> str:
    props = task["properties"]
    sections = []

    # Task identity
    name = task["name"]
    description = props.get("description") or ""
    instructions = props.get("instructions") or ""

    identity_parts = [f"# Task: {name}"]
    if description:
        identity_parts.append(description)
    if dispatch_meta:
        trigger = dispatch_meta.get("trigger", "manual")
        mode = "manually dispatched" if trigger == "manual" else f"auto-dispatched ({trigger})"
        identity_parts.append(f"**Dispatch:** {mode}")
    sections.append("\n".join(identity_parts))

    if instructions:
        sections.append(f"## Instructions\n{instructions}")

    if queue_context:
        sections.append(queue_context)

    if manifest and manifest.strip():
        sections.append(f"## Task Registry\n{manifest}")

    # Subscription file list — agent reads contents via tools as needed
    sub_globs = props.get("subscriptions")
    if sub_globs:
        sub_files = git.resolve_glob_files(project_dir, sub_globs)
        if sub_files:
            file_list = "\n".join(f"- `{f['path']}` ({f['size']}B)" for f in sub_files)
            sections.append(f"## Subscribed Files\nThese files are relevant to your task. Read them as needed.\n{file_list}")

    sections.append("## Your Turn\n" + _build_closing_directive(dispatch_meta))

    return "\n\n".join(sections)


def _build_closing_directive(dispatch_meta: dict | None) -> str:
    """Build a trigger-appropriate closing directive for the agent."""
    trigger = (dispatch_meta or {}).get("trigger", "manual")
    if trigger == "commit":
        return (
            "Changes in your subscribed files triggered this dispatch. "
            "Review the triggering commits above and respond according to your instructions. "
            "If nothing needs updating, say so briefly."
        )
    if trigger == "dependency":
        return (
            "An upstream task has completed. Review what changed above "
            "and respond according to your instructions. "
            "If nothing needs updating, say so briefly."
        )
    if trigger == "schedule":
        return (
            "Review the project state — your instructions, subscriptions, and context above. "
            "Identify what needs to be done and do it. If nothing needs updating, say so briefly."
        )
    # manual, resume, retry — direct action
    return (
        "Review the project state — your instructions, subscriptions, and context above. "
        "Identify what needs to be done and do it. If nothing needs updating, say so briefly."
    )


# ── Task manifest ─────────────────────────────────────────

async def build_task_manifest() -> str:
    """Build the task registry — all tasks with descriptions and subscriptions."""
    tasks = await db.list_tasks()
    if not tasks:
        return ""
    lines = ["Tasks in this project:"]
    for t in tasks:
        desc = t["properties"].get("description") or ""
        subs = t["properties"].get("subscriptions") or []
        summary = desc[:100] + "..." if len(desc) > 100 else desc
        sub_str = ", ".join(subs) if subs else "(none)"
        lines.append(f"- **{t['name']}** — {summary or '(no description)'}")
        lines.append(f"  Subscriptions: {sub_str}")
    return "\n".join(lines)


# ── Queue context ───────────────────────────────────────────

def _build_queue_context(dispatch: dict | None) -> str | None:
    """Build the 'why you're running' section from pre-built trigger context strings.

    Each trigger entry's 'context' field is built at the enqueue site —
    this function just renders them as bullet points.
    """
    if not dispatch:
        return None

    triggers = dispatch.get("triggers") or []

    reasons = []
    for entry in triggers:
        ctx = entry.get("context")
        if ctx:
            reasons.append(f"- {ctx}")

    if not reasons:
        return None

    # Deduplicate identical reason lines (e.g. repeated schedule fires)
    deduped = []
    counts = {}
    for r in reasons:
        if r in counts:
            counts[r] += 1
        else:
            counts[r] = 1
            deduped.append(r)
    reasons = [f"{r} ×{counts[r]}" if counts[r] > 1 else r for r in deduped]

    return "## Invocation\n" + "\n".join(reasons)


# ── Watch pattern matching ──────────────────────────────────

async def check_watch_triggers(commit_hash: str, project_dir: str) -> list[dict]:
    """Check which watch-mode tasks should be triggered by a commit."""
    changed_files = git.changed_files_in_commit(project_dir, commit_hash)
    if not changed_files:
        return []

    tasks = await db.list_tasks()
    triggered = []

    for task in tasks:
        props = task["properties"]
        patterns = props.get("subscriptions")
        if not patterns or props.get("running"):
            continue

        if _any_file_matches(changed_files, patterns):
            triggered.append(task)

    return triggered


def _any_file_matches(files: list[str], patterns: list[str]) -> bool:
    """Check if any file matches any glob pattern (supports ** recursive)."""
    from pathlib import PurePath
    for pattern in patterns:
        for f in files:
            if PurePath(f).match(pattern):
                return True
    return False


