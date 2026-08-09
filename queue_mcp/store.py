"""File-backed, session-keyed task queue.

One JSON file per Claude Code session under ``~/.queue-mcp/sessions``. Both the
MCP server and the ``q`` CLI go through this module, so items can be added from
another terminal while a session is mid-turn -- that is the whole point of the
queue, since anything typed at the Claude prompt steers the running turn instead
of waiting for it.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(os.environ.get("QUEUE_MCP_HOME", Path.home() / ".queue-mcp"))
SESSIONS = ROOT / "sessions"
LAST_SESSION = ROOT / "last-session"

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"

# A lock older than this is assumed to belong to a crashed process.
STALE_LOCK_SECONDS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_name(session_id: str) -> str:
    """Reject anything that could escape the sessions directory."""
    cleaned = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if not cleaned:
        raise ValueError(f"unusable session id: {session_id!r}")
    return cleaned


def _path(session_id: str) -> Path:
    return SESSIONS / f"{_safe_name(session_id)}.json"


@contextmanager
def _lock(session_id: str) -> Iterator[None]:
    """Cross-process lock so the server and CLI cannot interleave writes."""
    SESSIONS.mkdir(parents=True, exist_ok=True)
    lock_path = _path(session_id).with_suffix(".lock")
    deadline = time.monotonic() + STALE_LOCK_SECONDS
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > STALE_LOCK_SECONDS:
                lock_path.unlink(missing_ok=True)
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"queue for {session_id} stayed locked")
            time.sleep(0.05)
    try:
        os.close(fd)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _blank(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "created": _now(),
        "updated": _now(),
        "next_id": 1,
        "items": [],
    }


def _read(session_id: str) -> dict[str, Any]:
    path = _path(session_id)
    if not path.exists():
        return _blank(session_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Never lose the queue to a half-written file; park it and start clean.
        path.replace(path.with_suffix(f".corrupt-{int(time.time())}.json"))
        return _blank(session_id)


def _write(session_id: str, data: dict[str, Any]) -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    data["updated"] = _now()
    path = _path(session_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on Windows and POSIX
    LAST_SESSION.write_text(session_id, encoding="utf-8")


def _find(data: dict[str, Any], item_id: int) -> dict[str, Any]:
    for item in data["items"]:
        if item["id"] == item_id:
            return item
    raise KeyError(f"no item {item_id} in this queue")


def add(session_id: str, text: str, mode: str = "fork", position: int | None = None) -> dict[str, Any]:
    """Append an item, or insert it at ``position`` among the pending items."""
    text = text.strip()
    if not text:
        raise ValueError("queue items need text")
    if mode not in ("fork", "inline"):
        raise ValueError("mode must be 'fork' or 'inline'")
    with _lock(session_id):
        data = _read(session_id)
        item = {
            "id": data["next_id"],
            "text": text,
            "mode": mode,
            "status": PENDING,
            "added": _now(),
            "started": None,
            "finished": None,
            "result": None,
        }
        data["next_id"] += 1
        if position is None:
            data["items"].append(item)
        else:
            data["items"].insert(max(0, position), item)
        _write(session_id, data)
        return item


def listing(session_id: str, include_finished: bool = False) -> dict[str, Any]:
    data = _read(session_id)
    items = data["items"]
    if not include_finished:
        items = [i for i in items if i["status"] in (PENDING, IN_PROGRESS)]
    counts = {s: 0 for s in (PENDING, IN_PROGRESS, DONE, FAILED)}
    for item in data["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "session_id": session_id,
        "items": items,
        "counts": counts,
        "updated": data["updated"],
    }


def claim_next(session_id: str) -> dict[str, Any] | None:
    """Return the next pending item and mark it in progress.

    Returns None when the queue is drained, which is the signal to stop the
    fork chain rather than start another turn.
    """
    with _lock(session_id):
        data = _read(session_id)
        for item in data["items"]:
            if item["status"] == IN_PROGRESS:
                return item  # never run two at once
        for item in data["items"]:
            if item["status"] == PENDING:
                item["status"] = IN_PROGRESS
                item["started"] = _now()
                _write(session_id, data)
                return item
        return None


def current(session_id: str) -> dict[str, Any] | None:
    for item in _read(session_id)["items"]:
        if item["status"] == IN_PROGRESS:
            return item
    return None


def finish(session_id: str, item_id: int, result: str = "", failed: bool = False) -> dict[str, Any]:
    with _lock(session_id):
        data = _read(session_id)
        item = _find(data, item_id)
        item["status"] = FAILED if failed else DONE
        item["finished"] = _now()
        item["result"] = result or None
        _write(session_id, data)
        return item


def remove(session_id: str, item_id: int) -> dict[str, Any]:
    with _lock(session_id):
        data = _read(session_id)
        item = _find(data, item_id)
        data["items"] = [i for i in data["items"] if i["id"] != item_id]
        _write(session_id, data)
        return item


def move(session_id: str, item_id: int, position: int) -> dict[str, Any]:
    with _lock(session_id):
        data = _read(session_id)
        item = _find(data, item_id)
        data["items"] = [i for i in data["items"] if i["id"] != item_id]
        data["items"].insert(max(0, min(position, len(data["items"]))), item)
        _write(session_id, data)
        return item


def clear(session_id: str, finished_only: bool = True) -> int:
    with _lock(session_id):
        data = _read(session_id)
        before = len(data["items"])
        if finished_only:
            data["items"] = [i for i in data["items"] if i["status"] in (PENDING, IN_PROGRESS)]
        else:
            data["items"] = []
        _write(session_id, data)
        return before - len(data["items"])


def sessions() -> list[dict[str, Any]]:
    """Every known queue, newest activity first -- used by the CLI to pick a default."""
    if not SESSIONS.exists():
        return []
    out = []
    for path in SESSIONS.glob("*.json"):
        if path.name.endswith(".tmp") or ".corrupt-" in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pending = sum(1 for i in data["items"] if i["status"] == PENDING)
        running = sum(1 for i in data["items"] if i["status"] == IN_PROGRESS)
        out.append(
            {
                "session_id": data["session_id"],
                "pending": pending,
                "in_progress": running,
                "total": len(data["items"]),
                "updated": data.get("updated", ""),
            }
        )
    return sorted(out, key=lambda s: s["updated"], reverse=True)


def last_session() -> str | None:
    if LAST_SESSION.exists():
        value = LAST_SESSION.read_text(encoding="utf-8").strip()
        return value or None
    known = sessions()
    return known[0]["session_id"] if known else None
