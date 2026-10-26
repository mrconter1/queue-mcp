"""The optional bearer lock on /mcp (guard.py).

Run from the repository root::

    python -m pytest tests/test_guard.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from queue_mcp import guard

TOKEN = "t" * 40


async def _app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _call(app, path="/mcp", headers=None):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "path": path, "headers": headers or []}
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


LOCAL = [(b"host", b"127.0.0.1:1234")]


def test_unset_env_leaves_app_untouched(monkeypatch):
    monkeypatch.delenv(guard.ENV_TOKEN_FILE, raising=False)
    assert guard.wrap(_app) is _app


def test_right_token_passes():
    app = guard.protect(_app, TOKEN)
    assert _call(app, headers=LOCAL + [(b"authorization", f"Bearer {TOKEN}".encode())]) == 200


def test_missing_or_wrong_token_is_401():
    app = guard.protect(_app, TOKEN)
    assert _call(app, headers=LOCAL) == 401
    assert _call(app, headers=LOCAL + [(b"authorization", b"Bearer nope")]) == 401
    assert _call(app, headers=LOCAL + [(b"authorization", f"Basic {TOKEN}".encode())]) == 401


def test_foreign_host_or_origin_is_403():
    app = guard.protect(_app, TOKEN)
    auth = [(b"authorization", f"Bearer {TOKEN}".encode())]
    assert _call(app, headers=[(b"host", b"evil.example:1234")] + auth) == 403
    assert _call(app, headers=[] + auth) == 403
    assert _call(app, headers=LOCAL + auth + [(b"origin", b"https://evil.example")]) == 403
    assert _call(app, headers=[(b"host", b"localhost:1234"), (b"origin", b"http://localhost:1234")] + auth) == 200


def test_unreadable_file_locks_endpoint():
    app = guard.protect(_app, None)
    assert _call(app, headers=LOCAL + [(b"authorization", f"Bearer {TOKEN}".encode())]) == 503


def test_other_paths_are_not_guarded():
    app = guard.protect(_app, TOKEN)
    assert _call(app, path="/", headers=LOCAL) == 200


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_roundtrip_and_first_run_creates_token(tmp_path: Path):
    f = tmp_path / "tok.dpapi"
    made = guard.load_token(f)
    assert made and len(made) >= guard.MIN_LENGTH
    assert guard.read_token(f) == made
    assert f.read_bytes() != made.encode()  # not stored in plain text


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_corrupt_file_fails_closed(tmp_path: Path):
    f = tmp_path / "tok.dpapi"
    f.write_bytes(b"not a dpapi blob")
    assert guard.load_token(f) is None
