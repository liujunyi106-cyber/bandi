# 功能：知识库角色级作用域——权限键单源 + 运行时 ContextVar 传递
#
# 语义：OrgRole.permissions[KNOWLEDGE_BASES_PERMISSION_KEY] = 该角色可见知识库 base_id
# 列表；键不存在 = 不过滤（opt-in，默认态零影响）；键存在但为空列表 = fail-closed 全禁
# 。执行面经 ContextVar 传递——seat_executor 在 run_agent
# 前写入、执行后复位（await 链天然传播，工具侧只读）。
#
# 值列表成员 = 允许访问的库 id；seat 级按角色批量过滤与工具侧读取共用一份配置。

from __future__ import annotations

from contextvars import ContextVar, Token

# OrgRole.permissions 键——该角色可见的知识库 base_id 列表（库级起步）。
KNOWLEDGE_BASES_PERMISSION_KEY = "knowledge_base"

_scope_var: ContextVar[list[str] | None] = ContextVar("knowledge_bases_scope", default=None)
_employee_var: ContextVar[str | None] = ContextVar("knowledge_employee_scope", default=None)


def set_runtime_knowledge_bases(base_ids: list[str] | None) -> Token[list[str] | None]:
    """功能：写入当前执行链的知识库可见域（None = 不过滤）；返回 Token 供复位。"""
    return _scope_var.set(base_ids)


def reset_runtime_knowledge_bases(token: Token[list[str] | None]) -> None:
    """功能：复位可见域（执行结束防泄漏到同任务后续运行）。"""
    _scope_var.reset(token)


def get_runtime_knowledge_bases() -> list[str] | None:
    """功能：读取当前执行链的知识库可见域（None = 未配置不过滤；[] = 全禁）。"""
    return _scope_var.get()


def set_runtime_knowledge_employee(employee_id: str | None) -> Token[str | None]:
    """功能：写入当前执行链的员工可见域——资料库节点按员工收窄；None = 不过滤。"""
    return _employee_var.set(employee_id or None)


def reset_runtime_knowledge_employee(token: Token[str | None]) -> None:
    """功能：复位员工可见域。"""
    _employee_var.reset(token)


def get_runtime_knowledge_employee() -> str | None:
    """功能：读取当前执行链的员工可见域（None = 不按员工过滤）。"""
    return _employee_var.get()


def intersect_knowledge_bases(
    role_bases: list[str] | None, binding_bases: list[str] | None
) -> list[str] | None:
    # 功能：角色权限与员工绑定的交集——None 表示该侧不设限；空列表保持 fail-closed。
    if role_bases is not None and not role_bases:
        return []
    if role_bases is None:
        return list(binding_bases) if binding_bases is not None else None
    if binding_bases is None:
        return list(role_bases)
    allowed = set(binding_bases)
    return [base_id for base_id in role_bases if base_id in allowed]
