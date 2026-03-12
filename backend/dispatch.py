"""Dispatch engine — prompt assembly, dispatch lifecycle, watch triggers."""

import glob as globmod
import logging
import os
from typing import AsyncIterator

from backend import cli, database as db, git

log = logging.getLogger("maistro.dispatch")


# ── Dispatch lifecycle ──────────────────────────────────────

async def run_dispatch(
    dispatch_id: int,
    task: dict,
    project_dir: str,
    cancel_event=None,
    dispatch_method: str = "manual",
) -> AsyncIterator[dict]:
    """Build prompts, invoke Claude CLI, yield events.

    Only handles prompt assembly and CLI invocation.
    Lifecycle management (timestamps, chat storage) belongs to the worker.
    """
    props = task["properties"]

    log.info(f"[dispatch:{dispatch_id}] Starting task={task['id']}")

    dispatch_record = await db.get_dispatch(dispatch_id)
    queue_context = await _build_queue_context(dispatch_record, project_dir)
    manifest = await build_task_manifest()
    resume_session_id = dispatch_record.get("resume_session_id") if dispatch_record else None

    # Build dispatch metadata for prompt injection
    dispatch_meta = None
    if dispatch_record:
        dispatch_meta = {
            "auto_dispatched": dispatch_method == "auto",
        }

    system_prompt = build_dispatch_system_prompt(task, project_dir)
    user_prompt = build_user_prompt(task, project_dir, queue_context, manifest, dispatch_meta=dispatch_meta)

    log.info(f"[dispatch:{dispatch_id}] System: {len(system_prompt)} chars, User: {len(user_prompt)} chars"
             + (f", resuming session {resume_session_id}" if resume_session_id else ""))

    async for event in cli.invoke(
        prompt=user_prompt,
        system_prompt=system_prompt,
        cwd=project_dir,
        model=props.get("model"),
        allowed_tools=props.get("base_tools") or None,
        disallowed_tools=props.get("disallowed_tools") or None,
        cancel_event=cancel_event,
        resume_session=resume_session_id,
    ):
        if event["type"] == "error":
            log.error(f"[dispatch:{dispatch_id}] CLI error: {event.get('message', '')[:200]}")
        elif event["type"] == "tool_use":
            log.info(f"[dispatch:{dispatch_id}] Tool use: {event.get('tool', '?')}")
        yield event


# ── Prompt assembly ─────────────────────────────────────────

DISPATCH_SYSTEM_PROMPT = """\
You are an autonomous agent in mAistro, a development engine where tasks coordinate through git.

## Execution Mode
You are running in HEADLESS DISPATCH mode. There is no human in the loop.
- Do NOT ask questions, request clarification, or wait for confirmation.
- Make decisions autonomously based on available context.
- Read files, analyze the codebase, and do your work.
- If instructions are ambiguous, use your best judgment and document your reasoning.

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
        auto = dispatch_meta.get("auto_dispatched", False)
        mode = "auto-dispatched" if auto else "manually dispatched"
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

    sections.append(
        "## Your Turn\n"
        "Review the project state — your instructions, subscriptions, and context above. "
        "Identify what needs to be done and do it. If nothing needs updating, say so briefly."
    )

    return "\n\n".join(sections)


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

async def _build_queue_context(dispatch: dict | None, project_dir: str) -> str | None:
    """Build a cohesive 'why you're running' section from the dispatch's triggers."""
    if not dispatch:
        return None

    triggers = dispatch.get("triggers")
    if not triggers:
        triggers = [{"trigger": dispatch.get("trigger"),
                     "detail": dispatch.get("trigger_detail"),
                     "context": dispatch.get("context")}]

    # Build bullet points for each trigger reason
    reasons = []
    supplementary = []  # extra context sections (task summaries, etc.)

    for entry in triggers:
        trigger = entry.get("trigger")
        detail = entry.get("detail")
        ctx = entry.get("context")

        if trigger == "commit" and detail:
            summary = git.commit_oneline(project_dir, detail) or detail[:8]
            reasons.append(f"- **Commit** `{detail[:8]}`: {summary}")

        elif trigger == "manual":
            ref = f" @ `{detail[:8]}`" if detail else ""
            if ctx:
                reasons.append(f"- **Manual**{ref}: {ctx}")
            else:
                reasons.append(f"- **Manual**{ref}")

        elif trigger == "task_queue" and detail:
            queuing_task = await db.get_task(detail)
            task_name = queuing_task["name"] if queuing_task else detail
            if ctx:
                reasons.append(f"- **Task** ({task_name}): {ctx}")
            else:
                reasons.append(f"- **Task** ({task_name})")

            # Pull in the queuing task's summary files as supplementary context
            if queuing_task:
                q_subs = queuing_task["properties"].get("subscriptions") or []
                for pattern in q_subs:
                    for fpath in globmod.glob(os.path.join(project_dir, pattern), recursive=True):
                        if fpath.endswith("summary.md") and os.path.isfile(fpath):
                            content = git.read_file(project_dir, os.path.relpath(fpath, project_dir))
                            if content:
                                rel = os.path.relpath(fpath, project_dir)
                                supplementary.append(
                                    f"### {task_name}'s Summary\n"
                                    f"(from `{rel}`):\n```\n{content[:4000]}\n```"
                                )

        elif trigger == "resume":
            reasons.append(f"- **Resume** — continuing from a previous dispatch")

        elif trigger == "retry":
            reasons.append(f"- **Retry** — fresh re-dispatch of a previous run")

        elif trigger == "schedule":
            reasons.append(f"- **Schedule** (`{detail or 'cron'}`)")

    if not reasons:
        return None

    parts = ["## Invocation\n" + "\n".join(reasons)]
    parts.extend(supplementary)
    return "\n\n".join(parts)


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
        if not props.get("watch_enabled") or props.get("running"):
            continue

        patterns = props.get("subscriptions")
        if not patterns:
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


