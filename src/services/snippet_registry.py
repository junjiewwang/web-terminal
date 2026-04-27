"""排障脚本片段注册表（Snippet Registry）

核心职责：
- 从 config/snippets.yaml 加载 Snippet 元数据和脚本文件
- 提供领域/命令查询接口（被 REST API 和 MCP Tools 共同依赖）
- 脚本安全审计（危险命令检测 + SHA256 完整性校验）
- 命令模板渲染和参数校验
- 支持热加载（watchfiles 监听 yaml 变更后调用 reload）

架构定位：
  config/snippets.yaml (SSOT) → SnippetRegistry (内存) → REST API + MCP Tools
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import yaml

from src.models.snippet import (
    SnippetCommand,
    SnippetDomain,
    SnippetDomainSummary,
    SnippetsConfig,
)

logger = logging.getLogger(__name__)


class ScriptAuditError(ValueError):
    """脚本安全审计失败"""


class SnippetRegistry:
    """Snippet 核心注册表

    Single Source of Truth 的内存表示。
    被 REST API 和 MCP Tools 共同依赖，保证前后端数据一致。
    """

    _GLOBAL_DEFAULT_TIMEOUT: int = 30

    # 危险命令模式（加载脚本时扫描）
    _DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
        re.compile(r"\brm\s+-rf\s+/"),          # rm -rf /
        re.compile(r"\bmkfs\b"),                 # 格式化磁盘
        re.compile(r"\bdd\s+if="),               # dd 覆写
        re.compile(r">\s*/dev/sd"),              # 直写磁盘设备
        re.compile(r"\bcurl\b.*\|\s*bash"),      # curl | bash（远程代码执行）
        re.compile(r"\bwget\b.*\|\s*bash"),      # wget | bash
        re.compile(r"\bchmod\s+777\s+/"),        # 开放根目录权限
    ]

    def __init__(self) -> None:
        self._domains: dict[str, SnippetDomain] = {}
        self._config_path: Path | None = None
        self._script_base: Path | None = None

    # ── 属性 ──────────────────────────────────────

    @property
    def config_path(self) -> Path | None:
        return self._config_path

    @property
    def domain_count(self) -> int:
        return len(self._domains)

    # ── 加载 & 热加载 ─────────────────────────────

    def load_from_yaml(self, yaml_path: str | Path) -> None:
        """从 YAML 文件加载配置。

        启动时首次调用，以及 watchfiles 热加载时调用。
        加载过程中会对每个脚本文件执行安全审计。

        Raises:
            FileNotFoundError: YAML 文件不存在
            yaml.YAMLError: YAML 格式错误
            ScriptAuditError: 脚本安全审计失败
        """
        path = Path(yaml_path)
        if not path.exists():
            logger.warning("snippets.yaml 不存在: %s，Snippet 功能不可用", path)
            self._domains = {}
            return

        self._config_path = path
        self._script_base = path.parent

        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        config = SnippetsConfig.model_validate(raw or {})

        # 安全审计：校验每个脚本文件
        for domain in config.domains:
            if domain.script_file:
                self._audit_script(domain)

        self._domains = {d.id: d for d in config.domains}
        logger.info(
            "Snippet Registry 加载完成: %d 个领域 (%s)",
            len(self._domains),
            ", ".join(self._domains.keys()),
        )

    def reload(self) -> None:
        """热加载：重新读取 YAML 配置。

        由 watchfiles 监听回调触发。如果重新加载失败，
        保留上一次的有效配置（不会清空）。
        """
        if not self._config_path:
            logger.warning("SnippetRegistry 尚未初始化，无法热加载")
            return

        old_domains = self._domains.copy()
        try:
            self.load_from_yaml(self._config_path)
            logger.info("Snippet Registry 热加载成功")
        except Exception:
            self._domains = old_domains
            logger.exception("Snippet Registry 热加载失败，保留上一次配置")

    # ── 查询 ──────────────────────────────────────

    def list_domains(self) -> list[SnippetDomain]:
        """列出所有领域（含命令列表）"""
        return list(self._domains.values())

    def list_domain_summaries(self) -> list[SnippetDomainSummary]:
        """列出所有领域概要（不含命令详情，用于列表展示）"""
        return [
            SnippetDomainSummary(
                id=d.id,
                name=d.name,
                icon=d.icon,
                description=d.description,
                tags=d.tags,
                command_count=len(d.commands),
            )
            for d in self._domains.values()
        ]

    def get_domain(self, domain_id: str) -> SnippetDomain | None:
        """获取领域完整信息（含命令列表）"""
        return self._domains.get(domain_id)

    def get_command(self, domain_id: str, command_id: str) -> SnippetCommand | None:
        """获取单个命令定义"""
        domain = self._domains.get(domain_id)
        if not domain:
            return None
        return next((c for c in domain.commands if c.id == command_id), None)

    def get_script_content(self, domain_id: str) -> str | None:
        """读取领域脚本文件内容（用于 heredoc 注入远端）"""
        domain = self._domains.get(domain_id)
        if not domain or not domain.script_file or not self._script_base:
            return None
        script_path = self._script_base / domain.script_file
        if not script_path.exists():
            logger.warning("脚本文件不存在: %s", script_path)
            return None
        return script_path.read_text(encoding="utf-8")

    # ── 命令渲染 ──────────────────────────────────

    def resolve_command(
        self,
        domain_id: str,
        command_id: str,
        params: dict[str, str] | None = None,
    ) -> str | None:
        """解析命令模板，用实际参数替换占位符。

        required 参数缺失时返回 None（由调用方处理错误提示）。

        Returns:
            渲染后的命令字符串，或 None（命令不存在或 required 参数缺失）。
        """
        cmd = self.get_command(domain_id, command_id)
        if not cmd:
            return None

        resolved = cmd.template
        actual_params = params or {}

        for p in cmd.params:
            value = actual_params.get(p.name, p.default)
            if p.required and not value:
                return None  # required 参数缺失
            resolved = resolved.replace(f"{{{{{p.name}}}}}", value)

        # 压缩连续空格（可选参数为空时模板中会出现多余空格）
        return re.sub(r"\s+", " ", resolved).strip()

    def validate_params(
        self,
        domain_id: str,
        command_id: str,
        params: dict[str, str] | None = None,
    ) -> list[str]:
        """校验命令参数，返回错误信息列表（空列表表示校验通过）"""
        cmd = self.get_command(domain_id, command_id)
        if not cmd:
            return [f"命令不存在: {domain_id}/{command_id}"]

        errors: list[str] = []
        actual_params = params or {}
        for p in cmd.params:
            if p.required and not actual_params.get(p.name, "").strip():
                errors.append(f"参数 \"{p.name}\" 为必填项")
        return errors

    # ── 超时 ──────────────────────────────────────

    def get_timeout(self, domain_id: str, command_id: str) -> int:
        """获取命令超时（秒）。

        优先级：命令级 timeout > 领域级 default_timeout > 全局默认 30s
        """
        domain = self._domains.get(domain_id)
        if not domain:
            return self._GLOBAL_DEFAULT_TIMEOUT

        cmd = next((c for c in domain.commands if c.id == command_id), None)
        if cmd and cmd.timeout is not None:
            return cmd.timeout

        if domain.default_timeout is not None:
            return domain.default_timeout

        return self._GLOBAL_DEFAULT_TIMEOUT

    # ── 探测 ──────────────────────────────────────

    # 探测标记：使用不对称标记避免回显污染。
    # 回显行中同时包含两个标记字面量，所以判定时需要
    # 只检查输出的最后一行（实际执行结果），而非全文匹配。
    PROBE_YES = "__PROBE_YES__"
    PROBE_NO = "__PROBE_NO__"

    def get_probe_command(self, domain_id: str) -> str | None:
        """获取检测脚本是否已在远端加载的探测命令。

        通过 `type <first_command>` 判断函数是否已定义。
        使用不对称标记 + 最后行匹配，避免命令回显污染导致假阳性。

        Returns:
            探测命令字符串，或 None（领域不存在或无命令）。
        """
        domain = self._domains.get(domain_id)
        if not domain or not domain.commands:
            return None
        first_cmd = domain.commands[0].id
        return (
            f"type {first_cmd} >/dev/null 2>&1 "
            f"&& echo '{self.PROBE_YES}' "
            f"|| echo '{self.PROBE_NO}'"
        )

    # ── Heredoc 注入 ──────────────────────────────

    # heredoc 注入完成后的确认标记（公共常量，server.py 需引用）
    INJECT_DONE = "__SNIPPET_INJECTED__"

    def build_heredoc_loader(self, domain_id: str) -> str | None:
        """生成 heredoc 注入命令，将脚本加载到远端 /tmp/。

        命令会将脚本内容写入 /tmp/ts-{domain_id}.sh 并 source。
        末尾追加确认标记 echo，供 wait_for 精确等待注入完成。

        Returns:
            heredoc 加载命令字符串，或 None（领域/脚本不存在）。
        """
        content = self.get_script_content(domain_id)
        if not content:
            return None
        script_name = f"ts-{domain_id}.sh"
        return (
            f"cat << 'SNIPPET_EOF' > /tmp/{script_name}\n"
            f"{content}\n"
            f"SNIPPET_EOF\n"
            f"source /tmp/{script_name} && echo '{self.INJECT_DONE}'"
        )

    # ── 安全审计 ──────────────────────────────────

    def _audit_script(self, domain: SnippetDomain) -> None:
        """加载时审计脚本文件安全性。

        层1: 危险命令正则检测
        层2: SHA256 完整性校验（可选，仅当 script_sha256 字段有值时）

        Raises:
            ScriptAuditError: 审计失败
        """
        if not self._script_base:
            return
        script_path = self._script_base / domain.script_file
        if not script_path.exists():
            logger.warning("脚本文件不存在，跳过审计: %s", script_path)
            return

        content = script_path.read_text(encoding="utf-8")

        # 层1：危险命令检测
        for pattern in self._DANGEROUS_PATTERNS:
            match = pattern.search(content)
            if match:
                raise ScriptAuditError(
                    f"脚本安全审计失败: {domain.id} ({domain.script_file}) "
                    f"包含危险命令: {match.group()}"
                )

        # 层2：SHA256 完整性校验（可选）
        if domain.script_sha256:
            actual_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if actual_sha256 != domain.script_sha256:
                raise ScriptAuditError(
                    f"脚本完整性校验失败: {domain.id} ({domain.script_file}) "
                    f"期望 SHA256={domain.script_sha256}, 实际={actual_sha256}"
                )

        logger.debug("脚本审计通过: %s (%s)", domain.id, domain.script_file)
