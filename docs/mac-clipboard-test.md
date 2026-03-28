# Mac 环境下终端复制功能测试步骤

## 已知问题和解决方案

### 问题1：页面滚动和抖动

**现象**：按 Ctrl+C 后，页面滚动到复制的行，且有抖动

**原因**：tmux 的 `copy-selection` 命令会调整视图，导致 xterm.js 重绘

**解决方案**：
- 已添加调试日志
- 如果问题持续，可以尝试使用 `copy-selection-no-clear`

### 问题2：浏览器控制台无输出

**现象**：按 Ctrl+C 后，浏览器控制台没有日志

**可能原因**：
1. OSC 52 序列没有被发送
2. 前端没有接收到
3. tmux 的 `set-clipboard on` 没有生效

---

## 详细测试步骤

### 步骤1：刷新并连接

1. **强制刷新浏览器**：`Cmd + Shift + R`
2. **打开开发者工具**：`Cmd + Option + I`
3. **切换到 Console 标签**
4. **点击 dev-cloud 连接终端**

### 步骤2：生成测试文本

在终端中输入：
```bash
echo "Line 1: Test ABC"
echo "Line 2: Test DEF"
echo "Line 3: Test GHI"
```

### 步骤3：测试复制功能

1. **鼠标拖选 "Line 1: Test ABC"**
   - ✅ 应该看到文本高亮
   - ✅ 进入 copy-mode

2. **按 Ctrl+C**（注意是 Ctrl，不是 Cmd）
   - 观察 Console 标签：
     - ✅ 应该看到 `[WebSocket] 收到包含 OSC 52 的消息`
     - ✅ 应该看到 `[WebSocket] 解析到 OSC 52 剪贴板内容: Line 1: Test ABC`
   - 观察 toast：
     - ✅ 右上角应该显示绿色 toast "已复制到剪贴板"
   - 观察选择：
     - ✅ 高亮应该消失
   - **观察页面**：
     - ❌ 是否有滚动？
     - ❌ 是否有抖动？

3. **立即再次拖选 "Line 2: Test DEF"**
   - ✅ 应该能成功选择（说明没有退出 copy-mode）

4. **按 q**
   - ✅ 退出 copy-mode

### 步骤4：测试粘贴

1. 打开任意文本编辑器（如 VS Code、记事本）
2. 按 `Cmd + V`
3. ✅ 应该粘贴出 "Line 2: Test DEF"（最后一次复制的内容）

---

## 诊断信息收集

如果出现问题，请收集以下信息：

### 1. 浏览器控制台输出

在 Console 标签中：
- 拖选文本后，是否有任何输出？
- 按 Ctrl+C 后，是否有任何输出？
- 是否有错误信息（红色）？

**请截图或复制完整输出**

### 2. 网络请求检查

1. 切换到 Network 标签
2. 筛选 "WS"（WebSocket）
3. 找到 WebSocket 连接
4. 点击 "Messages" 标签
5. 拖选文本并按 Ctrl+C
6. 查看是否有包含 `\u001b]52;;` 的消息

**请截图 Messages 标签**

### 3. 后端日志

在终端执行：
```bash
docker logs wetty-mcp-terminal-wetty-mcp-1 --tail 50 | grep -E "(copy-buffer|clipboard)"
```

**复制输出结果**

### 4. tmux 配置检查

在终端执行：
```bash
# 检查 Ctrl+C 绑定
docker exec wetty-mcp-terminal-wetty-mcp-1 tmux list-keys -T copy-mode | grep " C-c "

# 检查 set-clipboard 配置
docker exec wetty-mcp-terminal-wetty-mcp-1 tmux show-options -g | grep clipboard
```

**复制输出结果**

---

## Mac 特殊说明

### Ctrl 键位置

- Mac 键盘上，Ctrl 键通常在左下角
- 不是 Command (⌘) 键
- 不是 Option (⌥) 键

### 备选方案：Shift + 鼠标拖选

如果 Ctrl+C 不工作，可以：

1. **按住 Shift 键**
2. **鼠标拖选文本**（绕过 tmux mouse mode）
3. **按 Cmd+C**（浏览器原生复制）

### 测试 Ctrl 键是否工作

在终端中测试：
```bash
# 测试 Ctrl+C（应该发送 SIGINT）
sleep 100
# 然后按 Ctrl+C，应该看到 ^C 并中断
```

---

## 预期的完整工作流程

```
用户操作               | 系统响应                      | 终端状态
-----------------------|------------------------------|------------------
鼠标拖选文本           | 进入 copy-mode, 文本高亮     | copy-mode
按 Ctrl+C             | 复制 + 清除选择 + OSC 52     | copy-mode (保持)
                      | 前端解析 OSC 52              |
                      | 显示 toast                   |
立即拖选新文本         | 新文本高亮                   | copy-mode
按 Ctrl+C             | 再次复制 + 清除              | copy-mode
按 q                  | 退出 copy-mode              | normal mode
```

---

## 已实施的修复

1. ✅ OSC 52 解析（前端）
2. ✅ 无权限复制（execCommand）
3. ✅ Toast 提示
4. ✅ 调试日志添加
5. ✅ Ctrl+C 绑定

## 待确认问题

1. ❓ OSC 52 是否真的被发送？
2. ❓ 页面滚动/抖动是否严重？
3. ❓ 是否能连续复制多段文本？

---

## 请反馈

请按照上述步骤测试，并告诉我：

1. **Console 是否有输出**？
   - 有 → 复制完整输出
   - 无 → 说明 OSC 52 没有发送或接收

2. **页面滚动和抖动是否严重**？
   - 轻微 → 可以接受
   - 严重 → 需要优化

3. **是否能连续选择多段文本**？
   - 能 → 功能正常
   - 不能 → 说明退出了 copy-mode
