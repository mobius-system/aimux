---
name: aimux
description: Use aimux when you need a long-lived shell session — a REPL, a build/server you want to interact with across multiple commands, or anything running on a remote SSH host. Use bash directly for one-shot commands. aimux gives you session primitives, file-transfer helpers, and remote-host helpers; this skill explains how to combine them.
---

# Install

```bash
pip install aimux # or `uv pip install aimux` if using uv venv
```

# Usage

`aimux` is tool that enables tmux-like capability cross ssh.

```bash
(.venv) /home/.../...$ aimux -h

Usage: aimux [OPTIONS] COMMAND [ARGS]...

aimux — AI-agent-friendly tmux wrapper.

─ Options
--help  -h        Show this message and exit.

─ Commands 
ls                          List all aimux sessions.
new                         Create a new session.
attach                      Attach to a session. ⚠️  Human use only — agents should use send-keys + capture.
send-keys                   Send keys to a session. Behaves exactly like 'tmux send-keys': supports -F/-H/-K/-l/-M/-R/-X/-N <count>. Pass 'Enter' as a key to submit a line.     
send-files                  Upload multiple local files or directories to an SSH host using sftp.
get-files                   Download multiple remote files or directories from an SSH host using sftp.
sync-dir                    Differentially synchronize a local directory with a bridge remote directory.
read                        Read a remote file and print a metadata header followed by numbered content.
apply-patch                 Apply a patch to create, edit, or delete files (absolute paths only).
capture                     Capture pane output (last N lines).
kill                        Destroy a single session. Does not accept wildcards or batch.
wait-last-command-complete  Block until the pane's foreground process is back at a shell. Local sessions only.
remote                      Remote-host management (reads/appends ~/.ssh/config).
```

# Example

1. adding and naming a remote host in `aimux`.

First, usually you will get a ssh address, for example, 

```text
HostName localhost
Port 8824
User root
```

To begin, you will need to check whether this address is already inside `aimux`'s storage:
```bash
$ aimux remote ls
HOST              USER  HOSTNAME    PORT  STATUS         RTT   
github.com        git   github.com  22    auth-required  2261ms
local-8824        root  localhost   8824  reachable      709ms
```

If not in storage, you will have to add that server using:

```bash
aimux remote add --host localhost --port 8824 --user root --name local-8824 --timeout 2s
aimux remote test local-8824 --timeout 2s
```

2. Creating a session and run command in it.

```bash
aimux new --remote local-8824 --name testsession
aimux send-keys "local-8824/testsession" -- 'ls -la' Enter
aimux capture "local-8824/testsession" --lines 200
```

3. Upload and download files.

```bash
# aimux send-files REMOTE REMOTE_DIR LOCAL_PATH... [--gitignore]
# aimux get-files REMOTE LOCAL_DIR REMOTE_PATH...
aimux send-files local-8824 /tmp/upload ~/aimux-tests
aimux get-files local-8824 ~/aimux /tmp/upload
```

Both commands list every path on both sides as an absolute path — once before
the transfer starts and once after it finishes — so the output alone says where
each file came from and where it landed.

Use `--gitignore` on upload when ignored files should be skipped:

```bash
aimux send-files local-8824 /tmp/upload ~/aimux-tests --gitignore
```

4. Reach a port on another bridge client's loopback.

A reverse client can only be connected to for the ports it explicitly allowed,
and the tunnel is byte-transparent (HTTP, SSE, and WebSocket all pass through):

```bash
# on client A
aimux reverse connect http://SERVER:8765 --identifier=a-box --token=<token> \
  --allow-http-ports=8000

# on client B: local 9000 -> a-box's 127.0.0.1:8000
aimux local-http-port-reroute --local-port=9000 --remote-client-id=a-box \
  --remote-client-http-port=8000 --bridge-url=http://SERVER:8765 --token=<token>
```

5. Clean up.

```bash
aimux kill "local-8824/testsession"
```

# Logs

One line per command at `INFO` on stderr; the bridge's own polling
(`file.stat` every 0.3s, `session.capture` every 5s) is `DEBUG`, so a long
command stays quiet. Set `AIMUX_LOG_LEVEL=DEBUG` to see every command it runs.

## Computer use (Windows x64)

If the remote was connected with `aimux reverse connect --enable-gui`, you can
serve GUI accessibility tools alongside the classic ones:

```bash
aimux mcp serve --remote <device> --toolset classic-and-gui
```

Flow: `remote_gui_list_windows` -> `remote_gui_look` (outline + screenshot +
refs) -> `remote_gui_act` on a ref (`policy=ax_only` by default: semantic UIA
delivery, no real input) -> `remote_gui_read_text` / `remote_gui_wait_for` to
verify. Menus and popups appear as NEW roots — list windows again after
clicking one instead of re-reading the original window. Elevated (admin)
windows cannot be driven unless the client itself is elevated.

Check capability with `aimux remote ls` (GUI column) or `remote_gui_diagnostics`.
