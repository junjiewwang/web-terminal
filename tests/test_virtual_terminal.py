"""虚拟终端渲染测试 — 差分渲染 / 全屏快照 / 模式检测 / resize。"""

from __future__ import annotations

import pytest

from src.services.virtual_terminal import VirtualTerminal, _color_to_sgr


# ══════════════════════════════════════════════
# 颜色转换
# ══════════════════════════════════════════════


class TestColorToSgr:
    def test_default_yields_empty(self):
        assert _color_to_sgr("default", True) == ""
        assert _color_to_sgr("default", False) == ""

    @pytest.mark.parametrize(
        "color,fg_code",
        [("black", "30"), ("red", "31"), ("green", "32"), ("blue", "34"), ("white", "37")],
    )
    def test_basic_foreground_colors(self, color, fg_code):
        assert _color_to_sgr(color, True) == fg_code

    def test_background_offset_is_40(self):
        assert _color_to_sgr("red", False) == "41"

    def test_brown_and_yellow_both_map_to_3(self):
        """pyte 用 brown 表示 yellow。"""
        assert _color_to_sgr("brown", True) == "33"
        assert _color_to_sgr("yellow", True) == "33"

    def test_bright_colors_use_90_range(self):
        assert _color_to_sgr("brightred", True) == "91"
        assert _color_to_sgr("brightred", False) == "101"

    def test_case_insensitive(self):
        assert _color_to_sgr("RED", True) == "31"

    def test_256_color_index(self):
        assert _color_to_sgr("123", True) == "38;5;123"
        assert _color_to_sgr("123", False) == "48;5;123"

    def test_index_bounds(self):
        assert _color_to_sgr("0", True) == "38;5;0"
        assert _color_to_sgr("255", True) == "38;5;255"

    def test_out_of_range_index_ignored(self):
        assert _color_to_sgr("999", True) == ""

    def test_unknown_color_ignored(self):
        assert _color_to_sgr("chartreuse", True) == ""


# ══════════════════════════════════════════════
# 基本属性 / resize
# ══════════════════════════════════════════════


class TestDimensions:
    def test_defaults(self):
        vt = VirtualTerminal()
        assert (vt.cols, vt.rows) == (80, 24)

    def test_custom_size(self):
        vt = VirtualTerminal(cols=120, rows=40)
        assert (vt.cols, vt.rows) == (120, 40)

    def test_zero_and_negative_clamped_to_one(self):
        assert (VirtualTerminal(cols=0, rows=0).cols, VirtualTerminal(cols=0, rows=0).rows) == (1, 1)
        vt = VirtualTerminal(cols=-5, rows=-5)
        assert (vt.cols, vt.rows) == (1, 1)


class TestResize:
    def test_updates_dimensions(self):
        vt = VirtualTerminal(80, 24)
        vt.resize(100, 30)
        assert (vt.cols, vt.rows) == (100, 30)

    def test_clamps_to_minimum_one(self):
        vt = VirtualTerminal(80, 24)
        vt.resize(0, 0)
        assert (vt.cols, vt.rows) == (1, 1)

    def test_same_size_is_noop(self):
        vt = VirtualTerminal(80, 24)
        vt.resize(80, 24)
        assert (vt.cols, vt.rows) == (80, 24)

    def test_content_survives_resize(self):
        vt = VirtualTerminal(80, 24)
        vt.feed_and_render("hello")
        vt.resize(100, 30)
        assert "hello" in vt.full_screen_dump()


# ══════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════


class TestFeedAndRender:
    def test_plain_text_appears(self):
        vt = VirtualTerminal(40, 10)
        out = vt.feed_and_render("hello world")
        assert "hello world" in out

    def test_no_change_returns_empty(self):
        """第二次 feed 空串没有 dirty 行，应返回空。"""
        vt = VirtualTerminal(40, 10)
        vt.feed_and_render("hello")
        assert vt.feed_and_render("") == ""

    def test_diff_render_targets_changed_line(self):
        vt = VirtualTerminal(40, 10)
        vt.feed_and_render("line1\r\nline2\r\n")
        out = vt.feed_and_render("line3")
        # 差分渲染包含定位 + 清行序列
        assert "\x1b[" in out
        assert "\x1b[2K" in out

    def test_full_dump_fallback_on_heavy_change(self):
        """dirty 行占比超过阈值时回退全屏渲染（含归位序列）。"""
        vt = VirtualTerminal(20, 5)
        out = vt.feed_and_render("\r\n".join(f"row{i}" for i in range(5)))
        assert "\x1b[H" in out

    def test_carriage_return_overwrites_line(self):
        vt = VirtualTerminal(40, 5)
        vt.feed_and_render("first\r")
        vt.feed_and_render("second")
        dump = vt.full_screen_dump()
        assert "second" in dump
        assert "first" not in dump

    def test_ansi_color_is_parsed_and_reemitted(self):
        """颜色被 pyte 解析进字符属性，渲染时按属性重新生成 SGR。"""
        vt = VirtualTerminal(40, 5)
        vt.feed_and_render("\x1b[31mRED\x1b[0m")
        dump = vt.full_screen_dump()

        assert "RED" in dump
        assert "\x1b[31m" in dump, "红色属性应被保留并重新输出"
        # 渲染结果由属性生成，而非原样透传：行尾会补上重置序列
        assert dump.count("\x1b[0m") >= 1


class TestFullScreenDump:
    def test_contains_home_sequence(self):
        vt = VirtualTerminal(20, 3)
        vt.feed_and_render("hi")
        assert "\x1b[H" in vt.full_screen_dump()

    def test_is_repeatable(self):
        vt = VirtualTerminal(20, 3)
        vt.feed_and_render("stable")
        assert vt.full_screen_dump() == vt.full_screen_dump()

    def test_reflects_latest_content(self):
        vt = VirtualTerminal(20, 3)
        vt.feed_and_render("old\r\n")
        vt.feed_and_render("new")
        assert "new" in vt.full_screen_dump()


class TestFeedOnly:
    def test_returns_none_and_clears_dirty(self):
        vt = VirtualTerminal(40, 5)
        assert vt.feed_only("passthrough") is None
        # dirty 已清空，随后 feed 空串无输出
        assert vt.feed_and_render("") == ""

    def test_still_updates_internal_screen(self):
        """直通模式仍需同步 pyte 状态，供快照使用。"""
        vt = VirtualTerminal(40, 5)
        vt.feed_only("recorded")
        assert "recorded" in vt.full_screen_dump()


# ══════════════════════════════════════════════
# DEC Private Mode 检测
# ══════════════════════════════════════════════


class TestMouseTracking:
    def test_disabled_by_default(self):
        assert VirtualTerminal().mouse_tracking_enabled is False

    @pytest.mark.parametrize("mode", ["1000", "1002", "1003"])
    def test_enabled_after_set_sequence(self, mode):
        vt = VirtualTerminal()
        vt.feed_only(f"\x1b[?{mode}h")
        assert vt.mouse_tracking_enabled is True

    def test_disabled_after_reset_sequence(self):
        vt = VirtualTerminal()
        vt.feed_only("\x1b[?1000h")
        vt.feed_only("\x1b[?1000l")
        assert vt.mouse_tracking_enabled is False

    def test_unrelated_mode_does_not_enable(self):
        vt = VirtualTerminal()
        vt.feed_only("\x1b[?25h")  # 显示光标
        assert vt.mouse_tracking_enabled is False


class TestAlternateScreen:
    def test_inactive_by_default(self):
        assert VirtualTerminal().alternate_screen_active is False

    @pytest.mark.parametrize("mode", ["1049", "1047", "47"])
    def test_active_after_switch(self, mode):
        vt = VirtualTerminal()
        vt.feed_only(f"\x1b[?{mode}h")
        assert vt.alternate_screen_active is True

    def test_inactive_after_exit(self):
        """vim/top 退出时发送 ?1049l 回到 normal screen。"""
        vt = VirtualTerminal()
        vt.feed_only("\x1b[?1049h")
        vt.feed_only("\x1b[?1049l")
        assert vt.alternate_screen_active is False

    def test_mouse_and_alternate_are_independent(self):
        vt = VirtualTerminal()
        vt.feed_only("\x1b[?1049h")
        assert vt.alternate_screen_active is True
        assert vt.mouse_tracking_enabled is False
