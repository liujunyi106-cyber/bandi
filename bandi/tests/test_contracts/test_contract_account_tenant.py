# 功能：FE-账号/tenant 轻量抽象契约
# 背景：账号层 User 模型 + tenant_id 落库 + RoleBinding 关联；不做登录/密码/token/UI。
# 契约钉行为：AccountStore CRUD/持久化/tenant 默认/roles 白名单/RoleBinding 关联/
# ensure_local_user 幂等/Entity.tenant_id 字段与落库往返。
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.core.account import AccountStore

_DEFAULT_USER_ID = "local"
_DEFAULT_TENANT = "default"


def _make_store(tmp_path: Path) -> AccountStore:
    return AccountStore(str(tmp_path / "account.db"))


class TestAccountStoreCrud:
    def test_create_and_get_user(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.create_user(user_id="alice", display_name="Alice", tenant_id="acme")
        user = store.get_user("alice")
        assert user is not None
        assert user.user_id == "alice"
        assert user.display_name == "Alice"
        assert user.tenant_id == "acme"

    def test_default_tenant_and_roles(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.create_user(user_id="bob", display_name="Bob")
        user = store.get_user("bob")
        assert user.tenant_id == _DEFAULT_TENANT, "未指定 tenant 默认 default（单机预留）"
        assert "member" in store.list_user_roles("bob"), "默认绑定 member 角色"

    def test_persist_across_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "account.db"
        store = AccountStore(str(path))
        store.create_user(user_id="carol", display_name="Carol")
        store.close()
        store2 = AccountStore(str(path))
        assert store2.get_user("carol") is not None, "sqlite 落库后重开可读回"
        store2.close()

    def test_create_duplicate_raises(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.create_user(user_id="dave", display_name="Dave")
        with pytest.raises(ValueError, match="exists"):
            store.create_user(user_id="dave", display_name="Dave2")

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert store.get_user("nobody") is None

    def test_list_users(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.ensure_local_user()
        store.create_user(user_id="erin", display_name="Erin")
        ids = {u.user_id for u in store.list_users()}
        assert {"local", "erin"} <= ids


class TestRoleWhitelist:
    def test_invalid_role_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with pytest.raises(ValueError, match="unknown role"):
            store.create_user(user_id="frank", display_name="Frank", roles=["superadmin"])

    def test_valid_builtin_roles_accepted(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        for role in ("admin", "power_user", "member", "readonly"):
            store.create_user(user_id=f"u-{role}", display_name=role, roles=[role])
            assert role in store.list_user_roles(f"u-{role}")


class TestRoleBinding:
    def test_bind_unbind_roundtrip(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.create_user(user_id="grace", display_name="Grace")
        store.bind_role("grace", "admin", scope="global")
        assert "admin" in store.list_user_roles("grace")
        store.unbind_role("grace", "admin")
        assert "admin" not in store.list_user_roles("grace")

    def test_bind_invalid_role_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.create_user(user_id="heidi", display_name="Heidi")
        with pytest.raises(ValueError, match="unknown role"):
            store.bind_role("heidi", "root")

    def test_bind_unknown_user_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with pytest.raises(ValueError, match="unknown user"):
            store.bind_role("ghost", "admin")


class TestLocalUser:
    def test_ensure_local_user_idempotent(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        first = store.ensure_local_user()
        second = store.ensure_local_user()
        assert first.user_id == _DEFAULT_USER_ID
        assert first.tenant_id == _DEFAULT_TENANT
        assert second.user_id == first.user_id, "重复 ensure 不创建重复账号"
        assert len(store.list_users()) == 1
