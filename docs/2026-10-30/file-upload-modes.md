# MCP 文件上传方案（三种模式 + 分片上传）（2026-10-30）

## 架构

```
客户端（文件在这里，agent 在这里）
  ↓ 上传
MCP 服务端（web-terminal 容器）
  ↓ PTY 传输
远端终端（SSH 目标节点，如 tce-d12）
```

关键约束：
- MCP 工具参数是「传值」不是「传引用」，服务端跨机器读不到客户端文件系统。
- 文件内容进服务端有两条通道：base64 塞 MCP 工具参数（进 LLM context、耗 token）vs HTTP multipart（从磁盘直发、不耗 token）。

## 三种上传模式（`upload_file` 工具）

| 模式 | 参数 | 适用 | 说明 |
|------|------|------|------|
| ① base64 直传 | `content_base64` | 小文件 ≤750KB | agent 一次调用搞定 |
| ② 容器内文件 | `local_path` | 文件已在容器内 | 上传容器已有文件 |
| ③ 一次性 token | 只传 `remote_path` | 大文件 >750KB | 返回完整 curl 指令 |

### 模式 ③ 详情（一次性 token）

agent 调 `upload_file(remote_path="/tmp/xxx")` → 返回一条完整可执行 curl：

```bash
curl -X POST 'http://host:8000/api/terminal/<sid>/upload?token=<一次性token>' \
  -F 'file=@/path/to/文件' \
  -F 'remote_path=/tmp/xxx'
```

- token 一次性、绑定 session + remote_path、120 秒时效
- 不需要 `WETTY_API_TOKEN`，文件从客户端磁盘直发，不耗 token

## 分片上传（突破 10M 单次 PTY 限制）

### 限制说明

单次 PTY 传输上限是「**压缩后 ≤ 10MB**」（`_MAX_TRANSFER_SIZE`），原始上限 50MB。

| 文件 | 一次性上传 | 分片上传 |
|------|-----------|---------|
| 可压缩文本（50M 日志） | ✅（gzip 后 <10M） | ✅ |
| 不可压缩二进制（20M tar.gz） | ❌ 413 | ✅ |
| 超大文件（>50M） | ❌（超原始上限） | ✅（每片独立请求，协议上无上限） |

> 注：分片上传每片是独立 HTTP 请求（每片 8M，小于所有单次限制），因此总大小不受 50M 约束；
> 实际限制是 PTY 传输时间（50-200KB/s）和远端磁盘空间。

### 客户端分片 + 串行上传（已实测）

```bash
# 1. 分片（每片 8M，留余量）
split -b 8M bigfile.tar.gz part_

# 2. 串行上传（复用上传端点，每片一次 curl）
for f in part_*; do
  curl -F "file=@$f" -F "remote_path=/tmp/$f" \
    http://服务端:8000/api/terminal/<sid>/upload
done

# 3. 远端合并 + 校验
run_command("cat /tmp/part_* > /tmp/bigfile.tar.gz && md5sum /tmp/bigfile.tar.gz")
```

实测（tce-d12 节点，20M 随机数据）：
- 一次性上传 → 413（压缩后 20M 超限）
- 分 3 片（8M+8M+4M）串行上传 → 全部成功
- 远端合并 → 20,971,520 字节完整
- MD5 一致（`283fe43362b359d4eea8c537e56ee68a`）

### 后续优化方向（服务端透明分片）

把分片逻辑封装进上传端点内部，agent 无感知：

```
agent 直接传完整文件（≤50M）
  → 服务端检测「压缩后 >10M」→ 自动分片 → 逐片 PTY → 远端合并 → MD5 校验
```

agent 只需传一次完整文件，突破 10M 限制但体验不变。

## 变更文件

- `src/mcp_server/server.py`：`upload_file` 三种模式（base64 / local_path / 一次性 token 返回 curl 指令）
- `src/api/file_transfer.py`：一次性上传 token 机制（`_register_upload_token`）+ 上传端点 token 校验
- `src/main.py`：auth_middleware 放行带 token 的上传请求
