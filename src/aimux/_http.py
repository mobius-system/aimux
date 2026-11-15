"""Keep-alive HTTP(S) connections for the bridge control channel.

The reverse client posts every command result, and the MCP server posts every
bridge request, through ``urllib`` by default -- which opens a new TCP/TLS
connection for every call. Against a broker reached through a public gateway
that is ~180 ms per request versus ~60 ms on a reused connection, and one
``remote_exec_command`` makes eight of them.

This module keeps one connection per origin and drops it when the peer has
closed it (nginx closes idle keep-alive sockets after ~75 s, which is shorter
than the gap between two agent tool calls). A request is never retried after it
was written: retrying a POST whose response was lost could run a command
twice. Stale detection happens *before* sending, so the common idle-close case
reconnects cleanly without that risk.

Requests that must go through a proxy fall back to ``urllib`` so environment
and registry proxy settings keep working.
"""
from __future__ import annotations

import http.client
import select
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

_LOCK = threading.RLock()
_CONNS: dict[tuple[str, str, int], http.client.HTTPConnection] = {}


def _needs_proxy(scheme: str, host: str) -> bool:
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return False
    if not proxies.get(scheme):
        return False
    try:
        return not urllib.request.proxy_bypass(host)
    except Exception:
        return True


def _split(url: str) -> tuple[str, str, int, str]:
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, path


def _stale(conn: http.client.HTTPConnection) -> bool:
    """True when the peer already closed this idle keep-alive socket."""
    sock = conn.sock
    if sock is None:
        return False
    try:
        ready, _, _ = select.select([sock], [], [], 0)
        if not ready:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def _drop(key: tuple[str, str, int]) -> None:
    conn = _CONNS.pop(key, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def close_all() -> None:
    """Close every cached connection (tests and process shutdown)."""
    with _LOCK:
        for key in list(_CONNS):
            _drop(key)


def _urllib_request(method: str, url: str, headers: dict[str, str],
                    body: bytes | None, timeout: float):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def request(method: str, url: str, headers: dict[str, str],
            body: bytes | None = None, *, timeout: float = 10.0):
    """Return ``(status, headers, body_bytes)``; non-2xx is not an exception.

    A reused connection is only used after checking the peer has not closed it;
    a failure while sending or reading is raised as-is (no replay).
    """
    scheme, host, port, path = _split(url)
    if not host:
        raise ValueError(f"bad url: {url!r}")
    if scheme not in ("http", "https") or _needs_proxy(scheme, host):
        return _urllib_request(method, url, headers, body, timeout)

    key = (scheme, host, port)
    with _LOCK:
        conn = _CONNS.get(key)
        if conn is not None and _stale(conn):
            _drop(key)
            conn = None
        if conn is None:
            cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            conn = cls(host, port, timeout=timeout)
            _CONNS[key] = conn
        # The socket was created with the first timeout; later calls may use a
        # different one, so update the connection and the live socket.
        conn.timeout = timeout
        if conn.sock is not None:
            try:
                conn.sock.settimeout(timeout)
            except OSError:
                pass
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.will_close:
                _drop(key)
            return resp.status, resp.headers, data
        except (http.client.HTTPException, OSError):
            _drop(key)
            raise
