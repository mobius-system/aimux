"""SFTP upload helpers for `aimux send_file`."""
from __future__ import annotations

import posixpath
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class TransferError(RuntimeError):
    """Raised for local planning errors before an sftp upload starts."""


class SftpUploadError(TransferError):
    """Raised when the sftp subprocess fails."""

    def __init__(self, returncode: int, stdout: str, stderr: str, args: list[str]):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.args = args
        msg = stderr.strip() or stdout.strip() or f"sftp exited with status {returncode}"
        super().__init__(msg)


@dataclass(frozen=True)
class UploadEntry:
    local: Path
    remote: str


@dataclass(frozen=True)
class SendPlan:
    local_path: Path
    remote_path: str
    files: tuple[UploadEntry, ...]
    dirs: tuple[str, ...]
    skipped_files: int

    @property
    def has_work(self) -> bool:
        return bool(self.files or self.dirs)


@dataclass(frozen=True)
class SendResult:
    plan: SendPlan
    uploaded_files: int
    skipped_files: int


def send_path(host: str, local_path: str | Path, remote_path: str, *, gitignore: bool = False) -> SendResult:
    """Upload `local_path` to `host:remote_path` using sftp."""
    plan = build_send_plan(local_path, remote_path, gitignore=gitignore)
    if plan.has_work:
        _run_sftp_batch(host, build_sftp_batch(plan))
    return SendResult(
        plan=plan,
        uploaded_files=len(plan.files),
        skipped_files=plan.skipped_files,
    )


def build_send_plan(local_path: str | Path, remote_path: str, *, gitignore: bool = False) -> SendPlan:
    source = Path(local_path).expanduser()
    if not source.exists():
        raise TransferError(f"local path does not exist: {source}")
    source = source.resolve()
    _validate_sftp_path(str(source))
    _validate_sftp_path(remote_path)
    if not remote_path:
        raise TransferError("remote path must not be empty")

    if source.is_file():
        files = _included_files(source, gitignore=gitignore)
        skipped = 1 - len(files)
        entries = tuple(
            UploadEntry(local=file_path, remote=_remote_file_destination(remote_path, file_path.name))
            for file_path in files
        )
        dirs = _collect_remote_dirs(_remote_parent(entry.remote) for entry in entries)
        return SendPlan(source, remote_path, entries, dirs, skipped)

    if not source.is_dir():
        raise TransferError(f"local path is not a regular file or directory: {source}")

    all_files = _all_files(source)
    files = _included_files(source, gitignore=gitignore)
    skipped = len(all_files) - len(files)

    entries = tuple(
        UploadEntry(
            local=file_path,
            remote=_remote_join(remote_path, file_path.relative_to(source).as_posix()),
        )
        for file_path in files
    )

    dirs: set[str] = set()
    for remote_dir in _remote_dir_ancestors(remote_path):
        dirs.add(remote_dir)
    if not gitignore:
        for local_dir in sorted(p for p in source.rglob("*") if p.is_dir()):
            dirs.update(_remote_dir_ancestors(_remote_join(remote_path, local_dir.relative_to(source).as_posix())))
    for entry in entries:
        dirs.update(_remote_dir_ancestors(_remote_parent(entry.remote)))

    return SendPlan(source, remote_path, entries, _sort_remote_dirs(dirs), skipped)


def build_sftp_batch(plan: SendPlan) -> str:
    lines: list[str] = []
    for remote_dir in plan.dirs:
        lines.append(f"-mkdir {_sftp_quote(remote_dir)}")
    for entry in plan.files:
        lines.append(f"put {_sftp_quote(str(entry.local))} {_sftp_quote(entry.remote)}")
    return "\n".join(lines) + "\n"


def _run_sftp_batch(host: str, batch: str) -> None:
    if shutil.which("sftp") is None:
        raise TransferError("sftp executable not found")

    args = ["sftp", "-o", "BatchMode=yes", "-b", "-", host]
    try:
        result = subprocess.run(args, input=batch, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise TransferError("sftp executable not found") from e

    if result.returncode != 0:
        raise SftpUploadError(result.returncode, result.stdout, result.stderr, args)


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


def _remote_file_destination(remote_path: str, local_name: str) -> str:
    if remote_path.endswith("/"):
        return _remote_join(remote_path, local_name)
    return remote_path


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


def _validate_sftp_path(path: str) -> None:
    if "\0" in path or "\n" in path or "\r" in path:
        raise TransferError("sftp paths must not contain control newlines or NUL bytes")
