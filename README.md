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

─ Device Operation Commands
remote                      Remote-host management (reads/appends ~/.ssh/config).
bridge                      Bridge broker management.
reverse                     Reverse-connect this machine to an aimux bridge broker.
mcp                         Run a stdio MCP server exposing a bridge remote to external agents.

─ Session Operation Commands
ls                          List all aimux sessions.
new                         Create a new session.
attach                      Attach to a session. ⚠️  Human use only — agents should use send-keys + capture.
send-keys                   Send keys to a session. Behaves exactly like 'tmux send-keys': supports -F/-H/-K/-l/-M/-R/-X/-N <count>. Pass 'Enter' as a key to submit a line.
kill                        Destroy a single session. Does not accept wildcards or batch.
capture                     Capture pane output (last N lines).
wait-last-command-complete  Block until the pane's foreground process is back at a shell. Local sessions only.

─ File Operation Commands
send-files                  Shortcut over the file API: upload files or directories (mkdir + write).
get-files                   Shortcut over the file API: download files or directories (stat + read).
sync-dir                    Shortcut over the file API: differentially sync a directory (list + read/write + archive).

─ File Read/Edit Commands
read                        Read a remote file: metadata header (path, encoding, size, sha256), then content.
apply-patch                 Apply a patch to create, edit, or delete files (stdin, or --file).

─ Advanced Port Operation Commands
local-http-port-reroute     Listen on a local port and tunnel it to another bridge client's allowed HTTP port.
port                        Forward local HTTP traffic through an aimux bridge broker.
```

Every command also carries worked examples: run `aimux <command> -h`.


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

On a Windows client you can also opt into computer use. Pass `--enable-gui`
and the client downloads a small accessibility helper (UIA-based, single
binary, sha256-verified) to `~/.aimux/helpers/`, starts it, and reports the
GUI capability with its registration. The helper never needs an OS permission
prompt; it must simply run inside a logged-in desktop session (not over SSH).
A failed download or probe never blocks the connection — the remote just
registers with GUI off:

```bash
aimux reverse connect http://SERVER:8765 --identifier=winbox --token=<token> --enable-gui
```

`aimux remote ls` shows a `GUI` column (`on`/`off`) so the capability stays
visible.

4. Forward a browser-facing HTTP service through the bridge server.

```bash
aimux port forward https://SERVER/aimux_bridge \
  --local-port=8080 \
  --remote-host=127.0.0.1 \
  --remote-port=3000 \
  --token=<token>
```

Open `http://127.0.0.1:8080`. Ordinary HTTP requests, streaming SSE responses,
and WebSocket upgrades are forwarded to `http://127.0.0.1:3000` from the
bridge server's network namespace. Use `--remote-scheme=https` for an HTTPS
target.

5. Reach a port on *another bridge client's* loopback.

`port forward` (above) dials the target from the **broker's** network. When the
service instead lives on a reverse-connected machine behind NAT, the target
client has to do the dialling — and it will only do so for ports it explicitly
allowed.

```bash
# client A: declare which local ports other clients may reach (default: none)
aimux reverse connect http://SERVER:8765 \
  --identifier=a-box --token=<token> \
  --allow-http-ports=8000,8001

# client B: listen locally on 9000 and tunnel to a-box's 127.0.0.1:8000
aimux local-http-port-reroute \
  --local-port=9000 \
  --remote-client-id=a-box \
  --remote-client-http-port=8000 \
  --bridge-url=http://SERVER:8765 --token=<token>
```

Now `http://127.0.0.1:9000` on B reaches `http://127.0.0.1:8000` on A. The
tunnel moves raw bytes, so ordinary HTTP, streaming SSE responses, and
WebSocket upgrades all work — and, as with `port forward`, nothing is parsed or
rewritten in between.

6. Expose a remote to an MCP agent — optionally with computer use.

```bash
# classic tools only (remote_exec_command, remote_write_stdin, remote_ping, ...)
aimux mcp serve --remote=winbox

# classic + computer-use tools (remote_gui_diagnostics, remote_gui_list_windows,
# remote_gui_look, remote_gui_act, remote_gui_read_text, remote_gui_wait_for,
# remote_gui_focus_window, remote_gui_record_window_mp4)
aimux mcp serve --remote=winbox --toolset=classic-and-gui
```

GUI tools drive the remote's accessibility tree: `remote_gui_look` returns an
element outline (with refs) plus a screenshot, and `remote_gui_act` acts on a
ref semantically through UIA patterns. The default policy `ax_only` never
injects real mouse/keyboard input; raw input requires explicitly passing
`policy=foreground`. `remote_gui_record_window_mp4` records one window to an
MP4 and copies it back: H.264 video from the same capture path `look` uses,
plus AAC sound from the desktop's speakers or the microphone, encoded by
Windows itself (no FFmpeg) — Windows-only, other platforms fail immediately.
`record_fps` (1-30, default 10) and `max_dimension` (cap the longest side,
aspect ratio kept) are the two knobs that decide how big the file gets.
Serving `--toolset=classic-and-gui` against a remote without GUI fails fast
with the reason.

Start an MCP session with `remote_ping`: alongside the link's health it reports
where your commands land — the remote's OS and architecture, the shell profile
they run in with the syntax it expects, the directory they start from, and the
encoding their output comes back in — so an agent can write Windows- or
Linux-shaped commands without probing for the answer first. A remote whose
aimux predates the request still answers from what the broker registry knows.

The command is symmetric: run it on A too (naming B) to get the reverse
direction. Ports are checked twice — the broker refuses a port the target
never allowed, and the target re-checks the allowlist itself before dialling —
so a client that never passes `--allow-http-ports` cannot be connected to at
all. Targets are always dialled on `127.0.0.1`; there is no `--remote-host`, so
a tunnel can never become a jump host onto the rest of A's network.

Because the traffic is tunnelled rather than proxied, B's listener sees the
bytes exactly as A's service emitted them: absolute URLs, cookies, and `Host`
headers all refer to A's loopback address, not to B's.

The listener binds `127.0.0.1` by default. Pass `--share-to-non-localhost` to
bind `0.0.0.0` and let other machines reach the tunnel through B as well —
note that the listener carries no authentication of its own, so anyone who can
reach that port reaches the tunnelled service. `--local-host <addr>` picks a
specific interface instead.

6. Upload and download files.

## Logs

Logs go to stderr, one line per command, at `INFO`. The bridge polls while it
waits for a command (`file.stat` every 0.3s, `session.capture` every 5s) and
those polled commands are logged at `DEBUG`, so a long-running command does not
bury the log under thousands of lines.

```bash
AIMUX_LOG_LEVEL=DEBUG aimux reverse connect http://server:8765 --identifier box --token TOKEN
```

`AIMUX_LOG_LEVEL` accepts any loguru level (`TRACE`/`DEBUG`/`INFO`/`SUCCESS`/
`WARNING`/`ERROR`/`CRITICAL`); anything unrecognized falls back to `INFO`.

### File transfer for bridge remotes

`aimux send-files` and `aimux get-files` work the same way for bridge remotes
as for SSH hosts — the CLI auto-detects the remote type and dispatches
accordingly. Bridge transfers use a base64-encoded `file.*` message family
over the existing SSE+POST channel, with no sftp dependency on the client.

```bash
aimux send-files winbox 'C:\Users\dev\proj' ./src --gitignore
aimux get-files winbox ./logs 'C:\Users\dev\proj\logs'
```

Differences vs the SSH path:

- Single-file size limit defaults to 16 MiB (configurable via
  `AIMUX_BRIDGE_MAX_FILE_SIZE` if you want to override it in code).
- Files are written with mode 0o644; existing timestamps/permissions are not
  preserved.
- Symlinks are not followed on the remote side — they show up as `symlink`
  entries in directory listings and are skipped during recursive download.
- `..` segments in remote paths are rejected to prevent traversal.

```bash
# aimux send-files REMOTE REMOTE_DIR LOCAL_PATH... [--gitignore]
# aimux get-files REMOTE LOCAL_DIR REMOTE_PATH...
aimux send-files local-8824 /tmp/upload /home/testdir/tests
aimux get-files local-8824 /home/testdir/tests /tmp/upload
```

Both commands report every path on both sides as an absolute path — once before
the transfer starts, once after it finishes:

```text
uploading 2 file(s) to local-8824:/tmp/upload
  /home/testdir/tests/a.txt -> local-8824:/tmp/upload/tests/a.txt
  /home/testdir/tests/sub/b.txt -> local-8824:/tmp/upload/tests/sub/b.txt
uploaded 2 file(s) to local-8824:/tmp/upload
  /home/testdir/tests/a.txt -> local-8824:/tmp/upload/tests/a.txt
  /home/testdir/tests/sub/b.txt -> local-8824:/tmp/upload/tests/sub/b.txt
```

Use `--gitignore` on upload when ignored files should be skipped:

```bash
aimux send-files local-8824 /tmp/upload /home/testdir/tests --gitignore
```

5. Clean up.

```bash
aimux kill "local-8824/testsession"
```
