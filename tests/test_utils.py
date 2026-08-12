"""SSH 命令构造与密码哈希工具测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.utils.password_hash import hash_password, verify_password
from src.utils.ssh_command import build_ssh_command, build_ssh_command_for_host

_OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"


# ── build_ssh_command ─────────────────────────


class TestBuildSSHCommand:
    def test_interactive_auth_without_password_or_key(self):
        cmd = build_ssh_command("10.0.0.1", 22, "root")
        assert cmd == f"ssh {_OPTS} -p 22 root@10.0.0.1"

    def test_key_auth_includes_identity_file(self):
        cmd = build_ssh_command("10.0.0.1", 2222, "deploy", key_path="/root/.ssh/id_rsa")
        assert cmd == f"ssh {_OPTS} -i /root/.ssh/id_rsa -p 2222 deploy@10.0.0.1"

    def test_password_auth_uses_sshpass(self):
        cmd = build_ssh_command("10.0.0.1", 36000, "ops", password="p@ss")
        assert cmd == f"sshpass -p 'p@ss' ssh {_OPTS} -p 36000 ops@10.0.0.1"

    def test_key_takes_precedence_over_password(self):
        """同时提供密钥和密码时优先用密钥（不应调用 sshpass）。"""
        cmd = build_ssh_command("h", 22, "u", password="pw", key_path="/k")
        assert cmd.startswith("ssh ")
        assert "sshpass" not in cmd
        assert "pw" not in cmd

    def test_empty_password_falls_back_to_interactive(self):
        cmd = build_ssh_command("h", 22, "u", password="")
        assert cmd == f"ssh {_OPTS} -p 22 u@h"

    def test_host_key_checking_always_disabled(self):
        for kwargs in ({}, {"password": "p"}, {"key_path": "/k"}):
            assert "StrictHostKeyChecking=no" in build_ssh_command("h", 22, "u", **kwargs)


# ── build_ssh_command_for_host ────────────────


class TestBuildSSHCommandForHost:
    def test_extracts_fields_from_host_object(self):
        host = SimpleNamespace(
            hostname="bastion.example.com",
            port=36000,
            username="operator",
            private_key_path=None,
        )
        cmd = build_ssh_command_for_host(host)
        assert cmd == f"ssh {_OPTS} -p 36000 operator@bastion.example.com"

    def test_uses_host_private_key_path(self):
        host = SimpleNamespace(
            hostname="h", port=22, username="u", private_key_path="/root/.ssh/id_ed25519"
        )
        assert "-i /root/.ssh/id_ed25519" in build_ssh_command_for_host(host)

    def test_passes_decrypted_password_through(self):
        host = SimpleNamespace(hostname="h", port=22, username="u", private_key_path=None)
        cmd = build_ssh_command_for_host(host, decrypted_password="secret")
        assert cmd.startswith("sshpass -p 'secret' ")


# ── bcrypt 密码哈希 ────────────────────────────


class TestPasswordHash:
    # rounds=4 是 bcrypt 允许的最小值，仅用于加速测试
    FAST = 4

    def test_hash_verifies_against_original(self):
        hashed = hash_password("correct-horse", rounds=self.FAST)
        assert verify_password("correct-horse", hashed) is True

    def test_hash_rejects_wrong_password(self):
        hashed = hash_password("correct-horse", rounds=self.FAST)
        assert verify_password("wrong-password", hashed) is False

    def test_hash_is_salted_and_differs_each_call(self):
        first = hash_password("same", rounds=self.FAST)
        second = hash_password("same", rounds=self.FAST)
        assert first != second
        # 但两者都能通过校验
        assert verify_password("same", first)
        assert verify_password("same", second)

    def test_hash_does_not_contain_plaintext(self):
        assert "s3cret" not in hash_password("s3cret", rounds=self.FAST)

    def test_hash_uses_bcrypt_format(self):
        assert hash_password("pw", rounds=self.FAST).startswith("$2b$")

    def test_rounds_encoded_in_hash(self):
        assert f"${self.FAST:02d}$" in hash_password("pw", rounds=self.FAST)

    def test_handles_unicode_password(self):
        hashed = hash_password("密码🔐", rounds=self.FAST)
        assert verify_password("密码🔐", hashed) is True
        assert verify_password("密码", hashed) is False

    def test_verify_is_case_sensitive(self):
        hashed = hash_password("CaseSensitive", rounds=self.FAST)
        assert verify_password("casesensitive", hashed) is False

    def test_default_rounds_is_12(self):
        """默认 cost 应为 12（安全推荐最低值）。"""
        assert "$12$" in hash_password("pw")
