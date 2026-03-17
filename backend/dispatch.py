"""Dispatch engine — prompt assembly, task lifecycle, watch triggers."""

import functools
import logging
import os
import re
from typing import AsyncIterator

from backend import cli, database as db, git, mcp_config as mcp_cfg

log = logging.getLogger("maistro.dispatch")


# ── Task execution ──────────────────────────────────────────

async def run_task(
    task_id: int,
    job: dict,
    project_dir: str,
    cancel_event=None,
    task: dict | None = None,
    subordinates: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Build prompts, invoke Claude CLI, yield events.

    Only handles prompt assembly and CLI invocation.
    Lifecycle management (timestamps, chat storage) belongs to the worker.

    ``task`` may be supplied by the worker (already loaded) to avoid a
    redundant DB round-trip.  Falls back to fetching from DB when absent.

    ``subordinates`` — coalesced tasks whose context is merged into the
    invocation section so the agent addresses all triggers in one pass.
    """
    props = job["properties"]

    log.info("[task:%d] Starting job=%s", task_id, job["id"])

    task_record = task if task is not None else await db.get_task(task_id)
    queue_context = _build_queue_context(task_record, subordinates)
    manifest = await build_job_manifest()
    resume_session_id = task_record.get("resume_session_id") if task_record else None

    # Build task metadata for prompt injection
    task_meta = None
    if task_record:
        task_meta = {
            "trigger": task_record.get("trigger", "manual"),
        }

    system_prompt = build_dispatch_system_prompt(job, project_dir)
    user_prompt = build_user_prompt(job, project_dir, queue_context, manifest, task_meta=task_meta)

    log.info("[task:%d] System: %d chars, User: %d chars%s",
             task_id, len(system_prompt), len(user_prompt),
             f", resuming session {resume_session_id}" if resume_session_id else "")

    session_id = task_record.get("session_id") or ""
    external_servers = await db.list_mcp_servers()
    mcp_config_path = mcp_cfg.write_mcp_config(
        job=job,
        project_dir=project_dir,
        session_id=session_id,
        external_servers=external_servers,
    )
    log.info("[task:%d] MCP config written to %s", task_id, mcp_config_path)

    try:
        allowed = props.get("allowed_tools") or None
        disallowed = None
        if allowed:
            disallowed = sorted(cli.CLI_NATIVE_TOOLS - set(allowed))

        async for event in cli.invoke(
            prompt=user_prompt,
            system_prompt=system_prompt,
            cwd=project_dir,
            model=props.get("model"),
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            mcp_config_path=mcp_config_path,
            cancel_event=cancel_event,
            resume_session=resume_session_id,
        ):
            if event["type"] == "error":
                log.error("[task:%d] CLI error: %s", task_id, event.get("message", "")[:200])
            elif event["type"] == "tool_use":
                log.info("[task:%d] Tool use: %s", task_id, event.get("tool", "?"))
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
You are running in HEADLESS DISPATCH mode in {project_dir}. There is no human in the loop.
- Act autonomously — do not ask questions or wait for confirmation.
- If instructions are ambiguous, use your best judgment and document your reasoning in commit messages.
- Doing nothing is a valid outcome. If the triggering context doesn't require changes within your scope, say so briefly and stop. Not every trigger demands action.

## Output Standards
- Commit your changes with descriptive messages explaining what changed and why.
- Use the job name as a commit tag prefix: [{job_name}] description
- Stage and commit related changes together as logical units.
- Documentation files should be first-principle, as-is representations of current state — not task lists or work-in-progress notes. Reasoning and work context belong in commit messages."""


def build_dispatch_system_prompt(job: dict, project_dir: str) -> str:
    return DISPATCH_SYSTEM_PROMPT.format(
        job_name=job["name"],
        project_dir=project_dir,
    )


def build_user_prompt(job: dict, project_dir: str,
                      queue_context: str | None = None, manifest: str | None = None,
                      task_meta: dict | None = None) -> str:
    props = job["properties"]
    sections = []

    name = job["name"]
    description = props.get("description") or ""
    instructions = props.get("instructions") or ""

    identity_parts = [f"# Job: {name}"]
    if description:
        identity_parts.append(description)
    if task_meta:
        trigger = task_meta.get("trigger", "manual")
        mode = "manually dispatched" if trigger == "manual" else f"auto-dispatched ({trigger})"
        identity_parts.append(f"**Dispatch:** {mode}")
    sections.append("\n".join(identity_parts))

    if instructions:
        sections.append(f"## Instructions\n{instructions}")

    if queue_context:
        sections.append(queue_context)

    if manifest and manifest.strip():
        sections.append(f"## Job Registry\n{manifest}")

    sub_globs = props.get("subscriptions")
    if sub_globs:
        sub_files = git.resolve_glob_files(project_dir, sub_globs)
        if sub_files:
            file_list = "\n".join(f"- `{f['path']}` ({f['size']}B)" for f in sub_files)
            sections.append(f"## Subscribed Files\nThese files are relevant to your task. Read them as needed.\n{file_list}")

    primary_trigger = task_meta.get("trigger") if task_meta else None
    sections.append("## Your Turn\n" + _build_closing_directive(primary_trigger))

    return "\n\n".join(sections)


def _build_closing_directive(trigger: str | None = None) -> str:
    """Build the action directive tailored to the task trigger type."""
    if trigger == "commit":
        return (
            "Changes in your subscribed files triggered this dispatch. "
            "Review the triggering commits above and respond accordingly."
        )
    if trigger == "dependency":
        return (
            "An upstream job has completed — the Invocation section above has commit details. "
            "Use the commit range to inspect what changed, then respond accordingly."
        )
    if trigger == "retry":
        return (
            "This is a retry of a previous task that failed or produced insufficient results. "
            "Review context above, adjust your approach, and try again."
        )
    if trigger == "schedule":
        return (
            "This is a scheduled run. Check your subscribed files and project state "
            "for anything that needs attention. If nothing needs updating, say so briefly."
        )
    if trigger == "resume":
        return (
            "This is a resumed session — you are continuing previous work that was interrupted. "
            "Pick up where you left off."
        )
    return (
        "Review the project state — your instructions, subscriptions, and context above. "
        "Identify what needs to be done and do it. If nothing needs updating, say so briefly."
    )


# ── Job manifest ──────────────────────────────────────────

async def build_job_manifest() -> str:
    """Build the job registry — all jobs with descriptions and subscriptions."""
    jobs = await db.list_jobs()
    if not jobs:
        return ""
    lines = ["Jobs in this project:"]
    for j in jobs:
        desc = j["properties"].get("description") or ""
        subs = j["properties"].get("subscriptions") or []
        summary = desc[:100] + "..." if len(desc) > 100 else desc
        sub_str = ", ".join(subs) if subs else "(none)"
        lines.append(f"- **{j['name']}** — {summary or '(no description)'}")
        lines.append(f"  Subscriptions: {sub_str}")
    return "\n".join(lines)


# ── Queue context ───────────────────────────────────────────

def _build_queue_context(task: dict | None, subordinates: list[dict] | None = None) -> str | None:
    """Build the 'why you're running' section from task context strings.

    Each task has a single context string. When coalesced, subordinate
    contexts are collected alongside the root's.
    """
    if not task:
        return None

    reasons = []
    ctx = task.get("context")
    if ctx:
        reasons.append(f"- {ctx}")

    if subordinates:
        for sub in subordinates:
            sub_ctx = sub.get("context")
            if sub_ctx:
                reasons.append(f"- {sub_ctx}")

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

    header = "## Invocation"
    if subordinates and len(reasons) > 1:
        header += f"\nMultiple triggers have been coalesced into this task ({len(reasons)} items). Address them together."

    return header + "\n" + "\n".join(reasons)


# ── Watch pattern matching ──────────────────────────────────

async def check_watch_triggers(commit_hash: str, project_dir: str) -> list[dict]:
    """Check which watch-mode jobs should be triggered by a commit."""
    changed_files = git.changed_files_in_commit(project_dir, commit_hash)
    if not changed_files:
        return []

    jobs = await db.list_jobs()
    triggered = []

    for job in jobs:
        props = job["properties"]
        patterns = props.get("subscriptions")
        if not patterns or props.get("running"):
            continue

        if _any_file_matches(changed_files, patterns):
            triggered.append(job)

    return triggered


def _any_file_matches(files: list[str], patterns: list[str]) -> bool:
    """Check if any file matches any glob pattern (supports ** recursive)."""
    for pattern in patterns:
        regex = _glob_to_regex(pattern)
        for f in files:
            if regex.match(f):
                return True
    return False


@functools.lru_cache(maxsize=256)
def _glob_to_regex(pattern: str):
    """Convert a glob pattern to a compiled regex with proper ** support."""
    parts = []
    i = 0
    while i < len(pattern):
        if pattern[i:i+2] == '**':
            parts.append('.*')
            i += 2
            if i < len(pattern) and pattern[i] == '/':
                i += 1
        elif pattern[i] == '*':
            parts.append('[^/]*')
            i += 1
        elif pattern[i] == '?':
            parts.append('[^/]')
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile('^' + ''.join(parts) + '$')
