# 功能：账号/tenant 轻量抽象（ADR-34 预留）— User 模型 + tenant_id + RoleBinding 关联
# 范围：本地单机优先；不做登录/密码/token/UI。多人时起服务端不返工（DB 已预置结构）。

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from bandi.core.permission.roles import list_builtin_roles
from bandi.utils.db import connect_app_db

DEFAULT_TENANT = "default"
DEFAULT_LOCAL_USER_ID = "local"
DEFAULT_LOCAL_ROLE = "member"


@dataclass
class User:
    """平台账号（真实用户/后续服务端账号）。RoleBinding 经 AccountStore 关联。"""

    user_id: str
    display_name: str
    tenant_id: str = DEFAULT_TENANT
    created_at: float = field(default_factory=time.time)


@dataclass
class AccountRoleBinding:
    """账号-角色绑定（user_id 为用户账号，role_id 为组织岗位 id，scope 为生效范围）。"""

    user_id: str
    role_id: str
    scope: str = "global"


class AccountStore:
    """账号存储（独立 sqlite：~/.bandi/data/account.db，单机）。

    users 表（user_id/display_name/tenant_id/created_at）+ role_bindings 表
    （user_id/role_id/scope，UNIQUE 去重）。角色白名单 = core/permission/roles
    4 内置角色（admin/power_user/member/readonly），与 VIS-03 对齐。
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect_app_db(self._path, row_factory=True)
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS role_bindings (
                user_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'global',
                PRIMARY KEY (user_id, role_id, scope)
            )
            """
        )
        self._conn.commit()

    def _validate_role(self, role_id: str) -> None:
        if role_id not in list_builtin_roles():
            raise ValueError(f"unknown role: {role_id} (builtin roles: {list_builtin_roles()})")

    def _require_user(self, user_id: str) -> None:
        if self.get_user(user_id) is None:
            raise ValueError(f"unknown user: {user_id}")

    def ensure_local_user(self) -> User:
        """单机默认账号（幂等）：local @ default + member 角色。"""
        existing = self.get_user(DEFAULT_LOCAL_USER_ID)
        if existing is not None:
            return existing
        user = User(
            user_id=DEFAULT_LOCAL_USER_ID,
            display_name="Local User",
            tenant_id=DEFAULT_TENANT,
        )
        self._insert_user(user)
        self.bind_role(user.user_id, DEFAULT_LOCAL_ROLE)
        return user

    def _insert_user(self, user: User) -> None:
        self._conn.execute(
            "INSERT INTO users (user_id, display_name, tenant_id, created_at) VALUES (?,?,?,?)",
            (user.user_id, user.display_name, user.tenant_id, user.created_at),
        )
        self._conn.commit()

    def create_user(
        self,
        user_id: str,
        display_name: str,
        tenant_id: str = DEFAULT_TENANT,
        roles: list[str] | None = None,
    ) -> User:
        if self.get_user(user_id) is not None:
            raise ValueError(f"user already exists: {user_id}")
        for role in roles or []:
            self._validate_role(role)
        user = User(user_id=user_id, display_name=display_name, tenant_id=tenant_id)
        self._insert_user(user)
        for role in roles or [DEFAULT_LOCAL_ROLE]:
            self._bind_role_raw(user_id, role)
        return user

    def get_user(self, user_id: str) -> User | None:
        row = self._conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return User(
            user_id=row["user_id"],
            display_name=row["display_name"],
            tenant_id=row["tenant_id"],
            created_at=row["created_at"],
        )

    def list_users(self, tenant_id: str | None = None) -> list[User]:
        """列出账号。.6：传 tenant_id 时按租户过滤（None = 全量，保持既有行为）。"""
        if tenant_id is None:
            rows = self._conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE tenant_id = ? ORDER BY created_at", (tenant_id,)
            ).fetchall()
        return [
            User(
                user_id=r["user_id"],
                display_name=r["display_name"],
                tenant_id=r["tenant_id"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def bind_role(self, user_id: str, role_id: str, scope: str = "global") -> None:
        self._require_user(user_id)
        self._validate_role(role_id)
        self._bind_role_raw(user_id, role_id, scope)

    def _bind_role_raw(self, user_id: str, role_id: str, scope: str = "global") -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO role_bindings (user_id, role_id, scope) VALUES (?,?,?)",
            (user_id, role_id, scope),
        )
        self._conn.commit()

    def unbind_role(self, user_id: str, role_id: str, scope: str = "global") -> None:
        self._conn.execute(
            "DELETE FROM role_bindings WHERE user_id=? AND role_id=? AND scope=?",
            (user_id, role_id, scope),
        )
        self._conn.commit()

    def list_user_roles(self, user_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT role_id FROM role_bindings WHERE user_id=? ORDER BY role_id", (user_id,)
        ).fetchall()
        return [r["role_id"] for r in rows]

    def list_bindings(self) -> list[AccountRoleBinding]:
        rows = self._conn.execute("SELECT * FROM role_bindings").fetchall()
        return [
            AccountRoleBinding(user_id=r["user_id"], role_id=r["role_id"], scope=r["scope"])
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()
