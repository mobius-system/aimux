# Manual Test: localhost:8824 Remote Server

This test is meant to be read and run by a human. It verifies the server
described in the issue by using the public `aimux` commands only.

Do not edit `~/.ssh/config` by hand for this test. `aimux remote add` is the
only step that should add the SSH host entry.

Target server:

```text
HostName localhost
Port 8824
User root
```

The test has two levels:

1. Host-management behavior with `aimux remote add` and `aimux remote test`.
2. End-to-end remote session behavior with `aimux new --remote`, when SSH key
   authentication works.

## Prerequisites

- Run from the repository root.
- `ssh` is installed.
- `aimux` dependencies are installed:

```bash
python3 -m pip install -e '.[test]'
```

If `aimux` is already installed, you can skip that command.

Use a unique alias so repeated manual runs do not collide with existing SSH
config hosts:

```bash
ALIAS="aimux-local-8824-manual-$(date +%s)"
SESSION="manual-remote-8824"
```

## Part 1: Add the Host Through aimux

Run:

```bash
aimux remote add \
  --host localhost \
  --port 8824 \
  --user root \
  --name "$ALIAS" \
  --timeout 2s

echo "remote add exit code: $?"
```

Expected result when the server is reachable and SSH key authentication works:

```text
<alias>    reachable    rtt=<number>ms
appended to ~/.ssh/config (backup: <path>)
remote add exit code: 0
```

Expected result when the server is reachable but SSH key authentication is not
configured:

```text
error: probe to root@localhost:8824 got auth-required (network OK but no key auth)
...
remote add exit code: 4
```

That `auth-required` result is useful: it proves the network endpoint is there,
and it proves `aimux remote add` correctly refuses to add an unusable host.

Expected result when the server is not reachable:

```text
error: probe to root@localhost:8824 -> unreachable
remote add exit code: 2
```

Stop here if the exit code is `2`. Fix the server or port before continuing.

Stop here if the exit code is `4` unless you want to set up SSH key
authentication for `root@localhost:8824` and rerun this manual test.

## Part 2: Test the Added Host

Run this part only when Part 1 exits with `0`.

```bash
aimux remote test "$ALIAS" --timeout 2s
echo "remote test exit code: $?"
```

Expected result:

```text
<alias>    reachable    rtt=<number>ms
remote test exit code: 0
```

Also check that it appears in the host list:

```bash
aimux remote ls --timeout 2s
```

Expected result:

```text
HOST                      USER  HOSTNAME   PORT  STATUS     RTT
...
<alias>                   root  localhost  8824  reachable  <number>ms
```

## Part 3: Create a Remote Session

Run this part only when Part 1 and Part 2 exit with `0`.

Create the remote session:

```bash
aimux new --remote "$ALIAS" --name "$SESSION"
sleep 2
aimux capture "$ALIAS/$SESSION" --lines 30
```

Expected result:

- The captured output shows a remote shell prompt, or at least no SSH failure.
- There is no `Permission denied`, `Connection refused`, or `Could not resolve`
  error.

Send a command and look for a marker:

```bash
aimux send-keys "$ALIAS/$SESSION" -- 'printf "__AIMUX_REMOTE_OK__:%s:%s\n" "$USER" "$(hostname)"' Enter
sleep 1
aimux capture "$ALIAS/$SESSION" --lines 50
```

Expected result:

```text
__AIMUX_REMOTE_OK__:root:<remote-hostname>
```

Confirm `wait-last-command-complete` is rejected for remote sessions:

```bash
aimux wait-last-command-complete "$ALIAS/$SESSION" --timeout 2s
echo "wait exit code: $?"
```

Expected result:

```text
error: wait-last-command-complete only supports local sessions; for remote sessions, send-keys an explicit completion marker and poll capture
wait exit code: 3
```

Clean up the tmux session:

```bash
aimux kill "$ALIAS/$SESSION"
```

This test intentionally does not include a manual `~/.ssh/config` cleanup step.
`aimux` currently has `remote add`, `remote ls`, `remote test`, and
`remote hardware`, but no `remote remove` command.

## Pass Criteria

Pass the host-management part when:

- `aimux remote add` exits `0` and prints `reachable`, or exits `4` and prints
  `auth-required` for a reachable server without key authentication.
- `aimux remote add` exits `2` only when the server is genuinely unreachable.

Pass the full end-to-end remote-session part when:

- `aimux remote add` exits `0`.
- `aimux remote test "$ALIAS"` exits `0`.
- `aimux remote ls` shows the alias with `localhost`, port `8824`, user `root`,
  and status `reachable`.
- The remote session captures `__AIMUX_REMOTE_OK__:root:<remote-hostname>`.
- `wait-last-command-complete` returns exit code `3` for the remote session.
