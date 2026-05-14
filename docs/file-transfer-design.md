# 多跳节点文件传输功能

## 需求背景

在多跳 SSH 连接场景下（如：本地 → 堡垒机 → 目标节点），SCP/SFTP 无法直接使用，
因为堡垒机可能是菜单式（menu_send）入口，不支持 ProxyJump（`-J`）。

需要一种基于现有 PTY 通道的文件传输方案，在不依赖额外通道的情况下，
实现 Agent 与任意多跳目标节点之间的文件上传/下载。

## 设计方案：Snippet "远程站点" + PTY 管道传输

### 核心思路

1. 通过 Snippet 系统将文件传输脚本注入到目标节点（ft_recv / ft_send / ft_checksum）
2. 文件数据通过 base64 编码 + 标记协议在 PTY 通道上传输
3. Python 协调器（PtyFileTransfer）驱动整个传输流程

### 协议标记

| 标记 | 方向 | 含义 |
|------|------|------|
| `__FT_RECV_READY__` | 上传 | 远端准备好接收 |
| `__FT_CHUNK__:<seq>:<data>` | 双向 | 带序列号的 base64 数据块 |
| `__FT_ACK__:<seq>:OK` | 上传 | 第 seq 块接收成功 |
| `__FT_ACK__:<seq>:CORRUPT` | 上传 | 第 seq 块数据损坏，请重传 |
| `__FT_ACK__:<seq>:SEQ_ERR:<exp>` | 上传 | 序列号不匹配，期望 exp |
| `__FT_EOF__` | 上传 | 数据发送完毕 |
| `__FT_RECV_OK__:<bytes>` | 上传 | 接收成功，附字节数 |
| `__FT_RECV_ERR__:<msg>` | 上传 | 接收失败 |
| `__FT_SEND_BEGIN__:<size>` 或 `__FT_SEND_BEGIN__:<csize>:<osize>:C` | 下载 | 开始发送，附文件大小；压缩模式附压缩后/原始大小 + `C` 标记 |
| `__FT_SEND_END__:<md5>` | 下载 | 发送完毕，附 MD5 |
| `__FT_SEND_ERR__:<msg>` | 下载 | 发送失败 |
| `__FT_CHECKSUM__:<md5>:<sha256>` | 校验 | 文件校验和 |

### 性能参数

- 传输模式：**ACK 确认协议**（batch-ACK, 每批 5 个 chunk） + **-icanon 大 chunk**
- 块大小：36KB 原始 → 48KB base64（-icanon 解除 MAX_CANON 限制，且不超过 64KB PTY 内核缓冲区）
- ACK 超时：10 秒/块（超时自动重传）
- 最大重传次数：5 次/块
- 自适应延迟：初始 30ms，行合并时翻倍回退（上限 500ms），连续成功 20 个后减半加速（下限 5ms）
- 预估速率：200-500 KB/s（36KB chunk 大幅减少轮次开销）
- 5.8MB 文件：~166 chunks（vs 2.7KB 时 ~2228 chunks，减少 93%）
- 推荐文件上限：10MB
- 超时：基础 60s + 每 MB 30s，最大 600s
- 远端 read 超时：300s（防止永久阻塞）
- EOF 重发机制：首次等待 30s，超时后重发 EOF 再等剩余时间
- 短写保护：`os.write()` 循环写入确保 PTY 数据完整
- 终端模式：`stty -echo -icanon`（关闭回显 + 关闭 canonical，解除行长限制）
- 数据校验：Shell 端每块 base64 试解码校验，损坏自动请求重传
- 取消机制：先 `stty sane` 恢复终端规范模式，再发送 `__FT_EOF__` 让 ft_recv 正常退出
- **PTY 写入 EAGAIN 保护**：os.write() 遇到 PTY 缓冲区满（errno 11）时，使用 select 等待 fd 可写后重试，总超时 10 秒，避免数据丢失
- **智能 gzip 压缩**：传输前自动尝试 gzip 压缩，压缩率 > 20% 才启用；文本/日志压缩率可达 60-80%，已压缩文件自动跳过
  - 压缩级别：6（速度/压缩率平衡点）
  - 压缩阈值：压缩后 < 原始 × 0.80 才使用（至少节省 20%）
  - Shell 端：`ft_recv --compressed` 接收后自动 gunzip 解压
  - 前端：实时显示压缩率标签和压缩后传输量

## 实施记录

### 文件清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `config/snippets/file-transfer-snippet.sh` | ✅ 完成 | Shell 接收/发送/校验函数 |
| `src/services/pty_file_transfer.py` | ✅ 完成 | Python 协调器 (PtyFileTransfer) |
| `config/snippets.yaml` | ✅ 完成 | 新增 ft 域（3 个命令） |
| `src/mcp_server/server.py` | ✅ 完成 | 新增 upload_file / download_file MCP 工具 |
| `src/api/file_transfer.py` | ✅ 完成 | 浏览器端文件上传/下载 REST API |
| `frontend/src/components/FileTransferPanel.tsx` | ✅ 完成 | 前端文件传输面板组件 |
| `frontend/src/services/api.ts` | ✅ 完成 | 新增 uploadFile / downloadFile API |
| `frontend/src/components/TerminalView.tsx` | ✅ 完成 | 集成 FileTransferPanel + 状态栏按钮 |

### MCP 工具清单

| 工具 | 描述 |
|------|------|
| `upload_file` | 上传本地文件到远端节点（自动加载 ft snippet） |
| `download_file` | 从远端节点下载文件到本地（自动加载 ft snippet） |

### REST API 清单（浏览器端）

| 端点 | 方法 | 描述 |
|------|------|------|
| `/api/terminal/{session_id}/upload` | POST | 浏览器上传文件到远端节点（SSE 流式进度） |
| `/api/terminal/{session_id}/upload/cancel` | POST | 取消正在进行的上传（中断 PTY 传输） |
| `/api/terminal/{session_id}/download` | POST | 触发远端下载 + SSE 流式进度（query: remote_path） |
| `/api/terminal/{session_id}/download/{token}` | GET | 一次性 token 取回已下载的文件（token 120s 过期） |

### 架构图

```
Agent 上传流程 (batch-ACK 确认协议):
  Agent → PtyFileTransfer.upload()
    1. _ensure_ft_snippet_loaded() — 自动加载 ft snippet
    2. send_input("ft_recv '/path'")
    3. wait_for(__FT_RECV_READY__)
    4. 批量 ACK 循环（每批 5 个 chunk）:
       a. 批量发送: send_input("__FT_CHUNK__:<seq>:<base64>") × N
       b. 批量收集: _collect_batch_acks() 等待 N 个 ACK
       c. 如果 CORRUPT/SEQ_ERR → 逐个重传失败的 chunk（最多 5 次）
       d. 如果批量 ACK 超时 → 全批逐个重传
    5. send_input("__FT_EOF__")
    6. wait_for(__FT_RECV_OK__)
    7. inline md5sum/md5 → MD5 校验

Agent 下载流程:
  Agent ← PtyFileTransfer.download()
    1. _ensure_ft_snippet_loaded() — 自动加载 ft snippet
    2. send_input("ft_send '/path'")
    3. wait_for(__FT_SEND_BEGIN__)
    4. _collect_chunks() — 逐行扫描 __FT_CHUNK__ 数据
    5. wait_for(__FT_SEND_END__)
    6. base64 解码 → 写入本地文件
    7. MD5 比对校验

浏览器上传流程:
  Browser → POST /api/terminal/{session_id}/upload (multipart/form-data)
    → FastAPI 接收文件 → 保存临时文件
    → 返回 SSE 事件流（text/event-stream）
    → _ensure_ft_snippet_loaded() — 自动加载 ft snippet
    → PtyFileTransfer.upload(on_progress=callback)
      → 每发送 N 个 chunk 调用 on_progress → asyncio.Queue
      → SSE generator 读取 Queue → event: progress（实时进度）
    → event: complete（最终结果） 或 event: error（失败）
    → 清理临时文件

浏览器下载流程（两步式 SSE 进度 + token 取文件）:
  Step 1: Browser → POST /api/terminal/{session_id}/download?remote_path=...
    → _ensure_ft_snippet_loaded() — 自动加载 ft snippet
    → PtyFileTransfer.download(on_progress=callback)
      → _collect_chunks() 每 10 个 chunk 调用 on_progress → asyncio.Queue
      → SSE generator 读取 Queue → event: progress（实时进度）
    → event: complete（含 token、filename、file_size、md5）
    → 或 event: error（失败原因）
  Step 2: Browser → GET /api/terminal/{session_id}/download/{token}
    → 校验 token（一次性、120s TTL）→ FileResponse 返回文件
    → 清理临时文件 + token
```

## 待验证 & 遗留问题

1. ~~**PTY 缓冲区溢出**~~：已通过 `stty -echo` + 增加块间延迟 (0.15s) 缓解。
   仍需在 ≥5MB 文件场景下验证。

2. ~~**回显干扰**~~：已通过 `stty -echo` 解决。`ft_recv` 在接收模式期间关闭 PTY 回显，
   传输完毕后自动恢复（`trap ... RETURN`）。`__FT_EOF__` 匹配改为前缀匹配（`__FT_EOF__*)`）。

3. **二进制文件兼容性**：base64 编码确保了二进制安全，但需要测试包含特殊字符的文件名。

4. **断点续传**：当前不支持，每次传输都是从头开始。如有需要可在后续版本添加 offset 参数。

5. **并发传输**：当前设计一个 session 同一时间只能进行一次文件传输。如需并发，
   需要使用不同的终端会话。

## 已知问题修复记录

### Bug #1: 页面白屏 (crypto.randomUUID)
- **症状**：点击上传后页面白屏，DevTools 报 `crypto.randomUUID is not a function`
- **原因**：`crypto.randomUUID()` 需要 Secure Context（HTTPS），HTTP 环境下不可用
- **修复**：用 `Date.now() + Math.random()` 替代

### Bug #2: ft_recv command not found
- **症状**：上传时终端显示 `ft_recv: command not found`
- **原因**：`file_transfer.py` 中的 `_ensure_ft_snippet_loaded` 实现与 MCP server 不一致，
  使用了 `get_script_loader` 而非 `build_heredoc_loader`，等待 prompt 而非 `__SNIPPET_INJECTED__`
- **修复**：重写为与 MCP server 完全一致的逻辑

### Bug #3: 上传超时 (PTY 回显淹没)
- **症状**：文件上传到 100% 但终端被 base64 回显数据充满，最终超时
- **原因**：PTY 回显 send_input 的所有 base64 数据到 stdout，`wait_for` 扫描大量噪音数据超时
- **修复**：
  1. Shell: `ft_recv` 执行 `stty -echo` 关闭 PTY 回显，退出时 `stty "$_old_stty"` 恢复
  2. Python: 块间延迟从 0.05s 增加到 0.15s
  3. Python: 发送 `__FT_EOF__` 后记录 `_raw_buffer` 位置，`wait_for` 从此处开始扫描
  4. Python: 基础超时从 30s 增加到 60s，每 MB 超时从 10s 增加到 30s

### Bug #4: 上传卡住 (PTY MAX_CANON 行缓冲区溢出)
- **症状**：Snippet 注入成功、`__FT_RECV_READY__` 正常输出，但 shell 的 `read -r line`
  始终收不到 base64 数据，传输卡死直到超时
- **根因**：PTY canonical 模式下 Linux 内核的 line discipline 有 `MAX_CANON` 限制（通常 4096 字节）。
  原始 chunk 大小 48KB → base64 编码后单行约 65KB，远远超出此限制。
  内核 line discipline 会截断/丢弃超过 `MAX_CANON` 的输入行，导致 `read -r` 永远读不到完整数据。
  `stty -echo` 只关闭了回显，并未解除行缓冲区大小限制。
- **修复**：
  1. **Shell**: `ft_recv` 使用 `stty -echo` 关闭回显，保留 canonical 模式
     以获得内核级行边界完整性保证。
  2. **Python**: chunk 大小调整为 3060 字节（base64 后 4080 字节/行），
     在 canonical 模式 MAX_CANON (4096) 极限内最大化吞吐。
  3. **Python**: 块间延迟 10ms（canonical 模式行边界有保证，无需长延迟）。
  4. **Shell**: `ft_send` 默认 chunk 保持 2KB（下载方向不受 MAX_CANON 影响）。

### Bug #5: 大文件上传 413 Request Entity Too Large
- **症状**：5.8MB 二进制文件上传失败，错误 `Request Entity Too Large`
- **原因**：Nginx 默认 `client_max_body_size` 为 1MB，超过此大小的请求体直接返回 HTTP 413
- **修复**：在 `nginx.conf` 的 `/api/` location 中添加 `client_max_body_size 12m`
  （后端 FastAPI 限制 10MB + multipart 编码开销）

### Bug #6: 504 Gateway Time-out (Nginx proxy_read_timeout)
- **症状**：5.8MB 文件上传阶段 2（PTY 传输到远端）等待约 60 秒后失败，
  错误 `Gateway Time-out`
- **原因**：Nginx `/api/` location 未设置 `proxy_read_timeout`，默认 60 秒。
  文件传输 API 是同步阻塞的：FastAPI 需要等整个 PTY 传输完成才返回响应。
  5.8MB 文件 ÷ 2KB/chunk × 0.02s/chunk ≈ 58s，加上 snippet 注入和校验开销刚好超 60s。
- **修复**：为文件传输端点 `/api/terminal/{id}/(upload|download)` 添加独立 Nginx location，
  设置 `proxy_read_timeout 600s`（10 分钟，与后端 `_MAX_TIMEOUT_SECONDS` 一致），
  不影响其他 API 的默认 60s 超时。

### Bug #8: 进度 100% 后卡住（校验阶段无反馈）
- **症状**：进度条到 100% 后 UI 停滞不动，无任何状态变化，用户以为卡死了。
  实际是后端正在等待远端 `__FT_RECV_OK__`（base64 解码写入文件）+ `ft_checksum`（MD5 校验），
  但期间 SSE 流上没有任何事件推送。
- **根因**：`on_progress` 回调只在 chunk 发送循环和 EOF 发送后各调用一次，
  进入 verifying 阶段后再无进度事件。`progress_queue.get()` 无超时，SSE 流沉默。
- **修复**：
  1. **后端心跳**：`progress_queue.get()` 加 `timeout=1.5s`，超时重发最后一次进度快照（state=verifying），
     确保 SSE 流持续有数据，前端能感知到系统还在工作。
  2. **前端 verifying 状态**：`UploadProgressInfo` 增加 `ptyState` 字段，
     `_UploadProgress` 组件在 `ptyState=verifying` 时显示绿色满进度条 + "🔍 校验中..." 动画。
  3. **状态流转**：用户看到 `0% → 传输中 → 100% → 校验中... → 上传成功`，每个阶段都有反馈。

### Bug #9: 取消上传无效（远端 shell 未中断）
- **症状**：点击"取消"按钮后，前端 UI 回到 idle 状态，但终端里 `ft_recv` 仍然在
  `read -r` 等待数据，终端不可用（输入无反应，需要手动 Ctrl+C）。
- **根因**：前端 `handleCancel()` 只调用了 `abortRef.current.abort()` 断开 SSE fetch 流。
  后端 `_sse_upload_generator` 被 FastAPI 中断后，`_do_upload()` 的 `asyncio.Task` 未被 cancel，
  即使 task 最终结束，远端 shell 的 `ft_recv` 已经卡在 `read -r` 上不会自动退出。
- **修复（三层取消机制）**：
  1. **Python task cancel**：SSE generator 的 `except (CancelledError, GeneratorExit)` 中
     `upload_task.cancel()`，停止 `PtyFileTransfer` 继续发送 chunks。
  2. **PTY Ctrl+C 中断**：cancel 后调用 `_interrupt_pty(session)`，向 PTY 发送 `\x03`（Ctrl+C），
     触发远端 `ft_recv` 的 `trap INT TERM HUP` 信号处理。
  3. **前端双重保障**：`handleCancel()` 同时 abort SSE 流 + 调用 `POST /upload/cancel` API，
     确保即使 SSE 断连信号传递不及时，取消 API 也能主动 cancel task + Ctrl+C。
  4. **活跃任务注册表**：`_active_uploads: dict[session_id, Task]` 跟踪活跃上传，
     cancel API 通过 session_id 查找并取消对应 task。

### Bug #10: base64 解码失败 (echo 转义数据损坏)
- **症状**：文件传输进度 100% 后校验阶段报错 `base64 decode failed`
- **根因**：`file-transfer-snippet.sh` 第 97 行使用 `echo "${line#__FT_CHUNK__:}"` 将 base64
  数据追加到临时文件。部分 shell 的 `echo` 内建命令会解释字符串中的反斜杠转义序列
  （如 `\n` → 换行、`\t` → 制表符、`\\` → `\`），而 base64 编码结果中可能包含反斜杠字符，
  导致写入的数据与原始 base64 不一致，`base64 -d` 解码时失败。
- **修复**：将 `echo` 替换为 `printf '%s\n'`。`printf '%s\n'` 的 `%s` 格式化符保证
  原样输出字符串内容，不做任何转义解释，确保 base64 数据完整性。

### Bug #11: 大文件上传超时 (PTY 通道饱和 + EOF 丢失)
- **症状**：5.8MB 二进制文件上传进度到 100%，进入"📦 解码写入中..."后等待 2m39s，
  最终报错 `__FT_RECV_ERR__:read timeout (300s) or input closed`
- **根因分析**：
  1. **chunk 数量过多**：2KB/chunk → 5.8MB 文件需要 ~2969 个 chunk，
     每个 chunk 需要一次 `os.write()` + `read -r` 往返，传输时间约 2969×0.02s ≈ 59s
  2. **os.write() 短写风险**：Python `os.write(fd, data)` 可能只写入部分数据
     （尤其是大缓冲区），导致 PTY 行边界错乱，远端 `read -r` 读到不完整的行
  3. **EOF 信号丢失**：大量 chunk 充满 PTY 内核缓冲区后，`__FT_EOF__` 标记
     可能被延迟或部分覆盖，远端 `read -r` 超时退出
  4. **数据路径压力**：Python `send_input` → `os.write` → PTY 内核缓冲区 → SSH 通道
     → 远端 shell `read -r`，每一层都有吞吐瓶颈
- **修复（四项性能优化 O1/O3/O4/O6）**：见下方"性能优化记录"章节

### Bug #12: base64 解码失败 (PTY 行边界丢失)
- **症状**：数据接收完毕（`__FT_RECV_READY__` 正常），但 base64 解码阶段直接报
  `__FT_RECV_ERR__:base64 decode failed`
- **表面原因**：`base64 -d "$tmp_b64"` 将临时文件路径作为位置参数传给 `base64` 命令。
  GNU coreutils 的 `base64 -d` 不接受文件名作为位置参数，只从 stdin 读取。
  已修复为 `base64 -d < "$tmp_b64"` 输入重定向。
- **深层根因（PTY 行边界丢失）**：修复 base64 用法后仍然失败。通过 MCP 工具
  在远端直接分析 `.b64` 临时文件发现行合并现象：
  - **-icanon 模式 (48KB chunk, 50ms delay)**：1274 正常行 + 161 损坏行 → 损坏率 **11.2%**
  - **canonical 模式 (3060B chunk, 10ms delay)**：609 正常行 + 82 损坏行 → 损坏率 **11.8%**
  - 损坏行中 `grep` 发现 `__FT_CHUNK__:` 协议标记嵌在行中间（如 `q+N__FT_CHUNK__:QYluMp`）
  - **结论**：行合并不是 canonical vs non-canonical 的问题，而是 **PTY 写入速率**问题。
    Python 端 `os.write()` 连续快速写入时，`\n` 行分隔符在内核 PTY 缓冲区中丢失，
    导致远端 `read -r` 将两个 chunk 读成一行。
  - 根本原因：**块间延迟不足**——10ms 间隔不够让 PTY 内核完成上一行的刷新
- **最终修复（三项）**：
  1. **Shell**: 移除 `stty -icanon`，仅保留 `stty -echo`（减少 PTY 行规则干扰）
  2. **Python**: chunk 大小从 48KB 调整为 **3060 字节**（MAX_CANON 极限优化）
  3. **Python**: 块间延迟设为 **50ms**（确保 PTY 内核有足够时间处理行边界）
- **调试方法**：修改 `_ft_cleanup` 在传输失败时不删除 `.b64` 临时文件，
  用 `awk '{print length}' file | sort | uniq -c` 分析行长度分布，
  用 `sed -n 'Np' file | cut -c<offset>` 定位损坏位置

### Bug #13: ACK 后行合并（5ms 微延迟不够）
- **症状**：chunk 163 OK 后，chunk 164 发送时 SEQ_ERR:164，3 次重传全部失败
- **根因**：Python 收到 ACK 后仅等 5ms 就写入下一个 chunk，PTY 缓冲区中 ACK 回显和新 chunk 碰撞导致行合并
- **修复**：采用类 TCP 拥塞控制的动态自适应延迟——初始 30ms，行合并时翻倍回退（上限 500ms），连续成功 20 个后减半加速（下限 5ms）

### Bug #14: 小 chunk 大量 ACK 导致 SSH 崩溃
- **症状**：~170 轮 ACK 后 SSH 连接断开
- **根因**：2.7KB chunk → 2228 轮 ACK echo 堆积 PTY 缓冲区溢出
- **修复**：回归 -icanon + 48KB 大 chunk（5.8MB 只需 124 chunks），配合 ACK 确认保障可靠性

### Bug #15: chunk 0 即失败 — PTY 内核缓冲区溢出
- **症状**：`chunk 0/124 重传 5 次后仍失败 (delay=500ms)`，第一个 chunk 就传不过去，
  `ft_recv` 已输出 `__FT_RECV_READY__` 但 ACK 永远收不到
- **根因**：48KB raw → 64KB base64 + 前缀 `__FT_CHUNK__:0:` + `\n` ≈ 65552 字节，
  超过部分系统 PTY 内核缓冲区大小（64KB = 65536 字节）。数据被截断后，
  Shell 端 `read -r` 只能读到不完整的行（末尾没有 `\n`），一直在等完整行，
  而 Python 端也在等 ACK，两边互相死锁。
- **修复**：chunk 大小从 48KB 降到 36KB（base64 后 48KB + 前缀 ≈ 49KB，远低于 64KB 限制）

### Bug #16: 取消上传后 SSH 断连
- **症状**：点击取消后终端显示 `__FT_RECV_ERR__:interrupted by signal` → `^CConnection to ... closed`
- **根因**：`-icanon` 模式下 `\x03` (Ctrl+C) 不被 line discipline 拦截转换为 SIGINT，
  而是直接透传到 SSH 层，SSH 收到裸 `\x03` 后将其解释为断开连接的信号
- **修复**：`_interrupt_pty()` 改为发送 `__FT_EOF__\n` 标记让 `ft_recv` 正常退出
  （while-read 循环碰到 `__FT_EOF__*` 会 break 并走 `_ft_cleanup` 恢复 stty），
  不影响 SSH 连接

### Bug #17: 上传没有进度条显示
- **症状**：上传时始终显示"PTY 传输中，请稍候... Xs"的脉冲动画，无百分比进度条
- **根因**：前端 `_UploadProgress` 组件条件 `info.percent > 0` 在首个 SSE 进度事件到达前
  一直为 false，而大 chunk 的首个 ACK 确认需要较长时间
- **修复**：
  1. **后端**：在发送第一个 chunk 之前推送 0% 初始进度事件，让前端立即有数据
  2. **前端**：移除 `info.percent > 0` 的条件分支和脉冲动画，始终显示进度条
     （初始 percent=0 时进度条最小宽度 1% 作为视觉提示）

### Bug #20: wait_for 缓冲区扫描竞态条件（上传/下载/注入全超时）
- **症状**：上传时 `wait_for('__FT_RECV_READY__')` 超时 15s，但终端中明确已输出
  `__FT_RECV_READY__`。同理 snippet 注入的 `__SNIPPET_INJECTED__` 也超时。
- **根因**：`wait_for()` 默认 `start_pos = len(self._raw_buffer)`，即从调用时刻的
  缓冲区末尾开始扫描。但 `send_input()` 发送命令后，Shell 的响应可能在 `wait_for`
  记录 `start_pos` 之前就已经到达 `_raw_buffer` 中，扫描起始位置跳过了目标标记。
  与 ACK 循环对比：ACK 循环在 `send_input` 前用 `pre_pos = len(_raw_buffer)` 记录位置，
  并传入 `_start_pos=pre_pos`，所以不受此竞态影响。
- **修复**：统一采用 `pre_pos` 模式，在 `send_input()` 调用前记录缓冲区位置，
  传入 `wait_for(_start_pos=pre_pos)`。涉及三处：
  1. `pty_file_transfer.py` upload() — `ft_recv` 命令后等待 `RECV_READY`
  2. `pty_file_transfer.py` download() — `ft_send` 命令后等待 `SEND_BEGIN`
  3. `file_transfer.py` `_ensure_ft_snippet_loaded()` — heredoc 注入后等待 `INJECT_DONE`

## 性能优化记录

### O1: chunk 大小优化（36KB + -icanon + ACK）
- **历程**：2KB → 48KB（-icanon 盲发失败）→ 3060B（canonical 极限）→ 2730B（ACK 小 chunk）→ 48KB（-icanon + ACK）→ **36KB（修复 PTY 缓冲区溢出）**
- **2730B ACK 方案失败原因**：5.8MB 文件需要 ~2228 个 chunk，大量 ACK echo 回流
  堆积在 PTY 缓冲区中，约 170 轮后导致 PTY 缓冲区溢出和 SSH 连接崩溃
- **48KB 方案失败原因**：48KB raw → 64KB base64 + 前缀 ≈ 65.5KB，超过部分系统
  PTY 内核缓冲区大小（64KB = 65536 字节），导致 chunk 0 就失败
- **最终方案**：36KB chunk + -icanon + ACK 确认
  - -icanon 解除 MAX_CANON 限制，允许 ~48KB base64 行
  - 36KB raw → 48KB base64 + 前缀 ≈ 49KB，远低于 64KB PTY 内核缓冲区限制
  - ACK 协议保障可靠性：行合并 → CORRUPT/SEQ_ERR → 自动重传
  - 5.8MB 只需 ~166 chunks，远低于 170 的 SSH 崩溃阈值
  - 自适应延迟（30ms 初始，行合并翻倍，连续成功减半）
- **效果**：~166 chunks × ~50ms/chunk ≈ 8s（理论最优，实际含 RTT 约 10-30s）

### O2: 智能 gzip 压缩传输
- **问题**：文本/日志/配置文件等可压缩内容，原始传输浪费大量 PTY 带宽
- **方案**：传输前 Python 端 gzip 压缩，Shell 端 gunzip 解压
  1. Python 读取文件后 `gzip.compress(data, level=6)`
  2. 比较压缩后大小，只有压缩率 > 20%（compressed < original × 0.80）才启用
  3. 启用时命令改为 `ft_recv --compressed '/path/to/file'`
  4. 数据分块 + base64 + ACK 传输流程不变
  5. Shell 端接收完成后 base64 解码 → .gz → gunzip → 目标文件
  6. 已压缩文件（.zip/.tar.gz/.jpg 等）自动检测并跳过压缩
- **改动文件**：
  - `pty_file_transfer.py`：upload() 添加智能压缩逻辑 + TransferProgress 压缩字段
  - `file-transfer-snippet.sh`：ft_recv 支持 `--compressed` 参数 + gunzip 解压
  - `file_transfer.py`：SSE 进度事件传递压缩信息
  - `api.ts`：PtyTransferProgress 新增压缩字段
  - `FileTransferPanel.tsx`：显示压缩率标签和压缩后传输量
- **预估收益**（5.8MB 文本文件，假设 60% 压缩率）：
  - 传输数据量：5.8MB → ~2.3MB
  - chunk 数：~166 → ~66
  - 耗时：10-30s → 4-12s

### O7: ACK 确认 + 重传协议（彻底替代固定延迟）
- **问题**：固定延迟（10ms→50ms→100ms）本质是"盲猜"，不可靠且浪费时间
  - 延迟太短 → 行合并 → base64 损坏
  - 延迟太长 → 5MB 文件需要 3+ 分钟
  - 不同环境最佳延迟不同，无法硬编码
- **方案**：类 TCP stop-and-wait 确认协议
  1. Python 发送 `__FT_CHUNK__:<seq>:<base64>` 并等待 ACK
  2. Shell 收到后校验 base64 完整性 + 序列号正确性
  3. 回复 `__FT_ACK__:<seq>:OK/CORRUPT/SEQ_ERR`
  4. Python 收到 OK → 发下一个；CORRUPT/SEQ_ERR/超时 → 重传（最多 3 次）
- **优势**：
  - ACK 本身就是"远端已处理完"的证明，无需盲等
  - 行合并损坏 → CORRUPT → 自动重传修复
  - 自适应速率：快环境快传，慢环境自动降速
  - PTY 通道 RTT 通常 5-20ms，比固定 100ms 延迟快 5-10 倍

### O3: os.write 短写保护（循环写入）
- **问题**：`os.write(fd, data)` 可能只写入部分数据，
  导致 base64 行被截断，远端 `read -r` 读到不完整数据
- **改动**：`TerminalSession.write()` 方法改为循环写入：
  ```python
  raw = data.encode()
  offset = 0
  while offset < len(raw):
      written = os.write(self._fd, raw[offset:])
      if written <= 0:
          break
      offset += written
  ```
- **效果**：确保每次 send_input 的完整数据（含 `\n`）都写入 PTY

### O4: Shell 端收块进度上报（已被 O7 ACK 替代）
- **问题**：Python 端只知道"已发送 N 个 chunk"，不知道远端是否实际收到
- **改动**：`ft_recv` 每收到 10 个 chunk 输出 `__FT_RECV_PROGRESS__:<count>`
- **当前状态**：已被 O7 ACK 确认协议完全替代。ACK 是逐块确认，
  比每 10 块上报一次的 PROGRESS 更精准更及时。

### O6: EOF 重发容错机制
- **问题**：`__FT_EOF__` 在大量 chunk 之后发送，可能在 PTY 缓冲区中丢失
- **改动**：发送 EOF 后分两阶段等待：
  1. 第一次等待 `min(30s, timeout×0.5)`
  2. 如果超时，重发 `__FT_EOF__` 并等待剩余超时
- **效果**：EOF 丢失时自动重发，避免远端 `read -r` 白白等到 300s 超时

### O8: Snippet 精简 + 压缩注入
- **问题**：每次文件传输前需注入 snippet 到远端节点，原始 heredoc 注入需传输 10.7KB
  数据。多跳 SSH 场景下注入耗时明显，且版本更新时每个节点都需重新注入完整脚本。
- **方案**：三步优化
  1. **精简 snippet 内容**（324行/10.8KB → 104行/3.7KB，减少 65%）：
     - 删除全部非功能性注释（保留版本号和函数签名）
     - 提取 `_b64dec()` 辅助函数，消除 4 处 `base64 -d || base64 --decode` 重复
     - 合并压缩/非压缩两条 base64 解码路径
     - 精简变量名（`_ft_expected_seq` → `_exp` 等）
     - 合并信号处理函数到 trap 表达式中（`_ft_signal_handler` 内联）
  2. **移除 `ft_checksum` 函数**（-35行），改用 Python 端 inline 命令：
     ```
     md5sum '/path' 2>/dev/null | awk '{print $1}' ||
     md5 -q '/path' 2>/dev/null || echo unavailable
     ```
     `_verify_checksum()` 不再依赖 snippet 函数，减少 snippet 体积。
  3. **gzip+base64 压缩注入**（`build_heredoc_loader(compressed=True)`）：
     - 服务端 `gzip.compress(content, level=9)` + `base64.b64encode()`
     - 注入命令：`echo '<base64>' | base64 -d | gunzip > /tmp/ts-ft.sh && source ...`
     - 单行命令，无需 heredoc 多行传输
- **改动文件**：
  - `file-transfer-snippet.sh`：精简为 104 行，版本升至 2026.05.14.2
  - `snippet_registry.py`：`build_heredoc_loader()` 新增 `compressed` 参数（默认 True）
  - `pty_file_transfer.py`：移除 CHECKSUM/CHECKSUM_ERR 常量，`_verify_checksum()` 改用 inline
- **效果**：
  - 注入传输量：10.7KB → 2.1KB（节省 80.5%）
  - 注入速度提升约 5 倍（多跳 SSH 场景效果更显著）

### O9: 批量 ACK 传输（pipeline overlap）
- **问题**：O7 的 stop-and-wait ACK 协议每发一个 chunk 都要等 ACK 回复后才发下一个，
  Python 端在等 ACK 期间空闲（idle），PTY 通道利用率低。
  5.8MB 文件 166 chunks × (发送 + RTT) ≈ 166 × (~10ms + ~20ms) ≈ 5s，
  其中 ~3.3s 是纯等待 ACK 的空闲时间。
- **方案**：批量发送 + 批量收集 ACK
  1. **Python 端**每批发送 `_BATCH_SIZE = 5` 个 chunk（chunk 间保留 adaptive delay）
  2. 批量发送后调用 `_collect_batch_acks()` 一次性收集 5 个 ACK
  3. **Shell 端不变**：仍逐 chunk 回复 ACK（保留精确错误定位能力）
  4. 批内失败的 chunk 降级为逐个 stop-and-wait 重传（最多 5 次）
  5. 自适应延迟策略不变（初始 30ms，行合并翻倍，连续成功减半）
- **PTY 缓冲区安全分析**：
  - 5 个 chunk × 49KB base64 = 245KB 文本数据
  - PTY 内核缓冲区 64KB，但 Shell `read -r` 是串行处理的（读一行 → ACK → 读下一行）
  - 数据在 PTY 管道中排队，不需要全部同时驻留在 64KB 缓冲区中
  - Python `os.write()` 在缓冲区满时会阻塞（EAGAIN → select 等待），天然限流
- **改动文件**：
  - `pty_file_transfer.py`：
    - 新增 `_BATCH_SIZE = 5`、`_ACK_TIMEOUT = 15.0` 常量
    - 新增 `_collect_batch_acks()` 方法（扫描 `_raw_buffer` 收集多个 ACK）
    - 重写上传循环：外层 while 按批推进，内层处理 ACK 结果 + 失败重传
  - `file-transfer-snippet.sh`：版本升至 2026.05.14.3（Shell 端逻辑不变）
- **预估收益**（5.8MB 文件，166 chunks，batch=5）：
  - 批次数：166/5 = 34 批
  - 每批 overhead：1 次 ACK 等待（~20ms）vs 原 5 次（~100ms）
  - 理论加速：~2-3x（主要节省 ACK idle 时间，实际取决于网络 RTT）

### UX 改进: 校验阶段子步骤进度
- **需求**：校验阶段（verifying）只显示笼统的"🔍 校验中..."，用户不知道具体在做什么。
  远端校验实际分两步：① base64 解码并写入文件 ② 计算 MD5 校验和。
- **实现**：
  1. **后端**：`TransferProgress` 增加 `sub_step` 字段（`"decoding"` / `"checksumming"` / `""`），
     `upload()` 方法在发送 EOF 后推送 `sub_step="decoding"`（等待 RECV_OK 期间），
     收到 RECV_OK 后在调用 `_verify_checksum` 前推送 `sub_step="checksumming"`。
  2. **SSE**：进度事件 JSON 中包含 `sub_step` 字段。
  3. **前端**：`UploadProgressInfo` 增加 `ptySubStep` 字段，
     `_UploadProgress` 组件根据子步骤显示 "📦 解码写入中..." 或 "🔍 MD5 校验中..."。
- **用户体验流转**：`0% → 传输中 → 100% → 📦 解码写入中... → 🔍 MD5 校验中... → ✓ 上传成功`

### O12: 下载方向优化（大 chunk + 压缩传输 + dd 修复）
- **问题（三大下载瓶颈）**：
  1. **`dd bs=1` 逐字节读取**：`ft_send` 使用 `dd if=... bs=1 skip=N count=M`，每个字节一次
     系统调用，I/O 效率极低（36KB chunk 需要 36864 次 read syscall）
  2. **2KB 默认 chunk**：5.8MB 文件需要 ~2900 个 chunk，每个 chunk 50ms sleep = 145s 纯延迟
  3. **无压缩传输**：上传方向已有 O2 gzip 压缩，但下载方向直接传原始数据
- **方案**：
  1. **修复 dd 用法**：`dd bs=1 skip=N count=M` → `dd bs=CHUNK_SIZE skip=BLOCK_NUM count=1`
     （按块大小整数倍跳过，所有平台兼容；最后不完整块 dd 自动返回实际字节数）
  2. **大 chunk**：默认 36KB（与上传方向一致），减少 chunk 数：~2900 → ~166
  3. **减少延迟**：sleep 从 50ms → 5ms（下载方向是 Shell 端单向输出，无 ACK 竞态风险）
  4. **压缩传输**：`ft_send --compressed` 在远端 gzip 压缩后再 base64 分块传输
     - Shell 端：`gzip -c file > temp.gz`，对 .gz 文件分块 base64 传输
     - 新 `__FT_SEND_BEGIN__` 格式：`<compressed_size>:<original_size>:C`
     - Python 端：收到 base64 数据 → 解码 → `gzip.decompress()` → 写入文件
     - MD5 校验基于原始未压缩文件，压缩/解压对校验透明
     - gzip 不可用或压缩失败时自动回退为无压缩传输
  5. **前端**：`_DownloadProgress` 组件显示压缩标签和压缩后传输量
- **改动文件**：
  - `file-transfer-snippet.sh`：ft_send 重写（--compressed + 高效 dd + 36KB chunk + 5ms delay），版本升至 2026.05.14.5
  - `pty_file_transfer.py`：download() 请求压缩传输 + 解析新 SEND_BEGIN 格式 + gunzip 解压；_collect_chunks() 传递压缩信息
  - `file_transfer.py`：下载 SSE 进度事件添加压缩字段
  - `FileTransferPanel.tsx`：_DownloadProgress 组件显示压缩标签/解压提示
- **预估收益**（5.8MB 文本文件，假设 60% 压缩率）：
  - dd 修复：36KB chunk 从 36864 次 read → 1 次 read（单个 chunk 速度提升 ~1000x）
  - 大 chunk：~2900 → ~166 chunks（减少 94%），纯延迟 145s → 0.83s
  - 压缩：传输量 5.8MB → ~2.3MB，chunk 数 ~166 → ~66
  - 综合加速：**~145s → ~5-10s**（~15-30x 提升）

### Bugfix #23: 下载 base64 解码失败（chunk 拼接 + PTY 截断）
- **症状**：下载完成后 base64 解码报错 `Invalid base64-encoded string: number of data characters (240945) cannot be 1 more than a multiple of 4`
- **根因（双重问题）**：
  1. **拼接解码**：`download()` 把所有 chunk 的 base64 字符串 `"".join()` 后整体 `b64decode()`。
     每个 chunk 是 Shell 端 `dd | base64` 独立编码的，最后一个 chunk 可能有 `=` padding。
     拼接后 padding 出现在中间位置，但更关键的是——任何一个 chunk 被 PTY 截断 1 个字符，
     错误会累积到最终整体解码时才暴露，且无法定位是哪个 chunk。
  2. **PTY 行截断**：下载方向没有 ACK 确认机制（不同于上传方向的 ACK + base64 试解码 + 重传），
     Shell 端 `echo __FT_CHUNK__:...` 输出后不等确认就继续发送。如果 PTY 通道在某行传输时
     截断了末尾字符，Python 端 `_collect_chunks` 无法感知，静默收集了不完整的 base64 数据。
- **修复（两层防御）**：
  1. **`_collect_chunks` 逐 chunk 校验 + padding 补齐**：
     - 每收到一个 chunk 立即检查 `len(b64_data) % 4`
     - 非 4 的倍数时自动补齐 `=` padding（`4 - remainder` 个）并记录 warning
     - 同时做 `base64.b64decode()` 即时校验，解码异常时记录精确的 chunk 序号、
       长度、前/后 40 字符，方便定位问题
  2. **`download()` 逐 chunk 独立解码**：
     - 替代 `"".join(b64_chunks)` + 整体 `b64decode()`
     - 每个 chunk 独立 `b64decode()` → `raw_parts.append()`
     - 失败时精确报错：`chunk #N/M, b64_len=X, <error>`
     - 最终 `b"".join(raw_parts)` 拼接二进制数据
- **改动文件**：`pty_file_transfer.py`（`_collect_chunks` + `download`）

### Bugfix #23a: 浏览器刷新后终端回放 base64 乱码
- **症状**：下载传输期间刷新浏览器页面，终端中显示大量 base64 原始数据
- **根因**：`terminal_manager.py` 的 `_on_pty_readable()` 中 `_append_scrollback(data)` 在
  `_ws_muted` 检查 **之前** 执行。文件传输期间 `_ws_muted=True` 阻止了 WebSocket 广播，
  但 base64 数据已经写入 scrollback 缓冲区。浏览器刷新重连时 `add_ws_client()` 回放
  scrollback 历史，把传输期间的 base64 数据全部渲染到终端。
- **修复**：将 `_append_scrollback(data)` 移到 `_ws_muted` 检查 **之后**。
  静默模式下 Agent 缓冲区（`_raw_buffer`）仍正常写入（`wait_for` 扫描不受影响），
  但 scrollback、vterm feed、WebSocket 广播 **全部跳过**。
- **改动文件**：`terminal_manager.py`（`_on_pty_readable`）

### Bugfix #23b: base64 数据 ANSI 残留导致解码失败
- **症状**：Bugfix #23 实施后仍失败：`chunk #3/65, b64_len=800`，实际只有 797 个
  合法 base64 字符（3 个非法字符导致长度异常 → padding 补齐后仍解码失败）
- **根因**：`strip_ansi()` 基于正则匹配标准 ANSI 转义序列（`ESC[...`），但 PTY 传输
  过程中某些不完整的 ANSI 片段、控制字符（如 `\x00`-`\x1f`）无法被标准正则覆盖。
  base64 合法字符集是 `A-Za-z0-9+/=`，任何不属于该集合的字符都会使 `b64decode()` 失败。
- **修复**：在 `_collect_chunks()` 提取 `b64_data` 后，使用正则 `_NON_B64_RE = re.compile(r"[^A-Za-z0-9+/=]")`
  **主动清洗**所有非 base64 合法字符，而非依赖 `strip_ansi()` 的 ANSI 模式匹配。
  清洗前后长度不一致时记录 warning（含 chunk 序号、清洗前后长度、移除字符数）。
- **改动文件**：`pty_file_transfer.py`（模块级 `_NON_B64_RE` 常量 + `_collect_chunks` 清洗逻辑）

### Bugfix #23c → #23d: os.read 边界截断导致 chunk 数据丢失（行拼接修复）
- **症状**：#23b 实施后仍有 chunk 解码失败（`b64_len=2020, data chars 2017 余1`），
  且 gzip 解压报 `invalid distance too far back`——说明数据本身被截断，修补 padding 无法恢复。
- **根因（真正原因）**：`_on_pty_readable()` 中 `os.read(fd, 65536)` 不保证返回完整行。
  Shell 端 `ft_send` 的 `echo "__FT_CHUNK__:$(dd|base64|tr -d '\n')"` 输出的行长达 ~49KB，
  极容易在 `os.read` 边界被截断为两段：
  - 第 1 段：`__FT_CHUNK__:...partial_b64`（有前缀，数据不完整）
  - 第 2 段：`remaining_b64`（无前缀，是上一行的续行）

  `text.split("\n")` 将两段分别写入 `_raw_buffer`。`_collect_chunks` 只匹配
  `startswith("__FT_CHUNK__:")` 的行，**第 2 段（续行）被静默丢弃**，第 1 段缺尾。
  原来的 #23c"截尾 1 字符"虽然让 `b64decode` 不报错，但解出来的数据缺少真实字节，
  gzip 数据流被破坏。
- **修复（#23d 行拼接 line reassembly）**：
  在 `_collect_chunks` 中增加续行检测：如果当前行不含任何协议标记前缀
  （`__FT_CHUNK__:`/`__FT_SEND_END__`/`__FT_SEND_ERR__`），且已有收集的 chunks，
  则视为上一个 chunk 的续行，清洗后拼接到 `b64_chunks[-1]`。
  同时在 `download()` 的逐 chunk 解码阶段，对每个 chunk 做最终清洗 + padding 规范化
  （续行拼接可能引入额外的 ANSI 残留或 padding 错位）。
- **回滚 #23c**：移除"余 1 截尾"逻辑（会丢失真实数据），改为纯 ERROR 日志。
- **改动文件**：`pty_file_transfer.py`（`_collect_chunks` 续行拼接 + `download()` 解码前规范化）

## 使用示例

```
# AI Agent 使用流程：
1. connect_host("target-node")        # 连接到多跳目标节点
2. upload_file(session_id, "/local/config.yaml", "/tmp/config.yaml")
3. download_file(session_id, "/var/log/app.log", "/local/app.log")
```

## 变更日期

- 2026-05-13: Sprint 1 — Shell 脚本 + Python 协调器 + MCP 工具
- 2026-05-13: Sprint 2 — 浏览器端文件传输 UI（REST API + FileTransferPanel + TerminalView 集成）
- 2026-05-13: Bugfix #1-3 — crypto.randomUUID 白屏 + snippet 注入失败 + PTY 回显淹没超时
- 2026-05-13: Bugfix #4 — PTY MAX_CANON 行缓冲区溢出导致上传卡死（chunk 48KB→2KB + stty -icanon）
- 2026-05-13: Bugfix #5 — Nginx 413 Request Entity Too Large（添加 client_max_body_size 12m）
- 2026-05-13: Bugfix #6 — Nginx 504 Gateway Time-out（文件传输端点独立 location + proxy_read_timeout 600s）
- 2026-05-13: Bugfix #7 — ft_recv 信号处理：Ctrl+C 后终端不恢复 + read 无超时永久阻塞
- 2026-05-13: UX 改进 — 上传进度展示重构（两阶段步骤指示器 + 速度/大小/耗时信息 + PTY 传输脉冲动画）
- 2026-05-13: UX 改进 — 阶段2 SSE 实时进度（后端 on_progress 回调 → asyncio.Queue → SSE event:progress → 前端 ReadableStream 解析 → 真实进度条 + 速度 + 已传/总量）
- 2026-05-13: Bugfix #8 — 进度 100% 后卡住：SSE 心跳机制 + 前端 verifying 校验中状态展示
- 2026-05-13: Bugfix #9 — 取消上传无效：三层取消机制（cancel task + Ctrl+C PTY + cancel API 端点）
- 2026-05-13: Bugfix #10 — base64 decode failed：echo → printf '%s\n' 防止反斜杠转义数据损坏
- 2026-05-13: UX 改进 — 校验子步骤进度：拆分 verifying 为 "📦 解码写入中..." + "🔍 MD5 校验中..." 两个子状态
- 2026-05-13: Bugfix #11 — 大文件上传超时：PTY 通道饱和 + EOF 丢失（根因分析 + 四项优化）
- 2026-05-13: 性能优化 O3 — os.write 短写保护（TerminalSession.write 循环写入）
- 2026-05-13: 性能优化 O4 — Shell 端收块进度上报（__FT_RECV_PROGRESS__ 每 10 块上报）
- 2026-05-13: 性能优化 O6 — EOF 重发容错（首次 30s 超时后自动重发 __FT_EOF__）
- 2026-05-13: Bugfix #12 — base64 decode failed：① GNU base64 不接受文件名位置参数（改用输入重定向）② PTY 行边界丢失（-icanon 下高速写入 11% 行合并损坏）→ 回退 canonical 模式 + 3060 字节极限 chunk
- 2026-05-13: 性能优化 O1 — chunk 大小最终定为 2730 字节（ACK 协议 MAX_CANON 安全：2KB→48KB→3060B→2730B）
- 2026-05-13: 性能优化 O7 — ACK 确认 + 重传协议：彻底替代固定延迟盲发，Shell 端逐块 base64 校验 + 序列号验证 + ACK 回复，Python 端 send-wait-ACK 循环 + 超时重传（最多 3 次）
- 2026-05-13: Bugfix #13 — ACK 后行合并（5ms 微延迟不够）：采用类 TCP 拥塞控制的动态自适应延迟——初始 10ms，行合并时翻倍回退（上限 200ms），连续成功 50 个 chunk 后减半加速（下限 2ms），重传次数增加到 5 次
- 2026-05-13: Bugfix #14 — 小 chunk 大量 ACK 导致 SSH 崩溃：~170 轮 ACK echo 堆积 PTY 缓冲区溢出。方案：回归 -icanon + 48KB 大 chunk（5.8MB 只需 124 chunks），配合 ACK 确认保障可靠性，自适应延迟初始 30ms
- 2026-05-13: Bugfix #15 — chunk 0 即失败：48KB base64 + 前缀 ≈ 65.5KB 超过 PTY 内核 64KB 缓冲区。降为 36KB raw → 48KB base64（≈49KB，远低于 64KB 限制）
- 2026-05-13: Bugfix #16 — 取消上传 SSH 断连：-icanon 模式下 Ctrl+C 穿透 SSH。改发 __FT_EOF__ 让 ft_recv 正常退出
- 2026-05-13: Bugfix #17 — 上传无进度条：后端推送 0% 初始进度 + 前端移除 percent>0 条件，始终显示进度条
- 2026-05-13: O2 — 智能 gzip 压缩传输：Python 端 gzip 压缩 + Shell 端 gunzip 解压，压缩率 > 20% 自动启用，文本文件可减少 50-80% 传输数据量
- 2026-05-13: Bugfix #18a — PTY 写入 EAGAIN 数据丢失：os.write() 遇到 PTY 缓冲区满（errno 11）时只 warning 后丢弃数据，导致 chunk 从未到达远端、ACK 永远超时。修复为使用 select.select() 等待 fd 可写后重试，总超时 10 秒
- 2026-05-13: Bugfix #18b — 取消上传后 Ctrl+C 穿透 SSH 断连：_interrupt_pty 先发 `stty sane` 恢复终端规范模式（解除 -icanon），再发 __FT_EOF__，确保后续 Ctrl+C 被正确解释为 SIGINT 而非穿透到 SSH 层
- 2026-05-14: Bugfix #19 — zsh 兼容性：移除 `trap ... RETURN`（zsh 不支持 RETURN 信号），改为在所有退出路径手动调用 `_ft_cleanup` 恢复 stty。同时脚本中嵌入版本号 `__FT_SNIPPET_VERSION__`
- 2026-05-14: O3 — 版本化脚本注入：脚本嵌入 `__FT_SNIPPET_VERSION__="2026.05.14.1"` 版本号，探测时三步检查（函数存在 → 版本匹配 → 跳过注入），仅在首次或版本过期时才重新注入，减少不必要的 heredoc 传输
- 2026-05-14: Bugfix #20 — wait_for 缓冲区扫描竞态条件：`send_input()` 后 Shell 可能在 `wait_for()` 记录 `start_pos` 之前就输出了标记（如 `__FT_RECV_READY__`），导致扫描起始位置跳过目标标记、永远超时。修复：在 `send_input()` 前记录 `pre_pos = len(_raw_buffer)` 并传入 `wait_for(_start_pos=pre_pos)`，与 ACK 循环已有的正确模式保持一致。涉及三处：① upload() 的 RECV_READY ② download() 的 SEND_BEGIN ③ _ensure_ft_snippet_loaded() 的 INJECT_DONE
- 2026-05-14: O8 — Snippet 精简 + 压缩注入：① 精简 file-transfer-snippet.sh（324行/10.8KB → 104行/3.7KB），删除大段注释、合并 base64 解码重复逻辑、精简变量名；② 移除 `ft_checksum` 函数，改用 Python 端 inline `md5sum/md5` 命令；③ `build_heredoc_loader()` 支持 gzip+base64 压缩注入模式（`echo '<b64>' | base64 -d | gunzip > /tmp/ts-ft.sh`），最终注入传输量 10.7KB → 2.1KB（节省 80.5%）
- 2026-05-14: O9 — 批量 ACK 传输（pipeline overlap）：从 stop-and-wait 改为每批 5 个 chunk 发送 + 批量收集 ACK，Shell 端逻辑不变（仍逐 chunk 回复 ACK），Python 端新增 `_collect_batch_acks()` 方法 + 重写上传循环，失败 chunk 降级为逐个重传。预估加速 2-3x（主要节省 ACK idle 时间）
- 2026-05-14: O10a — 参数微调：batch_size 5→8（8×49KB=392KB，PTY 缓冲区安全）；初始延迟 30→15ms（自适应翻倍兜底，慢环境自动回退）
- 2026-05-14: O11 — 自适应 chunk size（借鉴 trzsz）：替代固定 36KB chunk，采用 slow-start + 指数增长 + 失败回退策略。核心变更：① `ChunkSizeController` 类（动态增长，失败立即减半，probing→stable 状态机）；② 动态 batch size（`_compute_batch_size()` 保持每批 ~400KB 恒定，chunk 越大 batch 越小）；③ 惰性切分（offset 指针 + controller.size 实时切片，替代预切分 `_split_into_chunks()`）；④ 移除固定 `_DEFAULT_CHUNK_SIZE`/`_BATCH_SIZE` 常量。Shell 端无需任何改动（`read -r` + ACK 逻辑完全 chunk-size 无关）。核心收益：新环境自动探测最优 chunk 大小，不稳定链路自动降级，稳定链路快速收敛到上限
- 2026-05-14: O11a — 自适应参数调优：初始 4KB + 3 批门槛导致探测期过长（5.6MB 文件 3.5MB 都在探测阶段，耗时 41s）。修复：`_CHUNK_SIZE_INIT` 4KB→16KB，`_CHUNK_GROW_THRESHOLD` 3→1。增长路径从 4 级 12 批（4→8→16→32→36KB）缩短为 2 级 2 批（16→32→36KB），探测期数据量从 3.5MB 降至 ~580KB
- 2026-05-14: Bugfix #22 — ft_send 终端 base64 乱码回显：下载时终端显示原始 base64 数据，原因是 `ft_send` 缺少 `stty -echo`（`ft_recv` 已有）。修复：ft_send 添加 `stty -echo` + `_ft_send_cleanup()` 恢复函数 + `trap INT TERM HUP` 信号处理，与 ft_recv 模式保持一致。snippet 版本升至 2026.05.14.4
- 2026-05-14: UX 改进 — 下载进度展示：将同步 GET/FileResponse 下载 API 重构为两步式架构：① POST 触发下载 + SSE 流式进度（progress/complete/error 事件，复用上传的 asyncio.Queue 桥接模式）；② GET token 取回文件（一次性 token，120s TTL）。后端 `_collect_chunks()` 增加 `on_progress` 回调（每 10 个 chunk 上报一次）；前端新增 `_DownloadProgress` 组件（进度条 + 速度 + 耗时，与 `_UploadProgress` 一致）
- 2026-05-14: O12 — 下载方向优化（大 chunk + 压缩传输 + dd 修复）：① ft_send 重写支持 `--compressed` 模式 + 36KB 默认 chunk + `dd bs=CHUNK skip=BLK count=1` 高效块读取（替代 `bs=1` 逐字节）+ 5ms 延迟；② Python download() 请求压缩传输 + 解析新 `__FT_SEND_BEGIN__:<csize>:<osize>:C` 格式 + gunzip 解压；③ 下载 SSE 进度事件 + 前端压缩标签。snippet 版本升至 2026.05.14.5。综合加速 ~15-30x（5.8MB 文本: ~145s → ~5-10s）
- 2026-05-14: Bugfix #23 — 下载 base64 解码失败（chunk 拼接 + PTY 截断）：所有 chunk 的 base64 直接 `"".join()` 后整体 `b64decode()`，PTY 截断 1 个字符导致长度非 4 倍数。修复：① `_collect_chunks` 逐 chunk 校验 + padding 补齐（`len % 4 != 0` 时补 `=`）；② `download()` 逐 chunk 独立 `b64decode()`（精确定位损坏 chunk 序号和长度）
- 2026-05-14: Bugfix #23a — 浏览器刷新后终端回放 base64 乱码：`_on_pty_readable()` 中 `_append_scrollback(data)` 在 `_ws_muted` 检查之前执行，传输期间 base64 数据写入 scrollback，刷新重连时全部回放。修复：将 scrollback/vterm/WS 广播全部移到 `_ws_muted` 检查之后，静默模式下仅写入 Agent 缓冲区
- 2026-05-14: Bugfix #23b — base64 数据 ANSI 残留导致解码失败：`strip_ansi()` 无法清除所有 PTY 残留字符（不完整 ANSI 片段、控制字符），导致 chunk 中混入 3 个非法字符（800 chars → 797 valid）。修复：新增 `_NON_B64_RE = re.compile(r"[^A-Za-z0-9+/=]")` 正则，在 `_collect_chunks` 中主动清洗所有非 base64 合法字符
- 2026-05-14: Bugfix #23c → #23d — os.read 边界截断导致 chunk 数据丢失：`os.read(fd, 65536)` 不保证返回完整行，~49KB 的 `__FT_CHUNK__:...` 行跨两次 read 被切成两段，第 2 段无前缀被 `_collect_chunks` 丢弃。#23c 的"截尾 1 字符"修复仅消除 b64decode 报错但丢失真实数据，导致 gzip 解压 `invalid distance too far back`。最终修复 #23d：在 `_collect_chunks` 中实现行拼接（line reassembly），无协议前缀的行视为上一个 chunk 续行拼接；`download()` 解码前做最终清洗 + padding 规范化
