"""安全工具测试 — 密码加解密 / API Token 认证。

注：src.utils.security 使用模块级全局单例（_fernet / _API_TOKEN），
测试中通过 monkeypatch 重置，避免用例间互相污染。
"""

from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet

from src.utils import security


@pytest.fixture(autouse=True)
def reset_security_globals(monkeypatch: pytest.MonkeyPatch):
    """每个用例前重置模块级全局状态。"""
    monkeypatch.setattr(security, "_API_TOKEN", None)
    monkeypatch.setattr(security, "_FERNET_KEY", None)
    monkeypatch.setattr(security, "_fernet", None)
    monkeypatch.delenv("WETTY_ENCRYPTION_KEY", raising=False)


# ── Bearer Token ──────────────────────────────


class TestApiToken:
    def test_generate_returns_urlsafe_token(self):
        token = security.generate_api_token()
        assert isinstance(token, str)
        assert len(token) > 0
        assert security.get_current_token() == token

    def test_generate_produces_distinct_tokens(self):
        first = security.generate_api_token()
        second = security.generate_api_token()
        assert first != second

    def test_verify_accepts_matching_token(self):
        token = security.generate_api_token()
        assert security.verify_api_token(token) is True

    def test_verify_rejects_wrong_token(self):
        security.generate_api_token()
        assert security.verify_api_token("not-the-token") is False

    def test_verify_allows_any_token_when_unconfigured(self):
        """未配置 Token 时放行（开发模式）。"""
        assert security.get_current_token() is None
        assert security.verify_api_token("anything") is True

    def test_get_current_token_none_before_generate(self):
        assert security.get_current_token() is None


# ── Fernet 加解密 ──────────────────────────────


class TestEncryptDecryptRoundTrip:
    def test_round_trip_returns_original(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())
        encrypted = security.encrypt_password("s3cret-pw")
        assert security.decrypt_password(encrypted) == "s3cret-pw"

    def test_encrypted_value_has_fernet_prefix(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())
        assert security.encrypt_password("pw").startswith("fernet:")

    def test_ciphertext_does_not_leak_plaintext(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())
        assert "s3cret-pw" not in security.encrypt_password("s3cret-pw")

    def test_same_plaintext_encrypts_differently(self, monkeypatch: pytest.MonkeyPatch):
        """Fernet 含随机 IV/时间戳，相同明文密文应不同。"""
        monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())
        assert security.encrypt_password("pw") != security.encrypt_password("pw")

    def test_round_trip_handles_unicode_and_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())
        for value in ("中文密码🔐", "", "  spaces  ", "a" * 500):
            assert security.decrypt_password(security.encrypt_password(value)) == value

    def test_auto_generates_key_when_env_missing(self):
        """未设置环境变量时自动生成密钥，同进程内仍可解密。"""
        encrypted = security.encrypt_password("pw")
        assert security.decrypt_password(encrypted) == "pw"


class TestDecryptFailures:
    def test_wrong_key_raises_value_error(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())
        encrypted = security.encrypt_password("pw")

        # 换一把新密钥
        monkeypatch.setattr(security, "_fernet", None)
        monkeypatch.setattr(security, "_FERNET_KEY", None)
        monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())

        with pytest.raises(ValueError, match="解密失败"):
            security.decrypt_password(encrypted)

    def test_corrupted_fernet_payload_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())
        with pytest.raises(ValueError):
            security.decrypt_password("fernet:not-a-valid-token")

    def test_invalid_legacy_payload_raises(self):
        with pytest.raises(ValueError, match="解密失败"):
            security.decrypt_password("!!!not-base64!!!")


class TestLegacyBase64Compat:
    def test_decrypts_legacy_base64_value(self):
        """向后兼容：无 fernet: 前缀的旧数据按 base64 解码。"""
        legacy = base64.b64encode(b"legacy-pw").decode()
        assert security.decrypt_password(legacy) == "legacy-pw"
