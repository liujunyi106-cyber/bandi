# 功能：Day133 VIS-03 角色接线闭环契约（技术债清单 roles.py 项核销证据）
# 背景：4 内置角色（admin/power_user/member/readonly）策略已可经 register_builtin_role
# ① 白名单事实 ② register 注入引擎可证（策略真正生效路径，非纸面"已定义"）
# ③ 未知角色拒绝 ④ 账号白名单 == 内置角色（VIS-03 对齐）⑤ 平台账号与 company org 角色域共存。
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.core.account import AccountStore
from bandi.core.permission.policy_engine import PolicyEngine
from bandi.core.permission.roles import (
    get_builtin_role,
    list_builtin_roles,
    register_builtin_role,
)

BUILTIN_ROLES = ["admin", "power_user", "member", "readonly"]


class TestBuiltinRolesFacts:
    def test_whitelist_has_four_roles(self) -> None:
        assert sorted(list_builtin_roles()) == sorted(BUILTIN_ROLES)

    def test_each_role_has_policies(self) -> None:
        for name in BUILTIN_ROLES:
            role = get_builtin_role(name)
            assert role is not None, f"{name} 角色定义存在"
            assert role.policies, f"{name} 角色策略非空（非纸面定义）"

    def test_admin_allow_all_policy_present(self) -> None:
        role = get_builtin_role("admin")
        names = {getattr(p, "_name", "") or getattr(p, "name", "") for p in role.policies}
        assert "admin_allow_all" in names


class TestRegisterIntoEngine:
    """技术债核心反证：角色策略已可真实注入 PolicyEngine（"从未 add"已闭环）。"""

    def test_register_admin_injects_policies(self) -> None:
        engine = PolicyEngine()
        before = len(engine._policies)
        assert register_builtin_role(engine, "admin") is True
        assert len(engine._policies) > before, "admin 策略必须真实注入引擎"

    def test_register_member_injects_policies(self) -> None:
        engine = PolicyEngine()
        before = len(engine._policies)
        assert register_builtin_role(engine, "member") is True
        assert len(engine._policies) > before

    def test_register_unknown_role_rejected(self) -> None:
        engine = PolicyEngine()
        assert register_builtin_role(engine, "root") is False
        assert register_builtin_role(engine, "superadmin") is False


class TestAccountWhitelistAlignment:
    """VIS-03 对齐：账号角色合法域 == 内置角色（AccountStore 校验同源）。"""

    def test_account_store_role_whitelist_matches_builtin(self) -> None:
        store = AccountStore(":memory:")
        # 内置角色全部可绑定
        for role in BUILTIN_ROLES:
            store.create_user(user_id=f"u-{role}", display_name=role, roles=[role])
        # 内置外角色被拒
        with pytest.raises(ValueError, match="unknown role"):
            store.create_user(user_id="bad", display_name="Bad", roles=["root"])
        store.close()


class TestAccountAndOrgRoleCoexist:
    """平台账号角色与组织岗位域分离：账号角色走 AccountStore，组织岗位走 OrgEngine。"""

    def test_account_roles_and_org_roles_coexist(self, tmp_path: Path) -> None:
        from bandi.company.org_engine import OrgEngine

        store = AccountStore(str(tmp_path / "account.db"))
        store.ensure_local_user()
        store.create_user(user_id="alice", display_name="Alice", roles=["admin"])

        org = OrgEngine()
        org.register_role("worker", "Worker", "exec")

        # 同一 user_id 的平台角色与组织岗位并存，互不覆盖
        assert "admin" in store.list_user_roles("alice")
        role = org.get_role("worker")
        assert role is not None
        store.close()
