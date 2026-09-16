"""The queue as a page, served beside the MCP endpoint.

The tools, the CLI and this board all go through :mod:`queue_mcp.store`, so
there is one queue and not two: whatever the board shows is the file the next
``queue_next`` will read, and an edit made here is seen by a session that is
mid-turn. The board is mounted on the MCP server's own app rather than run as a
second service, so there is still one process to start and one port to
remember.

The page itself is a file on disk, read per request. Editing ``board.html``
therefore shows up on a browser reload, with no restart of the server that is
holding the queue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from queue_mcp import store

BOARD = Path(__file__).with_name("board.html")

# The board polls; nothing it fetches may be answered from a cache, or it would
# show a queue that has already moved on.
NO_STORE = {"Cache-Control": "no-store"}


def _default_session(sessions: list[dict[str, Any]]) -> str | None:
    """Which queue the board opens on.

    The newest queue that still has work, rather than the one written to most
    recently: several Claude sessions run on this machine at once, and the last
    write is as likely to be another session finishing its last item as it is
    to be the queue somebody opened this page to watch. Sessions arrive newest
    first, so the first match is the newest one.
    """
    for session in sessions:
        if session["pending"] or session["in_progress"]:
            return session["session_id"]
    return sessions[0]["session_id"] if sessions else None


def _state(session_id: str | None) -> dict[str, Any]:
    """Everything the board draws: the queues there are, and the one in view."""
    sessions = store.sessions()
    known = {s["session_id"] for s in sessions}
    chosen = session_id if session_id in known else _default_session(sessions)
    if chosen is None:
        return {"sessions": sessions, "session_id": None, "items": [], "counts": {}, "updated": None}
    return {"sessions": sessions, **store.listing(chosen, include_finished=True)}


def attach(server: Any) -> None:
    """Add the board's routes to an MCP server instance."""

    @server.custom_route("/", methods=["GET"])
    async def board(request: Request) -> Response:  # noqa: ARG001 - Starlette hands us the request
        return HTMLResponse(BOARD.read_text(encoding="utf-8"), headers=NO_STORE)

    @server.custom_route("/api/state", methods=["GET"])
    async def state(request: Request) -> Response:
        try:
            return JSONResponse(_state(request.query_params.get("session")), headers=NO_STORE)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=NO_STORE)

    # Browsers ask for this unprompted; answering plainly keeps the log readable.
    @server.custom_route("/favicon.ico", methods=["GET"])
    async def favicon(request: Request) -> Response:  # noqa: ARG001
        return Response(status_code=204)
