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

# upload files or directories to an SSH host via sftp
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

## Tests

```bash
python3 -m pytest
```

For the issue server `root@localhost:8824`, see the human-readable manual test
in `tests/manual_localhost_8824_remote.md`. To verify a remote session by
listing files on the target host, see `tests/remote_test.md`.

Command surface: session commands, file transfer commands, and remote host commands. See
`design.md`.
