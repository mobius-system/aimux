"""SFTP transfer helpers for aimux file commands."""
from __future__ import annotations

import base64
import os
import posixpath
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from . import bridge as bridge_mod


class TransferError(RuntimeError):
    """Raised for local planning errors before an sftp upload starts."""


class SftpError(TransferError):
    """Raised when the sftp subprocess fails."""

    def __init__(self, returncode: int, stdout: str, stderr: str, args: list[str]):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.args = args
        msg = stderr.strip() or stdout.strip() or f"sftp exited with status {returncode}"
        super().__init__(msg)


class SftpUploadError(SftpError):
    """Raised when an sftp upload subprocess fails."""


class SftpDownloadError(SftpError):
    """Raised when an sftp download subprocess fails."""


@dataclass(frozen=True)
class UploadEntry:
    local: Path
    remote: str


@dataclass(frozen=True)
class SendFilesPlan:
    local_paths: tuple[Path, ...]
    remote_path: str
    files: tuple[UploadEntry, ...]
    dirs: tuple[str, ...]
    skipped_files: int

    @property
    def has_work(self) -> bool:
        return bool(self.files or self.dirs)


@dataclass(frozen=True)
class SendFilesResult:
    plan: SendFilesPlan
    uploaded_files: int
    skipped_files: int


@dataclass(frozen=True)
class DownloadEntry:
    remote: str
    local: Path


@dataclass(frozen=True)
class GetFilesPlan:
    remote_paths: tuple[str, ...]
    local_dir: Path
    entries: tuple[DownloadEntry, ...]

    @property
    def has_work(self) -> bool:
        return bool(self.entries)


@dataclass(frozen=True)
class GetFilesResult:
    plan: GetFilesPlan
    downloaded_paths: int


def send_paths(
    host: str,
    local_paths: Iterable[str | Path],
    remote_path: str,
    *,
    gitignore: bool = False,
) -> SendFilesResult:
    """Upload multiple local paths into `host:remote_path` using one sftp batch."""
    plan = build_send_files_plan(local_paths, remote_path, gitignore=gitignore)
    if plan.has_work:
        _run_sftp_batch(host, build_sftp_batch(plan), SftpUploadError)
    return SendFilesResult(
        plan=plan,
        uploaded_files=len(plan.files),
        skipped_files=plan.skipped_files,
    )


def get_paths(host: str, remote_paths: Iterable[str], local_dir: str | Path) -> GetFilesResult:
    """Download multiple remote paths into `local_dir` using one sftp batch."""
    plan = build_get_files_plan(remote_paths, local_dir)
    if plan.has_work:
        _run_sftp_batch(host, build_get_files_batch(plan), SftpDownloadError)
    return GetFilesResult(plan=plan, downloaded_paths=len(plan.entries))


def build_send_files_plan(
    local_paths: Iterable[str | Path],
    remote_path: str,
    *,
    gitignore: bool = False,
) -> SendFilesPlan:
    _validate_sftp_path(remote_path)
    if not remote_path:
        raise TransferError("remote path must not be empty")

    sources = tuple(_resolve_existing_local_path(path) for path in local_paths)
    if not sources:
        raise TransferError("send_files requires at least one local path")

    entries: list[UploadEntry] = []
    skipped = 0
    dirs: set[str] = set(_remote_dir_ancestors(remote_path))
    for source in sources:
        if source.is_file():
            files = _included_files(source, gitignore=gitignore)
            skipped += 1 - len(files)
            for file_path in files:
                remote_file = _remote_join(remote_path, file_path.name)
                entries.append(UploadEntry(local=file_path, remote=remote_file))
            continue

        all_files = _all_files(source)
        files = _included_files(source, gitignore=gitignore)
        skipped += len(all_files) - len(files)
        source_remote_root = _remote_join(remote_path, source.name)
        dirs.update(_remote_dir_ancestors(source_remote_root))
        if not gitignore:
            for local_dir in sorted(p for p in source.rglob("*") if p.is_dir()):
                rel = local_dir.relative_to(source).as_posix()
                dirs.update(_remote_dir_ancestors(_remote_join(source_remote_root, rel)))
        for file_path in files:
            remote_file = _remote_join(source_remote_root, file_path.relative_to(source).as_posix())
            entries.append(UploadEntry(local=file_path, remote=remote_file))

    _ensure_unique_remote_files(entries)
    for entry in entries:
        dirs.update(_remote_dir_ancestors(_remote_parent(entry.remote)))

    return SendFilesPlan(sources, remote_path, tuple(entries), _sort_remote_dirs(dirs), skipped)


def build_get_files_plan(remote_paths: Iterable[str], local_dir: str | Path) -> GetFilesPlan:
    remotes = tuple(remote_paths)
    if not remotes:
        raise TransferError("get_files requires at least one remote path")

    destination = Path(local_dir).expanduser()
    if destination.exists() and not destination.is_dir():
        raise TransferError(f"local destination is not a directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    _validate_sftp_path(str(destination))

    entries: list[DownloadEntry] = []
    for remote_path in remotes:
        _validate_sftp_path(remote_path)
        if not remote_path:
            raise TransferError("remote path must not be empty")
        entries.append(DownloadEntry(remote=remote_path, local=destination / _remote_basename(remote_path)))

    return GetFilesPlan(remotes, destination, tuple(entries))


def build_sftp_batch(plan: SendFilesPlan) -> str:
    lines: list[str] = []
    for remote_dir in plan.dirs:
        lines.append(f"-mkdir {_sftp_quote(remote_dir)}")
    for entry in plan.files:
        lines.append(f"put {_sftp_quote(str(entry.local))} {_sftp_quote(entry.remote)}")
    return "\n".join(lines) + "\n"


def build_get_files_batch(plan: GetFilesPlan) -> str:
    lines = [
        f"get -r {_sftp_quote(entry.remote)} {_sftp_quote(str(entry.local))}"
        for entry in plan.entries
    ]
    return "\n".join(lines) + "\n"


def _run_sftp_batch(host: str, batch: str, error_cls: type[SftpError]) -> None:
    if shutil.which("sftp") is None:
        raise TransferError("sftp executable not found")

    args = ["sftp", "-o", "BatchMode=yes", "-b", "-", host]
    try:
        result = subprocess.run(args, input=batch, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise TransferError("sftp executable not found") from e

    if result.returncode != 0:
        raise error_cls(result.returncode, result.stdout, result.stderr, args)


def _resolve_existing_local_path(local_path: str | Path) -> Path:
    source = Path(local_path).expanduser()
    if not source.exists():
        raise TransferError(f"local path does not exist: {source}")
    source = source.resolve()
    _validate_sftp_path(str(source))
    if not source.is_file() and not source.is_dir():
        raise TransferError(f"local path is not a regular file or directory: {source}")
    return source


def _included_files(source: Path, *, gitignore: bool) -> list[Path]:
    if not gitignore:
        return _all_files(source)

    git_files = _git_list_included_files(source)
    if git_files is None:
        return _all_files(source)
    return git_files


def _all_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    return sorted(p for p in source.rglob("*") if p.is_file())


def _git_list_included_files(source: Path) -> list[Path] | None:
    repo = _git_root(source)
    if repo is None:
        return None

    try:
        rel = source.relative_to(repo)
    except ValueError:
        return None
    pathspec = "." if rel == Path(".") else rel.as_posix()

    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "-c", "-o", "--exclude-standard", "--", pathspec],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git ls-files failed"
        raise TransferError(detail)

    files: list[Path] = []
    for item in result.stdout.split("\0"):
        if not item:
            continue
        path = (repo / item).resolve()
        if source.is_file():
            if path == source and path.is_file():
                files.append(path)
            continue
        if path.is_file() and path.is_relative_to(source):
            files.append(path)
    return sorted(dict.fromkeys(files))


def _git_root(source: Path) -> Path | None:
    cwd = source if source.is_dir() else source.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise TransferError("git executable not found; --gitignore requires git") from e

    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root).resolve() if root else None


def _remote_join(base: str, rel: str) -> str:
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel:
        return base.rstrip("/") or "."
    if base == "/":
        return f"/{rel}"
    cleaned_base = base.rstrip("/")
    if not cleaned_base:
        return rel
    return f"{cleaned_base}/{rel}"


def _remote_parent(remote_path: str) -> str:
    path = remote_path.rstrip("/")
    return posixpath.dirname(path)


def _remote_basename(remote_path: str) -> str:
    name = posixpath.basename(remote_path.rstrip("/"))
    if not name or name in (".", ".."):
        raise TransferError(f"remote path must include a file or directory name: {remote_path}")
    return name


def _ensure_unique_remote_files(entries: Iterable[UploadEntry]) -> None:
    seen: dict[str, Path] = {}
    for entry in entries:
        previous = seen.get(entry.remote)
        if previous is not None:
            raise TransferError(
                f"multiple local files map to remote path {entry.remote!r}: {previous} and {entry.local}"
            )
        seen[entry.remote] = entry.local


def _collect_remote_dirs(paths: Iterable[str]) -> tuple[str, ...]:
    dirs: set[str] = set()
    for path in paths:
        dirs.update(_remote_dir_ancestors(path))
    return _sort_remote_dirs(dirs)


def _remote_dir_ancestors(remote_dir: str) -> list[str]:
    cleaned = remote_dir.rstrip("/")
    if cleaned in ("", ".", "/"):
        return []

    if cleaned.startswith("/"):
        parts = [part for part in cleaned.split("/") if part]
        current = ""
        out: list[str] = []
        for part in parts:
            current = f"{current}/{part}"
            out.append(current)
        return out

    parts = [part for part in cleaned.split("/") if part and part != "."]
    current = ""
    out = []
    for part in parts:
        current = part if not current else f"{current}/{part}"
        out.append(current)
    return out


def _sort_remote_dirs(dirs: set[str]) -> tuple[str, ...]:
    return tuple(sorted(dirs, key=lambda d: (d.count("/"), d)))


def _sftp_quote(path: str) -> str:
    _validate_sftp_path(path)
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def validate_transfer_path(path: str) -> None:
    """Reject paths containing control characters that break sftp / bridge protocols."""
    if "\0" in path or "\n" in path or "\r" in path:
        raise TransferError("paths must not contain control newlines or NUL bytes")


def validate_bridge_path(path: str) -> None:
    """Stricter check for bridge transfers: also reject '..' to prevent traversal.

    Bridge clients touch their local filesystem directly via Path(), unlike
    sftp which is mediated by the remote sshd's path resolution. We must
    therefore refuse '..' segments that could escape an intended directory.
    """
    validate_transfer_path(path)
    parts = path.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise TransferError(f"bridge paths must not contain '..' segments: {path!r}")


# Backwards-compat alias used elsewhere in this module.
_validate_sftp_path = validate_transfer_path


# ---------------------------------------------------------------------------
# Bridge remote transfer (HTTP/SSE broker instead of sftp)
# ---------------------------------------------------------------------------

DEFAULT_BRIDGE_MAX_FILE_SIZE = 16 * 1024 * 1024


def bridge_send_files(
    client: "bridge_mod.BridgeControlClient",
    remote: str,
    local_paths: Iterable[str | Path],
    remote_path: str,
    *,
    gitignore: bool = False,
    max_file_size: int = DEFAULT_BRIDGE_MAX_FILE_SIZE,
) -> SendFilesResult:
    """Upload local paths to a bridge remote via the file.write message family.

    Reuses the same planning logic as send_paths; the sftp batch is replaced by
    per-file base64-encoded writes through the broker.
    """
    plan = build_send_files_plan(local_paths, remote_path, gitignore=gitignore)
    if not plan.has_work:
        return SendFilesResult(
            plan=plan,
            uploaded_files=0,
            skipped_files=plan.skipped_files,
        )
    for remote_dir in plan.dirs:
        validate_bridge_path(remote_dir)
        client.file_mkdir(remote=remote, path=remote_dir)
    for entry in plan.files:
        validate_bridge_path(entry.remote)
        data = entry.local.read_bytes()
        if len(data) > max_file_size:
            raise TransferError(
                f"file exceeds bridge size limit ({len(data)} > {max_file_size} bytes): {entry.local}"
            )
        content_b64 = base64.b64encode(data).decode("ascii")
        client.file_write(
            remote=remote,
            path=entry.remote,
            content_b64=content_b64,
            mode=0o644,
            max_size=max_file_size,
        )
    return SendFilesResult(
        plan=plan,
        uploaded_files=len(plan.files),
        skipped_files=plan.skipped_files,
    )


def bridge_get_files(
    client: "bridge_mod.BridgeControlClient",
    remote: str,
    remote_paths: Iterable[str],
    local_dir: str | Path,
    *,
    max_file_size: int = DEFAULT_BRIDGE_MAX_FILE_SIZE,
) -> GetFilesResult:
    """Download remote paths from a bridge remote via the file.read message family."""
    remotes = tuple(remote_paths)
    if not remotes:
        raise TransferError("get_files requires at least one remote path")
    destination = Path(local_dir).expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise TransferError(f"local destination is not a directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    validate_transfer_path(str(destination))

    entries: list[DownloadEntry] = []
    for remote_path in remotes:
        validate_bridge_path(remote_path)
        if not remote_path:
            raise TransferError("remote path must not be empty")
        stat = client.file_stat(remote=remote, path=remote_path)
        rtype = str(stat.get("type") or "none")
        if rtype == "dir":
            _bridge_download_dir(client, remote, remote_path, destination, max_file_size, entries)
        elif rtype == "file":
            local_file = destination / _remote_basename(remote_path)
            _bridge_download_one(client, remote, remote_path, local_file, int(stat.get("size") or 0), max_file_size)
            entries.append(DownloadEntry(remote=remote_path, local=local_file))
        else:
            raise TransferError(f"remote path does not exist: {remote_path}")

    plan = GetFilesPlan(remotes, destination, tuple(entries))
    return GetFilesResult(plan=plan, downloaded_paths=len(entries))


def _bridge_download_dir(
    client: "bridge_mod.BridgeControlClient",
    remote: str,
    remote_root: str,
    destination: Path,
    max_file_size: int,
    entries: list[DownloadEntry],
) -> None:
    base_name = _remote_basename(remote_root)
    listing = client.file_list(remote=remote, path=remote_root, recursive=True)
    items = listing.get("entries") if isinstance(listing, dict) else None
    if not isinstance(items, list):
        raise TransferError(f"bridge file.list returned no entries for {remote_root!r}")
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        item_path = str(item.get("path") or "")
        if item_type != "file" or not item_path:
            continue
        rel = _remote_relpath(remote_root, item_path)
        local_file = destination / base_name / rel
        local_file.parent.mkdir(parents=True, exist_ok=True)
        size = int(item.get("size") or 0)
        _bridge_download_one(client, remote, item_path, local_file, size, max_file_size)
        entries.append(DownloadEntry(remote=item_path, local=local_file))


def _bridge_download_one(
    client: "bridge_mod.BridgeControlClient",
    remote: str,
    remote_path: str,
    local_file: Path,
    size: int,
    max_file_size: int,
) -> None:
    if size and size > max_file_size:
        raise TransferError(
            f"file exceeds bridge size limit ({size} > {max_file_size} bytes): {remote_path}"
        )
    response = client.file_read(remote=remote, path=remote_path)
    content_b64 = str(response.get("content_b64") or "")
    try:
        data = base64.b64decode(content_b64, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        raise TransferError(f"invalid base64 from bridge for {remote_path!r}: {e}")
    if len(data) > max_file_size:
        raise TransferError(
            f"file exceeds bridge size limit ({len(data)} > {max_file_size} bytes): {remote_path}"
        )
    local_file.parent.mkdir(parents=True, exist_ok=True)
    local_file.write_bytes(data)


def _remote_relpath(base: str, target: str) -> str:
    """Return target path relative to base, normalized to forward slashes."""
    base_norm = base.rstrip("/")
    target_norm = target.rstrip("/")
    if target_norm == base_norm:
        return ""
    if target_norm.startswith(base_norm + "/"):
        return target_norm[len(base_norm) + 1:]
    return posixpath.basename(target_norm)
