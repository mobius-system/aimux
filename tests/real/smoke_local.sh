#!/usr/bin/env bash
# aimux local smoke test — run it on any machine right after installing aimux:
#
#     pip install aimux && bash smoke_local.sh
#
# Covers the paths that break silently: the session lifecycle (new -> send-keys
# -> wait-last-command-complete -> capture -> ls -> kill), --cwd actually taking
# effect, local apply-patch, and the help output shape. It needs tmux and writes
# its artifacts (help.txt, ls.json, sample.txt, basic_test.log) next to itself.
set -u
AIMUX="${AIMUX:-$HOME/.local/bin/aimux}"
DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION=aimux_basic_$$
pass=0
fail=0

say() { printf '\n### %s\n' "$*"; }
check() { # check <exit-code> <label>
  if [ "$1" = 0 ]; then echo "PASS  $2"; pass=$((pass + 1));
  else echo "FAIL  $2"; fail=$((fail + 1)); fi
}

cd "$DIR" || exit 1
echo "aimux  : $AIMUX ($("$AIMUX" -h 2>&1 | sed -n 's/^ aimux — //p' | head -1))"
echo "python : $(python3 -c 'import aimux;print(aimux.__version__)')"
echo "dir    : $DIR"
echo "host   : $(hostname)  tmux: $(tmux -V)"

say "1) 帮助输出（分面板 + 每命令都有例子）"
"$AIMUX" -h > help.txt 2>&1; check $? "aimux -h"
grep -q "Device Operation Commands" help.txt; check $? "顶层帮助是分面板的"
"$AIMUX" get-files -h 2>&1 | grep -q "Examples:"; check $? "子命令帮助带 Examples"

say "2) 本地 session 生命周期"
"$AIMUX" kill "$SESSION" >/dev/null 2>&1 || true
"$AIMUX" new --name "$SESSION" --cwd "$DIR"; check $? "new --name --cwd"

"$AIMUX" send-keys "$SESSION" -- 'pwd; echo AIMUX_OK_$((6*7))' Enter; check $? "send-keys"
"$AIMUX" wait-last-command-complete "$SESSION" --timeout 20s; check $? "wait-last-command-complete"

out="$("$AIMUX" capture "$SESSION" --lines 40)"
printf '%s\n' "$out" | tail -6
printf '%s\n' "$out" | grep -q "AIMUX_OK_42"; check $? "capture 里有命令输出"
printf '%s\n' "$out" | grep -q "$DIR"; check $? "--cwd 生效（pwd 输出目标目录）"

"$AIMUX" ls | grep -q "$SESSION"; check $? "ls 能列出该 session"
"$AIMUX" ls --json > ls.json 2>&1; check $? "ls --json"

say "3) apply-patch 本地改写"
printf 'alpha\nbeta\n' > sample.txt
printf '*** Begin Patch\n*** Update File: %s/sample.txt\n@@\n-beta\n+gamma\n*** End Patch\n' "$DIR" \
  | "$AIMUX" apply-patch; check $? "apply-patch（本地，无 --remote）"
grep -q '^gamma$' sample.txt; check $? "补丁落到文件"
cat sample.txt

say "4) 一个长一点的命令（多轮输出）"
"$AIMUX" send-keys "$SESSION" -- 'for i in 1 2 3; do echo "tick $i"; sleep 1; done' Enter
"$AIMUX" wait-last-command-complete "$SESSION" --timeout 30s --print-lines 5; check $? "wait + --print-lines"

say "5) 清理"
"$AIMUX" kill "$SESSION"; check $? "kill"
"$AIMUX" ls | grep -q "$SESSION" && { echo "FAIL  session 仍在列表里"; fail=$((fail + 1)); } \
  || { echo "PASS  session 已消失"; pass=$((pass + 1)); }

printf '\n==== %d passed, %d failed ====\n' "$pass" "$fail"
[ "$fail" = 0 ]
