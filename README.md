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

# upload a file or directory to an SSH host via sftp
aimux send_file ./dist worker /tmp/dist --gitignore

# done
aimux kill local/dev
```

Remote sessions are local tmux sessions whose first pane runs `ssh <host>`:

```bash
aimux remote add --host=10.0.1.5 --port=22 --user=deploy --name=worker
aimux new --remote=worker --name=task-1
```

## Tests

```bash
python3 -m pytest
```

For the issue server `root@localhost:8824`, see the human-readable manual test
in `tests/manual_localhost_8824_remote.md`. To verify a remote session by
listing files on the target host, see `tests/remote_test.md`.

Command surface: session commands, `send_file`, and remote host commands. See
`design.md`.
