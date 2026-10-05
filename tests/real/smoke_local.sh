#!/usr/bin/env bash
# aimux local smoke test — run it on any machine right after installing aimux:
#
#     pip install aimux && bash smoke_local.sh
#
# Covers the paths that break silently: the session lifecycle (new -> send-keys
# -> wait-last-command-complete -> capture -> ls -> kill), --cwd actually taking
# effect, local apply-patch, and the help output shape. Needs tmux.
#
# Log format, one block per command:
#     --- 测试N ---
#     运行命令（机器：xxx）：
#     <command, with $AIMUX/$DIR/$SESSION expanded to their real values>
#     返回结果：
#     <output>
#
# A check reports itself: the command prints PASS ... or FAIL ... as its result,
# and the closing line counts those.
set -u

AIMUX="${AIMUX:-$HOME/.local/bin/aimux}"
# 这台机器的名字（默认取 hostname，可用 AIMUX_SMOKE_MACHINE 覆盖成你习惯的别名）
MACHINE="${AIMUX_SMOKE_MACHINE:-$(hostname)}"
DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION="aimux_basic_$$"

pass=0
fail=0
group_no=0

cd "$DIR" || exit 1

group() { # group  → a new '--- 测试N ---' section
  group_no=$((group_no + 1))
  printf -- '--- 测试%d ---\n\n' "$group_no"
}

expand() { # expand <command string>  → substitute this script's own variables
  local s="$1"
  s="${s//\$AIMUX/$AIMUX}"
  s="${s//\$DIR/$DIR}"
  s="${s//\$SESSION/$SESSION}"
  printf '%s' "$s"
}

step() { # step <command string>  → print it expanded, run it, record the result
  local cmd out
  cmd="$(expand "$1")"
  printf '运行命令（机器：%s）：\n%s\n返回结果：\n' "$MACHINE" "$cmd"
  out="$(bash -c "$cmd" 2>&1)"
  if [ -n "$out" ]; then printf '%s\n' "$out"; fi
  printf '\n'
  pass=$((pass + $(printf '%s\n' "$out" | grep -c '^PASS')))
  fail=$((fail + $(printf '%s\n' "$out" | grep -c '^FAIL')))
}

printf 'aimux smoke test — %s\n主机: %s   目录: %s\n\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(hostname)" "$DIR"

group
step 'python3 -c "import aimux; print(\"aimux version:\", aimux.__version__)"'
step 'tmux -V'
step '$AIMUX -h'
step '$AIMUX -h | grep -q "File Read/Edit Commands" && echo "PASS 顶层帮助按面板分组" || echo "FAIL 顶层帮助没有面板分组"'
step '$AIMUX get-files -h | grep -q "Examples:" && echo "PASS 子命令帮助带 Examples" || echo "FAIL 子命令帮助没有 Examples"'

group
step '$AIMUX kill $SESSION >/dev/null 2>&1 || true'
step '$AIMUX new --name $SESSION --cwd $DIR'
step '$AIMUX send-keys $SESSION -- '"'"'pwd; echo AIMUX_OK_42'"'"' Enter'
step '$AIMUX wait-last-command-complete $SESSION --timeout 20s'
step '$AIMUX capture $SESSION --lines 40'
step '$AIMUX capture $SESSION --lines 40 | grep -q "AIMUX_OK_42" && echo "PASS 命令输出出现在 capture 里" || echo "FAIL 没看到命令输出"'
step '$AIMUX capture $SESSION --lines 40 | grep -q "$DIR" && echo "PASS --cwd 生效（pwd 就是目标目录）" || echo "FAIL --cwd 没生效"'
step '$AIMUX ls'
step '$AIMUX ls --json'
step '$AIMUX ls | grep -q "$SESSION" && echo "PASS ls 列出了该 session" || echo "FAIL ls 里没有该 session"'

group
step 'printf "alpha\nbeta\n" > sample.txt; cat sample.txt'
step 'printf "*** Begin Patch\n*** Update File: $DIR/sample.txt\n@@\n-beta\n+gamma\n*** End Patch\n" | $AIMUX apply-patch'
step 'cat sample.txt'
step 'grep -qx gamma sample.txt && echo "PASS 补丁已落到文件" || echo "FAIL 文件没被改"'

group
step '$AIMUX send-keys $SESSION -- '"'"'echo tick 1; sleep 1; echo tick 2; sleep 1; echo tick 3'"'"' Enter'
step '$AIMUX wait-last-command-complete $SESSION --timeout 30s --print-lines 5'
step '$AIMUX capture $SESSION --lines 40 | grep -q "tick 3" && echo "PASS 多轮输出完整（tick 3 在）" || echo "FAIL 输出不完整"'

group
step '$AIMUX kill $SESSION'
step '$AIMUX ls'
step '$AIMUX ls | grep -q "$SESSION" && echo "FAIL session 还在列表里" || echo "PASS session 已消失"'

printf '==== %d passed, %d failed ====\n' "$pass" "$fail"
[ "$fail" = 0 ]
