"""CLI 工具测试 — password_hash / reset_password / migrate_db。

CLI 的 main() 通过 monkeypatch sys.argv + 捕获 SystemExit 测试，
数据库副作用通过替换异步入口函数隔离。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import bcrypt
import pytest

from src.utils import migrate_db, password_hash, reset_password

FAST_ROUNDS = 4


# ══════════════════════════════════════════════
# password_hash CLI
# ══════════════════════════════════════════════


class TestPasswordHashMain:
    def test_prints_hash_for_argv_password(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["prog", "my-password"])
        monkeypatch.setattr(password_hash, "hash_password", lambda p: bcrypt.hashpw(
            p.encode(), bcrypt.gensalt(rounds=FAST_ROUNDS)
        ).decode())

        password_hash.main()

        out = capsys.readouterr().out
        assert "$2b$" in out
        assert "哈希验证通过" in out

    def test_reads_from_getpass_when_no_argv(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["prog"])
        monkeypatch.setattr("getpass.getpass", lambda prompt: "typed-password")
        monkeypatch.setattr(password_hash, "hash_password", lambda p: bcrypt.hashpw(
            p.encode(), bcrypt.gensalt(rounds=FAST_ROUNDS)
        ).decode())

        password_hash.main()

        assert "$2b$" in capsys.readouterr().out

    def test_empty_password_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["prog"])
        monkeypatch.setattr("getpass.getpass", lambda prompt: "")

        with pytest.raises(SystemExit) as exc:
            password_hash.main()

        assert exc.value.code == 1
        assert "不能为空" in capsys.readouterr().err

    def test_never_echoes_plaintext(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["prog", "s3cret-plain"])
        monkeypatch.setattr(password_hash, "hash_password", lambda p: bcrypt.hashpw(
            p.encode(), bcrypt.gensalt(rounds=FAST_ROUNDS)
        ).decode())

        password_hash.main()

        assert "s3cret-plain" not in capsys.readouterr().out

    def test_exits_1_when_verification_fails(self, monkeypatch, capsys):
        """哈希自检失败必须以非零码退出。"""
        monkeypatch.setattr(sys, "argv", ["prog", "pw"])
        monkeypatch.setattr(password_hash, "hash_password", lambda p: bcrypt.hashpw(
            p.encode(), bcrypt.gensalt(rounds=FAST_ROUNDS)
        ).decode())
        monkeypatch.setattr(password_hash, "verify_password", lambda p, h: False)

        with pytest.raises(SystemExit) as exc:
            password_hash.main()

        assert exc.value.code == 1


# ══════════════════════════════════════════════
# reset_password CLI
# ══════════════════════════════════════════════


class TestResetPasswordHelpers:
    def test_hash_is_bcrypt_and_verifies(self):
        hashed = reset_password._hash_password("new-password")
        assert hashed.startswith("$2b$")
        assert bcrypt.checkpw(b"new-password", hashed.encode())

    def test_hash_is_salted(self):
        assert reset_password._hash_password("same") != reset_password._hash_password("same")

    def test_jwt_secret_is_random_and_long(self):
        s1 = reset_password._generate_jwt_secret()
        s2 = reset_password._generate_jwt_secret()

        assert s1 != s2
        assert len(s1) >= 32, "JWT secret 应足够长以满足 HS256 要求"


class TestResetPasswordMain:
    @pytest.fixture
    def stub_reset(self, monkeypatch):
        """拦截真实的数据库重置，记录被调用的密码。"""
        calls: list[str] = []

        async def fake_reset(pw: str) -> None:
            calls.append(pw)

        monkeypatch.setattr(reset_password, "_reset_password", fake_reset)
        return calls

    def test_argv_password_skips_confirmation(self, monkeypatch, stub_reset, capsys):
        monkeypatch.setattr(sys, "argv", ["prog", "brand-new-pw"])

        reset_password.main()

        assert stub_reset == ["brand-new-pw"]

    def test_short_password_exits_1(self, monkeypatch, stub_reset, capsys):
        monkeypatch.setattr(sys, "argv", ["prog", "short"])

        with pytest.raises(SystemExit) as exc:
            reset_password.main()

        assert exc.value.code == 1
        assert "至少 6" in capsys.readouterr().err
        assert stub_reset == [], "校验失败不应触碰数据库"

    def test_interactive_mismatch_exits_1(self, monkeypatch, stub_reset, capsys):
        monkeypatch.setattr(sys, "argv", ["prog"])
        answers = iter(["first-password", "second-password"])
        monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))

        with pytest.raises(SystemExit) as exc:
            reset_password.main()

        assert exc.value.code == 1
        assert "两次输入不一致" in capsys.readouterr().err
        assert stub_reset == []

    def test_interactive_empty_exits_1(self, monkeypatch, stub_reset, capsys):
        monkeypatch.setattr(sys, "argv", ["prog"])
        monkeypatch.setattr("getpass.getpass", lambda prompt: "")

        with pytest.raises(SystemExit) as exc:
            reset_password.main()

        assert exc.value.code == 1

    def test_interactive_declined_exits_0_without_reset(self, monkeypatch, stub_reset, capsys):
        """用户在确认提示输入 N 时应干净退出，且不改数据库。"""
        monkeypatch.setattr(sys, "argv", ["prog"])
        monkeypatch.setattr("getpass.getpass", lambda prompt: "valid-password")
        monkeypatch.setattr("builtins.input", lambda prompt: "N")

        with pytest.raises(SystemExit) as exc:
            reset_password.main()

        assert exc.value.code == 0
        assert stub_reset == []
        assert "已取消" in capsys.readouterr().out

    def test_interactive_confirmed_runs_reset(self, monkeypatch, stub_reset):
        monkeypatch.setattr(sys, "argv", ["prog"])
        monkeypatch.setattr("getpass.getpass", lambda prompt: "valid-password")
        monkeypatch.setattr("builtins.input", lambda prompt: "y")

        reset_password.main()

        assert stub_reset == ["valid-password"]

    def test_warning_shown_before_reset(self, monkeypatch, stub_reset, capsys):
        monkeypatch.setattr(sys, "argv", ["prog", "brand-new-pw"])

        reset_password.main()

        out = capsys.readouterr().out
        assert "警告" in out
        assert "所有登录会话失效" in out


# ══════════════════════════════════════════════
# migrate_db CLI
# ══════════════════════════════════════════════


class TestMigrate:
    @pytest.mark.asyncio
    async def test_missing_source_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            await migrate_db.migrate(tmp_path / "nonexistent.db")

        assert exc.value.code == 1
        assert "不存在" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_empty_source_returns_without_write(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "empty.db"
        src.write_text("")

        monkeypatch.setattr(migrate_db, "_read_sqlite_hosts", AsyncMock(return_value=[]))
        write_mock = AsyncMock()
        monkeypatch.setattr(migrate_db, "_write_mysql_hosts", write_mock)

        await migrate_db.migrate(src)

        write_mock.assert_not_awaited()
        assert "无需迁移" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_reports_written_count(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "data.db"
        src.write_text("")

        monkeypatch.setattr(
            migrate_db, "_read_sqlite_hosts", AsyncMock(return_value=[{"id": 1}, {"id": 2}])
        )
        monkeypatch.setattr(migrate_db, "_write_mysql_hosts", AsyncMock(return_value=2))

        await migrate_db.migrate(src)

        out = capsys.readouterr().out
        assert "读取到 2 条" in out
        assert "成功写入 2 条" in out
        assert "迁移完成" in out


class TestReadSqliteHosts:
    @pytest.mark.asyncio
    async def test_reads_rows_as_dicts(self, tmp_path):
        """真实建一个 SQLite 文件，验证读取结果结构。"""
        from sqlalchemy import create_engine, text

        db = tmp_path / "src.db"
        sync = create_engine(f"sqlite:///{db}")
        with sync.begin() as conn:
            conn.execute(text("CREATE TABLE hosts (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.execute(text("INSERT INTO hosts (id, name) VALUES (2, 'b'), (1, 'a')"))
        sync.dispose()

        rows = await migrate_db._read_sqlite_hosts(db)

        assert [r["name"] for r in rows] == ["a", "b"], "应按 id 升序返回"
        assert rows[0]["id"] == 1


class TestMigrateMain:
    def test_default_source_path(self, monkeypatch):
        captured: list[Path] = []

        async def fake_migrate(path: Path) -> None:
            captured.append(path)

        monkeypatch.setattr(sys, "argv", ["prog"])
        monkeypatch.setattr(migrate_db, "migrate", fake_migrate)

        migrate_db.main()

        assert captured == [Path("data/wetty_mcp.db")]

    def test_source_flag_overrides(self, monkeypatch):
        captured: list[Path] = []

        async def fake_migrate(path: Path) -> None:
            captured.append(path)

        monkeypatch.setattr(sys, "argv", ["prog", "--source", "/data/custom.db"])
        monkeypatch.setattr(migrate_db, "migrate", fake_migrate)

        migrate_db.main()

        assert captured == [Path("/data/custom.db")]

    def test_invalid_flag_exits_2(self, monkeypatch):
        """argparse 对未知参数以退出码 2 结束。"""
        monkeypatch.setattr(sys, "argv", ["prog", "--bogus"])

        with pytest.raises(SystemExit) as exc:
            migrate_db.main()

        assert exc.value.code == 2
