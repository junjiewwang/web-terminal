# 页面滚动和抖动问题诊断

## 问题描述

用户反馈：按 Ctrl+C 复制后，页面会滚动到复制的行，并有抖动。

## 已尝试的方案

### 方案1：copy-selection（原始方案）
- ❌ 会滚动和抖动

### 方案2：copy-selection-no-clear + clear-selection
- ❌ 仍然会滚动和抖动

### 方案3：copy-selection-no-clear + begin-selection + cursor-left
- ⏳ 测试中

### 方案4：前端移除 OSC 52 序列
- ⏳ 测试中

---

## 可能的根本原因

### 原因1：tmux 的自动视图调整

tmux 在执行某些 copy-mode 命令时，会自动调整视图：
- 滚动到选择的位置
- 调整 pane 的可见区域

### 原因2：xterm.js 处理 OSC 52 的问题

当 xterm.js 接收到 OSC 52 序列时：
- 可能会触发某些重绘
- 可能会导致滚动位置变化

### 原因3：tmux 在发送 OSC 52 时的副作用

tmux 在发送 OSC 52 时，可能会：
- 先保存当前视图状态
- 执行复制操作
- 恢复视图状态（导致抖动）

---

## 诊断步骤

### 步骤1：检查是否是 OSC 52 导致的

**测试方法**：
1. 在终端中手动发送 OSC 52 序列（不通过 copy-mode）
2. 观察是否有滚动

**命令**：
```bash
# 在容器内执行
docker exec -it wetty-mcp-terminal-wetty-mcp-1 bash
# 进入正在运行的 tmux session
tmux attach -t wetty-dev-cloud
# 手动发送 OSC 52
printf "\x1B]52;;VGVzdCBNU0c=\x07"
```

如果手动发送 OSC 52 **不**导致滚动，说明问题在 tmux 的 copy 命令。

### 步骤2：检查 tmux 的具体命令

**测试方法**：
在 tmux copy-mode 中，手动执行不同的命令组合，观察哪个导致滚动。

**测试序列**：
1. 只执行 `copy-selection-no-clear`
2. 只执行 `clear-selection`
3. 只执行 `begin-selection`
4. 组合执行

### 步骤3：检查 xterm.js 的行为

**测试方法**：
在前端添加日志，记录每次数据写入时的滚动位置。

**代码**：
```javascript
console.log('Before write:', terminal.buffer.active.viewportY);
terminal.write(data);
console.log('After write:', terminal.buffer.active.viewportY);
```

---

## 可能的解决方案

### 方案A：完全禁用 tmux 的复制功能，使用前端实现

**实现方式**：
1. tmux mouse mode 改为 `mouse off`
2. 前端通过 xterm.js 的 selection API 处理复制
3. 不使用 OSC 52，直接在前端处理

**优点**：
- 完全避免 tmux 的副作用
- 更灵活的控制

**缺点**：
- 无法在 vim/top 等 alternate screen 模式下使用 tmux 的选择功能

### 方案B：使用 tmux 的 pipe-copy

**实现方式**：
```bash
bind-key -T copy-mode C-c send-keys -X pipe-copy-and-cancel "cat > /tmp/clipboard"
```

然后通过后端读取文件并推送到前端。

**优点**：
- 可能避免 OSC 52 的问题

**缺点**：
- 需要额外的文件 I/O

### 方案C：延迟清除选择

**实现方式**：
```bash
bind-key -T copy-mode C-c send-keys -X copy-selection-no-clear \; run-shell "sleep 0.1" \; send-keys -X clear-selection
```

**优点**：
- 可能通过延迟避免同步问题

**缺点**：
- 用户体验可能不够流畅

### 方案D：前端捕获并阻止滚动

**实现方式**：
在前端监听 xterm.js 的滚动事件，在特定条件下阻止滚动。

**代码示例**：
```javascript
terminal.onScroll(() => {
  if (isProcessingClipboard) {
    // 阻止滚动或恢复到之前的位置
  }
});
```

---

## 下一步行动

1. **请用户测试最新版本**（前端移除 OSC 52 + 新的 tmux 命令组合）
2. **如果仍然有问题**，执行诊断步骤1，手动发送 OSC 52 测试
3. **根据诊断结果**，选择合适的解决方案（A/B/C/D）

---

## 测试版本说明

### 当前版本（已部署）

**tmux 配置**：
```
bind-key -T copy-mode C-c send-keys -X copy-selection-no-clear \; send-keys -X begin-selection \; send-keys -X cursor-left
```

**前端逻辑**：
- 解析 OSC 52 并复制到剪贴板
- **从输出中移除 OSC 52 序列**（避免 xterm.js 处理）

**预期效果**：
- ✅ 复制功能正常
- ✅ 清除选择
- ✅ 保持 copy-mode
- ❓ 页面是否还会滚动？

---

## 用户反馈要点

请告诉我：

1. **页面滚动是否仍然存在**？
   - 如果是，滚动幅度多大？（几行？半屏？）

2. **抖动具体表现**？
   - 快速闪烁？
   - 内容上下跳动？
   - 持续时间多长？

3. **是否愿意尝试替代方案**？
   - 如：使用 Shift+鼠标拖选（浏览器原生选择）
   - 或：完全在前端实现复制（不依赖 tmux）
