# queue-mcp

A task queue for Claude Code. Line up work from another terminal, and Claude
drains it one item at a time.

## The problem

Type at the Claude Code prompt while a turn is running and your message gets
**steered** into that turn at the next tool boundary. It does not wait. That is
the right behaviour for "no, use the other file" mid-edit, but it makes it
impossible to line up three follow-ups and walk away.

This queue lives outside the conversation, so nothing you add can disturb the
running turn. Wanted upstream too:
[#50246](https://github.com/anthropics/claude-code/issues/50246) (217
reactions), [#73661](https://github.com/anthropics/claude-code/issues/73661),
[#82348](https://github.com/anthropics/claude-code/issues/82348),
[#63190](https://github.com/anthropics/claude-code/issues/63190).

## Install

Via [mcp-orchestrator](https://github.com/mrconter1/mcp-orchestrator), which
also keeps it running:

```
mcp_install("queue")
```

Or by hand:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run.ps1
claude mcp add --transport http --scope user queue http://127.0.0.1:8766/mcp
```

## Use

```powershell
.\q.ps1 add "fix the failing auth test"
.\q.ps1 add --next "urgent: revert the migration"
.\q.ps1 list
```

Claude claims one item, works it, marks it done, then claims the next. It stops
when the queue reports `drained`.

## Tools

| Tool | Purpose |
| --- | --- |
| `queue_add` | Append or insert an item |
| `queue_list` / `queue_current` | What is queued, what is running |
| `queue_next` | Claim the next item |
| `queue_complete` | Mark done or failed |
| `queue_remove` / `queue_move` / `queue_clear` | Edit the queue |
| `queue_sessions` / `queue_history` | Every known queue, and past work |

## Worth knowing

- **Queues are per session.** Every tool takes `session_id`, because an MCP
  server cannot tell which session is calling it.
- **Data lives in `~/.queue-mcp`.** Restarting the server loses nothing.
- **The CLI writes the same files as the server**, so adding work does not
  depend on the server being up.
- **The loop is model-mediated, not enforced.** Claude drains the queue because
  it is told to. A `Stop` hook would make it strict.

Config: `QUEUE_MCP_PORT`, `QUEUE_MCP_HOST`, `QUEUE_MCP_HOME`.
