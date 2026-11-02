"""
   会话互斥：run_agent_turn（CLI/API/公司链唯一执行入口）经 _acquire_session_slot
     non-blocking 认领；同 session 第二并发快速失败（SESSION_BUSY），防双击发送/
     双入口同时跑同一会话导致消息历史交叉、checkpoint 互踩。
   work item CAS：run_work_item 经 _try_claim_work_item check-and-set——进程内
     in-flight 集合原子判定 + 卡上 execution_claim 租约 double-check；他者有效
     租约（另一实例正在执行）直接拒，防双进程双双执行同一卡。

破坏者断言：
   - 同 session 并发 turn：第二个必须快速失败而非排队/并发执行
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest


@pytest.fixture()
def store(tmp_path: Any) -> Any:
    from bandi.company.store import WorkItemStore

    return WorkItemStore(db_path=str(tmp_path / "work_items.db"))


# ── 进程内 per-session 锁原语 ─────────────────────────────────────────


def test_session_slot_acquire_release_semantics() -> None:
    """
    不同 session 互不影响（non-blocking 语义——调用方快速失败，不排队）。"""
    from bandi.core.agent_runner import _acquire_session_slot, _release_session_slot

    assert _acquire_session_slot("sess-a") is True, "首次认领必须成功"
    assert _acquire_session_slot("sess-a") is False, (
        "同 session 未释放时二次认领必须失败——并发防护本体"
    )
    assert _acquire_session_slot("sess-b") is True, "不同 session 必须互不影响"
    _release_session_slot("sess-a")
    assert _acquire_session_slot("sess-a") is True, "释放后必须可再认领"
    _release_session_slot("sess-b")
    _release_session_slot("sess-a")


def test_session_slot_release_is_idempotent() -> None:
    from bandi.core.agent_runner import _acquire_session_slot, _release_session_slot

    assert _acquire_session_slot("sess-c") is True
    _release_session_slot("sess-c")
    _release_session_slot("sess-c")  # 幂等：二次 release 无操作不抛
    _release_session_slot("sess-never-acquired")  # 未认领的 session 释放同样无操作


# ── work item 认领 CAS ────────────────────────────────────────────────


class _SlowSeatExecutor:
    """可门控的 seat executor：run_turn 阻塞到 release 被置位，统计执行次数。"""

    def __init__(self) -> None:
        self.runs = 0
        self.gate = asyncio.Event()

    async def prepare_seat(self, runtime: Any, item: Any) -> None:
        return None

    async def run_turn(self, runtime: Any, item: Any) -> Any:
        from bandi.company.seat_executor import SeatResult

        self.runs += 1
        await asyncio.wait_for(self.gate.wait(), timeout=10)
        return SeatResult(success=True, output="done")

    async def interrupt(self, runtime: Any) -> None:
        return None

    async def checkpoint(self, runtime: Any) -> dict:
        return {}


def _build_dispatcher(store: Any, executor: Any) -> Any:
    from bandi.company.communication import CommunicationManager
    from bandi.company.company_mode import CompanyDispatcher
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter

    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        communication=CommunicationManager(workspace=".", project_id="p", session_id="s"),
        seat_executor=executor,
    )


def _make_item(store: Any, title: str = "cas-target") -> Any:
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    item = create_work_item(title=title, turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker")
    store.save(item)
    return item


@pytest.mark.asyncio
async def test_concurrent_run_work_item_executes_once(store: Any) -> None:
    executor = _SlowSeatExecutor()
    disp = _build_dispatcher(store, executor)
    item = _make_item(store)
    assert disp.claim("worker")

    first = asyncio.create_task(disp.run_work_item(item.id))
    await asyncio.sleep(0.05)  # 让第一个进入 run_turn（gate 卡住）
    second = await disp.run_work_item(item.id)
    executor.gate.set()
    await first

    assert executor.runs == 1, (
        f"同一卡并发认领必须只执行一次（实际 {executor.runs} 次）——CAS 缺失即双执行"
    )
    assert second.success is False
    assert "already" in second.output.lower(), (
        f"第二个认领必须快速失败并说明原因（实际 {second.output!r}）"
    )


@pytest.mark.asyncio
async def test_foreign_active_lease_blocks_claim(store: Any) -> None:
    """
    租约直接覆盖写自己的 instance_id 后照跑（跨进程双执行路径）。"""
    executor = _SlowSeatExecutor()
    disp = _build_dispatcher(store, executor)
    disp.instance_id = "this-instance"
    item = _make_item(store)
    # 他者实例的有效租约
    latest = store.get(item.id)
    assert latest is not None
    latest.metadata["execution_claim"] = {
        "instance_id": "other-instance",
        "claimed_at": datetime.now(UTC).isoformat(),
        "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
    }
    store.save(latest)
    assert disp.claim("worker")

    result = await disp.run_work_item(item.id)

    assert executor.runs == 0, "他者租约有效期间不得执行"
    assert result.success is False


@pytest.mark.asyncio
async def test_expired_or_own_lease_allows_execution(store: Any) -> None:
    """
    破坏正常单进程执行链）。"""
    executor = _SlowSeatExecutor()
    disp = _build_dispatcher(store, executor)
    disp.instance_id = "this-instance"
    item = _make_item(store)
    latest = store.get(item.id)
    assert latest is not None
    latest.metadata["execution_claim"] = {
        "instance_id": "long-dead-instance",
        "claimed_at": (datetime.now(UTC) - timedelta(seconds=600)).isoformat(),
        "lease_expires_at": (datetime.now(UTC) - timedelta(seconds=480)).isoformat(),
    }
    store.save(latest)

    assert disp.claim("worker")
    task = asyncio.create_task(disp.run_work_item(item.id))
    await asyncio.sleep(0.05)
    executor.gate.set()
    result = await task

    assert result.success is True, f"过期租约必须可接管执行（实际 {result.output!r}）"
    assert executor.runs == 1


# ── busy 早退零副作用 + 租约写在最新快照（复查返工）──────────────────


@pytest.mark.asyncio
async def test_busy_rejection_leaves_no_side_effects(tmp_path: Any) -> None:
    """
    用户消息不得落库（幽灵消息）。回归点：互斥检查原在资源构建后，被拒时
    消息已 append、全套 provider/ctx 已白构建。检查点必须前移到副作用之前。"""
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import (
        _acquire_session_slot,
        _release_session_slot,
        run_agent_turn,
    )
    from bandi.core.config import BandiConfig
    from bandi.memory.session.store import SessionStore

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        assert _acquire_session_slot(sid) is True
        try:
            result = await run_agent_turn(
                user_input="并发第二条",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )
            assert result.terminal_reason == "SESSION_BUSY"
            branch = store.get_branch(sid)
            user_msgs = [e for e in branch if e.get("entry_type") == "message"]
            assert user_msgs == [], (
                f"被拒 turn 不得落库任何消息（实际 {len(user_msgs)} 条）——幽灵用户消息"
            )
        finally:
            _release_session_slot(sid)
        # 释放槽位后同一会话可正常执行
        result2 = await run_agent_turn(
            user_input="释放后重试",
            session_id=sid,
            session_store=store,
            bandi_config=cfg,
        )
        assert result2.reply is not None, "释放槽位后必须可正常执行"
        store.close()


@pytest.mark.asyncio
async def test_claim_lease_written_on_freshest_snapshot(store: Any) -> None:
    """
    CAS 认领基于 store 内部最新读——卡上窗口期并发 metadata 变更不得被丢失。
    （回归点：认领/租约写入必须保留卡上既有字段，旧快照整体回写会吞掉并发变更。）"""
    executor = _SlowSeatExecutor()
    disp = _build_dispatcher(store, executor)
    item = _make_item(store)
    # 模拟检查窗口期他处的 metadata 变更（hook/其他进程）
    from bandi.company.phase import Phase

    fresh = store.get(item.id)
    assert fresh is not None
    fresh.metadata["window_change"] = "must-survive"
    fresh.phase = Phase.READY  # CAS 认领要求卡在 READY（QUEUED 先转 READY）
    store.save(fresh)

    assert disp._try_claim_work_item(item.id) is True

    final = store.get(item.id)
    assert final is not None
    assert final.metadata.get("window_change") == "must-survive", (
        f"认领必须保留卡上并发字段（实际 metadata={final.metadata}）"
    )
    assert "execution_claim" in final.metadata, "认领成功必须写租约"


@pytest.mark.asyncio
async def test_slot_released_when_pre_try_setup_fails(tmp_path: Any) -> None:
    """
    落库/EventBus/订阅抛）必须释放槽位——泄漏将导致该 session 后续所有 turn
    永远 SESSION_BUSY 直到进程重启（存量 bug：release 原只挂 loop.run 路径）。"""
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import (
        _acquire_session_slot,
        _release_session_slot,
        run_agent_turn,
    )
    from bandi.core.config import BandiConfig
    from bandi.memory.session.store import SessionStore

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""

        # 模拟 setup 段异常（磁盘满/sqlite 锁忙等——append_message 抛）
        def _boom(session_id: str, msg: Any) -> None:
            raise RuntimeError("simulated sqlite failure")

        original_append = store.append_message
        store.append_message = _boom  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError):
                await run_agent_turn(
                    user_input="触发 setup 异常",
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
        finally:
            store.append_message = original_append  # type: ignore[method-assign]

        # 槽位必须已释放——下个 turn 可正常认领执行
        assert _acquire_session_slot(sid) is True, (
            "setup 异常后槽位必须已释放——泄漏即永久 SESSION_BUSY"
        )
        _release_session_slot(sid)

        result = await run_agent_turn(
            user_input="恢复后执行",
            session_id=sid,
            session_store=store,
            bandi_config=cfg,
        )
        assert result.reply is not None
        store.close()
