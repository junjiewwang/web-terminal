"""虚拟终端渲染中间层 — Broker 模式专用

基于 pyte 实现的 ANSI 虚拟终端，解决 Broker 模式多客户端共享单一 PTY fd
时的渲染问题。

核心职责：
- 维护一个 pyte.Screen 字符矩阵（虚拟屏幕缓冲区）
- 将 PTY 原始输出 feed 到 Screen（ANSI 解析 → 字符矩阵更新）
- 基于 dirty tracking 进行差分渲染（仅发送变更行 → ANSI 转义序列）
- 支持 resize（Screen 尺寸动态调整）
- 提供全屏快照（新客户端连接时一次性恢复当前终端状态）

设计原则：
- VirtualTerminal 是纯数据结构，不涉及 I/O 或网络
- 仅在 Broker 模式下启用，TMUX 模式不受影响
- 性能保护：高速输出时可回退全屏 dump，避免差分开销
"""

from __future__ import annotations

import logging
import unicodedata

import pyte

logger = logging.getLogger(__name__)

# pyte 颜色名 → SGR 基本色偏移
_BASIC_COLORS: dict[str, int] = {
    "black": 0,
    "red": 1,
    "green": 2,
    "brown": 3,     # pyte 用 "brown" 表示 yellow
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}

# 高亮色（bright）偏移到 90-97 / 100-107
_BRIGHT_COLORS: dict[str, int] = {
    "brightblack": 0,
    "brightred": 1,
    "brightgreen": 2,
    "brightyellow": 3,
    "brightbrown": 3,
    "brightblue": 4,
    "brightmagenta": 5,
    "brightcyan": 6,
    "brightwhite": 7,
}


def _color_to_sgr(color: str, foreground: bool) -> str:
    """将 pyte 颜色值转换为 SGR 代码片段。

    pyte 的颜色格式：
    - "default"       → 不设色
    - "black"~"white" → 基本 8 色
    - "000"~"255"     → 256 色索引
    - 其他命名色      → 尝试映射
    """
    if color == "default":
        return ""

    lower = color.lower()

    # 基本 8 色
    if lower in _BASIC_COLORS:
        base = 30 if foreground else 40
        return str(base + _BASIC_COLORS[lower])

    # 高亮色
    if lower in _BRIGHT_COLORS:
        base = 90 if foreground else 100
        return str(base + _BRIGHT_COLORS[lower])

    # 256 色索引（pyte 可能返回纯数字字符串）
    try:
        idx = int(color)
        if 0 <= idx <= 255:
            prefix = 38 if foreground else 48
            return f"{prefix};5;{idx}"
    except ValueError:
        pass

    # 未识别的颜色，忽略
    return ""


class VirtualTerminal:
    """虚拟终端渲染中间层

    维护一个 pyte.Screen 作为内存字符矩阵。PTY 原始输出经
    pyte.Stream 解析后更新 Screen，然后通过 dirty tracking
    进行差分渲染，生成最小化的 ANSI 转义序列发送给客户端。
    """

    #: 当 dirty 行数占比超过此阈值时，回退到全屏 dump
    FULL_DUMP_THRESHOLD = 0.8

    # pyte 使用 mode_number * 32 存储 DEC Private Mode
    # 鼠标追踪相关 mode：
    #   ?1000 → 基本鼠标追踪（按下/释放）
    #   ?1002 → 按钮事件追踪（含拖动）
    #   ?1003 → 任意事件追踪（含移动）
    _MOUSE_TRACKING_MODES = frozenset({1000 << 5, 1002 << 5, 1003 << 5})

    # Alternate Screen Buffer 相关 mode：
    #   ?1049 → Save Cursor + Switch to Alternate Screen Buffer（最常用）
    #   ?1047 → Switch to Alternate Screen Buffer（较少见）
    #   ?47   → Switch to Alternate Screen Buffer（旧版）
    _ALTERNATE_SCREEN_MODES = frozenset({1049 << 5, 1047 << 5, 47 << 5})

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self._cols = max(cols, 1)
        self._rows = max(rows, 1)
        self._screen = pyte.Screen(self._cols, self._rows)
        self._stream = pyte.Stream(self._screen)
        # 初始清空 dirty（Screen 创建时可能标记所有行）
        self._screen.dirty.clear()

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    def feed_and_render(self, text: str) -> str:
        """将 PTY 输出 feed 到虚拟 Screen，返回差分渲染的 ANSI 文本。

        流程：
        1. pyte.Stream.feed(text) — 解析 ANSI 序列，更新字符矩阵
        2. 检查 Screen.dirty — 获取变更的行号集合
        3. 根据 dirty 比例决定差分或全屏渲染
        4. 清空 dirty
        5. 返回 ANSI 转义序列字符串

        Returns:
            ANSI 文本。如果无变化返回空字符串。
        """
        self._stream.feed(text)

        dirty = self._screen.dirty
        if not dirty:
            return ""

        # 高速输出保护：dirty 行数过多时回退全屏 dump
        dirty_ratio = len(dirty) / self._rows
        if dirty_ratio >= self.FULL_DUMP_THRESHOLD:
            result = self._full_screen_render()
        else:
            result = self._diff_render(sorted(dirty))

        dirty.clear()
        return result

    def full_screen_dump(self) -> str:
        """生成当前 Screen 的完整 ANSI 快照。

        用于新客户端连接时一次性恢复终端状态。
        """
        return self._full_screen_render()

    def resize(self, cols: int, rows: int) -> None:
        """调整虚拟 Screen 尺寸。"""
        cols = max(cols, 1)
        rows = max(rows, 1)
        if cols != self._cols or rows != self._rows:
            self._screen.resize(rows, cols)
            self._cols = cols
            self._rows = rows
            self._screen.dirty.clear()
            logger.debug("虚拟终端 resize: %dx%d", cols, rows)

    @property
    def mouse_tracking_enabled(self) -> bool:
        """远端是否启用了鼠标追踪模式。

        通过检测 pyte Screen.mode 中的 DEC Private Mode 判断：
        - ?1000 = 基本鼠标追踪（按下/释放）
        - ?1002 = 按钮事件追踪（含拖动）
        - ?1003 = 任意事件追踪（含移动）

        任一启用即返回 True，此时应放行前端发来的鼠标事件序列到 PTY。
        未启用时，前端鼠标事件应被过滤，由 xterm.js 本地处理（如滚动 scrollback）。
        """
        return bool(self._screen.mode & self._MOUSE_TRACKING_MODES)

    @property
    def alternate_screen_active(self) -> bool:
        """远端是否处于 Alternate Screen Buffer 模式。

        全屏程序（vim/top/less/htop 等）进入时会发送 ?1049h（或 ?1047h/?47h）
        切换到 alternate screen，退出时发送 ?1049l 回到 normal screen。

        Normal Screen → 输出应直通原始 ANSI（xterm.js 自然滚动）
        Alternate Screen → 输出应使用差分渲染（全屏程序无需 scrollback）
        """
        return bool(self._screen.mode & self._ALTERNATE_SCREEN_MODES)

    def feed_only(self, text: str) -> None:
        """仅将 PTY 输出 feed 到虚拟 Screen，不做差分渲染。

        用于 Normal Screen 直通模式下：原始 ANSI 直接发给 xterm.js，
        但仍需 feed 到 pyte 以保持内部状态同步（mouse_tracking_enabled、
        alternate_screen_active 检测、full_screen_dump 快照等）。
        """
        self._stream.feed(text)
        # 丢弃 dirty（直通模式不需要差分渲染）
        self._screen.dirty.clear()

    def _diff_render(self, dirty_lines: list[int]) -> str:
        """差分渲染：只输出变更的行。"""
        parts: list[str] = []

        for line_no in dirty_lines:
            if line_no >= self._rows:
                continue
            # 光标移动到行首
            parts.append(f"\x1b[{line_no + 1};1H")
            # 清除整行
            parts.append("\x1b[2K")
            # 渲染行内容
            parts.append(self._render_line(line_no))

        # 恢复光标到实际位置
        parts.append(self._cursor_position_seq())
        return "".join(parts)

    def _full_screen_render(self) -> str:
        """全屏渲染：输出完整 Screen 内容。"""
        parts: list[str] = []

        # 重置终端状态
        parts.append("\x1b[H")    # 光标归位
        parts.append("\x1b[2J")   # 清屏
        parts.append("\x1b[H")    # 再次归位（某些终端需要）

        for line_no in range(self._rows):
            if line_no > 0:
                parts.append("\r\n")
            parts.append(self._render_line(line_no))

        # 恢复光标位置
        parts.append(self._cursor_position_seq())
        return "".join(parts)

    def _render_line(self, line_no: int) -> str:
        """将 Screen 的一行转换为带 ANSI 属性的文本。

        注意宽字符处理：CJK 全角字符在 pyte 中占 2 列，第二列为占位符
        （char.data == ""）。渲染时必须跳过占位符列，否则会多输出一个空格。
        """
        buffer = self._screen.buffer
        if line_no not in buffer:
            return ""

        line = buffer[line_no]
        parts: list[str] = []
        prev_sgr = ""
        skip_next = False

        for col in range(self._cols):
            if skip_next:
                skip_next = False
                continue

            char = line.get(col, self._screen.default_char)
            sgr = self._build_sgr(char)
            if sgr != prev_sgr:
                parts.append(sgr)
                prev_sgr = sgr

            data = char.data if char.data else " "
            parts.append(data)

            # 如果当前字符是宽字符（占 2 列），跳过下一列的占位符
            if char.data and unicodedata.east_asian_width(char.data[0]) in ("W", "F"):
                skip_next = True

        # 行尾重置属性
        parts.append("\x1b[0m")
        return "".join(parts)

    def _cursor_position_seq(self) -> str:
        """生成光标定位 ANSI 序列。"""
        cursor = self._screen.cursor
        return f"\x1b[{cursor.y + 1};{cursor.x + 1}H"

    @staticmethod
    def _build_sgr(char: pyte.screens.Char) -> str:
        """根据 Char 属性构建 SGR (Select Graphic Rendition) 序列。"""
        codes: list[str] = []

        if char.bold:
            codes.append("1")
        if char.italics:
            codes.append("3")
        if char.underscore:
            codes.append("4")
        if getattr(char, "blink", False):
            codes.append("5")
        if char.reverse:
            codes.append("7")
        if char.strikethrough:
            codes.append("9")

        fg = _color_to_sgr(char.fg, foreground=True)
        if fg:
            codes.append(fg)

        bg = _color_to_sgr(char.bg, foreground=False)
        if bg:
            codes.append(bg)

        if not codes:
            return "\x1b[0m"
        return f"\x1b[{';'.join(codes)}m"
