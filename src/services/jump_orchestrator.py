"""多跳连接编排引擎

负责按 root -> ... -> target 的路径顺序执行每一跳：
1. 等待当前节点 ready_pattern
2. 执行子节点 entry（menu_send / ssh_command）
3. 执行 entry.steps（wait → send）
4. 等待 success_pattern，确认进入下一跳成功

说明：
- 文件名保留为 jump_orchestrator.py，便于与现有模块关系保持稳定；
  但内部实现已升级为通用多跳连接编排器。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from src.models.host import EntrySpecSchema, EntryType, Host, LoginStepSchema

logger = logging.getLogger(__name__)

_DEFAULT_READY_PATTERN = r"[\$#>%]\s*$|Opt>|password:|Password:"
_DEFAULT_SUCCESS_PATTERN = r"Last login|[\$#>%]\s*$"
_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


@dataclass
class ConnectionResult:
    """连接编排结果"""

    success: bool
    message: str
    window_name: Optional[str] = None
    actions_executed: int = 0
    skipped_reason: Optional[str] = None


def _resolve_variables(text: str, password: Optional[str] = None) -> tuple[str, bool]:
    """解析步骤中的变量占位符。"""
    needs_manual = False

    def _replacer(match: re.Match) -> str:
        nonlocal needs_manual
        var_name = match.group(1)
        if var_name == "password":
            return password or ""
        if var_name == "manual":
            needs_manual = True
            return ""
        logger.warning("未知变量: {{%s}}，保持原样", var_name)
        return match.group(0)

    resolved = _VAR_PATTERN.sub(_replacer, text)
    return resolved, needs_manual


class ConnectionOrchestrator:
    """通用多跳连接编排器。"""

    DEFAULT_READY_TIMEOUT = 15.0
    DEFAULT_STEP_TIMEOUT = 15.0
    DEFAULT_LOGIN_TIMEOUT = 30.0

    def __init__(self, pty_session) -> None:
        self._session = pty_session

    async def execute_path(
        self,
        path: Sequence[Host],
        tmux_session_name: str,
        window_name: str,
        skip_window_creation: bool = False,
    ) -> ConnectionResult:
        """按路径依次进入目标节点。"""
        if not path:
            return ConnectionResult(success=False, message="连接路径为空", window_name=window_name)

        if len(path) == 1:
            return ConnectionResult(success=True, message="根节点会话已就绪", window_name=window_name)

        logger.info(
            "开始多跳编排: %s (window=%s, skip_create=%s)",
            " -> ".join(node.name for node in path),
            window_name,
            skip_window_creation,
        )

        if not skip_window_creation:
            try:
                await self._create_tmux_window(tmux_session_name, window_name)
            except Exception as e:
                return ConnectionResult(
                    success=False,
                    message=f"创建 tmux 窗口失败: {e}",
                    window_name=window_name,
                )

        actions_executed = 0
        current = path[0]

        for next_node in path[1:]:
            ready_pattern = current.ready_pattern or _DEFAULT_READY_PATTERN
            try:
                await self._wait_for_pattern(ready_pattern, self.DEFAULT_READY_TIMEOUT)
            except TimeoutError:
                return ConnectionResult(
                    success=False,
                    message=f"等待节点 '{current.name}' 就绪超时 ({self.DEFAULT_READY_TIMEOUT}s)，未匹配到: {ready_pattern}",
                    window_name=window_name,
                    actions_executed=actions_executed,
                )

            result = await self._enter_node(next_node)
            actions_executed += result.actions_executed

            if not result.success:
                return ConnectionResult(
                    success=False,
                    message=f"进入节点 '{next_node.name}' 失败: {result.message}",
                    window_name=window_name,
                    actions_executed=actions_executed,
                    skipped_reason=result.skipped_reason,
                )

            if result.skipped_reason == "manual_input_required":
                return ConnectionResult(
                    success=True,
                    message=f"已进入到需要人工输入的步骤，请在浏览器终端中继续完成：{next_node.name}",
                    window_name=window_name,
                    actions_executed=actions_executed,
                    skipped_reason=result.skipped_reason,
                )

            current = next_node

        return ConnectionResult(
            success=True,
            message=f"多跳连接成功，已到达目标节点 '{current.name}'",
            window_name=window_name,
            actions_executed=actions_executed,
        )

    async def _enter_node(self, node: Host) -> ConnectionResult:
        entry = self._parse_entry_spec(node)
        if entry.type == EntryType.NONE or not entry.value:
            return ConnectionResult(success=False, message=f"节点 '{node.name}' 缺少有效入口动作")

        await self._send_text(entry.value)
        actions_executed = 1
        logger.info("执行入口动作: %s -> %s (%s)", entry.type.value, node.name, entry.value)

        entry_password = self._decrypt_entry_password(node)
        success_pattern = entry.success_pattern or node.ready_pattern or _DEFAULT_SUCCESS_PATTERN

        for idx, step in enumerate(entry.steps, start=1):
            step_result = await self._execute_step(
                step=step,
                success_pattern=success_pattern,
                password=entry_password,
                step_num=idx,
                total_steps=len(entry.steps),
            )
            actions_executed += step_result.actions_executed
            if step_result.skipped_reason:
                return ConnectionResult(
                    success=True,
                    message=step_result.message,
                    actions_executed=actions_executed,
                    skipped_reason=step_result.skipped_reason,
                )
            if not step_result.success:
                return ConnectionResult(
                    success=False,
                    message=step_result.message,
                    actions_executed=actions_executed,
                )

        try:
            await self._wait_for_pattern(success_pattern, self.DEFAULT_LOGIN_TIMEOUT)
        except TimeoutError:
            return ConnectionResult(
                success=False,
                message=f"等待节点 '{node.name}' 进入成功超时 ({self.DEFAULT_LOGIN_TIMEOUT}s)，未匹配到: {success_pattern}",
                actions_executed=actions_executed,
            )

        return ConnectionResult(success=True, message="OK", actions_executed=actions_executed)

    async def _execute_step(
        self,
        step: LoginStepSchema,
        success_pattern: str,
        password: Optional[str],
        step_num: int,
        total_steps: int,
    ) -> ConnectionResult:
        combined_pattern = f"(?P<step_wait>{step.wait})|(?P<login_ok>{success_pattern})"
        timeout = step.timeout or self.DEFAULT_STEP_TIMEOUT

        logger.info(
            "执行附加步骤 %d/%d: wait='%s', timeout=%.0fs",
            step_num,
            total_steps,
            step.wait,
            timeout,
        )

        try:
            output = await self._session.wait_for(pattern=combined_pattern, timeout=timeout)
        except TimeoutError:
            return ConnectionResult(success=False, message=f"等待步骤超时 ({timeout}s): {step.wait}")

        match = re.search(combined_pattern, output, re.MULTILINE)
        if match and match.group("login_ok"):
            return ConnectionResult(
                success=True,
                message="提前检测到登录成功",
                skipped_reason=f"matched_success_pattern:{match.group('login_ok')[:50]}",
            )

        send_text, needs_manual = _resolve_variables(step.send, password)
        if needs_manual:
            logger.info("步骤 %d/%d 需要人工输入", step_num, total_steps)
            return ConnectionResult(
                success=True,
                message=f"步骤 {step_num} 需要人工输入（如验证码 / MFA）",
                skipped_reason="manual_input_required",
            )

        await self._send_text(send_text)
        return ConnectionResult(success=True, message="OK", actions_executed=1)

    async def _create_tmux_window(self, tmux_session: str, window_name: str) -> None:
        cmd = f"tmux new-window -t {tmux_session} -n {window_name}\r"
        await self._session.send_input(cmd)

        import asyncio
        await asyncio.sleep(0.5)

        select_cmd = f"tmux select-window -t {tmux_session}:{window_name}\r"
        await self._session.send_input(select_cmd)
        await asyncio.sleep(0.5)
        logger.info("tmux 窗口已创建: %s:%s", tmux_session, window_name)

    async def _send_text(self, text: str) -> None:
        if not text.endswith("\r") and not text.endswith("\n"):
            text = f"{text}\r"
        await self._session.send_input(text)

    async def _wait_for_pattern(self, pattern: str, timeout: float) -> None:
        await self._session.wait_for(pattern=pattern, timeout=timeout)

    @staticmethod
    def _parse_entry_spec(node: Host) -> EntrySpecSchema:
        raw = node.get_entry_spec()
        if not raw:
            return EntrySpecSchema()
        try:
            return EntrySpecSchema(**raw)
        except Exception:
            logger.warning("节点 %s 的 entry_spec 解析失败，使用默认值", node.name)
            return EntrySpecSchema()

    @staticmethod
    def _decrypt_entry_password(node: Host) -> Optional[str]:
        if not node.entry_password_encrypted:
            return None
        try:
            from src.utils.security import decrypt_password
            return decrypt_password(node.entry_password_encrypted)
        except Exception as e:
            logger.warning("节点 %s 的 entry_password 解密失败: %s", node.name, e)
            return None


# 兼容旧导入名，避免局部重构时出现大范围断裂
JumpOrchestrator = ConnectionOrchestrator
JumpResult = ConnectionResult
