"""Optional bearer lock on this server's own ``/mcp`` endpoint.

Listening on 127.0.0.1 keeps other machines out, and the MCP SDK's Host check
keeps web pages out, but neither stops another program or another Windows user
on the same machine from reaching the port. One switch closes that, off unless
set, so an install without it behaves exactly as before:

``MCP_QUEUE_TOKEN_FILE``
    A DPAPI-protected file (current user) holding a bearer token. When set,
    every request under ``/mcp`` must send ``Authorization: Bearer <token>``,
    a ``Host`` of 127.0.0.1 or localhost, and no foreign ``Origin``. The token
    is made the first time if there is no file. A file that exists but cannot
    be read refuses every request (fail closed) instead of opening the door.

Same design as the orchestrator's ``guard.py``. The file is a raw
CryptProtectData blob with no entropy, so the orchestrator, this server and the
PowerShell headers helper (``ProtectedData.Unprotect``) all read the same file.
DPAPI is called through ctypes, so there is no extra dependency.
"""

from __future__ import annotations

import ctypes
import hmac
import json
import os
import secrets
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ENV_TOKEN_FILE = "MCP_QUEUE_TOKEN_FILE"
PROTECTED_PREFIX = "/mcp"
MIN_LENGTH = 32
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # 0x1 = CRYPTPROTECT_UI_FORBIDDEN: never show a prompt from a background server.
    ok = fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError(f"DPAPI {'protect' if protect else 'unprotect'} failed ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def token_file() -> Path | None:
    raw = os.environ.get(ENV_TOKEN_FILE, "").strip()
    return Path(raw) if raw else None


def read_token(path: Path, attempts: int = 5, delay: float = 0.1) -> str | None:
    """The token in ``path``; None if there is no file. Raises if the file
    exists but stays unreadable."""
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            last = exc
        else:
            try:
                token = _dpapi(blob, protect=False).decode("utf-8").strip()
            except Exception as exc:  # noqa: BLE001 -- DPAPI and decode raise several types
                last = exc
            else:
                if len(token) >= MIN_LENGTH:
                    return token
                last = ValueError("token too short")
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise RuntimeError(f"cannot read {path}: {type(last).__name__}")


def write_token(path: Path, token: str) -> None:
    if len(token) < MIN_LENGTH:
        raise ValueError("token too short")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(_dpapi(token.encode("utf-8"), protect=True))
    try:
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def load_token(path: Path) -> str | None:
    """The token to require, made once if there is no file. None means the
    file is there but unreadable: every request is then refused."""
    try:
        token = read_token(path)
    except RuntimeError:
        return None
    if token is None:
        token = secrets.token_urlsafe(32)
        write_token(path, token)
    return token


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _given(scope: dict[str, Any]) -> str | None:
    value = _header(scope, b"authorization")
    if value is None:
        return None
    scheme, _, given = value.partition(" ")
    return given.strip() if scheme.lower() == "bearer" else None


def _local_host(scope: dict[str, Any]) -> bool:
    host = _header(scope, b"host")
    if not host:
        return False
    name = urlparse(f"//{host}").hostname
    return name in LOCAL_HOSTS


def _local_origin(scope: dict[str, Any]) -> bool:
    origin = _header(scope, b"origin")
    if origin is None:
        return True  # non-browser clients send none
    return urlparse(origin).hostname in LOCAL_HOSTS


async def _refuse(send: Any, status: int, detail: str) -> None:
    body = json.dumps({"error": "unauthorized" if status == 401 else "refused", "detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="local"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def protect(app: Any, token: str | None) -> Any:
    """Wrap an ASGI app so every request under /mcp needs ``token``.

    ``token`` None (the file exists but could not be read) refuses everything.
    """

    async def guarded(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith(PROTECTED_PREFIX):
            if token is None:
                await _refuse(send, 503, "token file unreadable; endpoint locked")
                return
            if not _local_host(scope) or not _local_origin(scope):
                await _refuse(send, 403, "only local callers")
                return
            given = _given(scope)
            if given is None or not hmac.compare_digest(
                given.encode("utf-8", "surrogateescape"), token.encode("utf-8")
            ):
                await _refuse(send, 401, "missing or wrong bearer token")
                return
        await app(scope, receive, send)

    return guarded


def wrap(app: Any) -> Any:
    """``app`` behind the bearer check when a token file is configured, else as is."""
    path = token_file()
    if path is None:
        return app
    return protect(app, load_token(path))
