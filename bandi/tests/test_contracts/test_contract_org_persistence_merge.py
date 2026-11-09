# 功能：契约——org.json 写前重读合并 + revision（跨进程新增不丢）。
# （last-write-wins 不再吞掉他人新增）；同 id 条目以本进程内存为权威；
# revision 单调递增；盘上损坏文件不阻断保存。
from __future__ import annotations

import json
from pathlib import Path

from bandi.company.org_engine import OrgEngine
from bandi.company.org_persistence import load_org, save_org
from bandi.company.recruiter import Recruiter
from bandi.company.reorg_manager import ReorgChangeSet, ReorgRoleChange
from bandi.company.seat_recruiter import RecruitStatus


def _engine() -> tuple[OrgEngine, Recruiter]:
    return OrgEngine(), Recruiter()


def test_save_org_merges_concurrent_cli_writer(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    api_engine, api_recruiter = _engine()
    save_org(api_engine, api_recruiter, path)
    load_org(api_engine, api_recruiter, path)  # API 启动加载基线

    cli_engine, cli_recruiter = _engine()
    load_org(cli_engine, cli_recruiter, path)
    cli_engine.register_role("team_a_lead", "A 负责人", "", None, [], {})
    save_org(cli_engine, cli_recruiter, path)  # CLI 先写 team_a

    api_engine.register_role("team_b_lead", "B 负责人", "", None, [], {})
    save_org(api_engine, api_recruiter, path)  # API 后写（旧实现覆盖丢 team_a）

    fresh, fresh_recruiter = _engine()
    load_org(fresh, fresh_recruiter, path)
    ids = {r.id for r in fresh.list_agents()}
    assert "team_a_lead" in ids, "他进程新增角色不得被覆盖"
    assert "team_b_lead" in ids


def test_revision_advances_on_every_save(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    engine, recruiter = _engine()
    save_org(engine, recruiter, path)
    first = json.loads(path.read_text(encoding="utf-8"))["revision"]
    save_org(engine, recruiter, path)
    second = json.loads(path.read_text(encoding="utf-8"))["revision"]

    assert second == first + 1, "每次保存 revision 必须 +1"


def test_same_id_memory_wins_disk_extras_kept(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    seed_engine, seed_recruiter = _engine()
    seed_engine.register_role("r1", "旧名", "", None, [], {})
    seed_engine.register_role("r2", "保留角色", "", None, [], {})
    save_org(seed_engine, seed_recruiter, path)

    engine, recruiter = _engine()
    engine.register_role("r1", "新名", "", None, [], {})  # 未见 r2
    save_org(engine, recruiter, path)

    fresh, fresh_recruiter = _engine()
    load_org(fresh, fresh_recruiter, path)
    names = {r.id: r.name for r in fresh.list_agents()}
    assert names.get("r1") == "新名", "同 id 以本进程内存为权威"
    assert "r2" in names, "盘上独有角色必须保留"


def test_corrupt_disk_file_does_not_block_save(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    path.write_text("{ not json", encoding="utf-8")
    engine, recruiter = _engine()
    engine.register_role("r1", "正常角色", "", None, [], {})

    save_org(engine, recruiter, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["revision"] >= 1
    assert any(r["id"] == "r1" for r in payload["roles"])


# ---------- 合并语义边界：组织状态可删可改，union 只增不减会「复活删除」 ----------


def test_removed_role_is_not_resurrected_on_save(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    engine, recruiter = _engine()
    engine.register_role("alpha", "Alpha", "", None, [], {})
    save_org(engine, recruiter, path)
    load_org(engine, recruiter, path)  # 基线：本进程已镜像磁盘

    engine.remove_role("alpha")
    assert "alpha" not in {r.id for r in engine.list_agents()}
    save_org(engine, recruiter, path)

    fresh, fresh_recruiter = _engine()
    load_org(fresh, fresh_recruiter, path)
    assert "alpha" not in {r.id for r in fresh.list_agents()}, "删除的角色不得被写前合并复活"


def test_released_seat_is_not_resurrected_on_save(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    engine, recruiter = _engine()
    engine.register_role("beta", "Beta", "", None, [], {})
    recruiter.register_seat("beta")
    recruiter.recruit("beta")
    save_org(engine, recruiter, path)
    load_org(engine, recruiter, path)

    assert recruiter.release("beta") is True
    save_org(engine, recruiter, path)

    assert recruiter.get_seat("beta").status == RecruitStatus.AVAILABLE, "释放不得被盘上状态翻回"
    seats = json.loads(path.read_text(encoding="utf-8"))["seats"]
    assert seats["beta"] == RecruitStatus.AVAILABLE.value


def test_changeset_removed_role_is_not_resurrected(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    engine, recruiter = _engine()
    engine.register_role("gamma", "Gamma", "", None, [], {})
    save_org(engine, recruiter, path)
    load_org(engine, recruiter, path)

    engine.apply_changeset(
        ReorgChangeSet(role_changes=[ReorgRoleChange(action="remove", role_id="gamma")])
    )
    save_org(engine, recruiter, path)

    fresh, fresh_recruiter = _engine()
    load_org(fresh, fresh_recruiter, path)
    assert "gamma" not in {r.id for r in fresh.list_agents()}, "changeset 移除同样不得复活"


def test_deletion_and_concurrent_addition_coexist(tmp_path: Path) -> None:
    path = tmp_path / "org.json"
    seed, seed_recruiter = _engine()
    seed.register_role("doomed", "待删", "", None, [], {})
    save_org(seed, seed_recruiter, path)

    writer, writer_recruiter = _engine()
    load_org(writer, writer_recruiter, path)
    writer.register_role("from_other_process", "他进程新增", "", None, [], {})
    save_org(writer, writer_recruiter, path)

    owner, owner_recruiter = _engine()
    load_org(owner, owner_recruiter, path)
    owner.remove_role("doomed")
    save_org(owner, owner_recruiter, path)

    fresh, fresh_recruiter = _engine()
    load_org(fresh, fresh_recruiter, path)
    ids = {r.id for r in fresh.list_agents()}
    assert "doomed" not in ids, "删除必须生效"
    assert "from_other_process" in ids, "他进程新增仍须并入（不得退化为全量覆盖）"
