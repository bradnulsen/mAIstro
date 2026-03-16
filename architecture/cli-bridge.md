# CLI Bridge

The CLI bridge invokes Claude Code as a subprocess, manages its lifecycle, parses the NDJSON output stream, and translates raw CLI events into the platform's event schema.

**Module**: `backend/cli.py`

## Subprocess Invocation

Claude CLI is located via `shutil.which("claude")` at invocation time. The process is spawned with `subprocess.Popen` — not `asyncio.create_subprocess_exec` — because Windows `ProactorEventLoop` raises `NotImplementedError` for async subprocess pipes.

### Command Construction

```
claude -p --output-format stream-json --model <model> --max-turns 50 --verbose --dangerously-skip-permissions
```

Optional flags:
- `--resume <session_id>` for resume tasks
- `--allowedTools <tools...>` from job's `allowed_tools` property — sets which tools are auto-approved (permission scoping)
- `--disallowedTools <tools...>` **computed by the platform** — the complement of `allowed_tools`. Hides excluded tools from the agent entirely. This is the enforcement mechanism that actually restricts tool access, critical with `--dangerously-skip-permissions` where `--allowedTools` alone does not restrict visibility
- `--mcp-config <path>` for MCP server connections (internal platform server and any external servers enabled for the job)

The user configures only `allowed_tools`. The platform computes the complement and passes `--disallowedTools` to hide everything else. The agent's environment contains exactly the tools it can use — no more.

### Stdin Piping

The prompt and system prompt are piped via stdin rather than command-line arguments. This avoids Windows `.cmd` argument quoting issues with multi-line strings.

For fresh sessions, stdin contains:
```
<system-instructions>
{system_prompt}
</system-instructions>

{user_prompt}
```

For resume sessions, the system prompt is omitted (the CLI retains it from the original session).

## Thread-Based Pipe Reading

Two daemon threads read stdout and stderr simultaneously, pushing lines into a shared `asyncio.Queue` via `loop.call_soon_threadsafe()`. This bridges the synchronous pipe reads with the async event loop.

Both pipes are read because the CLI writes NDJSON to stderr when using `--resume`.

Sentinel: each thread pushes `None` when its pipe closes. The main loop counts two `None` values (one per pipe) before considering the process done.

## Event Translation

Raw NDJSON lines are parsed and translated through `_translate_event()`. Every valid JSON line yields two things:

1. A `_raw` event (always) — the original JSON line, stored as an audit trail
2. A translated event (when applicable) — normalized to the platform's schema

### Platform Event Schema

| Type | Content | Source |
|------|---------|--------|
| `text` | Streaming text delta | `content_block_delta` with `text_delta` |
| `thinking` | Extended thinking delta | `content_block_delta` with `thinking_delta` |
| `tool_use` | Tool invocation start | `content_block_start` with tool_use block, or `assistant` with tool_use content |
| `assistant_complete` | Full assistant turn text | `assistant` message type — authoritative, used for DB storage |
| `session_id` | CLI session identifier | `result`, `system`, or `init` events |
| `error` | Error message | `result` with `is_error=true`, process exit errors |

### Dual Text Paths

Text arrives in two forms:
- **Streaming deltas** (`text` events): real-time chunks for live display
- **Assistant complete** (`assistant_complete`): the full turn text, emitted after the turn finishes

The worker prefers `assistant_complete` for database storage (it's authoritative), falling back to concatenated streaming deltas if the complete message isn't received.

## Cancellation

The CLI bridge checks a shared `asyncio.Event` on each iteration of the read loop. When set:

1. `process.terminate()` (SIGTERM)
2. Wait up to 5 seconds for clean exit
3. If still alive, `process.kill()` (SIGKILL)
4. Yield a cancellation error event and return

## Error Handling

- Non-JSON lines from stdout/stderr are collected and reported if the process exits with a non-zero code
- Thread reader exceptions are marshaled back through the queue as `__ERROR__` sentinel strings
- Process-level exceptions (e.g., failed spawn) kill the process and yield an error event

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) invokes `cli.invoke()` via `run_dispatch()` and manages the lifecycle around it
- [Prompt Assembly](prompt-assembly.md) builds the prompts that are piped to stdin
- [Streaming and Sessions](streaming-and-sessions.md) consumes the events yielded by the bridge
- [Tool Mediation](tool-mediation.md) provides the internal MCP server that the CLI connects to
