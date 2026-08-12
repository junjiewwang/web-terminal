"""PTY 文本处理工具测试 — ANSI 清洗 / tmux 状态栏过滤。"""

from __future__ import annotations

from src.services.pty_session import is_tmux_status_line, strip_ansi, strip_tmux_status


class TestStripAnsi:
    def test_plain_text_unchanged(self):
        assert strip_ansi("hello world") == "hello world"

    def test_removes_sgr_color_codes(self):
        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_removes_bold_and_reset(self):
        assert strip_ansi("\x1b[1mbold\x1b[22m normal") == "bold normal"

    def test_removes_cursor_movement(self):
        assert strip_ansi("\x1b[2J\x1b[Hclear") == "clear"

    def test_removes_osc_title_sequence(self):
        assert strip_ansi("\x1b]0;window title\x07prompt$ ") == "prompt$ "

    def test_removes_charset_selection(self):
        assert strip_ansi("\x1b(Btext") == "text"

    def test_removes_keypad_mode(self):
        assert strip_ansi("\x1b=\x1b>text") == "text"

    def test_preserves_tab_newline_carriage_return(self):
        """\\t \\n \\r 是有意义的排版字符，必须保留。"""
        assert strip_ansi("a\tb\nc\rd") == "a\tb\nc\rd"

    def test_removes_other_control_characters(self):
        assert strip_ansi("a\x00b\x07c\x7f") == "abc"

    def test_realistic_colored_prompt(self):
        raw = "\x1b[01;32mroot@host\x1b[00m:\x1b[01;34m~\x1b[00m# "
        assert strip_ansi(raw) == "root@host:~# "

    def test_empty_string(self):
        assert strip_ansi("") == ""


class TestIsTmuxStatusLine:
    def test_detects_status_bar_line(self):
        line = '[wetty-tce0:sshpass*   "root@host:" 09:15 27-Mar-26'
        assert is_tmux_status_line(line) is True

    def test_ignores_normal_shell_output(self):
        assert is_tmux_status_line("total 48") is False

    def test_ignores_prompt(self):
        assert is_tmux_status_line("root@host:~# ls") is False

    def test_ignores_bracketed_text_without_timestamp(self):
        assert is_tmux_status_line("[INFO] service started") is False

    def test_tolerates_surrounding_whitespace(self):
        line = '   [wetty-x:win*  "h" 23:59 01-Jan-26   '
        assert is_tmux_status_line(line) is True


class TestStripTmuxStatus:
    def test_removes_status_line_keeps_content(self):
        text = 'line one\n[wetty-a:b*   "h" 09:15 27-Mar-26\nline two'
        assert strip_tmux_status(text) == "line one\nline two"

    def test_keeps_all_normal_lines(self):
        text = "alpha\nbeta\ngamma"
        assert strip_tmux_status(text) == text

    def test_removes_status_line_wrapped_in_ansi(self):
        """状态栏常带颜色码，去色后仍应被识别。"""
        text = 'ok\n\x1b[7m[wetty-a:b*   "h" 09:15 27-Mar-26\x1b[0m\ndone'
        assert strip_tmux_status(text) == "ok\ndone"

    def test_empty_string(self):
        assert strip_tmux_status("") == ""
