# 功能：运行时身份——member_session_id/role_session_id 解析

from __future__ import annotations

from dataclasses import dataclass, field

from bandi.company.session_scoping import scoped_member_session_id


@dataclass
class CompanyRuntimeIdentity:
    project_id: str
    session_scope_id: str
    role_id: str
    employee_id: str
    team_instance_id: str = ""
    explicit_id: str = ""
    # 员工持久历史键（只影响会话拼接；队列 tag 仍按 session_scope_id）
    history_scope_id: str = ""

    def member_session_id(self) -> str:
        return scoped_member_session_id(
            project_id=self.project_id,
            session_scope_id=self.session_scope_id,
            role_id=self.role_id,
            employee_id=self.employee_id,
            team_instance_id=self.team_instance_id,
            explicit_id=self.explicit_id,
            history_scope_id=self.history_scope_id,
        )

    def scope_key(self) -> str:
        prefix = self.session_scope_id or self.project_id
        parts = [prefix]
        if self.team_instance_id:
            parts.append(self.team_instance_id)
        return "::".join(parts)

    def __str__(self) -> str:
        return self.member_session_id()


@dataclass
class RuntimeIdentityIndex:
    identities: dict[str, CompanyRuntimeIdentity] = field(default_factory=dict)

    def register(self, identity: CompanyRuntimeIdentity) -> None:
        key = identity.member_session_id()
        self.identities[key] = identity

    def resolve(self, session_id: str) -> CompanyRuntimeIdentity | None:
        return self.identities.get(session_id)

    def resolve_by_role(self, role_id: str) -> list[CompanyRuntimeIdentity]:
        return [i for i in self.identities.values() if i.role_id == role_id]

    def remove(self, session_id: str) -> None:
        self.identities.pop(session_id, None)
