# queue-mcp

A per-session task queue for Claude Code, served over local HTTP MCP.

## Why this exists

Anything typed at the Claude Code prompt while a turn is running gets **steered**
into that turn at the next tool boundary - it does not wait for the turn to
finish. That is deliberate (it is how "no, use the other file" works mid-edit),
but it makes it impossible to line up several follow-ups and walk away.

This queue lives outside the conversation. You add items from a second terminal
or from another editor, so nothing you queue can disturb the running turn.
Claude drains the queue one item at a time, each starting only after the
previous one has fully finished.

Related upstream requests, none shipped as of Aug 2026:
[#63190](https://github.com/anthropics/claude-code/issues/63190) (deferred
messages), [#49373](https://github.com/anthropics/claude-code/issues/49373)
(true end-of-turn queueing), [#50246](https://github.com/anthropics/claude-code/issues/50246).

## Running it

No autostart yet - start it by hand and leave it running:

```powershell
cd C:\Users\rasmus.lindahl\Repos\queue-mcp
.\run.ps1
```

It binds `127.0.0.1:8766` only. Override with `QUEUE_MCP_PORT` /
`QUEUE_MCP_HOST`; queue data lives in `~/.queue-mcp` (`QUEUE_MCP_HOME`).

Already registered with Claude Code at user scope:

```
claude mcp add --transport http --scope user queue http://127.0.0.1:8766/mcp
```

## Adding work without steering the session

```powershell
cd C:\Users\rasmus.lindahl\Repos\queue-mcp
.\q.ps1 add "fix the failing auth test"
.\q.ps1 add --next "urgent: revert the migration"   # jump the queue
.\q.ps1 add --inline "small tweak"                  # run in main thread, not a fork
.\q.ps1 list
.\q.ps1 sessions
```

`--session` targets a specific session; without it the CLI uses whichever queue
was touched most recently.

## How the loop keeps going

An MCP server cannot start a Claude turn - tools only run inside a turn that is
already happening. The loop is driven by **forked subagents**: when a
background agent finishes, the harness re-invokes Claude, which claims the next
item. Claude stops when `queue_next` reports `drained: true`.

That makes the loop model-mediated rather than enforced. A `Stop` hook is the
hard-enforcement version and can be layered on later if drift shows up.

## Tools

| Tool | Purpose |
| --- | --- |
| `queue_add` | Append or insert an item (`mode`: `fork` or `inline`) |
| `queue_list` | Items plus per-status counts |
| `queue_next` | Claim the next pending item; `drained: true` means stop |
| `queue_current` | What is in progress |
| `queue_complete` | Mark done or failed, with a result summary |
| `queue_remove` | Drop an item |
| `queue_move` | Reorder; position 0 runs next |
| `queue_clear` | Clear finished items, or everything |
| `queue_sessions` | Every known queue, newest first |

Every per-session tool takes `session_id` explicitly. An MCP server has no way
to know which Claude Code session is calling it - unlike hooks, which receive
`session_id` on stdin. Claude reads its own session id from its scratchpad path.

## Layout

```
queue_mcp/store.py    file-backed queue, cross-process locking, atomic writes
queue_mcp/server.py   HTTP MCP server
queue_mcp/cli.py      out-of-band CLI (writes the same files)
```

The CLI and the server share the store rather than talking to each other, so
adding work does not depend on the server being up.

## Status

Verified: store claim/complete cycle, single-item-at-a-time claiming, drained
signal, corrupt-file recovery, all nine tools over HTTP, error handling for
unknown ids, and Claude Code reporting the server as connected.

Not yet done: autostart on login, the `Stop` hook safety net, and mirroring the
queue into Claude's native task list for `Ctrl+T` visibility.
