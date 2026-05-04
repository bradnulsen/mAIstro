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
    workspace_dir: str | None = None,
) -> AsyncIterator[dict]:
    """Build prompts, invoke Claude CLI, yield events.

    Only handles prompt assembly and CLI invocation.
    Lifecycle management (timestamps, chat storage) belongs to the worker.

    ``task`` may be supplied by the worker (already loaded) to avoid a
    redundant DB round-trip.  Falls back to fetching from DB when absent.

    ``subordinates`` — coalesced tasks whose context is merged into the
    invocation section so the agent addresses all triggers in one pass.

    ``workspace_dir`` — cwd for the CLI subprocess and for the MCP server's
    write-side tools. Phase 3a defaults to project_dir; Phase 3b will
    receive a per-task worktree path from the worker.
    """
    props = job["properties"]
    workspace = workspace_dir or project_dir

    log.info("[task:%d] Starting job=%s", task_id, job["id"])

    task_record = task if task is not None else await db.get_task(task_id)
    queue_context = _build_queue_context(task_record, subordinates)
    manifest = await build_job_manifest()
    resume_session_id = task_record.get("resume_session_id") if task_record else None

    system_prompt = build_dispatch_system_prompt(job, workspace)
    user_prompt = build_user_prompt(job, project_dir, queue_context, manifest)

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
        task_id=task_id,
        workspace_dir=workspace,
    )
    log.info("[task:%d] MCP config written to %s", task_id, mcp_config_path)

    try:
        allowed = props.get("allowed_tools") or []
        if not allowed:
            # Empty list = all tools enabled (UI default)
            allowed = sorted(cli.CLI_NATIVE_TOOLS)
        disallowed = sorted(cli.CLI_NATIVE_TOOLS - set(allowed))

        async for event in cli.invoke(
            prompt=user_prompt,
            system_prompt=system_prompt,
            cwd=workspace,
            model=props.get("model"),
            max_turns=props.get("max_turns", 100),
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
Your job is {job_name}.

## Execution Mode
You are running in HEADLESS DISPATCH mode. Your working directory is {cwd} — a dedicated git worktree for this task on its own branch. Edits and commits made here are integrated into the operator's main branch when the task completes successfully. There is no human in the loop.
- Act autonomously — do not ask questions or wait for confirmation.
- If ambiguous, use your best judgment and document your reasoning in commit messages.
- Doing nothing is a valid outcome. If the triggering context doesn't require changes within your scope, say so briefly and stop. Not every trigger demands action.

## Output Standards
- Commit your changes with descriptive messages explaining what changed and why.
- Use the job name as a commit tag prefix: [{job_name}] description
- Stage and commit related changes together as logical units.
- Documentation files should be first-principle, as-is representations of current state — not task lists or work-in-progress notes. Reasoning and work context belong in commit messages."""


def build_dispatch_system_prompt(job: dict, cwd: str) -> str:
    return DISPATCH_SYSTEM_PROMPT.format(
        job_name=job["name"],
        cwd=cwd,
    )


def build_user_prompt(job: dict, project_dir: str,
                      queue_context: str | None = None, manifest: str | None = None) -> str:
    props = job["properties"]
    sections = []

    name = job["name"]
    summary = props.get("summary") or ""
    description = props.get("description") or ""

    if description:
        sections.append(f"# {name}\n{description}")
    elif summary:
        sections.append(f"# {name}\n{summary}")
    else:
        sections.append(f"# {name}")

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

    sections.append(
        "## Your Turn\n"
        "Review the project state — your instructions, subscriptions, and context above. "
        "Identify what needs to be done and do it. If nothing needs updating, say so briefly."
    )

    return "\n\n".join(sections)


# ── Job manifest ──────────────────────────────────────────

async def build_job_manifest() -> str:
    """Build the job registry — all jobs with descriptions and subscriptions."""
    jobs = await db.list_jobs()
    if not jobs:
        return ""
    lines = ["Jobs in this project:"]
    for j in jobs:
        summary = j["properties"].get("summary") or ""
        subs = j["properties"].get("subscriptions") or []
        short = summary[:100] + "..." if len(summary) > 100 else summary
        sub_str = ", ".join(subs) if subs else "(none)"
        lines.append(f"- **{j['name']}** — {short or '(no summary)'}")
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


# ── Trigger context ─────────────────────────────────────────

def build_trigger_context(
    trigger: str,
    *,
    project_dir: str | None = None,
    commit_hash: str | None = None,
    start_commit: str | None = None,
    result_commit: str | None = None,
    upstream_name: str | None = None,
    upstream_task_id: int | None = None,
    original_task_id: int | None = None,
    schedule_expr: str | None = None,
    user_context: str | None = None,
) -> str:
    """Build the pre-formatted context string for a task trigger.

    Single entry point for all trigger context formatting.  Each enqueue site
    calls this with trigger-specific parameters; this function resolves git
    context through ``git.build_commit_context`` and applies consistent
    formatting.
    """
    if trigger == "manual":
        ref = f" @ `{commit_hash[:8]}`" if commit_hash else ""
        if user_context:
            return f"**Manual**{ref}: {user_context}"
        return f"**Manual**{ref}"

    if trigger == "commit":
        summary = ""
        if project_dir and commit_hash:
            summary = git.commit_oneline(project_dir, commit_hash) or commit_hash[:8]
        return f"**Commit** `{commit_hash[:8]}`: {summary}"

    if trigger == "schedule":
        head_note = f" at {commit_hash[:8]}" if commit_hash else ""
        return f"**Schedule** (`{schedule_expr}`){head_note}"

    if trigger == "cascade":
        header = f"**Cascade** — triggered by completion of {upstream_name} (task #{upstream_task_id})"
        git_ctx = None
        if project_dir and start_commit and result_commit:
            git_ctx = git.build_commit_context(project_dir, start_commit, result_commit)
        if git_ctx:
            # Indent multi-line git context under the header
            indented = git_ctx.replace("\n", "\n  ")
            header += f"\n  {indented}"
        elif result_commit:
            header += f", commit `{result_commit[:8]}`"
        return header

    if trigger == "resume":
        return f"**Resume** — continuing from task #{original_task_id}"

    if trigger == "reply":
        parts = [f"**Reply** to task #{original_task_id}"]
        git_ctx = None
        if project_dir and start_commit and result_commit:
            git_ctx = git.build_commit_context(project_dir, start_commit, result_commit)
        if git_ctx:
            parts[0] += f" — commits {start_commit[:8]}..{result_commit[:8]}"
        if user_context:
            parts.append(user_context)
        return " — ".join(parts) if len(parts) == 1 else "\n".join(parts)

    if trigger == "agent":
        parts = [f"**Agent** — dispatched by {upstream_name} (task #{upstream_task_id})"]
        if user_context:
            parts.append(user_context)
        return "\n".join(parts)

    # Fallback — unknown trigger type
    return user_context or f"**{trigger.title()}**"


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
