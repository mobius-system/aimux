---
name: mobius-gui-operation
description: AIMUX操作远程设备的图形界面。两种方法：① 模拟人类（只回截图 + 模型在图上定位 + 坐标注入真实键鼠，万能通用但慢）② 图形转文本结构（走无障碍元素树，快但不可靠）。当需要操作远程设备的桌面软件（IM、浏览器、办公软件、Qt/Electron 客户端等）时使用。含实测跑通的案例步骤，每次操作后都要确认是否生效。
---




# GUI操作方法1 - 模拟人类

`look` 只回截图 → 模型在图上定位并给出像素坐标 → `act` 注入真实键鼠 → 再 `look` 确认生效。
人怎么做，它就怎么做：看屏幕、点、再看一眼。

## 优缺点

优点：万能，通用
缺点：速度慢，开销大，需要模型有原生多模态能力；如果是纯文本模型 + 外挂多模态分析工具，理论上也可以，但需要注意【坐标矫正】（见下面的案例）

## 案例

目的：发送钉钉消息

步骤（React循环，主要是 remote_gui_look -> remote_gui_act -> remote_gui_look -> remote_gui_act -> ...，直到目标达成）：

- 使用 `remote_gui_diagnostics` 工具，目的是确认远端 GUI 辅助组件可用（accessibility 与 screenRecording 均为 true）。
- 使用 `remote_gui_list_windows` 工具，目的是找到目标窗口「阿里钉」（iDingTalk.exe），记下它的 `rootRef`。
- 使用 `remote_gui_focus_window` 工具，目的是把目标窗口切到前台并置顶；其中工具的参数来源于上一步的 `rootRef`。
- 使用 `remote_gui_look` 工具，目的是拿到窗口截图；其中工具的参数来源于 `rootRef`，并传 `include_image=true` + `max_output_tokens=1`（屏蔽无障碍元素树，只回图像）。本次截图 `1024×660`，窗口 `framePoints=(4247,139,2326,1500)`。
- 使用 `remote_gui_act` 工具（`action="click"`），目的是点中消息输入框；其中工具的参数 `look_id` 来源于紧邻上一步 `remote_gui_look` 的返回值（必填），`x=697, y=554` 来源于上一步的截图——模型直接在图上定位到输入框并取它的像素坐标；另传 `policy="foreground"`、`preserve_focus=true`（这两个参数**每次 act 都要带**）。
- 使用 `remote_gui_look` 工具（同上，`include_image=true` + `max_output_tokens=1`），目的是查看图片，确认上一步点击是否生效。
- 使用 `remote_gui_act` 工具（`action="typeText"`），目的是输入消息正文；其中工具的参数 `text="hello world"` 是正文内容本身，`look_id` 来源于紧邻上一步 `remote_gui_look` 的返回值（必填），`x=697, y=554` 沿用上一步截图坐标（输入框内任意点均可，作用只是让输入焦点落在框内）。
- 使用 `remote_gui_look` 工具（同上），目的是确认上一步打字是否生效。
- 使用 `remote_gui_act` 工具（`action="click"`），目的是点中「发送(S)」按钮；其中工具的参数 `look_id` 来源于紧邻上一步 `remote_gui_look` 的返回值（必填），`x=949, y=629` 来源于截图——模型在图上定位到发送按钮并取它的像素坐标。
- 使用 `remote_gui_look` 工具（同上），目的是确认上一步发送是否生效，检查：消息气泡「hello world」出现在「我」的会话里，且输入框已清空。

坐标问题：
- `act` 的 `x,y` 是 `look` 返回的那张【缩放截图】的像素，工具内部会反演回物理像素，不需要你操心。
- 如果你不具备多模态能力，依靠工具调用【外挂多模态模型】实现图像理解，请务必注意观察【外挂多模态模型】是否有涉及分辨率变换的预处理步骤。如果涉及分辨率变化，那记得做【坐标矫正】。

取图口径：`remote_gui_look` 没有「只回图像」的开关（`include_image=false` 是反过来，只有大纲）。本方法一律传 `include_image=true` + `max_output_tokens=1`：响应里不含 `outline` 字段，只剩窗口元数据与 `image_path`（大纲仍会构建，省掉的是响应体积，完整大纲约 10 万字符量级）。


---

# GUI操作方法2 - 图形转文本结构

用 `look` 的无障碍元素树（每个元素带 `ref` / `role` / `name` / `rect`）直接按元素操作，不注入真实键鼠。

## 优缺点

优点：效率高，速度快
缺点：不适用于复杂软件，不可靠

## 案例

目的：用计算器算 12×79

步骤：

- 使用 `remote_exec_command` 工具，目的是启动计算器（`calc.exe`）。
- 使用 `remote_gui_list_windows` 工具，目的是找到「计算器」窗口（win32calc.exe），记下它的 `rootRef`。
- 使用 `remote_gui_look` 工具，目的是拿到无障碍元素树；其中工具的参数来源于 `rootRef`，`include_image=false`、`max_output_tokens` 放大以取全树。本次返回 28 个 `canPress=true` 的按钮，每个都带 `ref` / `role` / `title` / `rect`。
- 使用 `remote_gui_act` 工具（`action="press"`），目的是按下数字与运算符键；其中工具的参数 `ref` 来源于上一步元素树里 `title` 等于目标字符的按钮（依次取 `title="1"` → `"2"` → `"乘"` → `"7"` → `"9"` → `"等于"`）。本方法**不需要** `policy` / `preserve_focus`——返回 `delivery: ax` 说明走的是无障碍语义路径，没有注入真实键鼠。
- 使用 `remote_gui_read_text` 工具，目的是读回计算结果；其中工具的参数 `ref` 来源于元素树中 `title="结果"` 的元素。本次读回 `948`，与 12×79 相符。

补充：`press` 驱动的是 UIA 的 Invoke / Toggle 模式，`outcome: worked` 即表示控件已响应；若控件不暴露这些模式，工具会回退到键鼠注入，那时 `delivery` 会变成 `hid`。
