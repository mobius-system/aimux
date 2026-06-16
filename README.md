# aimux

`aimux` is an AI-agent-friendly tmux wrapper. 

Use aimux when you need a long-lived shell session — a REPL, a build/server you
want to interact with across multiple commands, or anything running on a remote
SSH host. Use bash directly for one-shot commands. aimux gives you session
primitives, file-transfer helpers, and remote-host helpers; this skill
explains how to combine them.

## Install

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
send_files                  Upload multiple local files or directories to an SSH host using sftp.
get_files                   Download multiple remote files or directories from an SSH host using sftp.
capture                     Capture pane output (last N lines).
kill                        Destroy a single session. Does not accept wildcards or batch.
wait-last-command-complete  Block until the pane's foreground process is back at a shell. Local sessions only.
remote                      Remote-host management (reads/appends ~/.ssh/config).
```

## Example

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

3. Creating a bridge session.

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

4. Upload and download files.

```bash
# aimux send_files REMOTE REMOTE_DIR LOCAL_PATH... [--gitignore]
# aimux get_files REMOTE LOCAL_DIR REMOTE_PATH...
aimux send_files local-8824 /tmp/upload /home/testdir/tests
aimux get_files local-8824 /home/testdir/tests /tmp/upload
```

Use `--gitignore` on upload when ignored files should be skipped:

```bash
aimux send_files local-8824 /tmp/upload /home/testdir/tests --gitignore
```

5. Clean up.

```bash
aimux kill "local-8824/testsession"
```
