"""SFTP transfer helpers for aimux file commands."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import posixpath
import shutil
import subprocess
import tarfile
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


@dataclass(frozen=True)
class SyncFile:
    rel: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SyncSnapshot:
    files: dict[str, SyncFile]
    dirs: set[str]
    root_exists: bool = True


@dataclass(frozen=True)
class SyncDirPlan:
    changed_files: tuple[str, ...]
    deleted_paths: tuple[str, ...]
    dirs: tuple[str, ...]

    @property
    def has_work(self) -> bool:
        return bool(self.changed_files or self.deleted_paths or self.dirs)


@dataclass(frozen=True)
class SyncDirResult:
    direction: str
    remote: str
    remote_path: str
    local_path: Path
    synced_files: int
    deleted_paths: int
    created_dirs: int


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
DEFAULT_BRIDGE_MAX_ARCHIVE_SIZE = 64 * 1024 * 1024


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
            local_file = destination / _bridge_remote_basename(remote_path)
            _bridge_download_one(client, remote, remote_path, local_file, int(stat.get("size") or 0), max_file_size)
            entries.append(DownloadEntry(remote=remote_path, local=local_file))
        else:
            raise TransferError(f"remote path does not exist: {remote_path}")

    plan = GetFilesPlan(remotes, destination, tuple(entries))
    return GetFilesResult(plan=plan, downloaded_paths=len(entries))


def bridge_sync_dir(
    client: "bridge_mod.BridgeControlClient",
    remote: str,
    remote_path: str,
    local_path: str | Path,
    *,
    to_remote: bool = False,
    from_remote: bool = False,
    max_archive_size: int = DEFAULT_BRIDGE_MAX_ARCHIVE_SIZE,
) -> SyncDirResult:
    """Synchronize one directory through a bridge remote using Python-built tar archives."""
    if to_remote == from_remote:
        raise TransferError("exactly one of --to-remote or --from-remote is required")
    validate_bridge_path(remote_path)
    if not remote_path:
        raise TransferError("remote path must not be empty")

    local_root = Path(local_path).expanduser()
    validate_transfer_path(str(local_root))

    if to_remote:
        source = _local_dir_snapshot(local_root, must_exist=True)
        destination = _bridge_dir_snapshot(client, remote, remote_path, must_exist=False)
        plan = _build_sync_dir_plan(source, destination)
        if plan.has_work:
            archive_b64 = build_sync_archive_b64(
                local_root.resolve(),
                plan.changed_files,
                plan.dirs,
                max_archive_size=max_archive_size,
            )
            client.file_apply_archive(
                remote=remote,
                path=remote_path,
                archive_b64=archive_b64,
                delete_paths=list(plan.deleted_paths),
                max_size=max_archive_size,
            )
        return SyncDirResult(
            direction="to-remote",
            remote=remote,
            remote_path=remote_path,
            local_path=local_root.resolve(),
            synced_files=len(plan.changed_files),
            deleted_paths=len(plan.deleted_paths),
            created_dirs=len([d for d in plan.dirs if d != "."]),
        )

    source = _bridge_dir_snapshot(client, remote, remote_path, must_exist=True)
    destination = _local_dir_snapshot(local_root, must_exist=False)
    plan = _build_sync_dir_plan(source, destination)
    if plan.has_work:
        response = client.file_archive(
            remote=remote,
            path=remote_path,
            rel_paths=list(plan.changed_files),
            dirs=list(plan.dirs),
            max_size=max_archive_size,
        )
        archive_b64 = str(response.get("archive_b64") or "")
        apply_sync_archive_b64(
            local_root,
            archive_b64,
            delete_paths=plan.deleted_paths,
            max_archive_size=max_archive_size,
        )
    return SyncDirResult(
        direction="from-remote",
        remote=remote,
        remote_path=remote_path,
        local_path=local_root.resolve(),
        synced_files=len(plan.changed_files),
        deleted_paths=len(plan.deleted_paths),
        created_dirs=len([d for d in plan.dirs if d != "."]),
    )


def build_sync_archive_b64(
    root: Path,
    rel_paths: Iterable[str],
    dirs: Iterable[str],
    *,
    max_archive_size: int = DEFAULT_BRIDGE_MAX_ARCHIVE_SIZE,
) -> str:
    data = build_sync_archive_bytes(root, rel_paths, dirs, max_archive_size=max_archive_size)
    return base64.b64encode(data).decode("ascii")


def build_sync_archive_bytes(
    root: Path,
    rel_paths: Iterable[str],
    dirs: Iterable[str],
    *,
    max_archive_size: int = DEFAULT_BRIDGE_MAX_ARCHIVE_SIZE,
) -> bytes:
    root = _resolve_sync_root(root, must_exist=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in _sort_sync_dirs(set(dirs)):
            if rel == ".":
                continue
            _validate_sync_rel(rel)
            target = _resolve_under_root(root, rel)
            if target.exists() and not target.is_dir():
                raise TransferError(f"sync directory entry is not a directory: {rel}")
            info = tarfile.TarInfo(rel)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            tar.addfile(info)
        for rel in sorted(dict.fromkeys(rel_paths)):
            _validate_sync_rel(rel)
            path = _resolve_under_root(root, rel)
            if not path.is_file() or path.is_symlink():
                raise TransferError(f"sync file entry is not a regular file: {rel}")
            info = tar.gettarinfo(str(path), arcname=rel)
            if not info.isfile():
                raise TransferError(f"sync file entry is not a regular file: {rel}")
            info.mtime = 0
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    data = buf.getvalue()
    if len(data) > max_archive_size:
        raise TransferError(
            f"sync archive exceeds bridge size limit ({len(data)} > {max_archive_size} bytes)"
        )
    return data


def apply_sync_archive_b64(
    root: str | Path,
    archive_b64: str,
    *,
    delete_paths: Iterable[str] = (),
    max_archive_size: int = DEFAULT_BRIDGE_MAX_ARCHIVE_SIZE,
) -> dict[str, int]:
    try:
        data = base64.b64decode(archive_b64 or "", validate=True)
    except (ValueError, binascii.Error) as e:
        raise TransferError(f"invalid sync archive base64 payload: {e}")
    return apply_sync_archive_bytes(
        root,
        data,
        delete_paths=delete_paths,
        max_archive_size=max_archive_size,
    )


def apply_sync_archive_bytes(
    root: str | Path,
    archive: bytes,
    *,
    delete_paths: Iterable[str] = (),
    max_archive_size: int = DEFAULT_BRIDGE_MAX_ARCHIVE_SIZE,
) -> dict[str, int]:
    if len(archive) > max_archive_size:
        raise TransferError(
            f"sync archive exceeds bridge size limit ({len(archive)} > {max_archive_size} bytes)"
        )
    root_path = _resolve_sync_root(Path(root), must_exist=False)
    root_path.mkdir(parents=True, exist_ok=True)

    deleted = 0
    for rel in sorted(dict.fromkeys(delete_paths), key=lambda p: (p.count("/"), p), reverse=True):
        if rel == ".":
            continue
        _validate_sync_rel(rel)
        target = _resolve_under_root(root_path, rel)
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        deleted += 1

    files = 0
    dirs = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            rel = member.name
            if rel == ".":
                continue
            _validate_sync_rel(rel)
            target = _resolve_under_root(root_path, rel)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                dirs += 1
                continue
            if not member.isfile():
                raise TransferError(f"sync archive contains unsupported entry: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise TransferError(f"sync archive file has no content: {rel}")
            with extracted, target.open("wb") as out:
                shutil.copyfileobj(extracted, out)
            try:
                os.chmod(target, member.mode & 0o777)
            except OSError:
                pass
            files += 1
    return {"files": files, "dirs": dirs, "deleted": deleted}


def _local_dir_snapshot(root: Path, *, must_exist: bool) -> SyncSnapshot:
    root = _resolve_sync_root(root, must_exist=must_exist)
    if not root.exists():
        return SyncSnapshot(files={}, dirs={"."}, root_exists=False)
    if not root.is_dir():
        raise TransferError(f"local path is not a directory: {root}")

    files: dict[str, SyncFile] = {}
    dirs: set[str] = {"."}
    for child in sorted(root.rglob("*")):
        rel = child.relative_to(root).as_posix()
        _validate_sync_rel(rel)
        if child.is_symlink():
            continue
        if child.is_dir():
            dirs.add(rel)
        elif child.is_file():
            stat = child.stat()
            files[rel] = SyncFile(rel=rel, size=stat.st_size, sha256=_sha256_file(child))
    return SyncSnapshot(files=files, dirs=dirs, root_exists=True)


def _bridge_dir_snapshot(
    client: "bridge_mod.BridgeControlClient",
    remote: str,
    remote_path: str,
    *,
    must_exist: bool,
) -> SyncSnapshot:
    validate_bridge_path(remote_path)
    stat = client.file_stat(remote=remote, path=remote_path)
    rtype = str(stat.get("type") or "none")
    if rtype == "none":
        if must_exist:
            raise TransferError(f"remote path does not exist: {remote_path}")
        return SyncSnapshot(files={}, dirs={"."}, root_exists=False)
    if rtype != "dir":
        raise TransferError(f"remote path is not a directory: {remote_path}")

    listing = client.file_list(remote=remote, path=remote_path, recursive=True)
    items = listing.get("entries") if isinstance(listing, dict) else None
    if not isinstance(items, list):
        raise TransferError(f"bridge file.list returned no entries for {remote_path!r}")

    files: dict[str, SyncFile] = {}
    dirs: set[str] = {"."}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        item_path = str(item.get("path") or "")
        if not item_path:
            continue
        rel = _bridge_remote_relpath(remote_path, item_path)
        if not rel:
            continue
        _validate_sync_rel(rel)
        if item_type == "dir":
            dirs.add(rel)
            continue
        if item_type != "file":
            continue
        size = int(item.get("size") or 0)
        sha256 = str(item.get("sha256") or "")
        if not sha256:
            response = client.file_read(remote=remote, path=item_path)
            content_b64 = str(response.get("content_b64") or "")
            try:
                data = base64.b64decode(content_b64, validate=True)
            except (ValueError, binascii.Error) as e:
                raise TransferError(f"invalid base64 from bridge for {item_path!r}: {e}")
            size = len(data)
            sha256 = hashlib.sha256(data).hexdigest()
        files[rel] = SyncFile(rel=rel, size=size, sha256=sha256)
    return SyncSnapshot(files=files, dirs=dirs, root_exists=True)


def _build_sync_dir_plan(source: SyncSnapshot, destination: SyncSnapshot) -> SyncDirPlan:
    changed = [
        rel
        for rel, src in source.files.items()
        if rel not in destination.files
        or destination.files[rel].size != src.size
        or destination.files[rel].sha256 != src.sha256
    ]
    stale_files = set(destination.files) - set(source.files)
    stale_dirs = set(destination.dirs) - set(source.dirs)
    deleted = sorted(stale_files | stale_dirs, key=lambda p: (p.count("/"), p), reverse=True)
    missing_dirs = set(source.dirs - destination.dirs)
    if not destination.root_exists:
        missing_dirs.add(".")
    dirs = _sort_sync_dirs(missing_dirs)
    return SyncDirPlan(tuple(sorted(changed)), tuple(deleted), tuple(dirs))


def _sort_sync_dirs(dirs: set[str]) -> tuple[str, ...]:
    return tuple(sorted(dirs, key=lambda d: (d.count("/"), d)))


def _resolve_sync_root(root: Path, *, must_exist: bool) -> Path:
    path = root.expanduser()
    if must_exist and not path.exists():
        raise TransferError(f"local path does not exist: {path}")
    if path.exists() and not path.is_dir():
        raise TransferError(f"local path is not a directory: {path}")
    return path.resolve()


def _resolve_under_root(root: Path, rel: str) -> Path:
    _validate_sync_rel(rel)
    target = (root / rel).resolve()
    if target != root and not target.is_relative_to(root):
        raise TransferError(f"sync path escapes root: {rel}")
    return target


def _validate_sync_rel(rel: str) -> None:
    validate_transfer_path(rel)
    if not rel or rel == ".":
        return
    normalized = rel.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("../"):
        raise TransferError(f"sync path must be relative: {rel!r}")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TransferError(f"sync path contains unsafe segment: {rel!r}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bridge_download_dir(
    client: "bridge_mod.BridgeControlClient",
    remote: str,
    remote_root: str,
    destination: Path,
    max_file_size: int,
    entries: list[DownloadEntry],
) -> None:
    base_name = _bridge_remote_basename(remote_root)
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
        rel = _bridge_remote_relpath(remote_root, item_path)
        if not rel:
            continue
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


def _bridge_remote_basename(remote_path: str) -> str:
    """Return the final component of a bridge path using either POSIX or Windows separators."""
    cleaned = remote_path.rstrip("/\\")
    index = max(cleaned.rfind("/"), cleaned.rfind("\\"))
    name = cleaned[index + 1:] if index >= 0 else cleaned
    if not name or name in (".", ".."):
        raise TransferError(f"remote path must include a file or directory name: {remote_path}")
    return name


def _bridge_remote_relpath(base: str, target: str) -> str:
    """Return a bridge target path relative to base, accepting '/' or '\\' from the client."""
    base_norm = base.replace("\\", "/").rstrip("/")
    target_norm = target.replace("\\", "/").rstrip("/")
    if target_norm == base_norm:
        return ""
    if target_norm.startswith(base_norm + "/"):
        return target_norm[len(base_norm) + 1:]
    return _bridge_remote_basename(target_norm)
