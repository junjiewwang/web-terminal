# 浏览器剪贴板功能测试指南

> **测试日期**: 2026-03-28  
> **修复内容**:  
> 1. 修复 tmux hook 变量展开问题  
> 2. 改用 `document.execCommand("copy")` 避免权限提示  
> 3. 添加成功/失败 toast 提示

---

## 一、准备工作

### 1. 确认服务运行
```bash
docker ps | grep wetty-mcp-terminal
# 应该看到 wetty-mcp-terminal-wetty-mcp-1 容器运行中
```

### 2. 打开浏览器
- 访问: http://localhost:8000
- 打开浏览器开发者工具（F12）
- 切换到 Console 和 Network 标签页

---

## 二、测试步骤

### 步骤 1: 连接终端

1. 点击左侧主机列表中的 **"🖥 dev-cloud"** 按钮
2. 等待终端连接成功（状态栏显示 "已连接 (ws ✓)"）

### 步骤 2: 生成测试文本

在终端中输入命令生成一些文本：
```bash
echo 'This is a test line for clipboard verification - 2026-03-28'
```

按 Enter 执行，确保文本显示在终端中。

### 步骤 3: 进行文本复制

**方法 A: 使用 tmux copy-mode（推荐）**

1. **鼠标拖选文本**：在终端输出的文本上按住鼠标左键拖动选择
   - 这会自动进入 tmux copy-mode（因为配置了 `mouse on`）
   
2. **按 Enter 键**：松开鼠标后，按键盘上的 Enter 键
   - 这会复制选中的文本并退出 copy-mode
   - **应该看到**：右上角出现绿色 toast "已复制到剪贴板"（持续 2 秒）

3. **验证剪贴板**：
   - 在任意文本编辑器（如记事本、VS Code）中按 Ctrl+V
   - **应该看到**：刚才选中的文本被粘贴出来

**方法 B: 使用 Shift+鼠标（兜底方案）**

如果方法 A 不工作，可以：

1. **按住 Shift 键**：然后鼠标拖选终端文本
   - 这会绕过 tmux 的鼠标模式，使用浏览器的原生选择
   
2. **按 Ctrl+C**：选中文本后按 Ctrl+C
   - **应该看到**：绿色 toast "已复制到剪贴板"

---

## 三、检查点

### ✅ 成功标志

| 检查项 | 预期结果 |
|--------|----------|
| 权限提示 | **无浏览器权限弹窗**（改用 execCommand 后不再提示） |
| Toast 提示 | 右上角绿色 toast "已复制到剪贴板"（2 秒后消失） |
| 后端日志 | `docker logs --tail 20` 显示 "收到 copy-buffer 请求" + "已推送到前端" |
| 剪贴板内容 | 在其他应用中可以粘贴出选中的文本 |

### ❌ 失败排查

| 问题 | 可能原因 | 解决方法 |
|------|----------|----------|
| 无 toast 提示 | WebSocket 连接断开 | 刷新页面重新连接 |
| toast 显示"复制失败" | 浏览器不支持 execCommand | 更换现代浏览器（Chrome/Firefox/Edge） |
| 后端日志无记录 | tmux hook 未触发 | 检查终端是否真的在 copy-mode（看左下角状态） |

---

## 四、后端日志验证

在终端执行复制操作后，立即检查后端日志：

```bash
docker logs wetty-mcp-terminal-wetty-mcp-1 --tail 30 | grep -E "(copy-buffer|clipboard)"
```

**预期输出**：
```
INFO     收到 copy-buffer 请求:              terminal.py:243
                             session_name=wetty-dev-cloud
INFO     读取 buffer 文件成功:               terminal.py:268
                             /tmp/tmux-copy-wetty-dev-cloud (XX chars)
INFO     tmux copy-buffer 已推送到前端:      terminal.py:282
                             wetty-dev-cloud (XX chars)
INFO:     127.0.0.1:XXXXX - "POST /api/tmux/copy-buffer HTTP/1.1" 204 No Content
```

---

## 五、容器内手动测试（调试用）

如果浏览器测试不工作，可以手动测试后端流程：

```bash
# 1. 进入容器
docker exec -it wetty-mcp-terminal-wetty-mcp-1 bash

# 2. 创建测试 buffer
echo "Manual test content" > /tmp/tmux-copy-wetty-dev-cloud

# 3. 手动调用 API
python3 -c "
import http.client, json
conn = http.client.HTTPConnection('127.0.0.1', 8001)
conn.request('POST', '/api/tmux/copy-buffer', 
  json.dumps({'session_name': 'wetty-dev-cloud'}),
  {'Content-Type': 'application/json'}
)
print(f'Status: {conn.getresponse().status}')
"

# 4. 检查后端日志
# 应该看到 "收到 copy-buffer 请求" + "已推送到前端"
```

---

## 六、常见问题

### Q1: 为什么有时候复制不成功？

**A**: tmux copy-mode 需要正确操作：
1. 鼠标拖选后**必须**按 Enter 或 Ctrl+C 才会复制
2. 按 q 键会取消选择，不会复制
3. 确保终端连接正常（状态栏显示 "已连接"）

### Q2: 浏览器权限提示在哪里？

**A**: 本次修复已改用 `document.execCommand("copy")`，**不会出现权限提示**。
如果还是看到提示，可能是之前的代码缓存，请：
- 强制刷新页面（Ctrl+Shift+R 或 Cmd+Shift+R）
- 清除浏览器缓存

### Q3: toast 提示一闪而过看不清？

**A**: toast 默认显示 2 秒。如果需要更长时间：
- 修改 `frontend/src/components/TerminalView.tsx` 第 83 行的 `setTimeout` 时间
- 或者查看浏览器控制台的日志输出

---

## 七、技术原理

### 完整数据流

```
用户操作 → tmux copy-mode
         ↓
         tmux save-buffer
         ↓
         after-copy-mode hook 触发
         ↓
         写入 /tmp/tmux-copy-{session_name}
         ↓
         POST /api/tmux/copy-buffer
         ↓
         后端读取文件 → WebSocket 推送 {type: "clipboard"}
         ↓
         前端 useWebSocket 收到消息
         ↓
         document.execCommand("copy")
         ↓
         Toast 提示 "已复制到剪贴板"
```

### 关键改进

1. **变量转义**: `\$TMUX_BUF` 防止提前展开
2. **无权限复制**: 使用 `execCommand` 替代 Clipboard API
3. **用户反馈**: 成功/失败 toast + 2 秒自动消失

---

## 八、测试报告模板

测试完成后，请填写以下信息：

| 测试项 | 结果 | 备注 |
|--------|------|------|
| 连接终端 | ✅/❌ | |
| 生成文本 | ✅/❌ | |
| 鼠标拖选 | ✅/❌ | 是否自动进入 copy-mode？ |
| 按 Enter 复制 | ✅/❌ | |
| Toast 提示 | ✅/❌ | 内容：______ |
| 权限提示 | ✅无/❌有 | 如果有，内容：______ |
| 后端日志 | ✅/❌ | API 调用次数：______ |
| 剪贴板粘贴 | ✅/❌ | 粘贴内容：______ |

**整体评价**: 
- [ ] 完全正常
- [ ] 基本正常，有小问题
- [ ] 不工作

**问题描述**:
（如有问题，请详细描述操作步骤和错误现象）
