# aimux 设计文档

> 面向 AI Agent 的 tmux 包装器：单一本地 tmux server 统一管理本地与远程（SSH）会话；远程 = 本地 tmux session 内跑 `ssh`；**每个 session 只有一个 window、一个 pane**；session id 不可变；**执行命令的唯一入口是 send-keys**。

---

## 1. 项目概述

`aimux` 是构建在 `tmux` 之上的 Python CLI。极简模型：

1. **单一本地 tmux server**（专属 socket `aimux`）—— 不和远端 tmux 对话；"远程 session" 就是本地 session 内跑 `ssh <host>`
2. **元数据存在 tmux user options 里**（`@aimux-location`、`@aimux-host` 等）—— 没有 registry，没有状态同步
3. **一 session 一 window 一 pane** —— 不暴露 window/pane 管理；server 配置层解绑分屏快捷键
4. **session 不可变** —— 创建后名字不能改；执行命令只能用 `send-keys`，观察只能用 `capture`，没有任何"高层封装"
5. **少量命令足矣**

aimux 不替换 tmux，**代理** tmux：把"对 agent 友好"的命令映射成等价的 `tmux -L aimux ...` 调用。

---

## 2. 设计目标

| 目标 | 说明 |
|---|---|
| Agent-first | 输出可机读，命令幂等，错误码语义清晰 |
| 本地远程透明 | 同一套命令操作两类会话，区别只在 location 标签 |
| 极简模型 | 一 session 一 pane；id 就是 `<location>/<name>`，不可变 |
| 极简表面 | 会话命令保持稳定；新增 `bridge deploy` 与 `reverse connect` 只负责反向连接 |
| 最小持久化状态 | tmux session 元数据仍在 tmux user options；bridge 只写一个本机 runtime 文件用于发现 broker |
| 安全围栏 | 禁用 kill-server，独占 socket，解绑分裂/新建 window 的快捷键 |
| 极简实现 | Python + stdlib + typer；调用 `tmux` 子进程 |

非目标：
- 不做 tmux 完整功能镜像
- 不支持分屏、多 window、多 pane
- 不做命令执行的高层封装（无 `run`、无 prompt 检测、无超时管理）
- 不做 SSH 远端 agent 守护进程；bridge reverse client 是显式启动的前台进程
- 不做权限/多租户
- 不做配置系统、自检命令、审计日志读取

---

## 3. 架构

```
       user / agent
            │
            ▼
     ┌──────────────┐
     │  aimux CLI   │   Python (typer)
     └──────┬───────┘
            │ subprocess
            ▼
   ┌────────────────────┐     专属 socket /tmp/tmux-<uid>/aimux
   │  tmux -L aimux     │     与用户默认 tmux server 完全隔离
   │  + aimux.conf      │     启动配置：unbind 分屏/新窗口快捷键
   └─────────┬──────────┘
             │
   ┌─────────┴──────────┐
   ▼                    ▼
session "dev"      session "build"
 └─ pane: bash       └─ pane: ssh prod-api    ← "远程" session
                                                每个 session 恒为单 window/单 pane
```

要点：
- **一个 tmux server，一个 socket**（`-L aimux`），aimux 自己拉起、独占控制
- **远程 = 本地 session 里跑 ssh**，tmux 把按键透传给 pane 再透传给远端 shell
- **元数据 = tmux user options**，每个 session 创建时打上标签
- **结构不变量**：每个 session 恒等于 1 window × 1 pane；name 创建后不可变

### 3.1 为什么用专属 socket

1. **隔离** —— 用户日常的 `tmux` 命令不影响 aimux server，反之亦然
2. **kill-server 隔离** —— 即便有人在 session 内执行 `tmux kill-server`（不带 `-L`），杀的是默认 socket
3. **干净 ls** —— 用户日常 tmux session 不污染 `aimux ls` 输出
4. **配置独立** —— aimux 自有 `aimux.conf` 可以放心 unbind 分屏快捷键

### 3.2 单 window/pane 的执行细节

aimux 通过两层约束实现：

1. **CLI 层不暴露**：没有 `new-window` / `split-window` / `select-pane` / `list-panes` 等子命令
2. **配置层加固**：aimux server 启动时载入 `aimux.conf`：
   ```
   unbind-key c            # prefix c 新建 window
   unbind-key '"'          # prefix " 水平分屏
   unbind-key %            # prefix % 垂直分屏
   unbind-key !            # prefix ! break-pane
   ```

session 内只允许 attach、detach、滚动浏览、send-keys。需要新环境就开新 session。

### 3.3 远端如何访问

- SSH remote 通过 `~/.ssh/config` 中的 host 名访问（`--remote=<host>`）
- Bridge remote 通过 `aimux reverse connect --identifier=<name>` 注册到正在运行的 bridge broker，仍然用 `--remote=<name>` 创建 session
- SSH remote 不需要 `--profile`；bridge remote 必须显式指定 `--profile`
- bridge runtime remote 不写入 `~/.ssh/config`，但会出现在 `aimux remote ls/test` 中
- 强烈建议 SSH 用户在 ssh config 里配 `ControlMaster auto` + `ControlPersist 60s`

---

## 4. 核心概念

### 4.1 Session 标识

人读形式：`<location>/<name>`

```
local/dev
prod-api/build
staging-db/repl
```

- `location`：来自 tmux session 的 `@aimux-location` user option
  - `local` —— 本地 shell
  - `<host>` —— SSH 到该 host（同 ssh config 中的 host 名）
  - `<bridge-identifier>` —— bridge reverse client 注册名
- `name` —— tmux session 名，**创建后不可变**

CLI 引用：
- 全限定：`aimux capture prod-api/build --lines=200`
- 短名：`aimux capture build --lines=200`（仅当 name 全局唯一；不唯一时报错并列出候选）

**没有** `:window.pane` 后缀——一 session 一 pane，target 就是 session。

### 4.2 元数据：tmux user options

| Option | 含义 |
|---|---|
| `@aimux-location` | `local` 或 `<ssh-host-name>` |
| `@aimux-host` | 远端 host 名（local 时为空） |
| `@aimux-created-at` | ISO8601 时间戳 |
| `@aimux-created-by` | `$AIMUX_ACTOR` 或 `$USER` |
| `@aimux-output-sync-file` | 若启用了 `--output-sync-to-file`，记录绝对路径；否则为空 |

读取：

```bash
tmux -L aimux ls -F '#{session_name}|#{@aimux-location}|#{@aimux-host}|#{@aimux-created-at}'
```

外部用户用 `tmux -L aimux new ...` 创建的 session 没有这些 option，aimux 显示为 `local (unmanaged)`。

aimux 不暴露 `set-option` 子命令，因此这些元数据在 CLI 表面不可被修改。

### 4.3 SSH 与 bridge remote 来源

`~/.ssh/config` 是唯一来源。`--remote=<host>` 必须能被 `ssh -G <host>` 解析；否则 aimux 拒绝。

Bridge remote 来自当前 bridge broker 的 runtime registry。`aimux bridge deploy`
启动 broker 后会写入 `~/.aimux/bridge/runtime.json`（可用
`AIMUX_BRIDGE_RUNTIME` 覆盖），`aimux reverse connect` 成功连接后注册
`identifier`。Bridge identifier 不允许为 `local`，不允许包含 `/`，并且不能
和已有 SSH Host 同名。

---

## 5. 命令设计

核心会话命令保持不变；bridge 新增两个连接管理命令：

```
aimux ls                            列出会话
aimux new                           创建会话
aimux attach                        attach（仅限人类）
aimux send-keys                     发送按键（不修改用户输入）
aimux send-files                    用 sftp 批量上传本地文件/目录到 remote 目录
aimux get-files                     用 sftp 批量下载 remote 文件/目录到本地目录
aimux capture                       抓取 pane 内容
aimux kill                          销毁会话
aimux wait-last-command-complete    阻塞等待命令完成（仅本地，基于 pane 前台进程）

aimux remote ls                     列出 ssh-config 主机并探测可达性
aimux remote test                   探测单个 ssh-config 主机
aimux remote add                    探测后追加新主机到 ssh-config
aimux remote hardware               探测主机硬件（GPU 数量/型号、CPU 数量、内存大小）

aimux bridge deploy                 启动 SSE/HTTP bridge broker
aimux reverse connect               反向连接本机到 bridge broker
aimux local-http-port-reroute       把本机端口隧道到另一个 bridge client 的端口
```

存在性检查：直接用 `aimux ls --json` 解析，或 `aimux ls --json | jq -e '.[] | select(.id=="...")'`。aimux 不再单独提供 `has` 子命令——`ls` 已经覆盖此用途。

通用约定：
- 退出码：`0` 成功，`1` 用户错误，`2` SSH 配置不可解析，`3` 安全围栏拒绝
- `--json` JSON 输出
- `--quiet` 仅打印关键值
- `AIMUX_ACTOR` 环境变量写入 `@aimux-created-by`

### 5.1 `aimux ls`

```bash
aimux ls [--json] [--remote-only] [--local-only]
```

输出：

```
LOCATION    NAME    STATUS    CREATED
local       dev     attached  2026-04-25 10:00
local       work    detached  2026-04-25 11:20
prod-api    build   detached  2026-04-25 10:05
staging-db  repl    detached  2026-04-25 09:40
```

实现：

```bash
tmux -L aimux ls -F '#{session_name}|#{?session_attached,attached,detached}|#{@aimux-location}|#{@aimux-host}|#{@aimux-created-at}'
```

server 未启动 → 截获 "no server running"，输出空列表（不视为错误）。

JSON：

```json
[
  {"id":"local/dev","location":"local","host":null,"name":"dev","status":"attached","created_at":"2026-04-25T10:00:00Z","output_sync_file":null},
  {"id":"prod-api/build","location":"prod-api","host":"prod-api","name":"build","status":"detached","created_at":"2026-04-25T10:05:00Z","output_sync_file":"/var/log/aimux/build.log"}
]
```

### 5.2 `aimux new`

```bash
aimux new --name=<name> [--remote=<host>] [--cwd=<dir>] [--cmd=<initial-cmd>] \
          [--profile=<bridge-profile>] [--output-sync-to-file=<path>] [--reuse]
```

行为：
- 不带 `--remote` → 本地：`tmux -L aimux new-session -d -s <name> [-c <cwd>] [<cmd>]`
- 带 `--remote=<host>` 且 `<host>` 是 SSH Host：
  1. 校验 `<host>` 是 `~/.ssh/config` 中的具体 Host，并用 `ssh -G <host>` 校验配置可解析（不实际连接）
  2. 构造 ssh 启动命令（见 §7.3）
  3. `tmux -L aimux new-session -d -s <name> "<ssh-cmd>"`
  4. 设置 user options：`@aimux-location`、`@aimux-host`、`@aimux-created-at`、`@aimux-created-by`
- 带 `--remote=<identifier>` 且 `<identifier>` 是 bridge remote：
  1. 必须提供 `--profile`
  2. broker 校验 client 在线且支持该 profile
  3. broker 通过 SSE 下发 `session.create` 给 reverse client
  4. session id 仍是 `<identifier>/<name>`
- `--profile` 只允许用于 bridge remote；本地 session 和 SSH remote 传入 `--profile` 会被拒绝
- `--output-sync-to-file` 只支持 tmux-backed local/SSH session；bridge session 会被拒绝
- **始终 detach 创建**；不提供 `--attach`（attach 只为人类，见 §5.3）
- 同名已存在 → 退出码 1；`--reuse` 时返回 0

#### 5.2.1 `--output-sync-to-file=<path>`

把 pane 的所有输出**实时**追加到指定文件。基于 tmux 内置的 `pipe-pane`：

```bash
tmux -L aimux pipe-pane -t <name> 'cat >> <path>'
```

行为细节：
- `<path>` 支持 `~` 展开和相对路径（相对当前工作目录）；aimux 立刻将其解析为绝对路径再传给 tmux
- **追加模式**：用 `>>`，已有文件不会被截断；新文件不会自动建目录——父目录必须存在，否则退出码 1
- **写入是 raw 字节**：包含 ANSI escape、控制字符、PTY 原样输出。需要纯文本的 agent 自己 `sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' file` 后处理
- 适用本地与远程 session；远程时同步的也是本地 pane（即 ssh 收到的字节流），所以远端打印什么都能录下来
- session 销毁（`aimux kill` 或 pane 退出）时 pipe-pane 自动停止，文件不会被关闭丢内容（写完缓冲就停）
- 路径写入 user option `@aimux-output-sync-file`，`aimux ls --json` 会带出来便于 agent 查询

设置只在创建时生效；不提供运行时增删 sync 文件的命令——要换文件就 `kill` + `new`。这和"session 不可变"的整体原则一致。

冲突 / 安全：
- aimux 不检查目标路径是否安全（不防止覆盖系统文件等）；这是用户的责任
- 多个 session 写同一个文件 = 多路复用混合输出，aimux 不阻止但也不推荐

### 5.3 `aimux attach` ⚠️ 仅限人类使用

```bash
aimux attach <id> [--read-only]
```

实现：tmux-backed session 执行 `tmux -L aimux attach -t <name>`。bridge
session 没有本地 tmux pane，`attach` 直接拒绝，提示使用 `send-keys` +
`capture`。

**这条命令不应被 AI agent 调用**。理由：
- attach 进入交互式终端，会接管整个 stdin/stdout，agent 无法以非交互方式与之配合
- agent 操作 session 的标准做法是 `send-keys` + `capture`
- 调用了也不会出错，但 agent 会卡在交互式会话直到外部 detach

aimux 在 CLI help 文本中明确标注 "Human use only — agents should use send-keys + capture"。不做强制阻止（识别"调用方是不是 agent"不可靠），仅靠文档与命名约定。

### 5.4 `aimux send-keys` ★ 唯一执行入口

**完全等同于 tmux 的 `send-keys` 子命令**，aimux 只负责把 `<id>` 解析成 tmux 的 `-t <name>` 然后透传所有参数。语义、行为、key 名识别、行为细节与 `tmux send-keys` **逐字一致**。

```bash
aimux send-keys <id> [-F] [-H] [-K] [-l] [-M] [-R] [-X] [-N <count>] -- <keys...>
```

参数（与 `tmux send-keys` 同名同义）：

| 标志 | 含义 |
|---|---|
| `-F` | expand formats in arguments where appropriate |
| `-H` | expect each key to be a hexadecimal number for an ASCII character |
| `-K` | deliver the key directly to a client without going through any pane |
| `-l` | disable key name lookup, send keys literally |
| `-M` | pass through a mouse event |
| `-R` | reset the terminal state for the pane |
| `-X` | send a command into the copy mode window |
| `-N <count>` | repeat key count times |

aimux 自身**不引入任何额外标志**（无 `--enter`、无 `--literal` 长名、无 `--track` 等）；要发送 Enter 就和 tmux 一样把 `Enter` 作为 key 写在末尾：

```bash
aimux send-keys prod-api/build -- "make test" Enter
aimux send-keys local/dev    -- C-c
aimux send-keys local/dev    -- -l "$(echo literally)"
aimux send-keys local/dev    -- -N 3 Up                # 上方向键 ×3
```

实现：

```bash
tmux -L aimux send-keys [-FHKlMRX] [-N <count>] -t <name> <keys...>
```

send-keys 不会修改 `<keys>` 内容；命令是否完成由独立的 `wait-last-command-complete` 通过观察 pane 前台进程判断（见 §5.7），不需要在按键里植入任何 sentinel。

这是 **agent 把命令送进 session 的唯一方式**。执行（send-keys）/ 等待（wait-last-command-complete）/ 观察（capture）是三个解耦的原语（见 §6.2）。

### 5.5 `aimux capture` ★

```bash
aimux capture <id> --lines=<N> [--ansi] [--json]
```

- `--lines=<N>` **必填**，显示 pane 最后 N 行（含 scrollback）。强制要求传入是为了让 agent 显式声明上下文长度，避免默认行为下"只抓可见区"或"全量抓 scrollback"造成歧义
- `tmux -L aimux capture-pane -p -S -<N> -t <name>`
- `--ansi` → `-e` 保留 escape
- `N` 必须是正整数；缺省或 ≤0 → 退出码 1

这是 **agent 观察 session 输出的唯一方式**。

### 5.6 `aimux kill`

```bash
aimux kill <id>
```

`tmux -L aimux kill-session -t <name>`。单 pane 模型下无误伤多 window 的风险，无需 `--force`。

### 5.7 `aimux wait-last-command-complete` ★

```bash
aimux wait-last-command-complete <id> --timeout=<dur> [--print-lines=<N>] [--json] [--poll-interval=2s]
```

阻塞等待 `<id>` 的 pane 前台进程回到 shell。基于 tmux 的 `pane_current_command` 字段做轮询，**不修改用户发送的命令**、**不依赖任何 sentinel**、**不依赖 prompt 启发式**。

参数：
- `<id>`：会话 id
- `--timeout=<dur>` **必填**，格式 `<int><unit>`，unit ∈ `s` / `m` / `h`，例如 `30s`、`5m`、`2h`。强制必填是为了让 agent 显式承诺等多久，避免无界阻塞
- `--print-lines=<N>` 可选；wait 结束后追加一次 `capture --lines=<N>`，把 pane 最后 N 行写到 stdout。等价于 `wait + capture` 的合并调用。`<N>` 必须是正整数；省略时不打印任何 pane 内容
- `--poll-interval`：默认 `2s`

#### 5.7.1 检测原理

每个 poll 周期跑：

```bash
tmux -L aimux list-panes -F '#{pane_current_command}' -t <name>
```

判定：
- 若返回的命令名属于已知 shell 列表 → 命令已完成，pane 回到 shell 提示符
- 若是其他名字（如 `make` / `cargo` / `vim` / `python` / `pytest` / `top`）→ 命令仍在运行
- list-panes 返回非 0 → session 已不存在，提前返回

shell 名集合常量（来自参考实现 `tmp/wait_tmux.py`）：

```python
SHELLS = {"bash", "zsh", "sh", "fish", "csh", "tcsh", "ksh", "dash", "ash"}
```

#### 5.7.2 退出码与输出

退出码：
- `0` —— 命令已完成（pane 前台是 shell）
- `1` —— session 不存在
- `3` —— **session 是远程类型，不支持 wait**（见 §5.7.3）
- `124` —— 超时

注：本方法**不返回命令本身的 exit code**——`pane_current_command` 只能告诉我们"还在跑没"，不知道具体退出值。需要 exit code 的 agent 应在 send-keys 时自己拼接 `; echo __EXIT_$?__` 然后从输出解析。

**输出位置约定**：

| 模式 | stdout | stderr |
|---|---|---|
| 不带 `--print-lines` | `status: completed` / `elapsed: 45.3s` | （空） |
| 带 `--print-lines=<N>` | pane 最后 N 行（不含 ANSI escape） | `status: completed` / `elapsed: 45.3s` |
| `--json` 不带 print-lines | `{"completed": true, "elapsed_ms": 45321}` | （空） |
| `--json` 带 `--print-lines=<N>` | `{"completed": true, "elapsed_ms": 45321, "content": "...last N lines..."}` | （空） |

设计目的：带 `--print-lines` 时 stdout 是纯净的 pane 内容，agent 可以直接：

```bash
output=$(aimux wait-last-command-complete local/build --timeout=5m --print-lines=500)
```

而无需再单独跑 `aimux capture`。

**超时也会打印**：`--print-lines=<N>` 在退出码 `124`（超时）时同样会输出 pane 当前最后 N 行，方便 agent 看进度并决定是否再等。退出码 `1`（session 不存在）/ `3`（远程拒绝）则不打印 pane 内容（无来源）。

超时 JSON（带 `--print-lines`）：
```json
{"error":{"code":124,"kind":"timeout","message":"command did not complete within 5m","elapsed_ms":300000},"content":"...last N lines..."}
```

#### 5.7.3 不支持远程 session

远程 session 的本地 pane 前台进程恒为 `ssh`，整段连接期间都不会变成 shell 名，`pane_current_command` 看不到远端真实状态——会一直等到 timeout。

aimux 在 wait 启动时检查 `@aimux-location`：

- `local` → 正常运行
- 任意其他值（即远程） → 立即退出码 `3`，stderr：
  ```
  wait-last-command-complete only supports local sessions; for remote
  sessions, send-keys an explicit completion marker and poll capture
  ```

agent 在远程 session 上的标准做法（无 wait）：

```bash
aimux send-keys prod-api/build -- "make test; echo __DONE__" Enter
# 循环：
aimux capture prod-api/build --lines=200
# 直到看到 "__DONE__" 字样为止；超时由 agent 自己控制
```

#### 5.7.4 典型用法（本地 session）

```bash
# 标准三步：send / wait / capture
aimux send-keys local/dev -- "make test" Enter
aimux wait-last-command-complete local/dev --timeout=5m
aimux capture local/dev --lines=500
```

或者把 wait + capture 合成一步：

```bash
aimux send-keys local/dev -- "make test" Enter
aimux wait-last-command-complete local/dev --timeout=5m --print-lines=500
# stdout = pane 最后 500 行
# stderr = status: completed / elapsed: 45.3s
```

#### 5.7.5 已知限制

- 用户在 shell 里手动启动了一个**和 shell 同名**的子进程（如 `bash -c '...'`）会被误判为"已回到 shell"
- 命令运行极快（< poll-interval）时，wait 可能在第一次轮询前命令就结束了——结果仍正确（看到 shell 即返回 completed），但 elapsed 可能 ≈ 一次 poll 周期

### 5.8 `aimux remote *` — 远端主机管理

`remote` 命名空间用来探测和登记 SSH 主机。aimux 不维护自己的主机表，仅读写 `~/.ssh/config`。

#### 5.8.1 `aimux remote ls`

```bash
aimux remote ls [--json] [--timeout=5s]
```

枚举 `~/.ssh/config`（含 `Include` 引入的文件）中所有具体 `Host` 条目（跳过含 `*` / `?` 的通配模式），并行探测可达性；如果 bridge broker 正在运行，同时列出 bridge reverse clients。

输出：

```
HOST          TYPE     USER/PLATFORM  HOSTNAME/PROFILES          PORT   STATUS         RTT
prod-api      ssh      deploy         10.0.1.5                   22     reachable      45ms
winbox        bridge   windows        cmd,powershell,pwsh        -      connected      -
```

状态分类（探测命令：`ssh -o BatchMode=yes -o ConnectTimeout=<N> <host> true`）：

| status | 退出码语义 |
|---|---|
| `reachable` | exit 0 —— 网络可达且免密认证成功 |
| `auth-required` | exit 255 + stderr 含 "Permission denied" —— 网络可达但需要交互式认证 |
| `unreachable` | exit 255 + stderr 含 timeout/refused/no route —— 网络不可达 |
| `unknown` | 其他错误 |

JSON：

```json
[
  {"name":"prod-api","type":"ssh","user":"deploy","hostname":"10.0.1.5","port":22,"status":"reachable","rtt_ms":45},
  {"name":"winbox","type":"bridge","status":"connected","platform":"windows","profiles":[{"name":"cmd","available":true}]}
]
```

#### 5.8.2 `aimux remote test <name>`

```bash
aimux remote test <name> [--timeout=5s] [--json]
```

对单个 remote 做一次探测。SSH remote 执行 SSH probe；bridge remote 检查 reverse client 是否 connected。退出码：
- `0` —— `reachable`
- `1` —— host 名在 ssh config 中不存在
- `2` —— `unreachable`
- `4` —— `auth-required`（区别于 `2`，表示网络通但认证不通）

#### 5.8.3 `aimux remote hardware <name>`

```bash
aimux remote hardware <name> [--timeout=10s] [--json]
```

通过 SSH 在远端主机执行一个小脚本，收集 4 个值并返回：

| 字段 | 来源 |
|---|---|
| `gpu_count` | `nvidia-smi -L \| grep -c '^GPU '`；无 nvidia-smi 时为 `0` |
| `gpu_model` | `nvidia-smi --query-gpu=name --format=csv,noheader \| head -n1`；无则 `null` |
| `cpu_count` | `nproc`（逻辑核心数） |
| `mem_total_kb` | `/proc/meminfo` 中 `MemTotal` 字段（KB） |

文本输出：

```
HOST       GPU_COUNT  GPU_MODEL          CPU_COUNT  MEMORY
prod-api   8          NVIDIA A100 80GB   192        2015.5 GB
```

JSON：

```json
{"name":"prod-api","gpu_count":8,"gpu_model":"NVIDIA A100 80GB","cpu_count":192,"mem_total_kb":2113929216,"mem_total_gb":2015.5}
```

退出码：
- `0` —— 探测成功
- `1` —— host 名在 ssh config 中不存在
- `2` —— SSH 失败（不可达 / 认证失败 / 远端无 `sh`）

注意：远端必须能用 `sh` 执行脚本；GPU 部分依赖 `nvidia-smi`，不存在时 `gpu_count=0` 且 `gpu_model=null`。该命令只读不写（既不写本地 ssh config，也不修改远端任何状态）。

#### 5.8.4 `aimux remote add`

```bash
aimux remote add --host=<host-or-ip> --port=<port> --user=<user> [--name=<alias>] [--identity=<path>] [--timeout=5s]
```

行为：
1. 用临时参数（不写文件）做一次探测：`ssh -o BatchMode=yes -o ConnectTimeout=<N> -p <port> [-i <identity>] <user>@<host> true`
2. 若不可达 → 退出码 2；若需交互式认证 → 退出码 4；都不写 ssh config
3. 若 `reachable` → 在 `~/.ssh/config` **末尾追加**新块：
   ```
   # Added by aimux at <ISO8601>
   Host <name>
       HostName <host>
       Port <port>
       User <user>
       IdentityFile <identity>      # 仅当传了 --identity
   ```
4. `--name` 缺省时使用 `<host>` 字面量作为别名

幂等与冲突：
- `<name>` 已存在于 ssh config（包括 Include 文件）→ 退出码 1，提示已存在；不提供 `--force`，让用户自行编辑
- 写入前先备份 `~/.ssh/config` 到 `~/.ssh/config.aimux.bak.<timestamp>`
- 仅 **追加**，从不修改/删除已有内容，从不重写文件主体

安全注意：写 `~/.ssh/config` 是 aimux **唯一会写用户文件系统**的命令族；其他所有命令都只读 ssh config 或者操作 tmux server。

---

## 6. 安全机制

### 6.1 强制围栏（不可关闭）

| 行为 | 围栏 |
|---|---|
| `aimux kill-server` | aimux **不暴露**此子命令 |
| send-keys 内含 `tmux kill-server` | 不拦截，但因专属 socket，杀的不是 aimux server |
| `aimux kill <id>` | 一次只杀一个，不支持通配符/批量 |
| `prefix c / % / "` | aimux server 启动配置已 unbind |
| 远端断线 | pane 退出，session 保留为空；agent 应 `kill` 后重新 `new` |
| 没有 window/pane 子命令 | 从 CLI 表面就不可能创建多 window |
| 没有 `run` 三合一子命令 | 执行/等待/观察是三个解耦原语：`send-keys` / `wait-last-command-complete` / `capture`（见 §6.2） |
| 没有 `rename` 子命令 | session id 一旦创建即不可变（见 §6.3） |
| 没有 `respawn` 子命令 | session 不可"修复"，要么用，要么 kill 重建 |
| 没有 `set-option` 子命令 | `@aimux-*` 元数据在 CLI 表面不可改 |
| 没有 `config` / `doctor` 子命令 | aimux 不持有任何本地状态文件 |

### 6.2 解耦原语：send-keys / wait / capture

aimux 把执行命令拆成三个互相独立的原语：

| 原语 | 职责 | 何时用 |
|---|---|---|
| `send-keys` | 把按键送入 pane | 任何时候要"打字" |
| `wait-last-command-complete` | 阻塞等 pane 前台回到 shell | 需要同步语义时（仅本地 session） |
| `capture` | 读 pane 内容 | 任何时候要"看屏幕" |

**不提供** `aimux run` 这种"执行+等待+读取"三合一封装。理由：

- **职责单一**：每个原语做一件事，组合方式由 agent 决定。需要异步（fire-and-forget）就只 send-keys；需要同步（仅本地 session）就 send-keys + wait + capture
- **不修改用户命令**：`wait` 检测靠 tmux 的 `pane_current_command`（pane 前台进程名），send-keys 的内容**原样发送**，aimux 不会包装任何 sentinel、不会启发式匹配 prompt
- **代价透明**：基于 pane 前台的方法只能告诉"还在跑没"，**不返回命令 exit code**；远程 session 因本地前台恒为 `ssh`，wait 不支持——直接退出码 3 提示 agent

agent 不想用 / 不能用 `wait`（远程 session、需要 exit code、目标是 TUI 程序如 vim/less/top）的标准退路——自己在 send-keys 时拼接完成标记 + 轮询 capture：

```bash
aimux send-keys <id> -- "<cmd>; echo __DONE_$?__" Enter
# 然后循环 capture 找 __DONE_<exit>__
```

aimux 不强迫使用 wait。

### 6.3 不变性：session id 与元数据

`<location>/<name>` 创建后不可改：

- 不提供 `aimux rename`
- 不提供 `aimux set-option`
- agent 一旦记住一个 id，可以长期复用，不用担心被改名

session 也不能"修复"：远端断线 → pane 退出 → 用 `kill` + `new` 重建。这让 session 的生命周期成为单调的 `new → 用 → kill`，没有中间态修复路径，agent 推理负担最小。

### 6.4 不做的事

- 不解析 send-keys 内容做"危险命令"识别（误报率高、易绕过、给虚假安全感）
- 不替用户管理 SSH 凭据
- 不限制 send-keys 速率
- 不做远端权限边界（由 SSH 用户决定）
- 不做 prompt 检测、不做基于 prompt 的命令完成判定（`wait` 仅用 `pane_current_command`；见 §6.2）
- 不做配置系统、自检、审计日志（见 §6.1 表）

---

## 7. 实现建议

### 7.1 语言与框架

- **Python 3.10+**
- CLI：`typer`（基于 click，类型友好，自动 help）
- 子进程：`subprocess.run` / `Popen`
- 输出：表格用 `rich`（可选，没装就纯文本对齐）；JSON 用 stdlib

无配置文件读取，无 audit 写入，无运行时状态文件。

### 7.2 模块结构

```
aimux/
  pyproject.toml
  src/aimux/
    __init__.py
    __main__.py            # python -m aimux 入口
    cli.py                 # typer app，所有子命令
    tmux.py                # tmux 子进程封装
    session.py             # id 解析 (<location>/<name>)、user-options 读写
    ssh.py                 # ssh -G 校验、构造启动命令
    remote.py              # ssh-config 解析（含 Include）、并行探测、追加写入
    wait.py                # wait-last-command-complete 的 pane_current_command 轮询
    output.py              # 表格 / JSON / quiet
    aimux.conf             # 资源文件：unbind 分屏/新窗口快捷键
  tests/
  design.md
```

固定常量（不走配置文件）：

```python
SOCKET_NAME = "aimux"
AIMUX_CONF  = importlib.resources.files("aimux") / "aimux.conf"
```

### 7.3 关键实现点

**aimux.conf**（打包进 wheel，运行时通过 `tmux -L aimux -f <path>` 在 server 拉起时加载）：

```tmux
# 防止分裂
unbind-key c
unbind-key '"'
unbind-key %
unbind-key !
```

**server 启动**：第一次写命令前，aimux 检查 `tmux -L aimux ls`；若 "no server running"，下一次 `new-session` 调用会自动拉起 server。`-f` 参数确保 aimux.conf 在拉起时加载：

```bash
tmux -L aimux -f /path/to/aimux.conf new-session -d -s <name> ...
```

后续命令不需要再传 `-f`。

**ssh 命令构造**：

```python
def build_ssh_command(host: str, cwd: str | None, cmd: str | None) -> str:
    parts = ["ssh", "-t", shlex.quote(host)]
    remote = []
    if cwd:
        remote.append(f"cd {shlex.quote(cwd)}")
    if cmd:
        remote.append(cmd)
    else:
        remote.append("exec $SHELL -l")
    parts.append(shlex.quote(" && ".join(remote)))
    return " ".join(parts)
```

**user options 批量读取**：

```bash
tmux -L aimux ls -F '#{session_name}|#{?session_attached,1,0}|#{@aimux-location}|#{@aimux-host}|#{@aimux-created-at}|#{@aimux-output-sync-file}'
```

每行 split；空字段表示 unmanaged。

**output-sync-to-file 启用**：

```python
def enable_output_sync(session: str, raw_path: str):
    p = Path(raw_path).expanduser().resolve()
    if not p.parent.exists():
        sys.exit(1)  # parent dir missing
    cmd = f"cat >> {shlex.quote(str(p))}"
    subprocess.run(
        ["tmux", "-L", "aimux", "pipe-pane", "-t", session, cmd],
        check=True,
    )
    set_user_option(session, "@aimux-output-sync-file", str(p))
```

`pipe-pane` 不带 `-I/-O` 时默认 output → shell-command 方向，正是我们想要的。pane 销毁时 tmux 自动结束 pipe，无需 aimux 显式清理。

**id 解析**：

```python
def resolve_id(s: str, all_sessions: list[Session]) -> Session:
    if "/" in s:
        loc, name = s.split("/", 1)
        for x in all_sessions:
            if x.location == loc and x.name == name:
                return x
        raise NotFound(s)
    matches = [x for x in all_sessions if x.name == s]
    if len(matches) > 1: raise Ambiguous(matches)
    if not matches:      raise NotFound(s)
    return matches[0]
```

**wait-last-command-complete 轮询**（参考 `tmp/wait_tmux.py`）：

```python
SHELLS = {"bash", "zsh", "sh", "fish", "csh", "tcsh", "ksh", "dash", "ash"}

def wait_complete(session: str, timeout_s: float, poll_s: float = 2.0,
                  print_lines: int | None = None, json_out: bool = False):
    """
    Poll pane_current_command until it's a known shell name.
      exit 0   completed
      exit 1   session not found    (no pane content printed)
      exit 3   remote session       (no pane content printed)
      exit 124 timeout              (pane content still printed if print_lines)
    """
    if get_user_option(session, "@aimux-location") not in (None, "", "local"):
        sys.exit(3)

    start = time.monotonic()
    deadline = start + timeout_s
    completed = False
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["tmux", "-L", "aimux", "list-panes",
             "-F", "#{pane_current_command}", "-t", session],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            sys.exit(1)
        cmds = [l.strip().lower() for l in r.stdout.splitlines() if l.strip()]
        if cmds and all(c in SHELLS for c in cmds):
            completed = True
            break
        time.sleep(min(poll_s, max(0, deadline - time.monotonic())))

    elapsed_ms = int((time.monotonic() - start) * 1000)
    content = capture_pane(session, lines=print_lines) if print_lines else None
    emit_result(completed, elapsed_ms, content, json_out)   # writes stdout/stderr
    sys.exit(0 if completed else 124)
```

**timeout 字符串解析**：

```python
_DUR = re.compile(r"^(\d+)([smh])$")
def parse_duration(s: str) -> float:
    m = _DUR.match(s)
    if not m: raise ValueError(f"invalid duration {s!r}; use <int>s|m|h")
    n, u = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600}[u]
```

**ssh-config 主机枚举**：解析 `~/.ssh/config`（递归处理 `Include`），抽取所有 `Host` 行，跳过含 `*` / `?` / `!` 的通配。可参考 `paramiko.config.SSHConfig` 实现，但为零依赖建议手写一个轻量解析器。

**主机探测**：

```python
def probe_host(name: str, timeout: int) -> ProbeResult:
    start = time.monotonic()
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes",
               "-o", f"ConnectTimeout={timeout}",
               "-o", "StrictHostKeyChecking=accept-new",
               name, "true"],
        capture_output=True, text=True, timeout=timeout + 2,
    )
    rtt = int((time.monotonic() - start) * 1000)
    if r.returncode == 0:
        return ProbeResult("reachable", rtt)
    err = r.stderr.lower()
    if "permission denied" in err:
        return ProbeResult("auth-required", rtt)
    if any(x in err for x in ("timed out", "refused", "no route", "unreachable")):
        return ProbeResult("unreachable", None)
    return ProbeResult("unknown", None)
```

`remote ls` 用 `concurrent.futures.ThreadPoolExecutor(max_workers=16)` 并发跑这个函数。

**追加 ssh-config**：

```python
def append_host(name, host, port, user, identity=None):
    cfg = Path.home() / ".ssh" / "config"
    if name in existing_host_names(cfg):
        raise Conflict(f"host {name!r} already exists")
    backup = cfg.with_suffix(f".aimux.bak.{utcnow_compact()}")
    shutil.copy2(cfg, backup)
    block = textwrap.dedent(f"""
        # Added by aimux at {utcnow_iso()}
        Host {name}
            HostName {host}
            Port {port}
            User {user}
        """).rstrip() + "\n"
    if identity:
        block = block.rstrip() + f"\n    IdentityFile {identity}\n"
    with cfg.open("a") as f:
        f.write("\n" + block)
```

### 7.4 错误对象（`--json`）

```json
{"error":{"code":3,"kind":"safety_denied","message":"kill-server is disabled by aimux"}}
```

---

## 8. 使用示例

### 8.1a 本地 session：用 wait（标准三原语）

```bash
# 1. 创建本地 session
aimux new --name=build --cwd=/srv/app

# 2. 发送命令（不被 aimux 改写）
aimux send-keys local/build -- "make test" Enter

# 3. 阻塞等 pane 前台回到 shell
aimux wait-last-command-complete local/build --timeout=5m
# stdout: status: completed  elapsed: 45.3s

# 4. 取输出
aimux capture local/build --lines=500

# 5. 完事销毁
aimux kill local/build
```

### 8.1b 远程 session：手动 sentinel + 轮询（wait 不支持远程）

```bash
aimux new --remote=prod-api --name=build --cwd=/srv/app

# 自带完成标记
aimux send-keys prod-api/build -- "make test; echo __DONE__" Enter

# agent 自己循环 capture，看到 "__DONE__" 即完成；超时由 agent 自己控
aimux capture prod-api/build --lines=500
# ... agent 决策中 ...
aimux capture prod-api/build --lines=500

aimux kill prod-api/build
```

### 8.1d 实时录制 pane 输出到文件

```bash
mkdir -p /var/log/aimux
aimux new --name=build --cwd=/srv/app --output-sync-to-file=/var/log/aimux/build.log
aimux send-keys local/build -- "make test 2>&1" Enter

# 另一个终端实时跟随
tail -f /var/log/aimux/build.log

# session 销毁后日志依然完整保留
aimux kill local/build
ls -lh /var/log/aimux/build.log
```

### 8.1c 不用 wait（fire-and-forget 或 TUI 程序）

```bash
aimux send-keys local/dev -- "tail -f /var/log/app.log" Enter
aimux capture local/dev --lines=100
```

### 8.2 列出全部会话

```bash
$ aimux ls --json
[
  {"id":"local/dev","location":"local","host":null,"name":"dev","status":"attached","created_at":"2026-04-25T10:00:00Z"},
  {"id":"prod-api/build","location":"prod-api","host":"prod-api","name":"build","status":"detached","created_at":"2026-04-25T10:05:00Z"}
]
```

### 8.3 远端断线后重建

```bash
$ aimux capture prod-api/build --lines=50
... Connection to prod-api closed.
[process exited]

# 不能 respawn —— 直接 kill 重建
$ aimux kill prod-api/build
$ aimux new --remote=prod-api --name=build --cwd=/srv/app
```

### 8.4 误操作演示

```bash
$ aimux kill-server
error: unknown command "kill-server" — aimux does not allow killing the tmux server
exit 1

$ aimux rename local/dev --to=mydev
error: unknown command "rename" — aimux session ids are immutable
exit 1
```

attach 后按 `prefix c`：什么都不会发生（已 unbind）。

### 8.5 用户在 aimux 之外手动创建的 session

```bash
$ tmux -L aimux new -d -s scratch
$ aimux ls
LOCATION              NAME     STATUS    CREATED
local (unmanaged)     scratch  detached  -
local                 dev      attached  2026-04-25 10:00
```

### 8.6 人类 attach（agent 不应使用）

```bash
$ aimux attach prod-api/build         # 进入交互式 ssh 会话
... [在远端 shell 中操作] ...
# 按 prefix d 退出回到本地
```

### 8.7 Agent 发现并登记新远端主机

```bash
# 1. 看看现有哪些 host 可用
$ aimux remote ls --json
[
  {"name":"prod-api","status":"reachable","rtt_ms":45,...},
  {"name":"old-server","status":"unreachable",...}
]

# 2. 探测一个具体 host（比如怀疑某台机器掉线）
$ aimux remote test staging-db
staging-db  admin  db.staging.example  22  reachable  120ms
exit 0

# 3. 加一台新机器：先探测，可达再写入 ssh config
$ aimux remote add --host=10.0.2.7 --port=22 --user=deploy --name=worker-2
worker-2  reachable  rtt=33ms
appended to ~/.ssh/config (backup: ~/.ssh/config.aimux.bak.20260425T103312Z)

# 4. 立刻就能用
$ aimux new --remote=worker-2 --name=task-1
```

---

## 9. 命令一览（最终）

**会话操作**

| 命令 | 作用 | 可被 agent 调用 |
|---|---|---|
| `aimux ls` | 列出所有 session | ✅ |
| `aimux new --name=<n> [--remote=<name>] [--profile=<p>] [--output-sync-to-file=<path>]` | 创建 session：不带 `--remote` 是本地，SSH remote 保持旧 API；bridge remote 必须带 `--profile`；`--output-sync-to-file` 只支持 tmux-backed session | ✅ |
| `aimux attach <id>` | attach 到 tmux-backed session；bridge session 直接拒绝 | ❌ **仅限人类** |
| `aimux send-keys <id> [tmux-flags] -- <keys...>` | 发送按键（**执行入口**）；行为与 `tmux send-keys` 完全一致，支持 `-F/-H/-K/-l/-M/-R/-X/-N` | ✅ |
| `aimux send-files <remote> <remote-dir> <local>... [--gitignore]` | 用一次 sftp batch 上传多个本地文件或目录到 ssh-config host 的远端目录；目录会放在远端目录下自己的 basename 中；**传输前后各打印一次两侧的绝对路径** | ✅ |
| `aimux get-files <remote> <local-dir> <remote-path>...` | 用一次 sftp batch 下载多个远端文件或目录到本地目录；下载目标名来自远端路径 basename；**传输前后各打印一次两侧的绝对路径** | ✅ |
| `aimux capture <id> --lines=<N>` | 抓取 pane 最后 N 行（必填） | ✅ |
| `aimux kill <id>` | 销毁 session（**一次只允许 kill 一个**，不支持通配符/批量） | ✅ |
| `aimux wait-last-command-complete <id> --timeout=<dur> [--print-lines=<N>]` | 阻塞等待命令完成（基于 `pane_current_command`，**仅本地 session**）；`--print-lines` 完成后顺带打印最后 N 行 | ✅ |

**远端主机管理**（读写 `~/.ssh/config`）

| 命令 | 作用 | 可被 agent 调用 |
|---|---|---|
| `aimux remote ls` | 枚举 ssh-config host 并列出 bridge reverse clients | ✅ |
| `aimux remote test <name>` | 探测单个 SSH 或 bridge remote | ✅ |
| `aimux remote hardware <name>` | 探测主机硬件（GPU 数量/型号、CPU 数量、内存大小） | ✅ |
| `aimux remote add --host=<h> --port=<p> --user=<u> [--name=<n>]` | 探测通过后将新 host 追加到 `~/.ssh/config` | ✅ |

**Bridge 连接管理**

| 命令 | 作用 | 可被 agent 调用 |
|---|---|---|
| `aimux bridge deploy --port=<p>` | 在服务器侧启动 HTTP/SSE broker，监听 reverse client | ✅ |
| `aimux reverse connect <url> --identifier=<name> --token=<token> [--allow-http-ports=<p,...>]` | 在 client 侧反向连接 broker，并注册 bridge remote；`--allow-http-ports` 声明本机哪些端口允许被其他 client 连接（缺省为空 = 谁都连不上） | ✅ |
| `aimux local-http-port-reroute --local-port=<p> --remote-client-id=<name> --remote-client-http-port=<q> [--share-to-non-localhost\|--local-host=<addr>]` | 本地监听 `<p>`，字节级隧道到 `<name>` 的 `127.0.0.1:<q>`；HTTP / SSE / WebSocket 全部透传。默认只绑 `127.0.0.1`（`--share-to-non-localhost` 绑 `0.0.0.0` 供同网段共用，该监听无鉴权） | ✅ |

被有意删除的命令（不暴露）：`run`、`rename`、`respawn`、`has`、`new-window`、`list-windows`、`select-window`、`rename-window`、`kill-window`、`split-window`、`list-panes`、`select-pane`、`kill-pane`、`kill-server`、`hosts`（被 `remote ls` 取代）、`prune`、`set-option`、`history`、`log`、`config`、`doctor`。

`has` 删除理由：`ls --json` 已可被 agent 解析判断存在性，单独的 `has` 是冗余 API。

---

## 10. 设计取舍备查

aimux 的最终形态来自一系列简化决策：

| 维度 | 早期想法 | 最终决定 | 理由 |
|---|---|---|---|
| 远端模型 | 双 tmux server，aimux 双向对话 | 单一本地 server，远端=ssh pane | 删除远端探测、stale、unreachable 处理 |
| 元数据 | `~/.aimux/registry.json` | tmux user options | 删除状态同步问题 |
| 多 window | 支持 | 不支持 | 删除 window/pane 子命令族 |
| 分屏 | 支持 | server 配置层 unbind | 删除 split 相关风险 |
| 命令执行 | `aimux run` 同步封装 | 仅 `send-keys` | 完成判定权交回给 agent |
| 命名 | `aimux rename` 可变 | 不可变 | id 永远稳定 |
| 修复 | `aimux respawn` 重连 | 不支持，kill 重建 | session 生命周期单调 |
| 配置 | TOML 配置 + `aimux config` | 全部硬编码 | 删除配置系统 |
| 审计 | JSONL 日志 + `aimux history` | 不写日志 | 没人读的状态不写 |
| 自检 | `aimux doctor` | 删除 | 错误自然暴露即可 |
| attach | 通用命令 | 标注"仅限人类" | agent 应只用 send-keys + capture |

最终：**7 个会话子命令 + 2 个文件传输命令 + 4 个 remote 子命令、0 个 aimux 自有状态文件、1 个 tmux socket**。`remote add` 会写 `~/.ssh/config`（且仅追加），那是用户的文件，不是 aimux 的状态。命令完成检测靠 tmux 自带的 `pane_current_command`，不引入任何持久化文件。

---

## 11. 开放问题

- **多 agent 并发**：tmux server 自身串行处理命令，无并发问题
- **远端长任务断网**：ssh 断了 pane 退；agent 看到 `[process exited]` 后 `kill` + `new` 重建，但任务进度会丢。如确实需要续跑，让 agent 在 send-keys 时自己用 `nohup` / `screen` 包裹命令
- **TUI 监控**（可选未来扩展）：`aimux top`，实时看所有 session 最新几行（agent 行为可视化）


## Computer use (GUI helper) — 0.3.64

**Shape:** the native helper is a single Rust binary speaking JSON lines on
stdin/stdout (protocol 4, upstream pi-computer-use windows-bridge). The
reverse client owns its whole lifecycle.

- **Install:** CDN manifest (`helpers.windows-x64`) -> download -> sha256
  verify -> atomic rename under `~/.aimux/helpers/`, with a `.meta.json`
  sidecar recording the installed digest. `AIMUX_GUI_HELPER_PATH` bypasses the
  CDN for dev/debug (and the platform gate, so mocks run anywhere).
- **Gate:** `aimux reverse connect --enable-gui`. Install/spawn/probe failures
  NEVER block the connection; the registration simply carries
  `gui: {available: false, error}` instead.
- **Transport:** one new bridge message, `gui.request` `{cmd, args}` ->
  `result` of the helper response. Requests are serialized client-side; the
  broker routes it like any other per-remote command (`POST /api/gui/<remote>`,
  timeout 1-180s, clamped). Old brokers/clients ignore the extra fields, so
  mixed versions interoperate.
- **MCP:** `aimux mcp serve --toolset classic|classic-and-gui`. The default
  `classic` toolset is byte-identical to the pre-GUI server. The gui toolset
  adds seven `remote_gui_*` tools; serving it against a GUI-less remote exits
  with the reason. `remote_gui_act` defaults to `policy=ax_only` (semantic UIA
  patterns only); raw input needs an explicit `policy=foreground`.
- **Visibility:** `aimux remote ls` gains a `GUI` column (`on`/`off`/`-` for
  clients that never spoke about GUI).

## Remote self-description (`remote_ping`) — 0.3.73

**Problem:** an agent driving a bridge remote sees a black box. It cannot tell
whether its commands land on Windows or Linux, which shell runs them, or which
directory they start in — and path syntax, quoting, and which tools exist all
follow from those three answers. The workaround was to probe with `hostname` /
`uname` on every device switch, which is a round trip the agent has to know to
spend, and which fails outright when the command that would answer it is itself
platform-specific.

- **Shape:** one new optional bridge message, `sys.info` `{}` -> the client's
  own report, routed like any other per-remote command
  (`POST /api/remotes/<remote>/system-info`, timeout clamped to 1-60s). The
  report is gathered on demand rather than on every reconnect: it is bulky
  (shell table, env, locale) and the registration payload is sent on every SSE
  re-attach.
- **Content:** OS identity and architecture, the full shell table with the
  command each profile actually starts, `home`/`cwd`, `path_style`, output
  encoding, python and aimux versions, GUI capability. A whitelist in the MCP
  layer decides what reaches the model, so the client can grow fields without
  spending agent context on them.
- **MCP:** `remote_ping` answers with the broker's health *and* the remote's
  description, plus a one-line `summary` the model can act on, and a
  `session` block for what the server itself will do (bound profile, shell
  family and syntax, effective workdir and where it came from). It stays a
  ping: one call answers "is the link alive" and "what is on the other end".
- **Compatibility:** best-effort in both directions. A client that predates
  `sys.info` answers `bad_request`; an old broker has no such route. Either
  way the ping degrades to the registry row (`platform`, `profiles`,
  `client_version`) and says so in `notes` + `live: false` rather than failing
  the call — an agent asking where it is must never be answered with an error
  it cannot act on.
