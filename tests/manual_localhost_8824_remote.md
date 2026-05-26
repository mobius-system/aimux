# Manual Test: localhost:8824 Remote Server

This test is meant to be read and run by a human. It verifies the server
described in the issue:

```sshconfig
Host <alias>
    HostName localhost
    Port 8824
    User root
```

The test has three parts:

1. A safe probe that does not modify your real `~/.ssh/config`.
2. An `aimux remote` host-management check, only if SSH key authentication
   works for `root@localhost:8824`.
3. An end-to-end `aimux` remote-session check, only if SSH key authentication
   works for `root@localhost:8824`.

## Prerequisites

- Run from the repository root.
- `ssh` is installed.
- `aimux` dependencies are installed:

```bash
python3 -m pip install -e '.[test]'
```

If you already installed the package, you can skip that command.

## Part 1: Safe SSH Probe

This uses a temporary home directory so `StrictHostKeyChecking=accept-new` does
not write to your real `~/.ssh/known_hosts`.

```bash
TEST_HOME="$(mktemp -d)"
mkdir -p "$TEST_HOME/.ssh"
chmod 700 "$TEST_HOME/.ssh"

HOME="$TEST_HOME" ssh \
  -o BatchMode=yes \
  -o ConnectTimeout=2 \
  -o StrictHostKeyChecking=accept-new \
  -p 8824 \
  root@localhost \
  true

echo "ssh exit code: $?"
```

Expected result:

- Exit code `0`: the server is reachable and key authentication works.
- Exit code `255` with `Permission denied`: the server is reachable, but key
  authentication is not configured. This is enough to prove the endpoint exists,
  but `aimux remote add` will refuse to append the host.
- Exit code `255` with `Connection refused`, `timed out`, or `Could not resolve`:
  the server is not reachable from this machine.

Clean up:

```bash
rm -rf "$TEST_HOME"
```

## Part 2: Check aimux Remote Host Commands

Run this part only when Part 1 exits with `0`.

Use a unique alias so the test does not collide with existing SSH config hosts:

```bash
ALIAS="aimux-local-8824-manual-$(date +%s)"
SESSION="manual-remote-8824"
```

Add the host through `aimux`:

```bash
aimux remote add \
  --host localhost \
  --port 8824 \
  --user root \
  --name "$ALIAS" \
  --timeout 2s
```

Expected result:

```text
<alias>    reachable    rtt=<number>ms
appended to ~/.ssh/config (backup: <path>)
```

If the command exits with code `4` and prints `auth-required`, key
authentication is not configured. Configure SSH key login for
`root@localhost:8824`, then rerun this part.

Verify the host can be tested:

```bash
aimux remote test "$ALIAS" --timeout 2s
echo "aimux remote test exit code: $?"
```

Expected result:

```text
<alias>    reachable    rtt=<number>ms
aimux remote test exit code: 0
```

## Part 3: Check an aimux Remote Session

Create a remote session:

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

Clean up:

```bash
aimux kill "$ALIAS/$SESSION"
```

`aimux remote add` prints the backup path it created before modifying
`~/.ssh/config`. If you want to remove the test alias, either restore that backup
or delete the appended `Host $ALIAS` block manually.

## Pass Criteria

Pass the manual test when:

- Part 1 proves `root@localhost:8824` is reachable.
- Part 2 reports `reachable` with exit code `0`.
- Part 3 captures `__AIMUX_REMOTE_OK__:root:<remote-hostname>`.
- `wait-last-command-complete` returns exit code `3` for the remote session.
