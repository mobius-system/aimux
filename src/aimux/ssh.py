"""SSH config helpers: validate hostnames, build remote launch commands,
parse and append to ~/.ssh/config."""
from __future__ import annotations

import datetime as _dt
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def is_resolvable(host: str) -> bool:
    """`ssh -G <host>` always succeeds for any string; check that it actually
    looks up a hostname (i.e., the host either matches a Host pattern or
    falls back to defaults). We treat a returncode of 0 as resolvable."""
    r = subprocess.run(
        ["ssh", "-G", host],
        capture_output=True, text=True, timeout=5,
    )
    return r.returncode == 0


def build_remote_launch(host: str, *, cwd: str | None, cmd: str | None) -> str:
    """Build the shell command tmux will run for a remote session's first pane.

    The returned string is a single argv-suitable string that tmux passes to
    the user's local shell as the pane command.
    """
    inner_parts: list[str] = []
    if cwd:
        inner_parts.append(f"cd {shlex.quote(cwd)}")
    if cmd:
        inner_parts.append(cmd)
    else:
        inner_parts.append("exec $SHELL -l")
    remote_script = " && ".join(inner_parts)
    return f"ssh -t {shlex.quote(host)} {shlex.quote(remote_script)}"


# ---------- ssh config parsing ----------

_CONFIG_PATH = Path.home() / ".ssh" / "config"


@dataclass
class SshHost:
    name: str
    hostname: str | None
    user: str | None
    port: int


def _read_config_with_includes(path: Path, seen: set[Path] | None = None) -> list[str]:
    if seen is None:
        seen = set()
    rp = path.expanduser().resolve()
    if rp in seen or not rp.exists():
        return []
    seen.add(rp)
    lines = rp.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    for ln in lines:
        m = re.match(r"\s*Include\s+(.+?)\s*(#.*)?$", ln, re.IGNORECASE)
        if m:
            for inc in m.group(1).split():
                inc_path = Path(inc).expanduser()
                if not inc_path.is_absolute():
                    inc_path = rp.parent / inc_path
                # ssh supports glob patterns in Include
                for f in sorted(inc_path.parent.glob(inc_path.name)):
                    out.extend(_read_config_with_includes(f, seen))
        else:
            out.append(ln)
    return out


def _split_blocks(lines: Iterable[str]) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """Group lines into (host_patterns, [(key, value), ...]) blocks."""
    blocks: list[tuple[list[str], list[tuple[str, str]]]] = []
    current_hosts: list[str] | None = None
    current_kvs: list[tuple[str, str]] = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^Host\s+(.+?)\s*$", s, re.IGNORECASE)
        if m:
            if current_hosts is not None:
                blocks.append((current_hosts, current_kvs))
            current_hosts = m.group(1).split()
            current_kvs = []
            continue
        if current_hosts is None:
            continue
        kv = re.match(r"^(\S+)\s+(.+?)\s*$", s)
        if kv:
            current_kvs.append((kv.group(1).lower(), kv.group(2)))
    if current_hosts is not None:
        blocks.append((current_hosts, current_kvs))
    return blocks


_WILDCARD_CHARS = set("*?!")


def list_hosts() -> list[SshHost]:
    """Enumerate concrete hosts (no wildcard patterns) defined in ~/.ssh/config."""
    if not _CONFIG_PATH.exists():
        return []
    lines = _read_config_with_includes(_CONFIG_PATH)
    blocks = _split_blocks(lines)
    out: list[SshHost] = []
    seen_names: set[str] = set()
    for hosts, kvs in blocks:
        for h in hosts:
            if any(c in _WILDCARD_CHARS for c in h):
                continue
            if h in seen_names:
                continue
            seen_names.add(h)
            kv = dict(kvs)
            try:
                port = int(kv.get("port", "22"))
            except ValueError:
                port = 22
            out.append(
                SshHost(
                    name=h,
                    hostname=kv.get("hostname"),
                    user=kv.get("user"),
                    port=port,
                )
            )
    return out


def host_exists(name: str) -> bool:
    return any(h.name == name for h in list_hosts())


# ---------- probing ----------

@dataclass
class ProbeResult:
    name: str
    status: str  # reachable | auth-required | unreachable | unknown
    rtt_ms: int | None
    stderr: str = ""


def _classify(returncode: int, stderr: str) -> str:
    if returncode == 0:
        return "reachable"
    err = stderr.lower()
    if "permission denied" in err or "publickey" in err or "no such identity" in err:
        return "auth-required"
    for needle in ("timed out", "timeout", "refused", "no route", "unreachable", "could not resolve", "name or service not known"):
        if needle in err:
            return "unreachable"
    return "unknown"


def probe(name: str, timeout: int = 5) -> ProbeResult:
    """Probe a hostname (must already be in ssh config or resolvable).
    Uses BatchMode=yes so the probe never prompts."""
    import time
    start = time.monotonic()
    try:
        r = subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={timeout}",
                "-o", "StrictHostKeyChecking=accept-new",
                name,
                "true",
            ],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        rtt_ms = int((time.monotonic() - start) * 1000)
        status = _classify(r.returncode, r.stderr)
        rtt = rtt_ms if status in ("reachable", "auth-required") else None
        return ProbeResult(name=name, status=status, rtt_ms=rtt, stderr=r.stderr)
    except subprocess.TimeoutExpired:
        return ProbeResult(name=name, status="unreachable", rtt_ms=None, stderr="probe wall-clock timeout")


@dataclass
class HardwareInfo:
    gpu_count: int
    gpu_model: str | None
    cpu_count: int | None
    mem_total_kb: int | None


class HardwareError(Exception):
    pass


_HARDWARE_PROBE_SCRIPT = r"""
gpu_count=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ')
gpu_model=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1)
cpu_count=$(nproc 2>/dev/null)
mem_kb=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null)
printf 'gpu_count=%s\ngpu_model=%s\ncpu_count=%s\nmem_kb=%s\n' "$gpu_count" "$gpu_model" "$cpu_count" "$mem_kb"
"""


def hardware_info(name: str, timeout: int = 10) -> HardwareInfo:
    """SSH to `name` and gather GPU/CPU/memory info. Raises HardwareError on failure."""
    try:
        r = subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={timeout}",
                "-o", "StrictHostKeyChecking=accept-new",
                name,
                "sh", "-s",
            ],
            input=_HARDWARE_PROBE_SCRIPT,
            capture_output=True, text=True, timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired:
        raise HardwareError("ssh wall-clock timeout")

    if r.returncode != 0:
        raise HardwareError(r.stderr.strip() or f"ssh exited {r.returncode}")

    fields: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()

    def _opt_int(s: str | None) -> int | None:
        if s is None or s == "":
            return None
        try:
            return int(s)
        except ValueError:
            return None

    return HardwareInfo(
        gpu_count=_opt_int(fields.get("gpu_count")) or 0,
        gpu_model=(fields.get("gpu_model") or None),
        cpu_count=_opt_int(fields.get("cpu_count")),
        mem_total_kb=_opt_int(fields.get("mem_kb")),
    )


class SshScriptError(Exception):
    """Raised when a remote shell script (run via `ssh <host> sh -s`) times out.

    Non-zero exit codes are NOT raised here — callers inspect the returned
    CompletedProcess so they can read structured stdout (e.g. ERR rows) and
    specific exit codes emitted by the script itself.
    """


def run_script(name: str, script: str, *, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a POSIX shell script on a remote host via `ssh <name> sh -s`.

    Feeds ``script`` on stdin (BatchMode=yes, StrictHostKeyChecking=accept-new)
    and returns the CompletedProcess with decoded stdout/stderr. Only a
    wall-clock timeout raises SshScriptError; a non-zero exit is returned to
    the caller for inspection. Used by the single-file SSH operations
    (list/stat/mkdir/delete) where structured stdout is required — sftp batch
    only signals success via return code.
    """
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={timeout}",
                "-o", "StrictHostKeyChecking=accept-new",
                name,
                "sh", "-s",
            ],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired as e:
        raise SshScriptError("ssh wall-clock timeout") from e
    return result


def probe_raw(host: str, port: int, user: str, identity: str | None, timeout: int = 5) -> ProbeResult:
    """Probe a host that is NOT yet in ssh config. Used by `remote add`."""
    import time
    args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-p", str(port),
    ]
    if identity:
        args += ["-i", identity]
    args += [f"{user}@{host}", "true"]
    start = time.monotonic()
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 5)
        rtt_ms = int((time.monotonic() - start) * 1000)
        status = _classify(r.returncode, r.stderr)
        rtt = rtt_ms if status in ("reachable", "auth-required") else None
        return ProbeResult(name=host, status=status, rtt_ms=rtt, stderr=r.stderr)
    except subprocess.TimeoutExpired:
        return ProbeResult(name=host, status="unreachable", rtt_ms=None, stderr="probe wall-clock timeout")


# ---------- append to ssh config ----------

def append_host(
    *,
    name: str,
    host: str,
    port: int,
    user: str,
    identity: str | None = None,
) -> Path:
    """Append a new Host block to ~/.ssh/config. Returns backup path."""
    _CONFIG_PATH.parent.mkdir(mode=0o700, exist_ok=True)
    if not _CONFIG_PATH.exists():
        _CONFIG_PATH.touch(mode=0o600)

    if host_exists(name):
        raise FileExistsError(f"host {name!r} already exists in ssh config")

    ts_compact = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ts_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    backup = _CONFIG_PATH.with_suffix(_CONFIG_PATH.suffix + f".aimux.bak.{ts_compact}")
    # _CONFIG_PATH is `.../config`, so .with_suffix replaces "" with ".aimux.bak.X".
    # On a file with no extension, `with_suffix` operates on the trailing component.
    backup = Path(str(_CONFIG_PATH) + f".aimux.bak.{ts_compact}")
    shutil.copy2(_CONFIG_PATH, backup)

    block_lines = [
        "",
        f"# Added by aimux at {ts_iso}",
        f"Host {name}",
        f"    HostName {host}",
        f"    Port {port}",
        f"    User {user}",
    ]
    if identity:
        block_lines.append(f"    IdentityFile {identity}")
    block_lines.append("")
    with _CONFIG_PATH.open("a", encoding="utf-8") as f:
        f.write("\n".join(block_lines))
    return backup
