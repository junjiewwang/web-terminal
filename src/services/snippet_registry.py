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
        re.compile(r"\bdd\b.*\bof=/dev/"),       # dd 覆写磁盘设备（dd if=读取是安全的）
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

        # 安全审计：逐域校验脚本文件，失败的域跳过（不影响其他域）
        valid_domains: list[SnippetDomain] = []
        for domain in config.domains:
            if domain.script_file:
                try:
                    self._audit_script(domain)
                except ScriptAuditError as e:
                    logger.error("域 '%s' 脚本审计失败，已跳过: %s", domain.id, e)
                    continue
            valid_domains.append(domain)

        self._domains = {d.id: d for d in valid_domains}
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

    # ── 版本号常量（脚本中嵌入 __<DOMAIN_ID>_SNIPPET_VERSION__="..." ）
    # ★ 已废弃：版本号探测已由 MD5 探测替代（Optimization #1）
    _VERSION_PATTERN = re.compile(r'__(\w+)_SNIPPET_VERSION__="([^"]+)"')

    def get_script_version(self, domain_id: str) -> str | None:
        """从脚本内容中提取版本号。

        ★ 已废弃：保留供外部兼容调用，内部检测已由 MD5 替代。

        Returns:
            版本号字符串，或 None（脚本无版本声明）。
        """
        content = self.get_script_content(domain_id)
        if not content:
            return None
        for match in self._VERSION_PATTERN.finditer(content):
            if match.group(1).upper() == domain_id.upper():
                return match.group(2)
        return None

    # ── MD5 探测（Optimization #1: 替代版本号比对）──────

    def get_script_md5(self, domain_id: str) -> str | None:
        """计算本地脚本文件的 MD5 摘要。

        对脚本文件内容（UTF-8 编码）计算 MD5，用于与远端已注入脚本的 MD5 比对。
        任何脚本修改（包括注释、空格）都会导致 MD5 变化，自动触发重新注入。

        优于版本号方式：无需手动维护版本字符串。

        Returns:
            32 位十六进制 MD5 字符串，或 None（脚本不存在）。
        """
        content = self.get_script_content(domain_id)
        if not content:
            return None
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def get_md5_probe_command(self, domain_id: str) -> str | None:
        """获取检测远端已注入脚本 MD5 的探测命令。

        在远端对 /tmp/ts-{domain_id}.sh 文件计算 MD5，
        兼容 Linux（md5sum）和 macOS（md5 -q）。
        如果文件不存在，输出 __PROBE_MD5_RESULT__:none。

        输出格式：__PROBE_MD5_RESULT__:<md5_hex_or_none>

        ★ 使用 _RESULT__ 后缀区分输出行与命令回显行（与版本探测相同策略）。

        Returns:
            MD5 探测命令字符串，或 None（领域不存在）。
        """
        domain = self._domains.get(domain_id)
        if not domain:
            return None
        script_name = f"ts-{domain_id}.sh"
        # 优先 md5sum（Linux），回退 md5 -q（macOS），都没有则输出 none
        return (
            f'_ft_md5=$(md5sum /tmp/{script_name} 2>/dev/null | awk \'{{print $1}}\') || '
            f'_ft_md5=$(md5 -q /tmp/{script_name} 2>/dev/null) || '
            f'_ft_md5=none; '
            f'echo "__PROBE_MD5_RESULT__:$_ft_md5"'
        )

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

    def get_version_probe_command(self, domain_id: str) -> str | None:
        """获取检测远端脚本版本号的探测命令。

        ★ 已废弃：保留供外部兼容调用，内部检测已由 get_md5_probe_command() 替代。

        Returns:
            版本探测命令字符串，或 None（领域不存在）。
        """
        domain = self._domains.get(domain_id)
        if not domain:
            return None
        var_name = f"__{domain_id.upper()}_SNIPPET_VERSION__"
        # 使用 _RESULT__ 后缀区分输出行与回显行：
        #   回显行：echo "__PROBE_VER_RESULT__:${__FT_SNIPPET_VERSION__:-none}"
        #   输出行：__PROBE_VER_RESULT__:2026.05.14.3
        # wait_pattern 匹配 _RESULT__:\w 只会命中输出行（版本号以字母/数字开头）
        return f'echo "__PROBE_VER_RESULT__:${{{var_name}:-none}}"'

    # ── Heredoc 注入 ──────────────────────────────

    # heredoc 注入完成后的确认标记（公共常量，server.py 需引用）
    INJECT_DONE = "__SNIPPET_INJECTED__"

    def build_heredoc_loader(
        self, domain_id: str, *, compressed: bool = True
    ) -> str | None:
        """生成脚本注入命令，将脚本加载到远端 /tmp/。

        支持两种注入模式：
        - compressed=True（默认）：gzip+base64 压缩注入，传输量减少 60-80%
          远端通过 base64 -d | gunzip 解压还原脚本文件
        - compressed=False：传统 heredoc 注入（作为 fallback）

        末尾追加确认标记 echo，供 wait_for 精确等待注入完成。

        Returns:
            注入命令字符串，或 None（领域/脚本不存在）。
        """
        content = self.get_script_content(domain_id)
        if not content:
            return None
        script_name = f"ts-{domain_id}.sh"

        if compressed:
            return self._build_compressed_loader(content, script_name)
        return self._build_heredoc_loader(content, script_name)

    def _build_compressed_loader(
        self, content: str, script_name: str
    ) -> str:
        """gzip+base64 压缩注入：传输量最小化。

        生成命令：echo '<base64>' | base64 -d | gunzip > /tmp/ts-xxx.sh && source ...
        远端只需 base64 + gunzip，这两个在几乎所有 Linux/macOS 上都可用。
        """
        import base64
        import gzip

        compressed = gzip.compress(content.encode("utf-8"), compresslevel=9)
        b64_data = base64.b64encode(compressed).decode("ascii")

        logger.debug(
            "压缩注入 %s: %d → %d → %d (原始 → gzip → base64)",
            script_name, len(content), len(compressed), len(b64_data),
        )

        # 单行 echo + pipeline 命令，避免 heredoc 的多行传输开销
        return (
            f"echo '{b64_data}' | base64 -d 2>/dev/null | "
            f"gunzip -c > /tmp/{script_name} 2>/dev/null && "
            f"source /tmp/{script_name} && echo '{self.INJECT_DONE}'"
        )

    def _build_heredoc_loader(self, content: str, script_name: str) -> str:
        """传统 heredoc 注入（fallback 模式）。"""
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


# ── 公共注入逻辑（消除 REST API / MCP server 重复代码）──────


async def ensure_snippet_loaded(
    session,
    registry: SnippetRegistry,
    domain_id: str,
) -> str | None:
    """确保指定域的 snippet 已加载到远端会话且内容最新。

    三步探测逻辑（避免不必要的重注入）：
    1. 探测函数是否存在（type <first_command>）
    2. 如果存在，探测远端脚本文件 MD5 是否与本地一致
    3. 仅在函数不存在或 MD5 不匹配时才注入

    ★ Optimization #1: 由版本号比对改为 MD5 比对，无需手动维护版本字符串。
    ★ Bugfix #21: 修复探测回显/注入超时/日志等问题
    ★ Bugfix #22c: 注入期间静默 WebSocket 广播，避免 base64 刷屏浏览器终端

    公共方法——REST API 和 MCP server 共同调用，消除重复代码。

    Args:
        session: TerminalSession 实例（需要 send_input / wait_for / send_command /
                 _buffer_write_seq / set_ws_muted 方法）
        registry: SnippetRegistry 实例
        domain_id: snippet 域 ID（如 "ft"）

    Returns:
        None 表示加载成功，字符串表示错误信息。
    """
    import asyncio
    import re as _re

    domain = registry.get_domain(domain_id)
    if not domain:
        return f"snippet 域 '{domain_id}' 未在 snippets.yaml 中配置"

    need_inject = True
    inject_reason = "首次注入"

    # ── 步骤 1: 探测函数是否已加载 ──
    probe = registry.get_probe_command(domain_id)
    if probe:
        try:
            probe_output = await session.send_command(
                command=probe,
                wait_pattern=r"__PROBE_(?:YES|NO)__",
                timeout=10.0,
            )
            last_line = _extract_last_nonempty_line(probe_output)
            if SnippetRegistry.PROBE_YES in last_line:
                # 函数存在 → 步骤 2: 检查 MD5（Optimization #1）
                need_inject = False
                local_md5 = registry.get_script_md5(domain_id)
                if local_md5:
                    md5_probe = registry.get_md5_probe_command(domain_id)
                    if md5_probe:
                        try:
                            md5_output = await session.send_command(
                                command=md5_probe,
                                wait_pattern=r"__PROBE_MD5_RESULT__:\w",
                                timeout=5.0,
                            )
                            remote_md5 = _extract_md5_from_output(md5_output)
                            if (
                                remote_md5
                                and remote_md5 != "none"
                                and remote_md5 == local_md5
                            ):
                                logger.debug(
                                    "%s snippet MD5 一致 (%s)，无需注入",
                                    domain_id, local_md5[:8],
                                )
                                return None  # MD5 一致，无需注入
                            else:
                                logger.info(
                                    "%s snippet MD5 不匹配: 远端=%s, 本地=%s, 将重新注入",
                                    domain_id,
                                    (remote_md5 or "unknown")[:8],
                                    local_md5[:8],
                                )
                                need_inject = True
                                inject_reason = "MD5 不匹配"
                        except (TimeoutError, ConnectionError):
                            logger.debug("MD5 探测超时，将重新注入")
                            need_inject = True
                            inject_reason = "MD5 探测超时"
                else:
                    # 本地脚本不存在，已加载就够了
                    return None
        except (TimeoutError, ConnectionError):
            pass  # 探测失败，继续注入

    if not need_inject:
        return None

    # ── 步骤 3: 注入脚本 ──
    loader = registry.build_heredoc_loader(domain_id)
    if not loader:
        return f"{domain_id} 域脚本文件不存在"

    logger.info("注入 %s snippet（%s）", domain_id, inject_reason)

    inject_confirmed = False
    try:
        # ★ Bugfix #22c: 静默 WebSocket 广播 ★
        # 注入期间的 PTY 输出（loader base64 回显、source 输出等）不发送到浏览器。
        # Agent 缓冲区不受影响，wait_for 仍能检测到 __SNIPPET_INJECTED__ 标记。
        session.set_ws_muted(True)

        # ★ Bugfix #21d: 使用 _buffer_write_seq 替代 len(_raw_buffer)
        pre_inject_pos = session._buffer_write_seq
        await session.send_input(loader + "\n")
        await session.wait_for(
            pattern=_re.escape(SnippetRegistry.INJECT_DONE),
            timeout=15.0,
            _start_pos=pre_inject_pos,
        )
        logger.info("%s snippet 注入完成", domain_id)
        inject_confirmed = True
    except TimeoutError:
        logger.warning(
            "%s snippet 注入等待确认超时，进行 post-inject 探测验证", domain_id
        )
    except ConnectionError as e:
        return f"脚本注入失败: {e}"
    finally:
        # ★ Bugfix #22c: 恢复 WebSocket 广播（无论注入成功/失败/超时）★
        try:
            session.set_ws_muted(False)
        except Exception:
            logger.debug("恢复 WebSocket 广播失败（会话可能已断开）")

    # ★ Bugfix #21b: 注入超时时，做一次快速 probe 验证
    if not inject_confirmed and probe:
        try:
            verify_output = await session.send_command(
                command=probe,
                wait_pattern=r"__PROBE_(?:YES|NO)__",
                timeout=5.0,
            )
            last_line = _extract_last_nonempty_line(verify_output)
            if SnippetRegistry.PROBE_YES in last_line:
                logger.info("post-inject 探测确认: %s 函数可用", domain_id)
                inject_confirmed = True
            else:
                logger.error("post-inject 探测确认: %s 函数不可用", domain_id)
        except (TimeoutError, ConnectionError):
            logger.error(
                "post-inject 探测超时，%s snippet 可能未成功加载", domain_id
            )

    if not inject_confirmed:
        return (
            f"{domain_id} 脚本注入失败：注入超时且 post-inject 探测确认函数不可用，"
            "请检查远端终端状态后重试"
        )

    return None


def _extract_last_nonempty_line(text: str) -> str:
    """从文本中提取最后一个非空行（避免回显行干扰）。"""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _extract_version_from_output(output: str) -> str | None:
    """从版本探测输出中提取远端版本号。

    ★ 已废弃：保留供外部兼容调用。

    从最后一行向上扫描，过滤回显行残留（含 $ { } 等未展开变量引用）。
    """
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if "__PROBE_VER_RESULT__:" in stripped:
            val = stripped.split("__PROBE_VER_RESULT__:", 1)[1].strip()
            if val and "$" not in val and "{" not in val:
                return val
    return None


def _extract_md5_from_output(output: str) -> str | None:
    """从 MD5 探测输出中提取远端脚本 MD5。

    从最后一行向上扫描，过滤回显行残留（含 $ { } 等未展开变量引用）。
    合法 MD5 为 32 位十六进制字符串。

    Returns:
        MD5 字符串（32 位 hex），或 "none"（文件不存在），或 None（解析失败）。
    """
    _MD5_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if "__PROBE_MD5_RESULT__:" in stripped:
            val = stripped.split("__PROBE_MD5_RESULT__:", 1)[1].strip()
            if val and "$" not in val and "{" not in val:
                # 合法值："none" 或 32 位 hex
                if val == "none" or _MD5_HEX_RE.match(val):
                    return val
    return None
