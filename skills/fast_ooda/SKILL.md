---
name: fast_ooda
description: Drive the local desktop from a script with fast_ooda_gui_*; no MCP, no bridge.
---

`from aimux.local_alignment import fast_ooda_gui_*` 与 `remote_gui_*` 同名同参，直连本机 helper，绕过 MCP。

使用方法：
（1）使用 remote_apply_patch 编写Python脚本
（2）使用 remote_exec_command 运行脚本

和 remote_gui_* 的区别：

remote_gui_ 是MCP工具，用Python驱动非常麻烦（需要套MCP的开销）
fast_ooda_gui_ 可以绕开MCP，一步到位

