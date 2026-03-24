"""主机管理服务测试"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.models.host import AuthType, Host, HostCreate, HostUpdate


class TestHostCreate:
    """HostCreate Schema 验证"""

    def test_valid_create(self):
        data = HostCreate(
            name="test-server",
            hostname="192.168.1.100",
            port=22,
            username="deploy",
            auth_type=AuthType.KEY,
            private_key_path="~/.ssh/id_rsa",
            description="测试服务器",
            tags=["dev", "test"],
        )
        assert data.name == "test-server"
        assert data.port == 22
        assert data.auth_type == AuthType.KEY

    def test_default_values(self):
        data = HostCreate(
            name="minimal",
            hostname="10.0.0.1",
            username="root",
        )
        assert data.port == 22
        assert data.auth_type == AuthType.KEY
        assert data.tags is None

    def test_invalid_port(self):
        with pytest.raises(Exception):
            HostCreate(
                name="bad-port",
                hostname="10.0.0.1",
                username="root",
                port=99999,  # 超出范围
            )


class TestHostUpdate:
    """HostUpdate Schema 验证"""

    def test_partial_update(self):
        data = HostUpdate(hostname="192.168.1.200")
        dump = data.model_dump(exclude_unset=True)
        assert dump == {"hostname": "192.168.1.200"}
        assert "port" not in dump

    def test_empty_update(self):
        data = HostUpdate()
        dump = data.model_dump(exclude_unset=True)
        assert dump == {}
