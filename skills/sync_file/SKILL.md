---
name: sync_file
description: |
    Use sync_file when you need to move files between the local workspace and an
    aimux remote SSH host. Use `send_files` for uploading one or more local files
    or directories, and `get_files` for downloading one or more remote files or
    directories back to local.
---


# Usage

`aimux` syncs files through `sftp`, using remote hosts already registered in
`~/.ssh/config`.

```bash
(.venv) /home/.../...$ aimux -h

Usage: aimux [OPTIONS] COMMAND [ARGS]...

aimux — AI-agent-friendly tmux wrapper.

─ Commands
send_files  Upload multiple local files or directories to an SSH host using sftp.
get_files   Download multiple remote files or directories from an SSH host using sftp.
remote      Remote-host management (reads/appends ~/.ssh/config).
```

# Example

1. Check or register the remote host.

First, list known remotes:

```bash
aimux remote ls
```

If the target is missing, add and test it:

```bash
aimux remote add --host localhost --port 8824 --user root --name local-8824 --timeout 2s
aimux remote test local-8824 --timeout 2s
```

2. Upload one or more local paths.

Use `send_files` for uploading. A single local path is valid:

```bash
aimux send_files local-8824 /tmp/upload ./README.md
aimux send_files local-8824 /tmp/upload ./dist --gitignore
```

Several local paths can go into the same remote directory:

```bash
aimux send_files local-8824 /tmp/upload ./README.md ./dist --gitignore
```

Files are uploaded as `/tmp/upload/<filename>`. Directories are uploaded under
their own basename, for example `./dist` becomes `/tmp/upload/dist/...`.

3. Download remote files or directories.

Use `get_files` to download one or more remote paths into a local directory:

```bash
aimux get_files local-8824 ./downloads /tmp/upload/README.md /tmp/upload/dist
```

The local directory is created if needed. Downloaded names come from the remote
path basename, for example `/tmp/upload/README.md` becomes
`./downloads/README.md`.

4. Verify the transfer.

For uploaded files, use an aimux remote session or another remote command path to
inspect the target:

```bash
aimux new --remote local-8824 --name sync-check --reuse
aimux send-keys local-8824/sync-check -- 'ls -la /tmp/upload' Enter
aimux capture local-8824/sync-check --lines 100
```

Clean up the check session when done:

```bash
aimux kill local-8824/sync-check
```

# Notes

- `--gitignore` applies only to upload commands.
- `--gitignore` uploads tracked files and untracked non-ignored files, while
  skipping files ignored by git ignore rules.
- `send_files` rejects duplicate remote targets before starting `sftp`.
- `get_files` uses recursive download, so it can fetch either remote files or
  remote directories.
