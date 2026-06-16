# aimux

AI-agent-friendly tmux wrapper. See `design.md` for the full specification.

## Install

```bash
pip install -e .
```

## Quick start

```bash
# create a local session
aimux new --name=dev

# send a command, wait, then read the last 200 lines
aimux send-keys local/dev -- "make test" Enter
aimux wait-last-command-complete local/dev --timeout=5m --print-lines=200

# list everything
aimux ls

# upload files or directories to an SSH or bridge host
aimux send_files worker /tmp/upload ./dist ./README.md --gitignore
aimux get_files worker ./downloads /tmp/upload/README.md /tmp/upload/dist

# done
aimux kill local/dev
```

Remote sessions are local tmux sessions whose first pane runs `ssh <host>`:

```bash
aimux remote add --host=10.0.1.5 --port=22 --user=deploy --name=worker
aimux new --remote=worker --name=task-1
```

Bridge remotes are reverse-connected clients. Start a broker on the server,
connect a client back to it, then create sessions through the same `--remote`
API. Bridge remotes require an explicit profile:

```bash
# server
aimux bridge deploy --port=8765

# client
aimux reverse connect http://SERVER:8765 --identifier=winbox --token=<token>

# server
aimux remote ls
aimux new --remote=winbox --profile=cmd --name=cmd-1
aimux send-keys winbox/cmd-1 -- "echo hello" Enter
aimux capture winbox/cmd-1 --lines=50
```

Windows bridge profiles include `cmd`, `powershell`, `pwsh`, and `mingw64`
when available. macOS exposes `mac-pty` and `mac-terminal`; Linux exposes
`posix-pty`.

### File transfer for bridge remotes

`aimux send_files` and `aimux get_files` work the same way for bridge remotes
as for SSH hosts — the CLI auto-detects the remote type and dispatches
accordingly. Bridge transfers use a base64-encoded `file.*` message family
over the existing SSE+POST channel, with no sftp dependency on the client.

```bash
aimux send_files winbox 'C:\Users\dev\proj' ./src --gitignore
aimux get_files winbox ./logs 'C:\Users\dev\proj\logs'
```

Differences vs the SSH path:

- Single-file size limit defaults to 16 MiB (configurable via
  `AIMUX_BRIDGE_MAX_FILE_SIZE` if you want to override it in code).
- Files are written with mode 0o644; existing timestamps/permissions are not
  preserved.
- Symlinks are not followed on the remote side — they show up as `symlink`
  entries in directory listings and are skipped during recursive download.
- `..` segments in remote paths are rejected to prevent traversal.

## Tests

```bash
python3 -m pytest
```

For the issue server `root@localhost:8824`, see the human-readable manual test
in `tests/manual_localhost_8824_remote.md`. To verify a remote session by
listing files on the target host, see `tests/remote_test.md`.

Command surface: session commands, file transfer commands, and remote host commands. See
`design.md`.
