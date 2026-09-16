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
# Append-only record of everything that left a queue, across all sessions. The
# per-session files are working state and get cleared; this is the log that is
# meant to survive, so nothing is ever rewritten or deleted here.
HISTORY = ROOT / "history.jsonl"

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"
# Not a queue status: an outcome recorded in history when an item is killed
# before it finished, which is as much a part of the record as completing one.
DROPPED = "dropped"

# A lock older than this is assumed to belong to a crashed process.
STALE_LOCK_SECONDS = 30

# How long to keep trying when the filesystem says "access denied".
#
# On Windows a file that is open cannot be replaced, and a file whose replace is
# in flight cannot be opened; both come back as access denied and both clear in
# well under a millisecond. It is not an exotic case: one writer against six
# readers - which is a session working while a board watches - produced 786
# failed writes against 454 that got through, and one read in ten failed. The
# answer to either is to wait a moment and try again, because the work is
# perfectly good; only the timing was unlucky.
BUSY_RETRY_SECONDS = 2.0


def _through_a_busy_file(action: Any) -> Any:
    """Run a file operation, waiting out a rename or an open that collides."""
    deadline = time.monotonic() + BUSY_RETRY_SECONDS
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.005)


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
    refused: OSError | None = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            refused = None
        except PermissionError as exc:
            # Windows, and only Windows: a file whose delete is still pending
            # cannot be opened at all, and the failure is "access denied"
            # rather than "it exists". That is another process releasing this
            # very lock, so it is a reason to wait like any other - but the
            # loop used to let it out as an exception, and an add, a claim or a
            # completion simply died. Measured at 2% of acquisitions with six
            # processes contending, which is a board and a session working
            # together on a busy queue. Kept in case the deadline runs out,
            # when a real permission problem is the honest thing to report.
            refused = exc

        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            age = 0.0  # gone, or going: try again rather than judge it
        if age > STALE_LOCK_SECONDS:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass  # another process broke it first, which is just as good
            continue
        if time.monotonic() > deadline:
            raise TimeoutError(f"queue for {session_id} stayed locked") from refused
        time.sleep(0.02)
    try:
        os.close(fd)
        yield
    finally:
        # The same delete-pending window: a release that collides with another
        # process looking at the lock must not throw, or the work that was just
        # written would be reported as having failed.
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _blank(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "created": _now(),
        "updated": _now(),
        "next_id": 1,
        "items": [],
    }


def _read_text(path: Path) -> str | None:
    """A file's contents, or None when it is not there, waiting out a busy moment."""
    try:
        return _through_a_busy_file(lambda: path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _read(session_id: str) -> dict[str, Any]:
    path = _path(session_id)
    raw = _read_text(path)
    if raw is None:
        return _blank(session_id)
    try:
        return json.loads(raw)
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
    # Atomic on Windows and POSIX, but on Windows it fails outright while any
    # reader has the file open - and a board polling this queue holds it open
    # briefly on every change. The caller holds the session lock, so nothing
    # else can be writing; waiting the reader out is safe and it is the
    # difference between a task being queued and an error in the user's face.
    _through_a_busy_file(lambda: tmp.replace(path))
    _through_a_busy_file(lambda: LAST_SESSION.write_text(session_id, encoding="utf-8"))


def _duration(started: str | None, finished: str | None) -> int | None:
    if not started or not finished:
        return None
    try:
        return int((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds())
    except ValueError:
        return None


def _archive(session_id: str, item: dict[str, Any], outcome: str) -> dict[str, Any]:
    """Append one immutable history record for an item leaving the queue.

    Callers already hold the session lock. Failures are swallowed on purpose:
    losing a history line is regrettable, but it must never take the queue down
    with it.
    """
    record = {
        "session_id": session_id,
        "id": item.get("id"),
        "text": item.get("text"),
        "mode": item.get("mode"),
        "outcome": outcome,
        "added": item.get("added"),
        "started": item.get("started"),
        # When the worker actually began, where it was said. Kept beside
        # "started" rather than replacing it: one is when the item was taken,
        # the other when it was worked, and the wait between them is worth
        # keeping in a record of what a queue actually did.
        "running": item.get("running"),
        "finished": item.get("finished"),
        "recorded": _now(),
        "duration_seconds": _duration(item.get("started"), item.get("finished")),
        "result": item.get("result"),
    }
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass
    return record


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
            "running": None,
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


def mark_running(session_id: str, item_id: int) -> dict[str, Any]:
    """Record that work on a claimed item has actually begun.

    ``claim_next`` only says an item has been handed over. When the work is done
    by a subagent that takes a while to get going, the gap between taken and
    being worked on is long enough to watch, and a board that calls both of them
    the same thing cannot tell you which one you are looking at.

    Said once: the first time is when work began, and saying it again does not
    move that. Items written before this field existed simply do not have it,
    which reads as claimed - the honest answer for a queue that was never told.
    """
    with _lock(session_id):
        data = _read(session_id)
        item = _find(data, item_id)
        if item["status"] != IN_PROGRESS:
            raise ValueError("only a claimed item can be running; claim it with queue_next first")
        if not item.get("running"):
            item["running"] = _now()
            _write(session_id, data)
        return item


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
        _archive(session_id, item, item["status"])
        _write(session_id, data)
        return item


def remove(session_id: str, item_id: int) -> dict[str, Any]:
    with _lock(session_id):
        data = _read(session_id)
        item = _find(data, item_id)
        # Killing an unfinished item is part of the record. A finished one was
        # already archived when it finished, so do not write it twice.
        if item["status"] in (PENDING, IN_PROGRESS):
            _archive(session_id, item, DROPPED)
        data["items"] = [i for i in data["items"] if i["id"] != item_id]
        _write(session_id, data)
        return item


def edit(session_id: str, item_id: int, text: str | None = None, mode: str | None = None) -> dict[str, Any]:
    """Change an item's wording, or which way it runs.

    Only while it is still in the queue. A finished item has already been
    written to history exactly as it was, and rewriting the queue copy would
    leave the two disagreeing about what was actually done.
    """
    with _lock(session_id):
        data = _read(session_id)
        item = _find(data, item_id)
        if item["status"] in (DONE, FAILED):
            raise ValueError("finished items are history; they cannot be edited")
        if text is not None:
            cleaned = text.strip()
            if not cleaned:
                raise ValueError("queue items need text")
            item["text"] = cleaned
        if mode is not None:
            if mode not in ("fork", "inline"):
                raise ValueError("mode must be 'fork' or 'inline'")
            item["mode"] = mode
        _write(session_id, data)
        return item


def reorder(session_id: str, ids: list[int]) -> list[int]:
    """Put the pending items in exactly this order.

    The whole order at once, rather than one move at a time, because that is
    what a drag on the board produces and it cannot half-apply. The ids must be
    exactly the pending ones: if the queue has moved on since the page drew it -
    an item claimed, added or dropped meanwhile - the reorder is refused rather
    than applied to a queue the user was not looking at.

    Finished and running items keep their places; only the pending slots are
    filled in the new order.
    """
    with _lock(session_id):
        data = _read(session_id)
        pending = [i for i in data["items"] if i["status"] == PENDING]
        by_id = {i["id"]: i for i in pending}
        if sorted(ids) != sorted(by_id):
            raise ValueError("the queue changed while you were dragging; it was left as it is")
        wanted = [by_id[i] for i in ids]
        fresh: list[dict[str, Any]] = []
        taken = 0
        for item in data["items"]:
            if item["status"] == PENDING:
                fresh.append(wanted[taken])
                taken += 1
            else:
                fresh.append(item)
        data["items"] = fresh
        _write(session_id, data)
        return [i["id"] for i in wanted]


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
            # Finished items are already in history; clearing them loses nothing.
            data["items"] = [i for i in data["items"] if i["status"] in (PENDING, IN_PROGRESS)]
        else:
            for item in data["items"]:
                if item["status"] in (PENDING, IN_PROGRESS):
                    _archive(session_id, item, DROPPED)
            data["items"] = []
        _write(session_id, data)
        return before - len(data["items"])


def changed_at(session_id: str | None = None) -> float:
    """When a queue last changed on disk, as a modification time.

    With a session, only that queue's own file: a write replaces it through a
    rename, so its time is fresh whenever it has moved. Without one, the
    directory's time, which answers the looser question "has any queue moved" -
    every write in every session bumps it.

    The two are deliberately separate. A board watching one session must not be
    redrawn because another session claimed an item, and this machine runs
    several at once; watching the directory for a particular queue did exactly
    that, several times a minute.

    Cheap enough to ask several times a second, which is what the live stream
    does instead of reading and comparing whole queues.
    """
    if session_id:
        try:
            return _path(session_id).stat().st_mtime
        except (OSError, ValueError):
            return 0.0
    try:
        return SESSIONS.stat().st_mtime
    except OSError:
        return 0.0


def sessions() -> list[dict[str, Any]]:
    """Every known queue, newest activity first -- used by the CLI to pick a default."""
    if not SESSIONS.exists():
        return []
    out = []
    for path in SESSIONS.glob("*.json"):
        if path.name.endswith(".tmp") or ".corrupt-" in path.name:
            continue
        try:
            raw = _read_text(path)
            data = json.loads(raw) if raw is not None else None
        except (json.JSONDecodeError, OSError):
            continue
        if data is None:
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


def _parse_when(value: str | None, end_of_day: bool = False) -> datetime | None:
    """Accept a bare date or a full ISO timestamp, always tz-aware."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"unparseable date {value!r}: use YYYY-MM-DD or an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # "until 2026-08-09" should include that whole day, not stop at midnight.
    if end_of_day and len(value.strip()) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed


def history(
    session_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    outcome: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Everything that has left a queue, newest first.

    ``session_id`` defaults to every session, which is the point: the per-session
    files cannot answer "what have we processed". Counts and totals describe the
    whole match, not just the page returned by ``limit``.
    """
    start = _parse_when(since)
    end = _parse_when(until, end_of_day=True)

    matched: list[dict[str, Any]] = []
    if HISTORY.exists():
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # one bad line must not hide the rest of the log
            if session_id and record.get("session_id") != session_id:
                continue
            if outcome and record.get("outcome") != outcome:
                continue
            if start or end:
                stamp = record.get("recorded") or record.get("finished")
                try:
                    when = datetime.fromisoformat(stamp) if stamp else None
                except ValueError:
                    when = None
                if when is None:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if start and when < start:
                    continue
                if end and when > end:
                    continue
            matched.append(record)

    matched.reverse()
    counts: dict[str, int] = {}
    for record in matched:
        key = record.get("outcome") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return {
        "records": matched[:limit],
        "counts": counts,
        "total": len(matched),
        "total_duration_seconds": sum(r.get("duration_seconds") or 0 for r in matched),
        "truncated": len(matched) > limit,
    }


def last_session() -> str | None:
    value = _read_text(LAST_SESSION)
    if value is not None:
        value = value.strip()
        return value or None
    known = sessions()
    return known[0]["session_id"] if known else None
