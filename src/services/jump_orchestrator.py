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

from src.models.host import EntrySpecSchema, EntryType, Host, InteractionMode, LoginStepSchema
from src.models.interaction_rule import InteractionRule
from src.services.event_service import AgentEvent, EventType, event_bus

logger = logging.getLogger(__name__)

_DEFAULT_READY_PATTERN = r"[\$#>%]\s*$|Opt>|password:|Password:"
_DEFAULT_SUCCESS_PATTERN = r"Last login|[\$#>%]\s*$"
_PASSWORD_PLACEHOLDER = "{{password}}"
_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


@dataclass
class ConnectionResult:
    """连接编排结果"""

    success: bool
    message: str
    window_name: Optional[str] = None
    actions_executed: int = 0
    skipped_reason: Optional[str] = None


_GLOBAL_INLINE_FLAGS_RE = re.compile(r"^(\s*\(\?[aiLmsux]+\))+")


def _wrap_inline_flags(pattern: str) -> str:
    """将正则中的全局 inline flags 转换为局部 flags 语法。

    例如: (?i)password → (?i:password)

    原因：组合匹配模式（命名分组嵌套）时，全局 flags 必须位于表达式开头，
    无法出现在分组内部，否则触发 re.error: global flags not at the start。
    """
    m = _GLOBAL_INLINE_FLAGS_RE.match(pattern)
    if not m:
        return pattern
    flags = "".join(dict.fromkeys(re.findall(r"[aiLmsux]", m.group(0))))
    rest = pattern[m.end():]
    return f"(?{flags}:{rest})"


def _resolve_variables(text: str, variables: Optional[dict[str, str]] = None) -> tuple[str, bool]:
    """解析步骤中的变量占位符。

    支持的变量：
    - {{password}}: 密码（由调用方提供）
    - {{user_id}}:  堡垒机用户选择 ID
    - {{username}}: 当前节点 SSH 用户名
    - {{manual}}:   人工输入标记（返回 needs_manual=True）

    Args:
        text: 含变量占位符的模板文本
        variables: 变量字典，如 {"password": "...", "user_id": "1"}

    Returns:
        (解析后的文本, 是否需要人工输入)
    """
    variables = variables or {}
    needs_manual = False

    def _replacer(match: re.Match[str]) -> str:
        nonlocal needs_manual
        var_name = match.group(1)
        if var_name == "manual":
            needs_manual = True
            return ""
        if var_name in variables:
            return variables[var_name]
        logger.warning("未知变量: {{%s}}，保持原样", var_name)
        return match.group(0)

    resolved = _VAR_PATTERN.sub(_replacer, text)
    return resolved, needs_manual


class ConnectionOrchestrator:
    """通用多跳连接编排器。"""

    DEFAULT_READY_TIMEOUT = 15.0
    DEFAULT_STEP_TIMEOUT = 15.0
    DEFAULT_LOGIN_TIMEOUT = 30.0
    MAX_AUTO_STEPS = 10  # 自动识别最大交互步数，防死循环

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
        await self._publish_action("发送入口值", entry.value, node)

        entry_password = await self._decrypt_entry_password(node)
        success_pattern = entry.success_pattern or node.ready_pattern or _DEFAULT_SUCCESS_PATTERN

        # 交互模式判定：
        # - steps 非空 → 手动执行 steps（精确控制）
        # - steps 空 + interaction_mode=auto → 规则引擎自动识别
        # - steps 空 + interaction_mode=manual → 直接等 success_pattern
        if entry.steps:
            for idx, step in enumerate(entry.steps, start=1):
                step_result = await self._execute_step(
                    node_name=node.name,
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
        elif entry.interaction_mode == InteractionMode.AUTO:
            rules = await self._load_enabled_rules(entry.rules)
            if rules:
                auto_result = await self._auto_interact(
                    node=node,
                    entry=entry,
                    success_pattern=success_pattern,
                    password=entry_password,
                    rules=rules,
                )
                auto_result.actions_executed += actions_executed
                return auto_result
            logger.info("节点 %s 无可用交互规则，直接等待 success_pattern", node.name)

        try:
            await self._wait_for_pattern(success_pattern, self.DEFAULT_LOGIN_TIMEOUT)
        except TimeoutError:
            return ConnectionResult(
                success=False,
                message=f"等待节点 '{node.name}' 进入成功超时 ({self.DEFAULT_LOGIN_TIMEOUT}s)，未匹配到: {success_pattern}",
                actions_executed=actions_executed,
            )

        return ConnectionResult(success=True, message="OK", actions_executed=actions_executed)

    async def _auto_interact(
        self,
        node: Host,
        entry: EntrySpecSchema,
        success_pattern: str,
        password: Optional[str],
        rules: list[InteractionRule],
    ) -> ConnectionResult:
        """使用交互规则库自动识别并完成登录交互。

        循环逻辑：
        1. 等待 success_pattern 或任一规则 wait 命中
        2. success_pattern 命中 → 登录成功
        3. 规则命中（按优先级取第一个未超 max_retries 的）→ 应用该规则
        4. 步数耗尽 → 人工兜底
        """
        applied: dict[str, int] = {}
        rule_vars = dict(entry.rule_vars or {})
        username = node.username or ""

        for step_count in range(self.MAX_AUTO_STEPS):
            # 构造组合匹配模式：login_ok + 各规则的命名分组
            alternatives: list[str] = []
            if success_pattern:
                alternatives.append(f"(?P<login_ok>{_wrap_inline_flags(success_pattern)})")
            for i, rule in enumerate(rules):
                alternatives.append(f"(?P<rule_{i}>{_wrap_inline_flags(rule.wait_pattern)})")
            combined = "|".join(alternatives)

            timeout = max((rule.timeout for rule in rules), default=self.DEFAULT_STEP_TIMEOUT)
            try:
                output = await self._session.wait_for(pattern=combined, timeout=timeout)
            except TimeoutError:
                return ConnectionResult(
                    success=False,
                    message=(
                        f"节点 '{node.name}' 自动识别超时 ({timeout}s)，"
                        f"未匹配到登录成功或任何交互提示"
                    ),
                )

            match = re.search(combined, output, re.MULTILINE)
            if match is None:
                # 防御性处理：wait_for 已匹配但 re.search 失败，继续等待
                continue

            # 1. 登录成功最高优先级
            login_ok_text = match.group("login_ok")
            if login_ok_text:
                # 单字符提示符（>、$、#）可能只是中间菜单/提示符（如 "Opt> "、"ID> "），
                # 而非真正的登录成功标志（如 "Last login"、"]#"、"]$"）。
                # 仅当是"强标志"（多字符）时才判定登录成功，避免误判提前结束。
                if len((login_ok_text or "").strip()) <= 1:
                    logger.info(
                        "节点 %s 的 success_pattern 匹配到单字符提示符 '%s'，可能为中间菜单，继续等待",
                        node.name, (login_ok_text or "").strip(),
                    )
                    continue
                await self._publish_action("登录成功", "", node)
                return ConnectionResult(success=True, message="自动识别登录成功")

            # 2. 按优先级取第一个可应用的规则
            matched_rule: InteractionRule | None = None
            for i, rule in enumerate(rules):
                if match.group(f"rule_{i}") and applied.get(rule.name, 0) < rule.max_retries:
                    matched_rule = rule
                    break

            if matched_rule is None:
                # 匹配到的规则均已耗尽，继续等待下一个提示
                logger.info("节点 %s 匹配的规则均已耗尽，继续等待", node.name)
                continue

            # 3. 密码缺失检查
            if _PASSWORD_PLACEHOLDER in matched_rule.send_text and not password:
                return ConnectionResult(
                    success=False,
                    message=(
                        f"节点 '{node.name}' 的规则 '{matched_rule.name}' 需要密码，"
                        "但未配置 credential_ref 或 entry_password"
                    ),
                )

            # 4. 变量解析
            send_text, needs_manual = _resolve_variables(
                matched_rule.send_text,
                {
                    "password": password or "",
                    "user_id": rule_vars.get("user_id", ""),
                    "username": username,
                },
            )
            if needs_manual:
                return ConnectionResult(
                    success=True,
                    message=f"规则 '{matched_rule.name}' 需要人工输入",
                    skipped_reason="manual_input_required",
                )

            await self._send_text(send_text)
            applied[matched_rule.name] = applied.get(matched_rule.name, 0) + 1
            logger.info(
                "自动识别应用规则: %s -> %s (第 %d 次)",
                matched_rule.name, node.name, applied[matched_rule.name],
            )
            # 发布事件到操作轨迹（不含 send_text，避免泄露密码）
            await self._publish_action(
                f"应用规则 {matched_rule.name}",
                matched_rule.description or matched_rule.wait_pattern,
                node,
            )

        # 步数耗尽：人工兜底
        return ConnectionResult(
            success=True,
            message=f"节点 '{node.name}' 自动识别达到最大步数 ({self.MAX_AUTO_STEPS})，请人工接管",
            skipped_reason="manual_input_required",
        )

    async def _execute_step(
        self,
        node_name: str,
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

        if _PASSWORD_PLACEHOLDER in step.send and not password:
            logger.warning("节点 %s 的步骤 %d/%d 需要密码，但未配置 entry_password 或 credential_ref", node_name, step_num, total_steps)
            return ConnectionResult(
                success=False,
                message=(
                    f"节点 '{node_name}' 的步骤 {step_num} 需要密码，但未配置 entry_password 或 credential_ref，"
                    "已阻止发送空密码"
                ),
            )

        send_text, needs_manual = _resolve_variables(step.send, {"password": password or ""})
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

    async def _publish_action(self, action: str, detail: str, node: Host) -> None:
        """将自动识别动作实时展示到终端屏幕 + 操作轨迹面板。

        仅描述动作与规则元信息（名称/说明/正则），绝不包含 send_text，
        避免泄露密码等敏感内容。展示失败不影响连接流程。
        """
        command = f"[自动识别] {action}"
        if detail:
            command = f"{command} · {detail}"

        # 1. 注入终端屏幕（浏览器 xterm 画面，不经过 PTY、不影响远端）
        try:
            notice_text = action + (f" · {detail}" if detail else "")
            self._session.broadcast_notice(notice_text)
        except Exception as e:  # noqa: BLE001
            logger.debug("注入终端提示失败: %s", e)

        # 2. 发布事件到操作轨迹面板
        try:
            session_id = getattr(self._session, "session_id", "") or ""
            await event_bus.publish(AgentEvent(
                event_type=EventType.COMMAND_START,
                session_id=session_id,
                host_name=node.name,
                data={"command": command},
            ))
        except Exception as e:  # noqa: BLE001
            logger.debug("发布自动识别事件失败: %s", e)

    async def _wait_for_pattern(self, pattern: str, timeout: float) -> None:
        await self._session.wait_for(pattern=pattern, timeout=timeout)

    @staticmethod
    async def _load_enabled_rules(names: list[str] | None = None) -> list[InteractionRule]:
        """从数据库加载启用的交互规则，按优先级降序。

        Args:
            names: 规则名称列表过滤；None 表示全部启用规则。

        容错：数据库访问失败或表不存在时降级返回空列表，
        自动识别退化为「直接等待 success_pattern」，不影响基础连接。
        """
        try:
            from src.models.database import async_session_factory
            from src.services.interaction_rule_service import InteractionRuleService

            async with async_session_factory() as db_session:
                service = InteractionRuleService(db_session)
                return await service.list_enabled(names)
        except Exception as e:
            logger.warning("加载交互规则失败，自动识别降级为直接等待 success_pattern: %s", e)
            return []

    @staticmethod
    def _parse_entry_spec(node: Host) -> EntrySpecSchema:
        raw = node.get_entry_spec()
        if not raw:
            return EntrySpecSchema()
        try:
            return EntrySpecSchema.model_validate(raw)
        except Exception:
            logger.warning("节点 %s 的 entry_spec 解析失败，使用默认值", node.name)
            return EntrySpecSchema()

    async def _decrypt_entry_password(self, node: Host) -> Optional[str]:
        """解密入口密码，用于 {{password}} 变量替换。

        优先级：
        1. 直接存储在 entry_password_encrypted 的加密密码
        2. 通过 credential_ref 查询共享凭据表

        注意: 从 @staticmethod 改为 async 方法，以便在 entry_password_encrypted
        为空时通过数据库查询 credential_ref 对应的密码。
        """
        # 1. 优先用存储的加密密码
        if node.entry_password_encrypted:
            try:
                from src.utils.security import decrypt_password
                return decrypt_password(node.entry_password_encrypted)
            except Exception as e:
                logger.warning("节点 %s 的 entry_password 解密失败: %s", node.name, e)
                return None

        # 2. 回退：通过 credential_ref 查凭据表
        if node.credential_ref:
            try:
                from src.models.database import async_session_factory
                from src.services.credential_service import CredentialService
                from src.utils.security import decrypt_password

                async with async_session_factory() as db_session:
                    cred_service = CredentialService(db_session)
                    cred = await cred_service.get_by_name(node.credential_ref)
                    if cred and cred.password_encrypted:
                        return decrypt_password(cred.password_encrypted)
            except Exception as e:
                logger.warning(
                    "节点 %s 的 credential_ref '%s' 密码解析失败: %s",
                    node.name, node.credential_ref, e,
                )
                return None

        return None


# 兼容旧导入名，避免局部重构时出现大范围断裂
JumpOrchestrator = ConnectionOrchestrator
JumpResult = ConnectionResult
