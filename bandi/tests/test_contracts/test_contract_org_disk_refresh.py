# 功能：契约——org.json 读面刷新（写前合并的读侧对偶）。
# 钉行为不钉实现：组织本体由他进程（CLI / 另一个后端 / 桌面壳）写入 org.json 后
# 本进程读面必须在**不做任何本进程写**的前提下看到新角色/员工/席位——否则
# 「团队已组建」校验（add_team）会把已落盘团队判成未组建/不存在。
# 边界：签名未变不重复读盘（幂等）/ 刷新只增不减不得复活本进程已删角色 /
# 同 id 内存权威不被盘上覆盖 / 盘上缺失或损坏不抛不改内存。
from __future__ import annotations

import json
from pathlib import Path

from bandi.company.org_engine import OrgEmployee, OrgEngine
from bandi.company.org_persistence import (
    load_org,
    refresh_org_from_disk,
    save_org,
)
from bandi.company.recruiter import Recruiter
from bandi.company.seat_recruiter import RecruitStatus


def _engine() -> tuple[OrgEngine, Recruiter]:
    return OrgEngine(), Recruiter()


def _team_on_disk(path: Path, team_id: str = "probe-team") -> OrgEngine:
    """模拟他进程实例化团队并落盘（角色 + 员工 + 已招募席位）。"""
    engine, recruiter = _engine()
    load_org(engine, recruiter, path)
    engine.register_role(
        f"{team_id}_lead",
        f"{team_id} 负责人",
        "带团队",
        None,
        [],
        {},
        team_id=team_id,
    )
    engine.register_employee(
        OrgEmployee(
            employee_id=f"{team_id}_lead",
            name=f"{team_id} 负责人",
            role_id=f"{team_id}_lead",
            metadata={"team_id": team_id},
        )
    )
    recruiter.register_seat(f"{team_id}_lead")
    recruiter.recruit(f"{team_id}_lead")
    save_org(engine, recruiter, path)
    return engine


def test_external_team_is_visible_after_refresh(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    reader, reader_recruiter = _engine()
    save_org(reader, reader_recruiter, path)
    load_org(reader, reader_recruiter, path)  # 启动基线（此时盘上还没有团队）

    _team_on_disk(path)  # 他进程写入

    assert "probe-team_lead" not in {r.id for r in reader.list_agents()}
    assert refresh_org_from_disk(reader, reader_recruiter, path) is True

    role = next((r for r in reader.list_agents() if r.id == "probe-team_lead"), None)
    assert role is not None, "读面刷新必须并入他进程落盘的角色"
    assert role.team_id == "probe-team", "team_id 必须随之落位（135a 已组建校验依赖它）"
    assert "probe-team_lead" in {e.employee_id for e in reader.list_employees()}
    seat = reader_recruiter.get_seat("probe-team_lead")
    assert seat is not None and seat.status == RecruitStatus.RECRUITED


def test_refresh_is_idempotent_when_disk_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    engine, recruiter = _engine()
    save_org(engine, recruiter, path)
    load_org(engine, recruiter, path)

    assert refresh_org_from_disk(engine, recruiter, path) is False
    assert refresh_org_from_disk(engine, recruiter, path) is False
    assert engine.list_agents()  # 内置角色仍在


def test_refresh_does_not_resurrect_removed_role(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    seed, seed_recruiter = _engine()
    seed.register_role("doomed", "待删", "", None, [], {})
    save_org(seed, seed_recruiter, path)

    engine, recruiter = _engine()
    load_org(engine, recruiter, path)
    engine.remove_role("doomed")
    assert "doomed" not in {r.id for r in engine.list_agents()}

    refresh_org_from_disk(engine, recruiter, path)
    assert "doomed" not in {r.id for r in engine.list_agents()}, "刷新只增不减也须尊重墓碑"


def test_refresh_keeps_memory_authoritative_for_same_id(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    seed, seed_recruiter = _engine()
    seed.register_role("dual", "盘上旧名", "", None, [], {})
    save_org(seed, seed_recruiter, path)

    engine, recruiter = _engine()
    load_org(engine, recruiter, path)
    role = engine.get_role("dual")
    assert role is not None
    role.name = "内存新名"

    refresh_org_from_disk(engine, recruiter, path)
    assert engine.get_role("dual").name == "内存新名", "同 id 内存必须保持权威"


def test_refresh_survives_missing_and_corrupt_disk(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    engine, recruiter = _engine()
    save_org(engine, recruiter, path)
    load_org(engine, recruiter, path)
    before = {r.id for r in engine.list_agents()}

    assert refresh_org_from_disk(engine, recruiter, tmp_path / "absent.json") is False
    assert {r.id for r in engine.list_agents()} == before

    path.write_text("{ not json", encoding="utf-8")
    assert refresh_org_from_disk(engine, recruiter, path) is False
    assert {r.id for r in engine.list_agents()} == before, "损坏文件不得清空内存组织"


def test_refresh_does_not_write_disk(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    engine, recruiter = _engine()
    save_org(engine, recruiter, path)
    load_org(engine, recruiter, path)
    _team_on_disk(path)
    revision = json.loads(path.read_text(encoding="utf-8"))["revision"]
    mtime = path.stat().st_mtime_ns

    refresh_org_from_disk(engine, recruiter, path)

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["revision"] == revision, "读面刷新不得抬 revision"
    assert path.stat().st_mtime_ns == mtime, "读面刷新不得改写 org.json"
