"""Session model + id resolution + user-options batch read."""
from __future__ import annotations

from dataclasses import dataclass

from . import bridge
from . import tmux
from .constants import (
    OPT_CREATED_AT,
    OPT_CREATED_BY,
    OPT_HOST,
    OPT_LOCATION,
    OPT_OUTPUT_SYNC_FILE,
)

# Format string for `tmux ls -F`. Field order MUST match Session.from_line below.
_LS_FORMAT = "|".join(
    [
        "#{session_name}",
        "#{?session_attached,1,0}",
        f"#{{{OPT_LOCATION}}}",
        f"#{{{OPT_HOST}}}",
        f"#{{{OPT_CREATED_AT}}}",
        f"#{{{OPT_CREATED_BY}}}",
        f"#{{{OPT_OUTPUT_SYNC_FILE}}}",
    ]
)


@dataclass(frozen=True)
class Session:
    name: str
    attached: bool
    location: str  # "local" or ssh-host name; "local" used as fallback for unmanaged
    host: str | None
    created_at: str | None
    created_by: str | None
    output_sync_file: str | None
    managed: bool  # False if @aimux-location was unset (created outside aimux)
    backend: str = "tmux"  # tmux | bridge
    transport: str = "local"  # local | ssh | bridge
    profile: str | None = None
    client_status: str | None = None

    @property
    def id(self) -> str:
        return f"{self.location}/{self.name}"

    @classmethod
    def from_line(cls, line: str) -> "Session":
        parts = line.split("|")
        # tmux format may emit fewer fields if trailing options are empty;
        # pad to length 7.
        while len(parts) < 7:
            parts.append("")
        name, att, loc, host, created_at, created_by, sync_file = parts[:7]
        managed = bool(loc)
        location = loc if loc else "local"
        return cls(
            name=name,
            attached=(att == "1"),
            location=location,
            host=host or None,
            created_at=created_at or None,
            created_by=created_by or None,
            output_sync_file=sync_file or None,
            managed=managed,
            backend="tmux",
            transport="local" if location == "local" else "ssh",
        )

    @classmethod
    def from_bridge(cls, data: dict) -> "Session":
        location = str(data.get("location") or data.get("remote") or data.get("host") or "")
        name = str(data.get("name") or "")
        return cls(
            name=name,
            attached=False,
            location=location,
            host=str(data.get("host") or location) if location else None,
            created_at=data.get("created_at"),
            created_by=data.get("created_by"),
            output_sync_file=None,
            managed=True,
            backend="bridge",
            transport="bridge",
            profile=data.get("profile"),
            client_status=data.get("client_status"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "location": self.location,
            "host": self.host,
            "name": self.name,
            "status": "attached" if self.attached else "detached",
            "created_at": self.created_at,
            "created_by": self.created_by,
            "output_sync_file": self.output_sync_file,
            "managed": self.managed,
            "backend": self.backend,
            "transport": self.transport,
            "profile": self.profile,
            "client_status": self.client_status,
        }


def list_sessions() -> list[Session]:
    sessions = [Session.from_line(ln) for ln in tmux.list_sessions_raw(_LS_FORMAT)]
    try:
        client = bridge.BridgeControlClient.from_runtime()
        sessions.extend(Session.from_bridge(row) for row in client.list_sessions(timeout=1.0))
    except bridge.BridgeError:
        pass
    return sessions


class IdError(Exception):
    """Raised when a session id cannot be resolved."""


def resolve(spec: str, sessions: list[Session] | None = None) -> Session:
    """Resolve `<location>/<name>` or short `<name>` to a unique Session."""
    if sessions is None:
        sessions = list_sessions()
    if "/" in spec:
        loc, _, name = spec.partition("/")
        for s in sessions:
            if s.location == loc and s.name == name:
                return s
        raise IdError(f"session {spec!r} not found")
    matches = [s for s in sessions if s.name == spec]
    if not matches:
        raise IdError(f"session {spec!r} not found")
    if len(matches) > 1:
        ids = ", ".join(s.id for s in matches)
        raise IdError(
            f"ambiguous session name {spec!r}; matches: {ids}. Use full <location>/<name>."
        )
    return matches[0]
