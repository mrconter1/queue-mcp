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

## Tell Claude to use it

**This step is what makes it automatic.** Installing the server only provides
the queue; Claude will not use it for anything until told to. Add this to your
`CLAUDE.md`, then talk to Claude exactly as usual and every task you give it
goes through the queue.

````markdown
## Task queue (queue-mcp)

The `queue` MCP server holds a per-session work queue. Drain it in order.

**Your session id** is the UUID in your scratchpad path. Pass it as
`session_id` to every queue tool. Never guess it, and never reuse one from an
earlier conversation.

**Everything the user asks you to do goes in the queue first, single tasks
included.** Do not weigh up whether a request is big enough to be worth
queueing. That judgment is exactly what fails: under momentum it always comes
out "this one is small, just start", and the rule quietly stops existing.

The test is **do vs say**, and it is the only judgment you make:
- If fulfilling the request means *doing* something, writing, editing, running,
  building, investigating, it goes in the queue before any of it starts.
- If it means only *saying* something, a question about this conversation, an
  opinion, a design you are discussing, answer inline and queue nothing.

On receiving a request that passes the test:

1. `queue_add` each step as its own item, in order, before doing any work. A
   chain ("do X, then Y") is one item per step.
2. Check `queue_current`. If nothing is in progress, `queue_next` immediately
   and start the first item.
3. If something is already in progress, just append and say so. Never jump the
   queue or run two items at once.

**The loop:**
1. `queue_next` claims one item and marks it in progress.
2. Do the work.
3. `queue_complete` with a one-line result. Mark `failed: true` if it did not
   work; do not silently mark it done.
4. Back to step 1. Stop when `queue_next` returns `drained: true`.

**Rules that matter:**
- **One at a time.** Never claim a new item while one is in progress.
- **`drained: true` means stop**, not "go look for more work".
- **A failed item does not block the queue.** Record it, report it, move on,
  and flag it in the final summary.
- **The queue records the user's intent, not yours.** Never queue work they did
  not ask for, beyond splitting their request into steps.
- **If the queue server is unreachable, say so and do the work anyway.** The
  queue is a coordination aid, not a gate.
````

Trim it to taste, but keep the do vs say test and the one-at-a-time rule. Those
two carry most of the behaviour.

## Adding work while it runs

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
- **The loop is model-mediated, not enforced.** Claude queues and drains
  because `CLAUDE.md` tells it to, not because anything makes it. A `Stop` hook
  would make it strict.

Config: `QUEUE_MCP_PORT`, `QUEUE_MCP_HOST`, `QUEUE_MCP_HOME`.
