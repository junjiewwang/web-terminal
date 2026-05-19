# Bugfix: 远端无 gunzip 时文件传输失败

> **日期**：2026-05-19
> **状态**：✅ 已修复

---

## 问题描述

在无 `gzip`/`gunzip` 的容器（如精简版 K8s Pod）中，文件上传失败：

```
bash: ft_recv: command not found
上传超时: 等待模式 '__FT_RECV_READY__|__FT_RECV_ERR__' 超时（15.0s）
```

## 根因分析

两个环节依赖 `gunzip`：

1. **Snippet 注入**（`ensure_snippet_loaded`）：
   - 默认使用 `compressed=True` 模式
   - 生成命令：`echo '<base64>' | base64 -d | gunzip -c > /tmp/ts-ft.sh && source ...`
   - 远端无 `gunzip` → 管道静默失败（`2>/dev/null`）→ 脚本文件为空或不存在
   - `ft_recv` 函数未被定义

2. **文件上传**（`PtyFileTransfer.upload`）：
   - 默认尝试 gzip 压缩传输
   - 发送 `ft_recv --compressed '/path'`
   - 即使 snippet 注入成功，`ft_recv --compressed` 也会因 `gunzip` 缺失输出 `__FT_RECV_ERR__:gunzip not available`

## 修复方案

### 核心策略：探测 + 自动降级

在两个关键路径增加远端 `gunzip` 可用性探测，不可用时自动降级：

```bash
command -v gunzip >/dev/null 2>&1 && echo '__GZ_YES__' || echo '__GZ_NO__'
```

### 1. Snippet 注入降级（`snippet_registry.py`）

```
探测 gunzip → 有: compressed 模式（gzip+base64 单行注入）
              → 无: heredoc 模式（cat << EOF 多行注入）
```

改动位置：`ensure_snippet_loaded()` 函数，在步骤 3（注入脚本）前增加探测步骤。

### 2. 文件上传降级（`pty_file_transfer.py`）

```
探测 gunzip → 有: 尝试 gzip 压缩（压缩率达标才用）
              → 无: 直接纯 base64 传输（跳过 --compressed）
```

改动位置：`PtyFileTransfer.upload()` 方法，在压缩决策前调用 `_probe_remote_gunzip()`。

### 3. 下载方向（无需修改）

`ft_send --compressed` 在 shell 脚本中已有优雅降级：
```bash
if command -v gzip >/dev/null 2>&1; then
    # 压缩传输
else
    _compressed=0  # gzip 不可用，回退
fi
```

## 代码变更

| 文件 | 变更 |
|------|------|
| `src/services/snippet_registry.py` | `ensure_snippet_loaded()` 增加 gunzip 探测步骤，传入 `compressed=False` 降级 |
| `src/services/pty_file_transfer.py` | 新增 `_probe_remote_gunzip()` 方法；`upload()` 中压缩决策前先探测 |

## 验证矩阵

| 场景 | 预期行为 |
|------|----------|
| 远端有 gunzip | 压缩注入 + 压缩传输（原有行为不变） |
| 远端无 gunzip | heredoc 注入 + 纯 base64 传输 |
| gunzip 探测超时 | 保守降级为 heredoc + 纯 base64 |
| 下载方向无 gzip | ft_send 自动回退为无压缩发送 |

## 影响评估

- 无 gunzip 环境：额外 1 次探测命令（~5s 超时），注入体积增大约 3x（heredoc vs compressed），但功能正常
- 有 gunzip 环境：增加 1 次快速探测（通常 <100ms），其余行为不变
- 性能影响：探测结果不缓存（每次注入/上传都探测），因为用户可能跳板到不同容器
