"""契约族：隐形 bug 修复（四路审计批次）。

覆盖：
1. 配置合并对象禁止全量回写 user 层（patch 语义）+ patch 更新不泄层
2. sop_progress 投影经 company_mode 落库（生产持久化，而非只写执行器对象）
3. release_execution_claim owner 校验（旧实例不得清新接管者租约）

生产调用路径：
- BandiConfig.load → api PUT /api/settings/model / CLI /model → save(patch=...)
- CompanyDispatcher.run_work_item → EngineSeatExecutor.run_turn → store 落库
- CompanyDispatcher run_work_item finally → store.release_execution_claim(item_id, instance_id=...)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item

_INSTANCE_A = "inst-a"
_INSTANCE_B = "inst-b"


def _mk_store(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "wi.db"))


def _mk_ready_item(store: WorkItemStore) -> Any:
    item = create_work_item(title="t1", turn_type=WorkItemTurnType.EXECUTE)
    item.phase = Phase.READY
    store.save(item)
    return store.get(item.id)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")


def _mk_layered_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """构造 user + project 两层配置环境，返回 (user_config, project_config)。"""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    user_config = home / ".bandi" / "config.yaml"
    _write_yaml(
        user_config,
        {"llm": {"default_model": "model-a"}, "agent": {"mode": "execute"}},
    )
    project = tmp_path / "proj"
    project.mkdir()
    proj_config = project / ".bandi" / "config.yaml"
    _write_yaml(
        proj_config,
        {
            "agent": {"mode": "plan"},
            "permissions": {"rules": [{"tool": "bash", "decision": "deny"}]},
        },
    )
    monkeypatch.chdir(project)
    return user_config, proj_config


def test_merged_config_full_save_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """破坏者：多层合并对象全量 save() 必须显式拒绝（否则 project 权限被提升为全局）。"""
    from bandi.core.config import BandiConfig, ConfigError

    user_config, _ = _mk_layered_home(tmp_path, monkeypatch)
    cfg = BandiConfig.load()
    assert cfg.agent.mode == "plan"  # project 层覆盖生效（合并对象）
    cfg.llm.default_model = "model-b"
    before = user_config.read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        cfg.save()
    assert user_config.read_text(encoding="utf-8") == before, "拒绝时不得写入"


def test_config_save_patch_updates_only_given_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正路径：patch 只更新指定字段，project 层值（agent.mode）与 rules 不泄入 user。"""
    from bandi.core.config import BandiConfig

    user_config, _ = _mk_layered_home(tmp_path, monkeypatch)
    cfg = BandiConfig.load()
    cfg.save(patch={"llm": {"default_model": "model-b"}})
    raw = yaml.safe_load(user_config.read_text(encoding="utf-8"))
    assert raw["llm"]["default_model"] == "model-b"  # 显式变更落盘
    assert raw.get("agent", {}).get("mode", "execute") == "execute", "project 层值不得泄入"
    assert "permissions" not in raw or "rules" not in (raw.get("permissions") or {}), (
        "project 层 rules 不得提升为全局"
    )


class _PassPreparer:
    """测试替身：task_preparer 直接放行（本契约只钉 run_turn 结果落库链）。"""

    async def prepare(self, item: Any, store: Any, recruiter: Any, runtime: Any) -> Any:
        from bandi.company.task_preparer import TaskPreparationResult

        return TaskPreparationResult(True)


def test_run_work_item_persists_sop_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """破坏者：执行器返回的 sop_progress 必须经 run_work_item 落库——
    此前只写执行器持有的对象，company_mode 重新 store.get 后丢弃（桥接生产失效）。"""
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.org_engine import OrgEngine
    from bandi.company.seat_executor import EngineSeatExecutor, SeatResult

    expected = {"active": {"skill_id": "sk_hb2"}, "scope_id": "scope-hb2"}

    class ProgressSeat(EngineSeatExecutor):
        async def run_turn(self, runtime: Any, item: Any) -> SeatResult:
            return SeatResult(success=True, output="done", metadata={"sop_progress": expected})

    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    disp = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=None,
        seat_executor=ProgressSeat(),
        task_preparer=_PassPreparer(),  # type: ignore[arg-type]
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    result = asyncio.run(disp.run_work_item(item.id))
    assert result.success is True
    final = store.get(item.id)
    assert final is not None
    assert final.runtime_metadata.get("sop_progress") == expected, (
        "执行器携带的投影必须落库（HB-2：只写执行器对象=生产链丢失）"
    )


@pytest.mark.asyncio
async def test_run_work_item_sop_progress_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：真实 EngineSeatExecutor + 帧文件 → run_work_item 后 store 读回投影。"""
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.company_runtime import CompanyRuntime
    from bandi.company.org_engine import OrgEngine
    from bandi.company.seat_executor import EngineSeatExecutor
    from bandi.skills.frame_store import save_frames
    from bandi.skills.progress import frame_scope_for_session
    from bandi.skills.runtime import SkillFrameStack, SkillFrameStatus

    frames_dir = tmp_path / "frames"
    store = _mk_store(tmp_path)
    item = create_work_item(title="hb2-e2e", turn_type=WorkItemTurnType.EXECUTE)
    item.phase = Phase.READY
    store.save(item)
    item = store.get(item.id)
    assert item is not None

    # 复现 dispatcher 的 runtime 派生（session_scope_id=item.id / role 缺省 unknown /
    # 持久键 staff——读写两侧同源派生，帧 scope 随员工会话键稳定）
    # 帧写到同一 fallback scope（生产链无 frame_scope 显式写入点）
    from bandi.company.company_mode import _COMPANY_HISTORY_SCOPE

    probe_runtime = CompanyRuntime(
        project_id="default",
        session_scope_id=item.id,
        role_id="unknown",
        employee_id="",
        team_instance_id="",
        history_scope_id=_COMPANY_HISTORY_SCOPE,
    )
    probe_runtime.sync_home_teams_from_roles([])
    runtime_scope = frame_scope_for_session(probe_runtime.member_session_id())
    stack = SkillFrameStack(
        scope_id=runtime_scope,
        active={
            "task_id": "task_hb2",
            "skill_id": "sk_hb2_e2e",
            "node_id": "n1",
            "status": SkillFrameStatus.ACTIVE,
            "created_at": "T00:00:00",
            "updated_at": "T00:00:00",
        },  # type: ignore[arg-type]
    )
    save_frames(stack, str(frames_dir))

    async def fake_run_agent(**kwargs: Any) -> tuple[str, str, int, int]:
        return "ok output", "COMPLETED", 10, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)

    disp = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=None,
        seat_executor=EngineSeatExecutor(frames_dir=str(frames_dir)),
        task_preparer=_PassPreparer(),  # type: ignore[arg-type]
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    result = await disp.run_work_item(item.id)
    assert result.success is True, result.output
    final = store.get(item.id)
    assert final is not None
    progress = final.runtime_metadata.get("sop_progress")
    assert progress is not None, "端到端：store 读回必须有 sop_progress"
    assert progress["active"]["skill_id"] == "sk_hb2_e2e"


def test_release_execution_claim_respects_owner(tmp_path: Path) -> None:
    """破坏者：旧实例的 release 不得清除新接管者的租约。"""
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    assert store.claim_for_execution(item.id, _INSTANCE_A, 120)
    # 模拟接管：库内租约归 B（A 的租约已过期被回收后 B 认领）
    current = store.get(item.id)
    assert current is not None
    current.metadata["execution_claim"]["instance_id"] = _INSTANCE_B
    store.save(current)

    store.release_execution_claim(item.id, instance_id=_INSTANCE_A)
    after_a = store.get(item.id)
    assert after_a is not None
    assert after_a.metadata.get("execution_claim"), "A 的 release 不得清除 B 的租约"

    store.release_execution_claim(item.id, instance_id=_INSTANCE_B)
    after_b = store.get(item.id)
    assert after_b is not None
    assert after_b.metadata.get("execution_claim") is None, "B 自己的 release 必须生效"
