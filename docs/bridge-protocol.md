# aimux Bridge Server 兼容性规范 v0.1

任何语言实现的 server，只要满足本规范，即可与现有 Python 客户端（`aimux reverse connect`）和 CLI（`aimux send-keys / capture / send-files / get-files` 等）互通。

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
- 响应：`200 {"ok": true, "version": "0.3.71"}`
- `version` = broker 自己跑的 aimux 版本。客户端（TUI/桌面端）在连接前读它，
  不一致就把本地 aimux 装成同一个版本（升/降都算）。老 broker 没有该字段 →
  客户端视为"版本未知"，什么都不做，绝不猜。

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
      "last_seen": "2026-06-16T...",
      "client_version": "0.1.36",
      "allow_http_ports": [8000, 8001],
      "server_version": "0.1.36",
      "version_mismatch": false
    }
  ]
}
```

- `allow_http_ports` — local ports this client permits other clients to reach
  (§2.6). Empty array means the client cannot be connected to at all. Show it
  in `remote ls`-style output: it is the whole security story for the tunnel.

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
      "created_by": "alice",
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
  ],
  "allow_http_ports": [8000, 8001]
}
```
- 响应：`200 {"ok": true}`
- `allow_http_ports`（可选，数组）：本客户端允许**其他客户端**连接的本机 loopback 端口。
  缺省 / 缺失 / 非法值一律视为**空**（谁都连不上）——白名单必须 fail closed。
  老 server 忽略该字段即可；老 client 不传该字段时，新 server 必须把它当作空集。
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

### 2.3 HTTP port forward

#### `ANY /api/forward`

该 endpoint 供 `aimux port forward` 的本地 HTTP 代理调用，同时承载普通
HTTP、SSE 和 WebSocket Upgrade。请求体和响应体必须流式转发，不得封装为
JSON/base64。

内部控制头：

- `X-Aimux-Target-Origin`: Bridge Server 网络视角下的目标 origin，例如
  `http://127.0.0.1:3000`。
- `X-Aimux-Request-Target`: 原始 origin-form path + query，例如
  `/api/items?q=1`。必须以 `/` 开头，禁止 CR/LF 和 fragment。
- `X-Aimux-Local-Origin`: 浏览器访问的本地 origin，用于重写 Location、
  Cookie Domain、CORS Origin。
- `X-Aimux-Upstream-Authorization`: 目标应用自己的 Authorization。Bridge
  完成自身 Bearer token 鉴权后，将其恢复为上游 Authorization。

Bridge 转发前必须删除所有 `X-Aimux-*`、Bridge `Authorization` 和
hop-by-hop headers。普通 HTTP 与 SSE 返回目标 status/headers/body；SSE
必须禁用 buffering 和总时长 timeout。带 `Upgrade: websocket` 的 GET 在同一
endpoint 完成目标握手后进入双向流转发。

### 2.4 Session 操作（CLI → server → client → server → CLI）

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
  "created_by": "alice",
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

### 2.5 File 操作（不绑定 session，只走 remote）

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

#### 流式数据面：`/client/data`（大文件不走上面这些）

上面 `read` / `write` 把整个文件放进一条 JSON 消息（base64），只适合小文件。超过阈值时走独立的裸字节通道，
由 **remote 自己发起到 broker**（反向连接的方向是 remote → broker，broker 无法主动连它）：

| 方向 | 发起方 | 端点 |
|---|---|---|
| remote → broker（下载） | remote `POST` | `POST /client/data?id=<request_id>` |
| broker → remote（上传） | remote `GET` | `GET /client/data?id=<request_id>` |

- body / 响应体是**裸字节**：没有 base64，也没有 JSON 包装。
- `request_id` 由 broker 在控制 SSE 的任务里下发（`file.open_read` / `file.open_write`），
  remote 拿到后自己拨号；bulk 数据因此不占用控制通道。
- **分片上传（v0.3.59 起）**：同一个 `id` 可以分多次 `POST`，用 `seq` 从 0 递增保序，
  最后一片带 `final=1` 才结束这条流。分片大小由 broker 在任务 payload 里用 `slice_size` 告知，
  remote 收到才分片；**老 remote 忽略该字段、只发一次 body，语义仍然合法**（收完即 EOF），
  因此两个方向都能混用新旧实现。
- broker 侧必须同时支持 `Transfer-Encoding: chunked` 与 `Content-Length`（中间代理会改写 framing）；
  两者都没有时按**空 body** 处理——**不能**读到连接关闭为止，keep-alive 连接没有 EOF，会一直挂到超时。
- 分片乱序到达时必须**拒绝**（而不是照样入队），否则文件会被静默拼错。

为什么要有分片：中间的反向代理（nginx 默认 `proxy_request_buffering on`）会把整个请求体收完才转发。
一个巨大的 POST 会让接收侧在整个上传期间一个字节都看不到——表现为"进度条出现前卡十几秒"，
而且文件越大卡得越久；分片同时还避开了代理默认的请求体大小上限。

### 2.6 客户端到客户端 HTTP 端口隧道

让**一个客户端**通过 broker 访问**另一个客户端的 loopback 端口**。

和 §2.3 的 `/api/forward` 的区别很关键：`/api/forward` 是 **broker 自己拨号**
（目标在 broker 的网络视角里可见）；隧道是**目标客户端自己拨号**，所以目标可
以藏在 NAT 后面——这正是 reverse connect 的意义。broker 不解析任何 HTTP。

隧道按**字节**转发，不做任何协议解析，因此普通 HTTP、流式 SSE、WebSocket
Upgrade 全都自动可用。

#### 建会话：`POST /api/http-ports/open`

```json
{"remote_client": "a-box", "port": 8000, "timeout": 15}
```

- 响应：`200 {"session": {"id": "<uuid>", "remote_client": "a-box", "port": 8000, "created_at": "..."}}`
- broker 必须**先**校验 `port ∈ remote_client.allow_http_ports`，不通过直接
  `403 {"kind": "port_not_allowed"}`，**不要**惊动目标客户端。
- 校验通过后，broker 在目标客户端的控制 SSE 上推
  `{"type": "http_port.open", "payload": {"port": 8000, "session_id": "<uuid>"}}`，
  并按 `timeout` 等它的 `POST /client/result`。
- 目标客户端必须**自己再校验一次**白名单（broker 可能被攻破），不通过回
  `port_not_allowed`；拨号失败回 `port_unreachable`。
- 目标客户端**只能拨 `127.0.0.1`**。协议里没有"目标主机"字段，别自作主张加。

#### 数据面：每条 TCP 连接一个会话，两侧各两条 HTTP 连接

方向按**载荷本身**命名，而不是按谁在问：

- `up` = 浏览器发出的字节（reroute 侧 → 目标）
- `down` = 目标回应的字节（目标 → reroute 侧）

| 方向 | 方法 | 端点 | 编码 |
|---|---|---|---|
| reroute 读 | `GET` | `/api/http-ports/stream?id=<sid>&direction=down` | SSE，`data:` 为 base64 JSON |
| reroute 写 | `POST` | `/api/http-ports/stream?id=<sid>&direction=up` | chunked 裸字节 |
| 目标 读 | `GET` | `/client/http-ports/stream?id=<sid>&direction=up` | SSE，`data:` 为 base64 JSON |
| 目标 写 | `POST` | `/client/http-ports/stream?id=<sid>&direction=down` | chunked 裸字节 |

两侧因此**读写方向相反**：reroute 侧读 `down`、写 `up`；目标侧读 `up`、写
`down`。`direction` 与端点族不匹配时必须 `400 bad_request`，绝不能返回一个
静默的空流。

**下行（SSE）帧格式**——`data:` 行是一个 JSON 对象：

```
data: {"b64": "aGVsbG8="}

data: {"eof": true}

data: {"error": {"kind": "remote_disconnected", "message": "...", "code": 2}}
```

- 二进制必须 base64（SSE 是文本协议）。上行用 POST body，本来就是二进制，
  **不要**再 base64，那是纯开销。
- `{"eof": true}` 表示对端这一半结束了。
- `{"error": ...}` 表示流因故障中断（如空闲超时），用 §3 的 kind。
- 响应头必须带 `X-Accel-Buffering: no`，否则反代会缓冲，流式当场失效。
- **空闲时每 15s 发一个注释帧 `: keepalive`**，否则 nginx 的
  `proxy_read_timeout` 之类的反代会掐掉一条只是暂时没数据的隧道。
- **流结束时必须给响应一个了断**：SSE 响应既没有 `Content-Length` 也不是
  chunked，唯一的终止方式就是关闭连接。结束前发完 `eof` / `error` 帧后，
  必须把连接关掉（Python 实现里是 `self.close_connection = True`）。
  只退出 handler 而不关闭，会让对端 reader 在一个永远不关的 socket 上挂死，
  同时泄漏一个 broker 线程、一个客户端 relay 线程和一个 fd。

**上行（POST）**：请求体用 `Transfer-Encoding: chunked`，内容就是裸字节。
请求体读完 = 该侧 EOF。单个 chunk 的声明长度不受信任，必须设上限（参考实现
用 1 MiB），否则一个伪造的超大长度会变成一次巨型读取。

#### 半关闭

不需要任何自定义关闭消息，TCP 的半关闭由 HTTP 的流结束语义承载：

- 某一侧的 **POST body 读完** = 该侧的 socket 收到 EOF
- broker 关掉对侧的 **SSE 并发出 `{"eof": true}`** = 把 EOF 传过去

两侧都结束时回收会话。broker 的转发队列**必须有界**：队列满时读线程阻塞，
停止读源 socket，从而把背压一路顶回最初的生产者。用无界队列会在快生产/慢
消费时把 broker 撑爆。

两个容易踩的坑，实现时务必避开：

1. **不要把 EOF 做成队列里的哨兵值**。哨兵要挤进一个已经满的队列，就只能丢
   掉一条真实数据——`32 x 64 KiB` 以上的传输会被静默截断，而且没有任何一方
   会察觉。EOF 用带外信号（事件 / 条件变量）表达。
2. **生产者阻塞在满队列上时，必须能被打断**。对端 reader 消失后，没人再来
   消费；若生产者只会永远阻塞，就永久泄漏一个 handler 线程加一个 socket。
   关闭会话时必须唤醒它（参考实现给每个方向一个 `abandoned` 事件）。

**空闲超时是按会话算的，不是按方向算的**：只单向流动的流（WebSocket 收多
发少、SSE 长连接）不能因为另一个方向安静就被掐掉。超时应在触发时给对方发
`{"error": {"kind": "timeout"}}` 并关闭连接。

#### 相关端点

- `GET /api/http-ports` → `{"sessions": [{"id", "remote_client", "port", "created_at", "streams_open"}]}`
- 目标客户端断线时，broker 必须关掉它名下所有隧道，好让 reroute 侧立刻察觉，
  而不是干等到空闲超时。

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
| `port_not_allowed` | 403 | 1 | 目标端口不在 `--allow-http-ports` 里（§2.6） |
| `port_unreachable` | 502 | 2 | 目标客户端拨不通自己的 `127.0.0.1:<port>`（§2.6） |

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
6. `aimux send-files X /remote/path /local/file` → 字节精确
7. `aimux get-files X /local/dir /remote/path` → 字节精确
8. `aimux kill X/Z`
9. SIGTERM broker → runtime.json 被删除

流式数据面（§2.5 末）另需跑通：

- 老 remote × 新 broker：单个不带 `seq` 的 `POST /client/data` → 字节精确，流在收完即结束
- 新 remote × 老 broker：broker 不下发 `slice_size` → remote 发单次 body → 字节精确
- 新 × 新：`slice_size` 分片上传 → 字节精确，且**首字节在几十毫秒内到达**（不是整个文件传完之后）
- 乱序 `seq` → 400，且流未被污染

端口隧道（§2.6）另需跑通：

10. 目标客户端带 `--allow-http-ports=<P>` 注册 → `/api/remotes` 里能看到 `allow_http_ports: [P]`
11. `POST /api/http-ports/open` 指向 `P` → 目标收到 `http_port.open` 并拨号成功
12. 四端点接通后，普通 HTTP 请求字节精确往返
13. **流式**：目标返回 `text/event-stream` 时，分块应**逐块**到达，而不是在响应结束时一次性吐出
14. WebSocket Upgrade 握手返回 `101` 且回显字节精确
15. 未在 `--allow-http-ports` 里的端口 → `403 port_not_allowed`，且目标**没有**收到任何事件
16. 端口允许但无服务监听 → `502 port_unreachable`
17. 一侧关闭连接 → 对侧收到 `{"eof": true}` 并在本地 socket 上表现为半关闭
18. 目标客户端断开 SSE → 其名下所有隧道被关闭

---

## 8. 推荐实现栈参考

- **Go**：`net/http` + `crypto/rand` 生成 token；标准库够用
- **Node.js**：`http` + `eventstream` 库；注意背压
- **Rust**：`axum` + `tokio` + `tower-http`
- **C++**：`cpp-httplib`（轻量）或 `Crow`（更现代）
