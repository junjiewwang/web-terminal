"""跳板连接编排引擎

编排堡垒机 → 二级主机的自动跳转流程：
1. 在堡垒机 tmux 会话中创建新窗口
2. 发送目标 IP
3. 执行 login_steps 步骤链（wait → send 原子操作）
4. 等待登录成功标志

设计原则：
- 每步同时检测 login_success_pattern，提前匹配则跳过剩余步骤
- 支持变量替换：{{password}} → 密码, {{manual}} → 暂停等待人工输入
- 超时和错误信息清晰，方便 Agent 理解和重试
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from src.models.host import Host, JumpHostConfigSchema, LoginStepSchema
from src.services.pty_session import PTYSession

logger = logging.getLogger(__name__)


# ── 编排结果 ──────────────────────────────────


@dataclass
class JumpResult:
    """跳板连接编排结果"""

    success: bool
    message: str
    window_name: Optional[str] = None
    steps_executed: int = 0
    skipped_reason: Optional[str] = None


# ── 变量替换 ──────────────────────────────────

# 变量占位符正则
_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def _resolve_variables(text: str, password: Optional[str] = None) -> tuple[str, bool]:
    """解析 login_steps 中的变量占位符

    Args:
        text: 含变量的文本（如 "{{password}}"）
        password: jump_host 配置的密码

    Returns:
        (resolved_text, needs_manual) - 解析后的文本 + 是否需要人工输入
    """
    needs_manual = False

    def _replacer(match: re.Match) -> str:
        nonlocal needs_manual
        var_name = match.group(1)
        if var_name == "password":
            return password or ""
        if var_name == "manual":
            needs_manual = True
            return ""  # manual 变量不替换，由调用方处理
        logger.warning("未知变量: {{%s}}，保持原样", var_name)
        return match.group(0)

    resolved = _VAR_PATTERN.sub(_replacer, text)
    return resolved, needs_manual


# ── 编排引擎 ──────────────────────────────────


class JumpOrchestrator:
    """跳板连接编排器

    协调 PTYSession 和 tmux 窗口管理，执行堡垒机跳板流程。
    """

    # 默认超时配置
    DEFAULT_READY_TIMEOUT = 15.0
    DEFAULT_STEP_TIMEOUT = 15.0
    DEFAULT_LOGIN_TIMEOUT = 30.0

    def __init__(self, pty_session: PTYSession) -> None:
        self._session = pty_session

    async def execute_jump(
        self,
        jump_host: Host,
        bastion: Host,
        tmux_session_name: str,
        window_name: str,
        skip_window_creation: bool = False,
    ) -> JumpResult:
        """执行完整的跳板连接编排

        流程：
        1. 在堡垒机 tmux session 中创建新窗口（可跳过）
        2. 等待堡垒机就绪（ready_pattern）
        3. 发送目标 IP
        4. 执行 login_steps（如有）
        5. 等待登录成功

        Args:
            jump_host: 二级主机 ORM 对象
            bastion: 父堡垒机 ORM 对象
            tmux_session_name: 堡垒机的 tmux 会话名
            window_name: 要创建的 tmux 窗口名
            skip_window_creation: 是否跳过 tmux 窗口创建。
                当调用方已通过 TmuxWindowManager.create_window(command=ssh_cmd)
                创建了带 SSH 命令的窗口时，应设为 True，避免重复创建。

        Returns:
            JumpResult 编排结果
        """
        # 解析堡垒机配置
        jump_config = self._parse_jump_config(bastion)
        login_steps = self._parse_login_steps(jump_host)

        # 解密 jump_host 密码（用于 {{password}} 变量替换）
        jump_password = self._decrypt_jump_password(jump_host)

        logger.info(
            "开始跳板编排: %s → %s (window=%s, steps=%d, skip_create=%s)",
            bastion.name, jump_host.name, window_name, len(login_steps),
            skip_window_creation,
        )

        # Step 1: 创建 tmux 新窗口（可跳过）
        if not skip_window_creation:
            try:
                await self._create_tmux_window(tmux_session_name, window_name)
            except Exception as e:
                return JumpResult(
                    success=False,
                    message=f"创建 tmux 窗口失败: {e}",
                    window_name=window_name,
                )

        # Step 2: 等待堡垒机就绪
        try:
            await self._wait_for_ready(jump_config.ready_pattern)
        except TimeoutError:
            return JumpResult(
                success=False,
                message=f"等待堡垒机就绪超时 ({self.DEFAULT_READY_TIMEOUT}s)，"
                        f"未匹配到 ready_pattern: {jump_config.ready_pattern}",
                window_name=window_name,
            )

        # Step 3: 发送目标 IP
        target_ip = jump_host.target_ip
        if not target_ip:
            return JumpResult(
                success=False,
                message=f"二级主机 {jump_host.name} 未配置 target_ip",
                window_name=window_name,
            )

        await self._session.send_input(f"{target_ip}\r")
        logger.info("已发送目标 IP: %s", target_ip)

        # Step 4: 执行 login_steps
        steps_executed = 0
        for i, step in enumerate(login_steps):
            result = await self._execute_step(
                step, i + 1, len(login_steps),
                jump_config.login_success_pattern,
                jump_password,
            )

            if result.skipped_reason:
                # 提前匹配到登录成功，跳过剩余步骤
                logger.info(
                    "步骤 %d/%d 检测到登录成功，跳过剩余: %s",
                    i + 1, len(login_steps), result.skipped_reason,
                )
                return JumpResult(
                    success=True,
                    message=f"跳板连接成功（提前匹配登录标志，执行了 {steps_executed} 步）",
                    window_name=window_name,
                    steps_executed=steps_executed,
                    skipped_reason=result.skipped_reason,
                )

            if not result.success:
                return JumpResult(
                    success=False,
                    message=f"步骤 {i + 1}/{len(login_steps)} 失败: {result.message}",
                    window_name=window_name,
                    steps_executed=steps_executed,
                )

            steps_executed += 1

        # Step 5: 等待登录成功
        try:
            await self._session.wait_for(
                pattern=jump_config.login_success_pattern,
                timeout=self.DEFAULT_LOGIN_TIMEOUT,
            )
        except TimeoutError:
            return JumpResult(
                success=False,
                message=f"等待登录成功超时 ({self.DEFAULT_LOGIN_TIMEOUT}s)，"
                        f"未匹配到: {jump_config.login_success_pattern}",
                window_name=window_name,
                steps_executed=steps_executed,
            )

        logger.info(
            "跳板编排完成: %s → %s (执行了 %d 步)",
            bastion.name, jump_host.name, steps_executed,
        )

        return JumpResult(
            success=True,
            message=f"跳板连接成功（执行了 {steps_executed} 步）",
            window_name=window_name,
            steps_executed=steps_executed,
        )

    # ── 内部步骤 ──────────────────────────────────

    async def _create_tmux_window(self, tmux_session: str, window_name: str) -> None:
        """在 tmux session 中创建新窗口

        使用 send_input 通过 PTY 发送 tmux 命令，
        而不是直接 subprocess，因为 PTY 连接是到 WeTTY 内部的。

        Args:
            tmux_session: tmux 会话名
            window_name: 窗口名
        """
        # 通过 tmux 命令前缀键(C-b)创建新窗口不可靠，
        # 改为在 PTY 中直接执行 tmux new-window 命令
        cmd = f"tmux new-window -t {tmux_session} -n {window_name}\r"
        await self._session.send_input(cmd)

        # 短暂等待窗口创建
        import asyncio
        await asyncio.sleep(0.5)

        # 切换到新窗口
        select_cmd = f"tmux select-window -t {tmux_session}:{window_name}\r"
        await self._session.send_input(select_cmd)
        await asyncio.sleep(0.5)

        logger.info("tmux 窗口已创建: %s:%s", tmux_session, window_name)

    async def _wait_for_ready(self, ready_pattern: str) -> None:
        """等待堡垒机就绪标志"""
        await self._session.wait_for(
            pattern=ready_pattern,
            timeout=self.DEFAULT_READY_TIMEOUT,
        )

    async def _execute_step(
        self,
        step: LoginStepSchema,
        step_num: int,
        total_steps: int,
        login_success_pattern: str,
        password: Optional[str],
    ) -> JumpResult:
        """执行单个 login_step

        同时检测 step.wait 和 login_success_pattern：
        - 匹配 step.wait → 发送 step.send
        - 匹配 login_success_pattern → 跳过（已登录成功）

        Args:
            step: LoginStepSchema 步骤
            step_num: 当前步骤编号
            total_steps: 总步骤数
            login_success_pattern: 登录成功标志
            password: 密码（用于 {{password}} 替换）
        """
        # 组合模式：step.wait OR login_success_pattern
        combined_pattern = f"(?P<step_wait>{step.wait})|(?P<login_ok>{login_success_pattern})"
        timeout = step.timeout or self.DEFAULT_STEP_TIMEOUT

        logger.info(
            "执行步骤 %d/%d: wait='%s', timeout=%.0fs",
            step_num, total_steps, step.wait, timeout,
        )

        try:
            output = await self._session.wait_for(
                pattern=combined_pattern,
                timeout=timeout,
            )
        except TimeoutError:
            return JumpResult(
                success=False,
                message=f"等待模式超时 ({timeout}s): {step.wait}",
            )

        # 判断匹配的是哪个分组
        match = re.search(combined_pattern, output, re.MULTILINE)
        if match and match.group("login_ok"):
            return JumpResult(
                success=True,
                message="提前检测到登录成功",
                skipped_reason=f"匹配到 login_success_pattern: {match.group('login_ok')[:50]}",
            )

        # 匹配到 step.wait，解析并发送 send 内容
        send_text, needs_manual = _resolve_variables(step.send, password)

        if needs_manual:
            logger.info(
                "步骤 %d/%d 需要人工输入（{{manual}}变量），暂停编排",
                step_num, total_steps,
            )
            return JumpResult(
                success=True,
                message=f"步骤 {step_num} 需要人工输入（如 MFA 验证码），"
                        "请在浏览器终端中手动完成",
                skipped_reason="manual_input_required",
            )

        # 发送内容（自动添加回车）
        if not send_text.endswith("\r") and not send_text.endswith("\n"):
            send_text += "\r"

        await self._session.send_input(send_text)
        logger.info("步骤 %d/%d 已发送: %s", step_num, total_steps, repr(send_text[:30]))

        return JumpResult(success=True, message="OK", steps_executed=1)

    # ── 辅助方法 ──────────────────────────────────

    @staticmethod
    def _parse_jump_config(bastion: Host) -> JumpHostConfigSchema:
        """解析堡垒机的 jump_config，使用默认值兜底"""
        raw = bastion.get_jump_config()
        if raw:
            try:
                return JumpHostConfigSchema(**raw)
            except Exception:
                logger.warning("堡垒机 %s 的 jump_config 解析失败，使用默认值", bastion.name)
        return JumpHostConfigSchema()

    @staticmethod
    def _parse_login_steps(jump_host: Host) -> list[LoginStepSchema]:
        """解析二级主机的 login_steps"""
        raw_steps = jump_host.get_login_steps()
        if not raw_steps:
            return []
        try:
            return [LoginStepSchema(**s) for s in raw_steps]
        except Exception:
            logger.warning("二级主机 %s 的 login_steps 解析失败", jump_host.name)
            return []

    @staticmethod
    def _decrypt_jump_password(jump_host: Host) -> Optional[str]:
        """解密二级主机的密码"""
        if not jump_host.password_encrypted:
            return None
        try:
            from src.utils.security import decrypt_password
            return decrypt_password(jump_host.password_encrypted)
        except Exception as e:
            logger.warning("二级主机 %s 密码解密失败: %s", jump_host.name, e)
            return None
