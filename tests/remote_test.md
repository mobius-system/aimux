# Remote Test: List Files on the Target Host

This is a human-readable test for the issue target:

```text
HostName localhost
Port 8824
User root
```

Use `aimux` commands for the remote workflow. Do not open SSH directly, and do
not edit `~/.ssh/config` by hand.

## Prerequisites

- Run from the repository root.
- `aimux` is installed:

```bash
python3 -m pip install -e '.[test]'
```

Use a unique alias and session name:

```bash
ALIAS="aimux-local-8824-ls-$(date +%s)"
SESSION="remote-ls"
```

## 1. Add the Target Host With aimux

```bash
aimux remote add \
  --host localhost \
  --port 8824 \
  --user root \
  --name "$ALIAS" \
  --timeout 2s
```

Expected success:

```text
<alias>    reachable    rtt=<number>ms
appended to ~/.ssh/config (backup: <path>)
```

If this prints `auth-required`, the server is reachable but SSH key
authentication is not configured. Stop here until key authentication works.

If this prints `unreachable`, stop here and fix the server, port, or network.

## 2. Verify the Host With aimux

```bash
aimux remote test "$ALIAS" --timeout 2s
```

Expected success:

```text
<alias>    reachable    rtt=<number>ms
```

List known remote hosts through `aimux`:

```bash
aimux remote ls --timeout 2s
```

Expected result includes the alias:

```text
HOST                 USER  HOSTNAME   PORT  STATUS     RTT
...
<alias>              root  localhost  8824  reachable  <number>ms
```

## 3. Create a Remote aimux Session

```bash
aimux new --remote "$ALIAS" --name "$SESSION"
```

Capture the session screen:

```bash
aimux capture "$ALIAS/$SESSION" --lines 30
```

Expected result:

- The screen shows a remote shell prompt, or at least no SSH failure.
- The screen does not contain `Permission denied`, `Connection refused`, or
  `Could not resolve`.

## 4. List Files on the Target Host

Send a remote command that prints the current directory and lists files. The
begin/end markers make the captured output easy to identify.

```bash
aimux send-keys "$ALIAS/$SESSION" -- 'printf "__AIMUX_REMOTE_LS_BEGIN__\n"; pwd; ls -la; printf "__AIMUX_REMOTE_LS_END__:%s\n" "$?"' Enter
```

Wait a moment, then capture the remote session:

```bash
aimux capture "$ALIAS/$SESSION" --lines 200
```

Expected result:

```text
__AIMUX_REMOTE_LS_BEGIN__
<remote-current-directory>
total <number>
drwx...
...
__AIMUX_REMOTE_LS_END__:0
```

The file listing between the markers is the listing from the target host
machine, not from the local machine.

If the end marker is missing, run the capture command again:

```bash
aimux capture "$ALIAS/$SESSION" --lines 200
```

## 5. Clean Up the aimux Session

```bash
aimux kill "$ALIAS/$SESSION"
```

This test only cleans up the tmux session. It does not manually edit
`~/.ssh/config`; `aimux` currently provides `remote add`, `remote ls`,
`remote test`, and `remote hardware`, but no `remote remove` command.

## Pass Criteria

The test passes when:

- `aimux remote add` reports `reachable`.
- `aimux remote test "$ALIAS"` reports `reachable`.
- `aimux remote ls` shows the alias with `root`, `localhost`, port `8824`, and
  status `reachable`.
- `aimux capture "$ALIAS/$SESSION" --lines 200` shows a file listing between
  `__AIMUX_REMOTE_LS_BEGIN__` and `__AIMUX_REMOTE_LS_END__:0`.
- `aimux kill "$ALIAS/$SESSION"` succeeds.
