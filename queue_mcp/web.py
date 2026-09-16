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
from urllib.parse import urlparse

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


def _from_the_board(request: Request) -> bool:
    """Whether a write really came from this page, and not from another site.

    The board answers on a loopback port, which any page in the browser can
    post to. Two cheap checks stop that: a cross-origin form post cannot set a
    JSON content type without a preflight the server never answers, and an
    Origin, when the browser sends one, has to be this server.
    """
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/json"):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True  # same-origin fetches often omit it; a form post cannot get this far
    host = urlparse(origin).hostname
    return host in ("127.0.0.1", "localhost", "::1")


async def _body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError:
        raise ValueError("expected a JSON body")
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def _session_of(payload: dict[str, Any]) -> str:
    session_id = payload.get("session")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("which queue? pass session")
    return session_id


def _item_of(payload: dict[str, Any]) -> int:
    try:
        return int(payload["id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("which item? pass id") from None


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

    async def _write(request: Request, apply) -> Response:
        """Run one change and answer with the queue as it now stands.

        The whole state comes back from every write, so the page redraws from
        the server's answer rather than from what it guessed would happen - the
        session it is watching may have claimed an item in the same moment.
        """
        if not _from_the_board(request):
            return JSONResponse({"error": "not from the board"}, status_code=403, headers=NO_STORE)
        try:
            payload = await _body(request)
            session_id = _session_of(payload)
            apply(session_id, payload)
            return JSONResponse(_state(session_id), headers=NO_STORE)
        except (KeyError, ValueError, TimeoutError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=NO_STORE)

    @server.custom_route("/api/add", methods=["POST"])
    async def add(request: Request) -> Response:
        return await _write(request, lambda session, body: store.add(
            session,
            str(body.get("text", "")),
            str(body.get("mode", "inline")),
            body.get("position"),
        ))

    @server.custom_route("/api/edit", methods=["POST"])
    async def edit(request: Request) -> Response:
        return await _write(request, lambda session, body: store.edit(
            session,
            _item_of(body),
            body.get("text"),
            body.get("mode"),
        ))

    @server.custom_route("/api/remove", methods=["POST"])
    async def remove(request: Request) -> Response:
        return await _write(request, lambda session, body: store.remove(session, _item_of(body)))

    @server.custom_route("/api/reorder", methods=["POST"])
    async def reorder(request: Request) -> Response:
        def apply(session: str, body: dict[str, Any]) -> None:
            ids = body.get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                raise ValueError("pass ids as a list of item ids, in the order you want them")
            store.reorder(session, ids)

        return await _write(request, apply)

    # Browsers ask for this unprompted; answering plainly keeps the log readable.
    @server.custom_route("/favicon.ico", methods=["GET"])
    async def favicon(request: Request) -> Response:  # noqa: ARG001
        return Response(status_code=204)
