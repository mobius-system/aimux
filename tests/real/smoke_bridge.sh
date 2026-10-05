#!/usr/bin/env bash
# aimux bridge smoke test — drive a session on a remote bridge client through the
# mobius aimux service and log every command with its result.
#
#     AIMUX_SMOKE_REMOTE=gptac-zs AIMUX_SMOKE_REMOTE_DIR=/tmp/x bash smoke_bridge.sh
#
# Requires: the remote is registered as a bridge client (aimux reverse connect),
# and the local aimux can reach the broker. Needs a POSIX profile on the remote.
set -u
AIMUX="${AIMUX:-$HOME/.local/bin/aimux}"
REMOTE="${AIMUX_SMOKE_REMOTE:-young_garden}"
RDIR="${AIMUX_SMOKE_REMOTE_DIR:-/home/fuqingxu/test_server/test_aimux}"
SESSION=aimux_bridge_basic
LOG="$(cd "$(dirname "$0")" && pwd)/bridge_test.log"

pass=0
fail=0
group_no=0

group() { group_no=$((group_no + 1)); printf -- '--- 测试%d ---\n\n' "$group_no"; }
step() {
  printf '运行命令：\n%s\n返回结果：\n' "$1"
  local out
  out="$(bash -c "$1" 2>&1)"
  if [ -n "$out" ]; then printf '%s\n' "$out"; fi
  printf '\n'
  pass=$((pass + $(printf '%s\n' "$out" | grep -c '^PASS')))
  fail=$((fail + $(printf '%s\n' "$out" | grep -c '^FAIL')))
}

{
printf 'aimux bridge smoke test — %s\n目标: %s:%s   驱动端: %s\n\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REMOTE" "$RDIR" "$(hostname)"

group
step "$AIMUX remote ls | grep -E \"HOST|$REMOTE\""
step "$AIMUX remote ls | grep -q \"$REMOTE.*connected\" && echo \"PASS $REMOTE 已注册且 connected\" || echo \"FAIL $REMOTE 未连接\""

group
step "$AIMUX kill $REMOTE/$SESSION >/dev/null 2>&1 || true"
step "$AIMUX new --remote $REMOTE --profile "${AIMUX_SMOKE_PROFILE:-posix-pty}" --name $SESSION --cwd $RDIR"
step "$AIMUX send-keys $REMOTE/$SESSION -- 'pwd; echo BRIDGE_OK_42' Enter"
step "sleep 3; $AIMUX capture $REMOTE/$SESSION --lines 40"
step "$AIMUX capture $REMOTE/$SESSION --lines 40 | grep -q 'BRIDGE_OK_42' && echo \"PASS 命令在远端跑出结果\" || echo \"FAIL 没拿到输出\""
step "$AIMUX capture $REMOTE/$SESSION --lines 40 | grep -q '$RDIR' && echo \"PASS 远端会话 cwd = $RDIR\" || echo \"FAIL cwd 不对\""
step "$AIMUX ls | grep -E 'LOCATION|$SESSION'"

group
step "$AIMUX send-files $REMOTE $RDIR $LOG"
step "$AIMUX kill $REMOTE/$SESSION"
step "$AIMUX ls | grep -q '$SESSION' && echo \"FAIL 会话还在\" || echo \"PASS 会话已销毁\""

printf '==== %d passed, %d failed ====\n' "$pass" "$fail"
} 2>&1 | tee "$LOG"

# 把日志本身回传到目标目录（走 bridge 的 file.write 通道）
"$AIMUX" send-files "$REMOTE" "$RDIR" "$LOG" >/dev/null 2>&1
