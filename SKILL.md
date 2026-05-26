---
name: aimux
description: |
  Use aimux when you need a long-lived shell session — a REPL, a build/server you
  want to interact with across multiple commands, or anything running on a remote
  SSH host. Use bash directly for one-shot commands. aimux gives you 6 session
  primitives + 3 remote-host helpers; this skill explains how to combine them.
---

# Using aimux as an AI agent

aimux is a tmux wrapper. Each "session" is a single shell on a single pane:
- **local** sessions run a local shell
- **remote** sessions run `ssh <host>` locally, transparently driving the remote shell

You drive sessions through three primitives — **send-keys** (type), **wait-last-command-complete** (block, local only), **capture** (read screen). Plus session lifecycle (`new` / `kill` / `ls`) and ssh-config helpers (`remote ls/test/add`).

**Exit codes are meaningful — always check them:**
- `0` success
- `1` user error (bad args, session not found)
- `2` ssh unreachable / config not resolvable
- `3` safety denied (e.g., wait on remote)
- `4` ssh auth required (network OK, no key auth)
- `124` timeout

**`--json` everywhere.** Always use it when parsing output programmatically.

---

## When to use aimux vs plain bash

| Task | Tool |
|---|---|
| One-shot `make build` and you only need the exit code + tail of output | bash directly |
| You'll run several commands that share working dir / env / a long-running process | `aimux new` + `send-keys` |
| You need to do anything on another machine | `aimux new --remote=<host>` |
| You need to interact with a REPL (psql, ipython, node) | `aimux new` + `send-keys` |
| You want to launch something and tail its log later | `aimux new --output-sync-to-file=...` |
| You want to "see what the user is currently looking at in their terminal" | **no** — aimux uses its own dedicated tmux server (socket `aimux`); it doesn't see the user's tmux |

---

## Cheat sheet

```
aimux ls [--json] [--remote-only|--local-only]
aimux new --name=<n> [--remote=<host>] [--cwd=<dir>] [--cmd=<cmd>] [--output-sync-to-file=<path>] [--reuse]
aimux send-keys <id> [tmux-flags] -- <keys...>          # tmux send-keys passthrough
aimux capture <id> --lines=<N> [--ansi] [--json]
aimux wait-last-command-complete <id> --timeout=<dur> [--print-lines=<N>] [--json]
aimux kill <id>
aimux attach <id>                                       # ⚠️ HUMAN ONLY — do not call

aimux remote ls [--json] [--timeout=5s]
aimux remote test <name> [--timeout=5s] [--json]
aimux remote add --host=<h> --port=<p> --user=<u> [--name=<alias>] [--identity=<path>]
```

`<id>` is `<location>/<name>` — e.g. `local/build`, `prod-api/dev`. Short `<name>` works if globally unique.
`<dur>` is `<int>s|m|h` — e.g. `30s`, `5m`, `2h`.

**Do not call `aimux attach`** — it takes over the terminal and there's no way for you to detach again. Always use `send-keys` + `capture`.

---

## The standard recipe (local session)

Use this exact pattern for any command on a local session where you need to read output:

```bash
aimux new --name=build
aimux send-keys local/build -- "make test" Enter
aimux wait-last-command-complete local/build --timeout=5m --print-lines=200
aimux kill local/build
```

`wait-last-command-complete --print-lines=N` returns:
- **stdout** = pane's last N lines (the command output, ready to read/parse)
- **stderr** = `status: completed` / `elapsed: 45.3s`
- **exit code** = 0 (completed) or 124 (timeout, in which case stdout still has whatever ran so far)

The cleanest agent invocation:

```bash
output=$(aimux wait-last-command-complete local/build --timeout=5m --print-lines=500 2>/dev/null)
rc=$?
# now: $output is pure pane content; $rc is 0/124
```

`wait` detects completion by polling `pane_current_command` — it returns when the pane's foreground process is back at a known shell name (`bash`, `zsh`, etc.). That means:

- It works for any command without modifying it (no sentinel injection, no PS1 dependency)
- It does **not** return the command's exit code. If you need the exit code, embed a marker yourself: `send-keys -- "make test; echo __EXIT_$?__" Enter` and grep for `__EXIT_` in capture output.

---

## Remote sessions: wait does NOT work

For remote sessions the local pane runs `ssh` forever, so `pane_current_command` is always `ssh`. **`wait-last-command-complete` exits with code 3 on remote sessions.** Use a sentinel + polling instead:

```bash
aimux new --remote=prod-api --name=task

aimux send-keys prod-api/task -- "make test; echo __DONE_\$?__" Enter

# Then poll capture until you see __DONE_
deadline=$(($(date +%s) + 300))   # 5-minute budget you control
while [ $(date +%s) -lt $deadline ]; do
    out=$(aimux capture prod-api/task --lines=500)
    if echo "$out" | grep -q __DONE_; then
        echo "$out"
        break
    fi
    sleep 2
done

aimux kill prod-api/task
```

Or sleep adaptively if you have a sense of how long it'll take.

---

## Remote-host setup

If `--remote=<host>` fails with "ssh host not resolvable", the host is not in `~/.ssh/config`. Add it:

```bash
# Will probe the host, only writes to ssh config if reachable
aimux remote add --host=10.0.1.5 --port=22 --user=deploy --name=worker-2
```

Probe outcomes:
- **reachable** → entry appended (with backup at `~/.ssh/config.aimux.bak.<ts>`)
- **auth-required** → exit 4, nothing written. Set up keys (e.g. `ssh-copy-id`) and retry
- **unreachable** → exit 2, nothing written. Check network/firewall

Discover existing hosts and their reachability:

```bash
aimux remote ls --json
# [{"name":"prod-api","status":"reachable","rtt_ms":45,...}, ...]
```

Test one host before using it:

```bash
aimux remote test prod-api
# exit 0 reachable / 2 unreachable / 4 auth-required
```

---

## Patterns

### Run a command and read the result (local)

```bash
aimux new --name=q --reuse
out=$(aimux wait-last-command-complete local/q --timeout=10s --print-lines=100 2>/dev/null) || true
aimux send-keys local/q -- "ls -la" Enter
out=$(aimux wait-last-command-complete local/q --timeout=10s --print-lines=100 2>/dev/null)
aimux kill local/q
echo "$out"
```

### Long-running process whose log you want to tail

```bash
mkdir -p /tmp/myrun
aimux new --name=server --output-sync-to-file=/tmp/myrun/server.log
aimux send-keys local/server -- "./bin/server --port=8080" Enter

# Don't wait — fire and forget. Tail the log file from a separate command:
tail -n 50 /tmp/myrun/server.log

# When done:
aimux send-keys local/server -- C-c
aimux kill local/server
```

The file contains raw PTY output (with ANSI escapes). Strip them with `sed 's/\x1b\[[0-9;]*[a-zA-Z]//g'` if you need clean text.

### Interactive REPL

```bash
aimux new --name=py
aimux send-keys local/py -- "python3" Enter
aimux wait-last-command-complete local/py --timeout=5s --poll-interval=200ms || true
# wait will likely time out: python is the foreground process. Just sleep + capture.
sleep 1
aimux capture local/py --lines=10   # confirm prompt
aimux send-keys local/py -- "1 + 1" Enter
sleep 0.5
aimux capture local/py --lines=5
aimux send-keys local/py -- "exit()" Enter
aimux wait-last-command-complete local/py --timeout=5s
aimux kill local/py
```

Note: while `python3` is running, `pane_current_command` is `python3`, not a shell name, so `wait` will time out. Use sleeps + capture polling for any time spent inside a non-shell program.

### Reuse / idempotent setup

```bash
aimux new --name=workspace --reuse   # exits 0 whether new or pre-existing
aimux send-keys local/workspace -- "cd /repo && git pull" Enter
aimux wait-last-command-complete local/workspace --timeout=30s --print-lines=20
```

### Cleanup all aimux sessions you created

```bash
aimux ls --json | python3 -c '
import json, subprocess, sys
for s in json.load(sys.stdin):
    if s.get("created_by") in ("agent", "claude"):  # whatever AIMUX_ACTOR you set
        subprocess.run(["aimux", "kill", s["id"]])
'
```

Set `AIMUX_ACTOR=<your-agent-name>` in your environment so sessions you create are tagged in `created_by` and you can reliably find them later.

---

## Pitfalls and gotchas

1. **Don't call `aimux attach`.** It captures the terminal and you can't detach. Use `send-keys` + `capture`.

2. **`wait` requires a local session.** Exit 3 on remote. Use sentinel + capture polling.

3. **`wait` doesn't return the command's exit code.** Use `; echo __EXIT_$?__` if you need it.

4. **`wait` mis-fires for sub-shells.** If your command is `bash -c '...'`, `pane_current_command` is `bash` (a shell name), so `wait` returns immediately. Use a marker.

5. **`--print-lines` is required for `capture`** and must be a positive integer. There is no "capture everything" mode — agents must declare context size.

6. **Send `Enter` as a key, not a flag.** aimux send-keys is a literal pass-through to `tmux send-keys`. There is no `--enter` shortcut.

7. **`send-keys` does not modify your input.** What you send is what tmux receives. Quoting is your responsibility — pass complete shell strings as a single arg if they contain spaces:
   ```bash
   aimux send-keys local/x -- "for i in 1 2 3; do echo \$i; done" Enter   # good
   aimux send-keys local/x -- for i in 1 2 3 ...                          # bad: each token sent separately
   ```

8. **Names must be unique within the aimux server.** `aimux new --name=foo` fails with exit 1 if `foo` already exists (use `--reuse` to make it idempotent).

9. **Remote pipe to file captures bytes from the local pane**, which is `ssh`'s stdout — i.e. everything your remote shell prints. ANSI escapes included.

10. **No `rename`, no `respawn`, no `run`.** Sessions are immutable. If a remote session disconnects, `kill` it and `new` a fresh one. There's no fix-in-place.

---

## Quick verification commands

Before assuming aimux is set up:

```bash
which aimux               # should print a path
aimux ls --json           # should print [] or a JSON array (never error if installed)
tmux -V                   # tmux 3.0+
ls ~/.ssh/config          # exists if any remote work is needed
```
