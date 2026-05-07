"""Governor — autonomous meta-analysis agent.

Monitors job/task health, identifies friction and improvement opportunities,
and surfaces structured findings (suggestions and observations). Runs every
10 completed tasks or on manual trigger. Not user-configurable.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile

from backend import cli, database as db, git, state
from backend.state import utcnow

log = logging.getLogger("maistro.governor")

_run_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """Schedule a Governor coroutine and hold a strong reference until it finishes.

    asyncio's loop only weakly references tasks; without this, a fire-and-forget
    create_task can be garbage-collected before its first await runs.
    """
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t

GOVERNOR_SYSTEM_PROMPT = """\
You are the mAistro Governor — an autonomous meta-analysis agent that monitors \
the health and effectiveness of the mAistro job/task system within a project.

## Your Scope
You are entirely meta-scoped. You analyze how jobs and tasks are performing \
within the mAistro platform. You do NOT touch project code, do NOT dispatch \
tasks, and do NOT interact conversationally. You analyze, produce findings, \
and exit.

## How Coalescing Works (read this before drawing conclusions)
When tasks are coalesced under a root, only the root actually runs. The \
subordinates inherit the root's outcome (status, completed_at, etc.) but \
their *contexts* are merged into the root's user prompt — so the agent that \
ran the root saw all subordinate contexts. Do NOT infer that "only the root \
has execution metrics" means "only the root's context was seen." The agent \
saw all of them.

## What You Analyze
- **Job effectiveness** — success rates, failure patterns, turn consumption, \
timeouts, cost. Are certain jobs consistently failing or exhausting turns?
- **Scope drift** — have subscription patterns grown stale relative to actual \
project activity? Are jobs watching files that no longer change?
- **Configuration friction** — are turn limits too low (frequent exhaustion)? \
Too high (wasted budget)? Are timeouts calibrated? Subscription patterns too \
broad or narrow?
- **Implied user intent** — based on manual dispatch patterns, approval \
decisions, reply/resume frequency. What does the operator seem to want?
- **Inter-agent coordination** — are dispatch chains healthy? Missing \
dependencies? Redundant jobs?

## Tools
Use the provided MCP tools to gather data. Call them to inspect jobs, recent \
tasks, git history, and health aggregates before forming your analysis.

## Output Format
After your analysis, output your findings as a JSON array. Each finding is:
```json
[
  {
    "type": "suggestion",
    "title": "Short title (under 80 chars)",
    "body": "Detailed analysis: what the problem is, what data supports it, and the specific proposed change."
  },
  {
    "type": "observation",
    "title": "Short title (under 80 chars)",
    "body": "Describe the pattern or insight. Why it matters."
  }
]
```

For suggestions, the body MUST describe the specific change (e.g., "increase \
max_turns on job X from 50 to 100"). For observations, describe the pattern.

Output ONLY the JSON array as your final message — no preamble or explanation \
outside the array. If you have no findings, output an empty array: []
"""

EXECUTION_SYSTEM_PROMPT = """\
You are the mAistro Governor executing an approved suggestion. You have write \
access to job configuration and queue settings.

Read the current state of the target entity, then apply the change described \
in the suggestion. Make only the change described — nothing more.

After making the change, output a brief JSON summary:
```json
{"action": "what you did", "details": "specifics of the change"}
```
"""


async def run_governor(trigger: str, task_count: int | None = None):
    """Run a Governor analysis pass."""
    if _run_lock.locked():
        log.info("[governor] Skipping — already running")
        return

    async with _run_lock:
        project_dir = state.PROJECT_DIR
        if not project_dir:
            log.info("[governor] No project open — skipping")
            return

        run_id = await db.create_governor_run(trigger, task_count)
        log.info("[governor] Starting run #%d (trigger=%s)", run_id, trigger)
        mcp_config_path = None

        try:
            context = await _build_context()
            user_prompt = _format_context(context)
            mcp_config_path = _write_mcp_config(project_dir, mode="read")

            response_text = await _invoke_cli(
                user_prompt, GOVERNOR_SYSTEM_PROMPT,
                project_dir, mcp_config_path,
            )

            findings = _parse_findings(response_text)
            for f in findings:
                await db.create_governor_finding(
                    run_id, f["type"], f["title"], f["body"],
                )

            await db.complete_governor_run(run_id, len(findings))
            log.info("[governor] Run #%d completed — %d findings", run_id, len(findings))

        except Exception as e:
            log.exception("[governor] Run #%d failed: %s", run_id, e)
            await db.complete_governor_run(run_id, 0, error=str(e))
        finally:
            if mcp_config_path and os.path.exists(mcp_config_path):
                os.unlink(mcp_config_path)


async def execute_suggestion(finding_id: int):
    """Execute an approved Governor suggestion."""
    finding = await db.get_governor_finding(finding_id)
    if not finding:
        log.warning("[governor] Finding #%d not found", finding_id)
        return
    if finding["type"] != "suggestion" or finding["status"] != "approved":
        log.warning("[governor] Finding #%d is not an approved suggestion", finding_id)
        return

    project_dir = state.PROJECT_DIR
    if not project_dir:
        await db.update_governor_finding(finding_id, status="failed",
                                          execution_result="No project open")
        return

    run_id = await db.create_governor_run("execution", task_count=None)
    mcp_config_path = None

    try:
        user_prompt = (
            f"## Approved Suggestion\n\n"
            f"**{finding['title']}**\n\n{finding['body']}\n\n"
            f"Execute this change now using the available tools."
        )
        mcp_config_path = _write_mcp_config(project_dir, mode="write")

        response_text = await _invoke_cli(
            user_prompt, EXECUTION_SYSTEM_PROMPT,
            project_dir, mcp_config_path,
        )

        await db.update_governor_finding(
            finding_id, status="executed",
            execution_result=response_text[:2000],
        )
        await db.complete_governor_run(run_id, 0)
        log.info("[governor] Suggestion #%d executed", finding_id)

    except Exception as e:
        log.exception("[governor] Suggestion #%d execution failed: %s", finding_id, e)
        await db.update_governor_finding(
            finding_id, status="failed",
            execution_result=str(e)[:2000],
        )
        await db.complete_governor_run(run_id, 0, error=str(e))
    finally:
        if mcp_config_path and os.path.exists(mcp_config_path):
            os.unlink(mcp_config_path)


async def _build_context() -> dict:
    """Gather all context the Governor needs for analysis."""
    jobs = await db.list_jobs()
    recent_tasks = await db.get_recent_tasks_for_governor(limit=50)
    health = await db.dashboard_health(7)
    findings = await db.get_governor_findings(limit=30)

    project_dir = state.PROJECT_DIR
    git_log = ""
    if project_dir:
        try:
            git_log = await git.log_oneline(project_dir, limit=30)
        except Exception:
            git_log = "(git log unavailable)"

    return {
        "jobs": jobs,
        "recent_tasks": recent_tasks,
        "health": health,
        "prior_findings": findings,
        "git_log": git_log,
    }


def _format_context(ctx: dict) -> str:
    """Format gathered context into the Governor's user prompt."""
    parts = []

    parts.append("## Current Jobs\n")
    if ctx["jobs"]:
        for j in ctx["jobs"]:
            props = j.get("properties", {})
            parts.append(f"### {j['name']} (id={j['id']})")
            if props.get("summary"):
                parts.append(f"Summary: {props['summary']}")
            if props.get("description"):
                desc = props["description"][:500]
                parts.append(f"Description: {desc}")
            parts.append(f"Model: {props.get('model', 'sonnet')}")
            parts.append(f"Max turns: {props.get('max_turns', 100)}")
            parts.append(f"Timeout: {props.get('timeout', 900)}s")
            subs = props.get("subscriptions", [])
            if subs:
                parts.append(f"Subscriptions: {', '.join(subs)}")
            parts.append("")
    else:
        parts.append("(no jobs configured)\n")

    parts.append("## Recent Tasks (last 50 terminal)\n")
    parts.append(
        "Note: tasks marked `coalesced→#N` were folded into task #N's "
        "execution — they did not run independently. The displayed "
        "turns/cost are inherited from #N's single run; do NOT sum "
        "metrics across coalesced subordinates.\n"
    )
    if ctx["recent_tasks"]:
        for t in ctx["recent_tasks"]:
            line = f"- Task #{t['id']} [{t['job_name']}] status={t['status']}"
            if t.get("is_subordinate"):
                line += f" coalesced→#{t['effective_root_id']}"
            if t.get("num_turns"):
                line += f" turns={t['num_turns']}"
            if t.get("cost_usd"):
                line += f" cost=${t['cost_usd']:.4f}"
            if t.get("error"):
                line += f" error=\"{t['error'][:100]}\""
            line += f" trigger={t.get('trigger', '?')}"
            parts.append(line)
        parts.append("")
    else:
        parts.append("(no recent tasks)\n")

    parts.append("## Job Health (7-day window)\n")
    if ctx["health"]:
        for h in ctx["health"]:
            total = h.get("total", 0)
            completed = h.get("completed", 0)
            rate = f"{completed/total*100:.0f}%" if total > 0 else "n/a"
            parts.append(
                f"- {h.get('job_name', '?')}: {total} tasks, "
                f"{completed} completed ({rate}), "
                f"{h.get('failed', 0)} failed, "
                f"{h.get('exhausted', 0)} exhausted, "
                f"{h.get('timed_out', 0)} timed out"
            )
        parts.append("")
    else:
        parts.append("(no health data)\n")

    parts.append("## Recent Git Activity\n")
    parts.append(ctx.get("git_log") or "(no git log)")
    parts.append("")

    if ctx["prior_findings"]:
        parts.append("## Your Prior Findings\n")
        for f in ctx["prior_findings"]:
            status_str = f"[{f['status']}]" if f["type"] == "suggestion" else f"[{f['status']}]"
            parts.append(f"- ({f['type']}) {status_str} {f['title']}")
            parts.append(f"  {f['body'][:200]}")
        parts.append("")

    parts.append(
        "Analyze the above data. Use the MCP tools if you need more detail "
        "on specific jobs or tasks. Then output your findings as a JSON array."
    )

    return "\n".join(parts)


def _write_mcp_config(project_dir: str, mode: str = "read") -> str:
    """Write a Governor-specific MCP config to a temp file."""
    server_script = os.path.join(os.path.dirname(__file__), "governor_mcp.py")
    config = {
        "mcpServers": {
            "governor": {
                "type": "stdio",
                "command": sys.executable,
                "args": [server_script],
                "env": {
                    "MAISTRO_PROJECT_DIR": project_dir,
                    "MAISTRO_BACKEND_PORT": "8420",
                    "MAISTRO_GOVERNOR_MODE": mode,
                },
            }
        }
    }
    fd, path = tempfile.mkstemp(suffix=".json", prefix="maistro-governor-mcp-")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


async def _invoke_cli(
    prompt: str,
    system_prompt: str,
    cwd: str,
    mcp_config_path: str,
) -> str:
    """Invoke Claude CLI for Governor, collect full response text."""
    full_response = []
    async for event in cli.invoke(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=cwd,
        model="sonnet",
        mcp_config_path=mcp_config_path,
        max_turns=20,
    ):
        etype = event.get("type", "")
        if etype == "text":
            full_response.append(event.get("content", ""))
        elif etype == "assistant_complete":
            full_response.append(event.get("content", ""))
        elif etype == "error":
            raise RuntimeError(event.get("message", "CLI error"))
    return "".join(full_response)


def _parse_findings(text: str) -> list[dict]:
    """Parse Governor output into a list of finding dicts."""
    # Try direct JSON parse
    try:
        data = json.loads(text.strip())
        if isinstance(data, list):
            return _validate_findings(data)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fences
    import re
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1).strip())
            if isinstance(data, list):
                return _validate_findings(data)
        except json.JSONDecodeError:
            pass

    # Try finding a JSON array anywhere in the text
    bracket_start = text.find("[")
    bracket_end = text.rfind("]")
    if bracket_start >= 0 and bracket_end > bracket_start:
        try:
            data = json.loads(text[bracket_start:bracket_end + 1])
            if isinstance(data, list):
                return _validate_findings(data)
        except json.JSONDecodeError:
            pass

    # Last resort: if there's meaningful text, create an observation about parse failure
    if text.strip():
        log.warning("[governor] Could not parse findings from response (len=%d)", len(text))
        return [{
            "type": "observation",
            "title": "Governor analysis (unstructured)",
            "body": text[:2000],
        }]

    return []


def _validate_findings(data: list) -> list[dict]:
    """Validate and normalize parsed findings."""
    valid = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ftype = item.get("type", "")
        if ftype not in ("suggestion", "observation"):
            continue
        title = str(item.get("title", ""))[:200]
        body = str(item.get("body", ""))
        if not title or not body:
            continue
        valid.append({"type": ftype, "title": title, "body": body})
    return valid
