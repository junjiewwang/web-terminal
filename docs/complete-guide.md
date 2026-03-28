# 终端复制功能 - 最终方案总结

## 🎯 核心矛盾

tmux 和 vim 都需要鼠标支持，但期望的行为不同：

| 场景 | 期望行为 | 冲突点 |
|------|----------|--------|
| **命令行** | 拖选 → tmux copy-mode | ✅ 无冲突 |
| **vim 中** | 拖选 → vim 可视模式 或 tmux copy-mode？ | ❌ 有矛盾 |

---

## 💡 完美解决方案：Shift 键区分

**核心思路**：用 Shift 键来区分两种选择模式

### 操作方式

| 操作 | 命令行模式 | vim/top 模式 |
|------|-----------|-------------|
| **鼠标拖选** | tmux copy-mode ✅ | **传递给 vim** ✅ |
| **Shift + 鼠标拖选** | 前端原生选择 ✅ | **tmux copy-mode** ✅ |
| **滚轮** | 翻看历史 | 滚动 vim |

### 详细说明

#### 命令行模式

```
方式1：鼠标拖选
  1. 鼠标拖选文本 → 进入 tmux copy-mode
  2. Ctrl+C → 复制
  3. q → 退出

方式2：Shift + 鼠标拖选
  1. 按住 Shift，鼠标拖选 → 前端原生选择
  2. Cmd+C → 复制（无滚动）
```

#### vim 模式

```
方式1：鼠标拖选（vim 原生）
  1. 鼠标拖选文本 → vim 可视模式（如果 vim 配置了 mouse=a）
  2. y → 复制到 vim 寄存器

方式2：Shift + 鼠标拖选（tmux copy-mode）
  1. 按住 Shift，鼠标拖选 → 进入 tmux copy-mode
  2. Ctrl+C → 复制
  3. q → 退出
```

---

## 🔧 配置实现

### tmux 配置

```bash
# 鼠标拖选：alternate screen 传递给应用，normal screen 进入 copy-mode
bind-key -T root MouseDrag1Pane \
  if-shell -Ft= "#{alternate_on}" \
    "send-keys -M" \
    "select-pane \; copy-mode \; send-keys -X begin-selection"
```

**工作原理**：
- `alternate_on = 1` (vim/top)：`send-keys -M`（传递给应用）
- `alternate_on = 0` (命令行)：进入 copy-mode

### Shift 键的作用

**在浏览器中**：
- **不按 Shift**：鼠标事件被 tmux 捕获
- **按住 Shift**：绕过 tmux mouse mode，使用浏览器原生选择

**tmux 层面**：
- **不按 Shift**：`MouseDrag1Pane` 触发
- **按住 Shift**：tmux 不拦截，事件传递给终端应用

**vim 层面**：
- **不按 Shift**：如果 vim 配置了 `mouse=a`，vim 处理；否则无响应
- **按住 Shift**：绕过 vim 的 mouse mode，tmux 捕获

---

## 🎯 完整操作指南

### 场景1：命令行中复制（无滚动）

```
1. 按住 Shift 键
2. 鼠标拖选文本
3. Cmd+C
4. ✅ 无滚动无抖动
```

### 场景2：命令行中复制（支持滚轮翻历史）

```
1. 鼠标拖选 → 自动进入 copy-mode
2. Ctrl+C → 复制
3. q → 退出
4. ⚠️ 可能有轻微滚动（tmux 限制）
```

### 场景3：vim 中复制（vim 原生）

```
1. 确保 vim 配置了 set mouse=a
2. 鼠标拖选 → vim 可视模式
3. y → 复制到 vim 寄存器
```

### 场景4：vim 中复制（使用 tmux）

```
1. 按住 Shift 键
2. 鼠标拖选 → 进入 tmux copy-mode
3. Ctrl+C → 复制
4. q → 退出
5. ⚠️ 可能有轻微滚动
```

### 场景5：vim 中复制（前端原生）

```
1. 按住 Shift 键
2. 鼠标拖选 → 浏览器选择
3. Cmd+C → 复制
4. ✅ 无滚动
5. ❌ 只能复制可见内容
```

---

## 📋 vim 配置建议

在服务器上的 `~/.vimrc` 中添加：

```vim
" 启用鼠标支持
set mouse=a

" 可视模式下复制到系统剪贴板
vmap <C-c> "+y
```

这样在 vim 中：
- 鼠标拖选 → vim 可视模式
- `Ctrl+C` → 复制到系统剪贴板

---

## ✅ 总结

### 推荐使用方式

| 场景 | 推荐方式 | 理由 |
|------|---------|------|
| 命令行 | **Shift + 拖选** | 无滚动，支持 Cmd+C |
| vim | **vim 原生** 或 **Shift + 拖选** | 取决于 vim 配置和个人偏好 |
| top/less | **Shift + 拖选** | 只能用这种方式 |

### 关键点

1. **Shift 键**：绕过 tmux/vim 的 mouse mode，使用浏览器原生选择
2. **不按 Shift**：
   - 命令行：tmux copy-mode
   - vim：vim 可视模式（如果配置了 `mouse=a`）
3. **tmux 滚动限制**：在 copy-mode 中操作会导致视图调整，无法避免

---

## 🧪 测试验证

请按以下步骤测试：

### 测试1：命令行中两种方式

```bash
# 生成文本
echo "Test Line 1"
echo "Test Line 2"

# 方式1：鼠标拖选 → Ctrl+C
# 方式2：Shift + 拖选 → Cmd+C
```

### 测试2：vim 中两种方式

```bash
# 打开 vim
vim test.txt

# 方式1：鼠标拖选（如果 vim 配置了 mouse=a）
# 方式2：Shift + 拖选 → Ctrl+C
```

### 测试3：滚轮功能

```bash
# 命令行：滚轮翻看历史
# vim 中：滚轮滚动 vim 内容
```
