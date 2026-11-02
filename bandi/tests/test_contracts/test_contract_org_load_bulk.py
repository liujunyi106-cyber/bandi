# 功能：契约——org.json 加载/合并不得触发持久化回调（CLI 启动写放大 O(N²) 根因）。
# 破坏者视角：load_org 每注册一个角色触发 save_org → save 内 merge 又注册盘上缺失角色
from __future__ import annotations

from pathlib import Path

from bandi.company.org_engine import OrgEngine
from bandi.company.org_persistence import load_org, save_org
from bandi.company.seat_recruiter import Recruiter


def _seed_org(path: Path, n: int = 10) -> None:
    # 功能：造盘上含 n 个角色的 org.json（无回调，正常写一次）
    org = OrgEngine()
    rec = Recruiter()
    for i in range(n):
        org.register_role(f"r{i}", f"R{i}", "", None, [], {})
    save_org(org, rec, path)


def test_contract_load_org_suppresses_persist_callback(tmp_path: Path) -> None:
    """契约（写放大根因）：load_org 期间注册角色/员工/席位不得触发持久化回调——
    回调由 CLI 绑定为 save_org，逐角色触发 + 写前合并再注册 = O(N²) 全量写盘。"""
    path = tmp_path / "org.json"
    _seed_org(path, n=10)

    org = OrgEngine()
    rec = Recruiter()
    calls: list[int] = []
    org._persist_callable = lambda: calls.append(1)
    load_org(org, rec, path)

    assert org.get_role("r9") is not None, "加载结果不受抑制影响"
    assert calls == [], f"load 期间不得触发持久化（实测 {len(calls)} 次）"


def test_contract_save_org_merge_no_nested_persist(tmp_path: Path) -> None:
    """契约（嵌套写）：save_org 的盘上合并注册缺失角色时不得再触发持久化回调
    （否则 save → merge → register → persist 嵌套递归）。"""
    path = tmp_path / "org.json"
    _seed_org(path, n=5)

    org = OrgEngine()
    rec = Recruiter()
    org.register_role("r2", "R2", "", None, [], {})  # 内存仅 1 个（盘上 5 个待合并）
    calls: list[int] = []
    org._persist_callable = lambda: calls.append(1)
    save_org(org, rec, path)

    assert calls == [], f"save 合并期间不得触发嵌套持久化（实测 {len(calls)} 次）"
    assert org.get_role("r0") is not None, "合并结果保留（盘上角色注册回内存）"


def test_contract_persist_callback_restored_after_load(tmp_path: Path) -> None:
    """契约（恢复不变式）：load 结束必须恢复持久化回调（抑制不得泄漏到加载后）。"""
    path = tmp_path / "org.json"
    _seed_org(path, n=3)

    org = OrgEngine()
    rec = Recruiter()
    calls: list[int] = []
    org._persist_callable = lambda: calls.append(1)
    load_org(org, rec, path)
    org.register_role("r-new", "新角色", "", None, [], {})
    assert len(calls) >= 1, "load 完成后回调必须恢复（后续变更仍要落盘）"


def test_contract_persist_callback_restored_on_error(tmp_path: Path, monkeypatch) -> None:
    """契约（复查加固·异常安全）：load 注册段抛非预期异常时，持久化回调必须仍
    恢复（修复前手工设置-恢复在异常路径泄漏 → 进程内后续变更静默不落盘）。"""
    import pytest

    path = tmp_path / "org.json"
    _seed_org(path, n=3)

    org = OrgEngine()
    rec = Recruiter()
    calls: list[int] = []
    org._persist_callable = lambda: calls.append(1)

    def failing_register_role(*args, **kwargs):
        raise RuntimeError("registration blew up")

    monkeypatch.setattr(org, "register_role", failing_register_role)
    with pytest.raises(RuntimeError):
        load_org(org, rec, path)

    assert callable(getattr(org, "_persist_callable", None)), (
        "异常路径必须恢复持久化回调（否则后续变更静默不落盘）"
    )
    org._persist()
    assert calls, "恢复后的回调必须可触发"
