"""HTTP MCP server exposing the per-session task queue.

Run it once and leave it running::

    .venv\\Scripts\\python.exe -m queue_mcp.server

It listens on 127.0.0.1 only. Every tool takes ``session_id`` explicitly,
because an MCP server has no way to know which Claude Code session is calling
it -- unlike hooks, which get ``session_id`` on stdin for free.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from queue_mcp import store

HOST = os.environ.get("QUEUE_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("QUEUE_MCP_PORT", "8766"))

server = MCPServer(
    "queue",
    instructions=(
        "Per-session task queue. Work items through it one at a time: call "
        "queue_next to claim the next item, do the work (spawn a forked "
        "subagent when the item's mode is 'fork'), then call queue_complete "
        "before claiming the next. Stop when queue_next returns nothing. "
        "Always pass the current Claude Code session id as session_id. "
        "queue_history answers what has already been processed, across all "
        "sessions, including items that were dropped before they ran."
    ),
)


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _err(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


@server.tool()
def queue_add(session_id: str, text: str, mode: str = "fork", position: int | None = None) -> dict[str, Any]:
    """Add an item to a session's queue.

    mode 'fork' runs the item in a forked subagent (keeps the main thread
    clear); mode 'inline' runs it in the main thread. position inserts rather
    than appends.
    """
    try:
        return _ok(item=store.add(session_id, text, mode, position))
    except (ValueError, TimeoutError) as exc:
        return _err(str(exc))


@server.tool()
def queue_list(session_id: str, include_finished: bool = False) -> dict[str, Any]:
    """List a session's queue with per-status counts."""
    try:
        return _ok(**store.listing(session_id, include_finished))
    except ValueError as exc:
        return _err(str(exc))


@server.tool()
def queue_next(session_id: str) -> dict[str, Any]:
    """Claim the next pending item and mark it in progress.

    Returns item=null when the queue is drained -- that is the signal to stop
    and hand control back to the user, not to look for more work.
    """
    try:
        item = store.claim_next(session_id)
    except (ValueError, TimeoutError) as exc:
        return _err(str(exc))
    if item is None:
        return _ok(item=None, drained=True)
    return _ok(item=item, drained=False)


@server.tool()
def queue_current(session_id: str) -> dict[str, Any]:
    """Show the item currently in progress, if any."""
    try:
        return _ok(item=store.current(session_id))
    except ValueError as exc:
        return _err(str(exc))


@server.tool()
def queue_complete(session_id: str, item_id: int, result: str = "", failed: bool = False) -> dict[str, Any]:
    """Mark an item done (or failed) and record a short result summary."""
    try:
        return _ok(item=store.finish(session_id, item_id, result, failed))
    except (KeyError, ValueError, TimeoutError) as exc:
        return _err(str(exc))


@server.tool()
def queue_remove(session_id: str, item_id: int) -> dict[str, Any]:
    """Drop an item from the queue entirely."""
    try:
        return _ok(item=store.remove(session_id, item_id))
    except (KeyError, ValueError, TimeoutError) as exc:
        return _err(str(exc))


@server.tool()
def queue_move(session_id: str, item_id: int, position: int) -> dict[str, Any]:
    """Reorder an item; position 0 puts it next."""
    try:
        return _ok(item=store.move(session_id, item_id, position))
    except (KeyError, ValueError, TimeoutError) as exc:
        return _err(str(exc))


@server.tool()
def queue_clear(session_id: str, finished_only: bool = True) -> dict[str, Any]:
    """Remove finished items, or everything when finished_only is false."""
    try:
        return _ok(removed=store.clear(session_id, finished_only))
    except (ValueError, TimeoutError) as exc:
        return _err(str(exc))


@server.tool()
def queue_history(
    session_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    outcome: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """What has actually been processed, newest first.

    Spans every session unless session_id is given. Each record carries the
    item, its outcome ('done', 'failed' or 'dropped'), when it was queued,
    started and finished, a computed duration_seconds and the result summary.
    since/until accept a bare YYYY-MM-DD or a full ISO timestamp, and until
    includes the whole of that day. counts and totals describe the full match,
    not just the page returned by limit.
    """
    try:
        return _ok(**store.history(session_id, since, until, outcome, limit))
    except ValueError as exc:
        return _err(str(exc))


@server.tool()
def queue_sessions() -> dict[str, Any]:
    """List every known queue, newest activity first."""
    return _ok(sessions=store.sessions(), last=store.last_session())


def main() -> None:
    print(f"queue-mcp listening on http://{HOST}:{PORT}/mcp  (data: {store.ROOT})")
    server.run(transport="streamable-http", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
