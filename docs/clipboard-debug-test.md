# 终端复制功能测试场景

## 预期行为

### 操作流程

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 1 | 鼠标拖选文本 | 进入 copy-mode，文本高亮显示 |
| 2 | 按 Ctrl+C | ✅ 文本已复制<br>✅ **选择高亮应该消失**<br>✅ **保持在 copy-mode**（不退出）<br>✅ 右上角显示绿色 toast |
| 3 | 可以继续选择新文本 | 仍然在 copy-mode，可以开始新的选择 |
| 4 | 按 Enter | 退出 copy-mode |
| 5 | 按 q | 取消选择并退出 copy-mode |

---

## 诊断测试

### 测试1：检查 Ctrl+C 后是否真的退出了

**操作步骤**：
1. 刷新浏览器页面 (Ctrl+Shift+R)
2. 连接终端 (点击 dev-cloud)
3. 输入命令生成文本：
   ```bash
   echo "Test Line 1"
   echo "Test Line 2"
   echo "Test Line 3"
   ```
4. **鼠标拖选 "Test Line 1"**（应该看到文本高亮）
5. **按 Ctrl+C**
6. **观察并记录**：
   - [ ] 是否看到 toast 提示 "已复制到剪贴板"？
   - [ ] 文本高亮是否消失？
   - [ ] **关键问题**：左下角或状态栏是否还显示 copy-mode 相关信息？
   - [ ] 尝试再次拖选文本，是否能选？

### 测试2：连续复制多段文本

**操作步骤**：
1. **鼠标拖选 "Test Line 1"**
2. **按 Ctrl+C**
3. **立即拖选 "Test Line 2"**（关键：不要按任何其他键）
4. **观察**：
   - [ ] 能否成功选择第二段文本？
   - [ ] 如果不能选择，说明已经退出 copy-mode

### 测试3：检查浏览器控制台

**操作步骤**：
1. 打开浏览器开发者工具 (F12)
2. 切换到 Console 标签
3. **拖选文本**
4. **按 Ctrl+C**
5. **查看控制台输出**：
   - [ ] 是否有 WebSocket 消息？
   - [ ] 是否有 OSC 52 序列 (`\u001b]52;;`)？
   - [ ] 是否有任何错误信息？

---

## 可能的问题诊断

### 问题A：Ctrl+C 后真的退出了 copy-mode

**症状**：
- 按 Ctrl+C 后，再次拖选文本无法选择
- 必须先按其他键才能开始新的选择

**可能原因**：
1. tmux 配置问题
2. 前端拦截了按键事件
3. xterm.js 处理问题

**诊断命令**：
```bash
# 在容器内检查配置
docker exec wetty-mcp-terminal-wetty-mcp-1 tmux list-keys -T copy-mode | grep " C-c "
# 预期输出: bind-key -T copy-mode C-c send-keys -X copy-selection
```

### 问题B：选择高亮没有清除

**症状**：
- 按 Ctrl+C 后，旧的选择高亮仍然存在
- 无法开始新的选择

**可能原因**：
1. `copy-selection` 没有清除选择
2. tmux 版本问题

**解决方案**：
需要修改 tmux 配置，添加手动清除选择的步骤

### 问题C：Mac 上的 Ctrl+C 不工作

**症状**：
- Mac 上按 Ctrl+C 没有反应
- 或者触发了浏览器的复制功能

**原因**：
- Mac 键盘布局问题
- 浏览器拦截

**解决方案**：
使用 Shift+鼠标拖选作为替代方案

---

## 请反馈以下信息

1. **操作系统**: Windows / Mac / Linux
2. **浏览器**: Chrome / Firefox / Safari / Edge
3. **测试1结果**:
   - 是否看到 toast？ 
   - 高亮是否消失？
   - 是否能立即再次选择？
4. **测试2结果**:
   - 能否连续选择两段文本？
5. **测试3结果**:
   - 控制台有什么输出？
   - 是否有错误？

---

## 配置文件检查

```bash
# 检查 tmux 配置
docker exec wetty-mcp-terminal-wetty-mcp-1 cat /root/.tmux.conf | grep -A 5 "copy-mode"

# 检查键绑定
docker exec wetty-mcp-terminal-wetty-mcp-1 tmux list-keys -T copy-mode
```

---

## 已知的 tmux 命令行为

| 命令 | 复制 | 清除选择 | 退出 copy-mode |
|------|------|----------|----------------|
| `copy-selection` | ✅ | ✅ | ❌ |
| `copy-selection-no-clear` | ✅ | ❌ | ❌ |
| `copy-pipe-and-cancel` | ✅ | ✅ | ✅ |
| `begin-selection` | ❌ | - | ❌ |
| `clear-selection` | ❌ | ✅ | ❌ |
| `cancel` | ❌ | ✅ | ✅ |

**我们使用的**: `copy-selection`
- ✅ 复制内容
- ✅ 清除选择高亮
- ❌ **不退出** copy-mode
