# 功能：自动补员——注册角色的员工/席位缺失时按角色档案补齐（认领前置条件）。

from __future__ import annotations

from typing import Any

from bandi.company.org_engine import OrgEmployee


def find_active_role_employee(
    org_engine: Any, role_id: str, *, exclude_employee_id: str = ""
) -> Any | None:
    """功能：返回占用该角色的在职/暂停员工（1:1 服务层校验单一入口）。"""
    role_key = str(role_id or "").strip()
    if not role_key:
        return None
    for employee in org_engine.list_employees(include_inactive=True):
        if employee.employee_id == exclude_employee_id:
            continue
        if str(getattr(employee, "role_id", "") or "") != role_key:
            continue
        if str(getattr(employee, "status", "active")) in {"active", "paused"}:
            return employee
    return None


def ensure_role_staffed(org_engine: Any, recruiter: Any, role_id: str) -> str:
    """功能：为已注册角色补齐员工与席位；返回 'ok' | 'role-not-registered' | 'failed'。

    只补注册角色（未注册角色的卡在创建面即被拒绝）；已存在员工不覆盖；
    不主动 recruit（席位保持 AVAILABLE，由 claim→dispatch 一步招募+占用）。
    """
    role_key = str(role_id or "").strip()
    if not role_key:
        return "failed"
    if recruiter.get_seat(role_key) is not None:
        return "ok"
    role_getter = getattr(org_engine, "get_role", None)
    if role_getter is None:
        # 轻量/桩组织引擎（无角色注册面）不参与补员判定
        return "skip"
    role = role_getter(role_key)
    if role is None:
        return "role-not-registered"
    try:
        if find_active_role_employee(org_engine, role_key) is None:
            org_engine.register_employee(
                OrgEmployee(
                    employee_id=role_key,
                    name=str(getattr(role, "name", "") or role_key),
                    role_id=role_key,
                    description=str(getattr(role, "responsibility", "") or ""),
                    metadata={"source": "auto_staffing"},
                )
            )
        recruiter.register_seat(role_key)
    except Exception:  # noqa: BLE001 — 补员失败按未补员处理（调度侧标记可见失败）
        return "failed"
    return "ok"
