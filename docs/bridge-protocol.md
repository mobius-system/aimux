# aimux Bridge Server 兼容性规范 v0.1

任何语言实现的 server，只要满足本规范，即可与现有 Python 客户端（`aimux reverse connect`）和 CLI（`aimux send-keys / capture / send_files / get_files` 等）互通。

参考实现：`src/aimux/bridge_server.py`（Python，约 400 行，仅用标准库）。

---

## 1. 外部文件契约

### 1.1 runtime.json（**唯一**的 broker ↔ CLI 发现机制）

**路径**（按优先级解析）：
1. `$AIMUX_BRIDGE_RUNTIME`（绝对路径，绕过目录拼接）
2. `$AIMUX_RUNTIME_DIR/bridge/runtime.json`
3. `~/.aimux/bridge/runtime.json`（默认）

**JSON 字段**（必须完整提供，CLI 端用 `KeyError → 视为不存在`）：
```json
{
  "url": "http://127.0.0.1:8765",
  "token": "EFbVRh1AoaLUE3ugow4qtHl8KpeND0rV",
  "pid": 1342650,
  "host": "127.0.0.1",
  "port": 8765,
  "created_at": "2026-06-16T14:44:26Z"
}
```

- `url` — CLI 用来打 HTTP 的基址。**注意**：当 broker 绑定 `0.0.0.0` / `::` 时，写入 runtime.json 的 url 必须改成 `127.0.0.1`（参见 `bridge.py:119 control_url_for_bind`），否则 CLI 连不上。
- `token` — 共享密钥，所有 HTTP 请求都要带 `Authorization: Bearer <token>`。
- `pid` — broker 进程 PID，仅供调试，缺失可写 `null`。
- `host` / `port` — 实际绑定的（保留原值，不要替换成 127.0.0.1）。
- `created_at` — ISO 8601 UTC，可有可无。

**文件权限**：`0600`，父目录 `0700`。CLI 不强制校验，但建议遵循。

**写入策略**：**原子写**（写临时文件 → `rename`）。CLI 在 broker 启动期可能并发读到，原子 rename 保证不会读到半截 JSON。

**生命周期**：
- broker 启动 → 写入
- broker 正常退出 → 删除
- broker 崩溃 → 残留（CLI 会以为 broker 还活着，第一次请求失败时报 `bridge broker is unreachable`）

### 1.2 其他文件
- **不写其他文件**。客户端列表、session 表、pending 请求都在内存。允许持久化但不是契约一部分。

---

## 2. HTTP 端点规范

所有路径以 `/api/` 或 `/client/` 开头。所有请求需要 `Authorization: Bearer <token>` 头（包括 SSE）。

**常量**：
- 默认超时：session 操作 30s，file 操作 60s，read/write 大文件 180s
- JSON 编码：UTF-8，`ensure_ascii=false`（中文不转义）

### 2.1 健康与查询

#### `GET /api/health`
- 响应：`200 {"ok": true}`

#### `GET /api/remotes`
- 响应：
```json
{
  "remotes": [
    {
      "name": "winbox",
      "type": "bridge",
      "status": "connected",
      "platform": "windows",
      "default_profile": "cmd",
      "profiles": [
        {"name": "cmd", "visible": true, "capture": "screen-buffer", "ansi": false, "available": true}
      ],
      "last_seen": "2026-06-16T..."
    }
  ]
}
```

#### `GET /api/sessions`
- 响应：
```json
{
  "sessions": [
    {
      "id": "winbox/task-1",
      "location": "winbox",
      "name": "task-1",
      "backend": "bridge",
      "transport": "bridge",
      "host": "winbox",
      "profile": "cmd",
      "status": "attached",
      "client_status": "connected",
      "created_at": "...",
      "created_by": "fuqingxu",
      "cmd": "cmd.exe",
      "cwd": "C:\\Users\\dev",
      "pid": 1234
    }
  ]
}
```

### 2.2 客户端反向通道

#### `POST /client/register`
- 客户端启动时调用一次，且每次 SSE 断线重连前也应重新调用，宣告自己的身份。
- 请求：
```json
{
  "identifier": "winbox",
  "client_id": "550e8400-e29b-41d4-a716-446655440000",
  "replace": false,
  "platform": "windows",
  "default_profile": "cmd",
  "profiles": [
    {"name": "cmd", "visible": true, "capture": "screen-buffer", "ansi": false, "available": true}
  ]
}
```
- 响应：`200 {"ok": true}`
- `client_id` 是客户端进程级稳定 ID；同一进程重连时保持不变，server 用它区分“同进程网络重连”（保留 session 记录）和“客户端进程重启”（清理 stale session 记录）。旧客户端可不传，server 必须兼容。
- 冲突语义：
  - `replace=false` 且同名已连接 → `409 {"error": {"kind": "name_conflict", ...}}`
  - `replace=true` → 踢掉旧连接（向其 SSE 队列投 `null`）
  - identifier 与现有 SSH host 重名 → `409 {"kind": "name_conflict"}`（**注意**：CLI 端做这个检查，server 可不实现）
- identifier 规则（必须拒绝）：
  - 空、`local`、含 `/`
  - 字符集仅 `[a-zA-Z0-9_.-]`

#### `GET /client/events?identifier=<name>`（**SSE 长连接**）
- 响应头：`Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、`Connection: keep-alive`
- 流程：
  1. server 把 `identifier` 对应的客户端标记为 `connected`
  2. 写入心跳/事件直到连接断开
  3. 断开时把客户端标记为 `disconnected`，丢弃队列，并把该客户端名下所有 pending 请求标记为 `remote_disconnected`
- **事件帧**（标准 SSE）：
  ```
  id: <request-uuid>
  event: message
  data: {"id":"<request-uuid>","type":"session.create","payload":{...}}

  ```
  每帧之间空行分隔。`id:` 行的值必须和 `data` 里的 `id` 字段一致。
- **心跳**（每 15s 一次，无事件时）：
  ```
  : heartbeat

  ```
  以 `:` 开头是 SSE 注释行，客户端会忽略，但能保持 NAT/代理不掐连接。
- **断开信号**：server 主动向队列投 `null` 时，连接正常关闭（不发任何事件，直接 EOF）。
- **重要**：未先 `POST /client/register` 就来连 SSE，必须返回 `400 {"kind": "not_registered"}`。

#### `POST /client/result`
- 客户端处理完一个请求后，把结果回传给 server，server 唤醒等待的 CLI 调用。
- 请求：
```json
{
  "id": "<request-uuid>",
  "ok": true,
  "payload": { ... }
}
```
或失败：
```json
{
  "id": "<request-uuid>",
  "ok": false,
  "error": {
    "kind": "bad_profile",
    "message": "...",
    "code": 1
  }
}
```
- 响应：`200 {"ok": true}`（无论 id 是否匹配）

### 2.3 Session 操作（CLI → server → client → server → CLI）

#### `POST /api/sessions`（create）
- 请求：
```json
{
  "remote": "winbox",
  "name": "task-1",
  "profile": "cmd",
  "cmd": "cmd.exe",
  "cwd": "C:\\Users\\dev",
  "created_at": "2026-06-16T...",
  "created_by": "fuqingxu",
  "timeout": 30
}
```
- 响应：
```json
{
  "session": {
    "id": "winbox/task-1",
    "name": "task-1",
    "profile": "cmd",
    "status": "running",
    "pid": 1234,
    "...": "..."
  }
}
```
- 校验：
  - `remote/name/profile` 必填 → 否则 `400`
  - `name` 含 `/` → `400`
  - 客户端不在线 → `502 {"kind": "remote_disconnected", "code": 2}`
  - 客户端不支持该 profile → `400 {"kind": "bad_profile"}`
  - 同名 session 已存在 → `409 {"kind": "already_exists"}`

#### `POST /api/sessions/{remote}/{name}/send-keys`
- URL 编码：`remote` 和 `name` 都用 `urllib.parse.quote(safe="")` 编码（即所有非字母数字都 `%XX`）。Go 实现可用 `url.PathEscape` 但要注意它不编码 `$&+,/:;=?@`——必须用更严格的转义。
- 请求：
```json
{
  "keys": ["echo", "hello", "Enter"],
  "flags": ["-l", "-N", "3"],
  "timeout": 30
}
```
- 响应：`200 {"ok": true}`

#### `POST /api/sessions/{remote}/{name}/capture`
- 请求：`{"lines": 200, "ansi": false, "timeout": 30}`
- 响应：
```json
{
  "content": "last 200 lines...\n",
  "lines": 200,
  "ansi": false,
  "source": "pty-ring-buffer"
}
```

#### `POST /api/sessions/{remote}/{name}/kill`
- 请求：`{"timeout": 30}`（可选）
- 响应：`200 {"ok": true}`

### 2.4 File 操作（不绑定 session，只走 remote）

#### `POST /api/remotes/{remote}/files/{action}`
- `action ∈ {stat, read, write, mkdir, list}`
- URL 同样需要严格编码。

#### `action = "stat"`
- 请求：`{"path": "/remote/file"}`
- 响应：`{"exists": true, "type": "file", "size": 1234}`
- `type` ∈ `file | dir | symlink | other | none`

#### `action = "read"`
- 请求：`{"path": "..."}`
- 响应：`{"content_b64": "aGVsbG8=", "size": 5}`
- 文件不存在 → `404 {"kind": "not_found"}`
- 不是普通文件 → `400 {"kind": "bad_request"}`

#### `action = "write"`
- 请求：
```json
{
  "path": "/remote/out/file",
  "content_b64": "aGVsbG8=",
  "mode": 420,
  "max_size": 16777216
}
```
- 响应：`{"size": 5}`
- base64 非法 → `400 {"kind": "bad_request"}`
- 超过 max_size → `400 {"kind": "too_large"}`

#### `action = "mkdir"`
- 请求：`{"path": "..."}`
- 响应：`{"ok": true}`
- 语义：`mkdir -p`

#### `action = "list"`
- 请求：`{"path": "...", "recursive": true}`
- 响应：
```json
{
  "entries": [
    {"path": "/remote/dir/a.txt", "type": "file", "size": 5},
    {"path": "/remote/dir/sub", "type": "dir", "size": 0}
  ]
}
```

---

## 3. 错误响应模型

所有错误统一格式：
```json
{
  "error": {
    "kind": "<machine_readable>",
    "message": "<human readable>",
    "code": <int>
  }
}
```

**kind → HTTP status → exit_code 映射**（CLI 依赖这个）：

| kind | HTTP | exit_code | 含义 |
|---|---|---|---|
| `auth_required` | 401 | 4 | token 错 |
| `not_found` | 404 | 1 | session/remote/path 不存在 |
| `name_conflict` | 409 | 1 | identifier 或 session 名冲突 |
| `already_exists` | 409 | 1 | session 已存在 |
| `bad_profile` | 400 | 1 | 客户端不支持该 profile |
| `bad_request` | 400 | 1 | 参数错误 |
| `remote_disconnected` | 502 | 2 | 客户端掉线 |
| `timeout` | 504 | 124 | 客户端响应超时 |
| `too_large` | 400 | 1 | 文件超限 |
| `client_error` | 500 | 1 | 客户端崩了 |

`code` 字段会作为 CLI 的退出码透传，所以必须准确。

---

## 4. SSE 协议细节（实现难点）

### 4.1 客户端 → server 的事件解析
客户端用标准 SSE 解析（`bridge_client.py:535 _iter_sse`）：
- 以 `\n\n` 分帧
- `data:` 行前缀去掉，多行 data 用 `\n` 拼接
- 忽略 `event:` / `id:` 行（**注意**：客户端只看 data，不读 id 字段）
- 注释行（`:` 开头）忽略
- 空白行是分帧边界

### 4.2 server 关键约束
1. **每个客户端一个独立队列**（Go: channel；Node: EventEmitter/queue；Rust: mpsc）
2. **请求排队**：CLI 调用 → server 把请求塞进队列 → SSE 写一帧 → 等待 `result POST` 回来 → 唤醒 CLI 协程
3. **超时清理**：CLI 默认 30s 等不到 result → server 主动从 pending 字典里删除该项，避免内存泄漏
4. **断线处理**：SSE 断开时，必须把该客户端名下所有 pending 请求标记为失败（`remote_disconnected`），否则 CLI 永远卡到超时

### 4.3 并发模型
- HTTP server：多线程/多协程均可（Python 用 `ThreadingHTTPServer`）
- 每个 SSE 连接独占一个写协程
- 队列读写要加锁（或用 channel 这种天然并发安全结构）

---

## 5. 与 SSH 远程共存

CLI 用同一个 `aimux remote ls` 同时显示 SSH 和 bridge 远程。冲突时：
- bridge 客户端 register 时，server **应该**检查 identifier 是否已是 SSH host（读 `~/.ssh/config`），是则拒绝
- 但这是软约束，CLI 端有 fallback：发现 `remote` 同时存在于两边时，报错让用户改名

实现其他语言版本时，**可以**实现这个检查（读 SSH config 比较麻烦，可不实现）。

---

## 6. 启动行为

`aimux bridge deploy --port <p>` 等价于：
1. 生成 token（如 `secrets.token_urlsafe(24)`，长度约 32 字符）
2. 绑定 TCP socket
3. **原子写** runtime.json
4. 打印到 stdout：
   ```
   bridge listening on 127.0.0.1:8765
   connect client with:
     aimux reverse connect http://127.0.0.1:8765 --identifier <name> --token <token>
   ```
5. 进入 serve 循环
6. SIGTERM/SIGINT → 删除 runtime.json → 退出

---

## 7. 最小验证清单（兼容性自测）

按顺序跑通即认为兼容：

1. `bridge_server.serve()` 启动 → 写出合法 runtime.json
2. Python 客户端 `reverse connect` 能 register 成功，SSE 不掉线 30s+
3. `aimux remote ls` 看到客户端
4. `aimux new --remote X --profile Y --name Z` 创建 session，客户端收到 `session.create`
5. `aimux send-keys X/Z -- echo hi Enter` → `aimux capture X/Z --lines=50` 拿到 "hi"
6. `aimux send_files X /remote/path /local/file` → 字节精确
7. `aimux get_files X /local/dir /remote/path` → 字节精确
8. `aimux kill X/Z`
9. SIGTERM broker → runtime.json 被删除

---

## 8. 推荐实现栈参考

- **Go**：`net/http` + `crypto/rand` 生成 token；标准库够用
- **Node.js**：`http` + `eventstream` 库；注意背压
- **Rust**：`axum` + `tokio` + `tower-http`
- **C++**：`cpp-httplib`（轻量）或 `Crow`（更现代）
