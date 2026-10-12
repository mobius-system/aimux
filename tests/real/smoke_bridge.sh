#!/usr/bin/env bash
# aimux bridge smoke test — drive a session on a remote bridge client through the
# mobius aimux service and log every command with its result.
#
#     AIMUX_SMOKE_REMOTE=young_garden bash smoke_bridge.sh
#
# Requires: the remote is registered as a bridge client (aimux reverse connect),
# and the local aimux can reach the broker. Needs a POSIX profile on the remote.
#
# 日志格式，每条命令一块，并标注它是在哪台机器上执行的：
#     --- 测试N（测试内容） ---
#
#     运行命令（机器：<机器>）：
#     <命令，变量全部展开>
#     返回结果：
#     <输出>
#
# 涉及两台机器：DRIVER（跑 aimux CLI 的一侧，这里是内网 mobius）和 REMOTE
# （bridge client，命令真正在其中执行的那台）。
set -u

AIMUX="${AIMUX:-$HOME/.local/bin/aimux}"
REMOTE="${AIMUX_SMOKE_REMOTE:-young_garden}"
RDIR="${AIMUX_SMOKE_REMOTE_DIR:-$HOME/test_server/test_aimux}"
SESSION="${AIMUX_SMOKE_SESSION:-aimux_bridge_basic}"
PROFILE="${AIMUX_SMOKE_PROFILE:-posix-pty}"
DRIVER="${AIMUX_SMOKE_DRIVER:-$(hostname)}"
LOG="$(cd "$(dirname "$0")" && pwd)/bridge_test.log"

pass=0
fail=0
group_no=0

group() { group_no=$((group_no + 1)); printf -- '--- 测试%d（%s） ---\n\n' "$group_no" "$1"; }

record() { # record <machine> <command-as-shown> <output>
  printf '运行命令（机器：%s）：\n%s\n返回结果：\n' "$1" "$2"
  if [ -n "$3" ]; then printf '%s\n' "$3"; fi
  printf '\n'
  pass=$((pass + $(printf '%s\n' "$3" | grep -c '^PASS')))
  fail=$((fail + $(printf '%s\n' "$3" | grep -c '^FAIL')))
}

step() { # step <command string>  → 在 DRIVER 上执行
  local out
  out="$(bash -c "$1" 2>&1)"
  record "$DRIVER" "$1" "$out"
}

remote_step() { # remote_step <remote command shown in log>  → 在 REMOTE 的会话里执行
  local shown="$1" out
  "$AIMUX" send-keys "$REMOTE/$SESSION" -- "$shown" Enter >/dev/null 2>&1
  sleep 3
  # 去掉远端提示符行和命令回显行，只留命令真正的输出
  out="$("$AIMUX" capture "$REMOTE/$SESSION" --lines 40 2>&1 | grep -vE '@|\$[[:space:]]*$' | sed '/./,$!d')"
  record "$REMOTE" "$shown" "$out"
}

{
printf 'aimux bridge smoke test — %s\n驱动端(DRIVER): %s   远端(REMOTE): %s:%s\n\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$DRIVER" "$REMOTE" "$RDIR"

group 'bridge client 注册状态'
step "$AIMUX remote ls | grep -E \"HOST|$REMOTE\""
step "$AIMUX remote ls | grep -q \"$REMOTE.*connected\" && echo \"PASS $REMOTE 已注册且 connected\" || echo \"FAIL $REMOTE 未连接\""

group 远端会话生命周期
step "$AIMUX kill $REMOTE/$SESSION >/dev/null 2>&1 || true"
step "$AIMUX new --remote $REMOTE --profile $PROFILE --name $SESSION --cwd $RDIR"
remote_step 'pwd; echo BRIDGE_OK_42'
step "$AIMUX capture $REMOTE/$SESSION --lines 40 | grep -q 'BRIDGE_OK_42' && echo \"PASS 命令在 $REMOTE 上跑出结果\" || echo \"FAIL 没拿到输出\""
step "$AIMUX capture $REMOTE/$SESSION --lines 40 | grep -q '$RDIR' && echo \"PASS 远端会话 cwd = $RDIR\" || echo \"FAIL cwd 不对\""
step "$AIMUX ls | grep -E 'LOCATION|$SESSION'"

group 日志回传与清理
step "$AIMUX send-files $REMOTE $RDIR $LOG"
step "$AIMUX kill $REMOTE/$SESSION"
step "$AIMUX ls | grep -q '$SESSION' && echo \"FAIL 会话还在\" || echo \"PASS 会话已销毁\""

printf '==== %d passed, %d failed ====\n' "$pass" "$fail"
} 2>&1 | tee "$LOG"

# 把日志本身回传到目标目录（走 bridge 的 file.write 通道）
"$AIMUX" send-files "$REMOTE" "$RDIR" "$LOG" >/dev/null 2>&1
