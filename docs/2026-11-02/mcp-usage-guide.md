# MCP 使用指引（agent 操作手册）

面向使用 web-terminal MCP 的 agent（或使用者），说明常用工具的正确用法，重点是「从客户端上传文件」。

## 架构

```
客户端（agent + 文件在这里）── MCP 协议 ──> web-terminal 服务端（容器）── PTY ──> 远端节点
```

- 文件在**客户端**（agent 所在机器），不在服务端
- MCP 工具参数是「传值」不是「传引用」，服务端读不到客户端文件系统
- 因此文件内容进服务端只有两条路：base64 塞参数（小文件）、HTTP multipart（大文件）

## 上传文件（从客户端）

### 步骤 1：连接节点，拿 session_id

```
connect_host(host_name="tce-m7")
→ 返回 Session ID: xxxx
```

### 步骤 2：按文件大小选上传方式

**小文件（≤750KB）** — 一次工具调用搞定：

```
upload_file(session_id="xxxx", remote_path="/tmp/app.sh", content_base64="<文件内容的base64>")
```

agent 在客户端读文件内容 → base64 → 填入参数。

**大文件（750KB ~ 50MB）** — 两步：

```
① upload_file(session_id="xxxx", remote_path="/tmp/app.tar.gz")
   → 返回一条完整 curl 指令（含一次性 token）
② 在客户端执行该 curl：curl -F "file=@客户端本地路径" http://服务端:8000/...?token=...
   文件从客户端磁盘直发，不耗 token
```

### 超大文件（>10MB 不可压缩）

服务端已支持**透明分片**，同上走大文件方式即可，无需 agent 手动 split/合并。

## 下载文件

```
download_file(session_id="xxxx", remote_path="/tmp/app.log")
→ 返回浏览器下载 URL，打开即可下载到本地
```

## 终端尺寸同步（CVM console 光标错乱时）

进入 `virsh console` 等**串口终端**后，前端尺寸不穿透，行编辑光标会错乱：

```
sync_terminal_size(session_id="xxxx")
→ 读取前端真实尺寸，发送 stty cols/rows 穿透到最内层 shell
```

## 其他常用工具

| 工具 | 用途 |
|------|------|
| `list_hosts(tag=...)` | 列出主机（可按键 tag 筛选） |
| `run_command(session_id, command)` | 执行命令并取输出 |
| `send_input(session_id, text)` | 发送原始输入（处理交互式菜单/密码） |
| `read_terminal(session_id)` | 读当前终端屏幕 |
| `get_session_status` | 查会话状态 |
| `disconnect(session_id)` | 断开连接 |

## 注意事项

1. **同一会话不要并发操作**：PTY 是单字节流，多路输入会互相污染（MCP 与浏览器、两个 agent 同时操作同一 session 会错乱）。
2. **一次性 token 120 秒时效**：大文件 curl 指令拿到后尽快执行，过期重新调 `upload_file` 拿新指令。
3. **传输速率约 50-200KB/s**：大文件（几十 MB）耐心等待，进度由 SSE 流推送。
4. **终端尺寸**：浏览器窗口过窄不影响传输（已做 -opost 保护），但会影响交互显示，建议窗口保持合理宽度。
