# 功能：契约 — 团队记忆提名钩子 + API（Phase2-2）
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.agent.verifier import Verdict


def _ctx(team_id: str = "team-a", **extra: Any) -> Any:
    meta: dict[str, Any] = {
        "team_id": team_id,
        "project_id": "p1",
        "employee_id": "e1",
        "session_id": "s1",
    }
    meta.update(extra)
    return SimpleNamespace(meta=meta, turn_index=7)


def _verdict(action: str = "pass", analysis: str = "") -> Verdict:
    return Verdict(action=action, reason="功能完成", analysis=analysis)


async def _no_emit(*a: Any, **k: Any) -> None:
    return None


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from bandi.memory import staging as staging_mod

    monkeypatch.setattr(staging_mod, "default_memory_store_dir", lambda: tmp_path / "pending")
    return tmp_path / "pending"


class TestNominationHook:
    def test_significant_verdict_nominated_with_dims(self, tmp_store: Path) -> None:
        """显著性门控：返工必提名，维度齐全 + 建议沉淀指向（员工/技能文件）。"""
        from bandi.agent.states import _nominate_team_lesson
        from bandi.memory.team_staging import TeamMemoryStaging

        _nominate_team_lesson(_ctx(), "优化慢查询", _verdict("rework", analysis="先看执行计划"))
        pending = TeamMemoryStaging(tmp_store).list_pending()
        assert len(pending) == 1
        payload = pending[0].payload
        assert payload["team_id"] == "team-a" and payload["project_id"] == "p1"
        assert payload["employee_id"] == "e1" and payload["turn_index"] == 7
        assert "优化慢查询" in pending[0].summary and "先看执行计划" in pending[0].summary
        # 提名卡须说清改什么文件/哪个员工技能（learned skill 落位提示）
        assert "建议沉淀" in pending[0].summary, pending[0].summary
        assert "SKILL.md" in pending[0].summary and "e1" in pending[0].summary

    def test_fail_and_escalate_not_nominated(self, tmp_store: Path) -> None:
        """仅返工/工作成功经验提名——fail/escalate 不提名（2026-10-16 拍板）。"""
        from bandi.agent.states import _nominate_team_lesson
        from bandi.memory.team_staging import TeamMemoryStaging

        _nominate_team_lesson(_ctx(), "优化慢查询", _verdict("fail", analysis="先看执行计划"))
        _nominate_team_lesson(_ctx(), "优化慢查询", _verdict("escalate", analysis="先看执行计划"))
        assert TeamMemoryStaging(tmp_store).list_pending() == []

    def test_plain_pass_not_nominated(self, tmp_store: Path) -> None:
        """纯 pass（无多步成功）不提名——不能每个工作轮都提名。"""
        from bandi.agent.states import _nominate_team_lesson
        from bandi.memory.team_staging import TeamMemoryStaging

        _nominate_team_lesson(_ctx(), "优化慢查询", _verdict("pass", analysis="先看执行计划"))
        assert TeamMemoryStaging(tmp_store).list_pending() == []

    def test_signature_dedup_across_turns(self, tmp_store: Path) -> None:
        """同团队同任务同结论跨轮去重——只留一条在途。"""
        from bandi.agent.states import _nominate_team_lesson
        from bandi.memory.team_staging import TeamMemoryStaging

        _nominate_team_lesson(_ctx(), "优化慢查询", _verdict("rework", analysis="先看执行计划"))
        ctx2 = _ctx()
        ctx2.turn_index = 8
        _nominate_team_lesson(ctx2, "优化慢查询", _verdict("rework", analysis="先看执行计划"))
        assert len(TeamMemoryStaging(tmp_store).list_pending()) == 1

    def test_no_team_no_nomination(self, tmp_store: Path) -> None:
        from bandi.agent.states import _nominate_team_lesson
        from bandi.memory.team_staging import TeamMemoryStaging

        _nominate_team_lesson(_ctx(team_id=""), "优化慢查询", _verdict())
        assert TeamMemoryStaging(tmp_store).list_pending() == []

    def test_hook_crash_silent(self, tmp_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """破坏者：暂存崩溃不得影响反思主链路。"""
        from bandi.agent.states import _nominate_team_lesson

        def _boom(*a: Any, **k: Any) -> str:
            raise RuntimeError("boom")

        import bandi.memory.team_staging as ts_mod

        monkeypatch.setattr(ts_mod, "TeamMemoryStaging", _boom)
        _nominate_team_lesson(_ctx(), "x", _verdict("rework"))  # 不得抛


class TestReflectingNominationGate:
    """`on_reflecting` 的提名门控——纯文本轮（无工具证据）不跑 judge；提名走
    显著性门控（2026-10-16 重设计）：失败/返工/升级与多步成功才提名，
    纯 pass 与非工作轮零提名（噪声闸门）。"""

    class _Verifier:
        def __init__(self, required: bool = False, action: str = "pass") -> None:
            self._required = required
            self._action = action
            self.judge_calls = 0

        def verification_required(self, ctx: Any) -> bool:
            return self._required

        async def evaluate(self, packet: Any) -> Verdict:
            self.judge_calls += 1
            return Verdict(
                action=self._action,
                reason=f"judge 判定{self._action}",
                analysis="逐条核对完成",
            )

        def apply_stagnation_cap(self, state: dict, verdict: Verdict) -> Verdict:
            return verdict

    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        from bandi.memory import staging as staging_mod
        from bandi.memory.team_staging import TeamMemoryStaging

        staging_dir = tmp_path / "pending"
        monkeypatch.setattr(staging_mod, "default_memory_store_dir", lambda: staging_dir)
        return SimpleNamespace(
            staging_dir=staging_dir, pending=lambda: TeamMemoryStaging(staging_dir).list_pending()
        )

    def _ctx(self, verifier: Any, *, work_item_id: str = "wi-1", **meta: Any) -> Any:
        full: dict[str, Any] = {
            "team_id": "team-a",
            "project_id": "p1",
            "employee_id": "e1",
            "session_id": "s1",
            "work_item_id": work_item_id,
        }
        full.update(meta)
        return SimpleNamespace(
            terminal_reason=None,
            final_message=None,
            raw_input="优化慢查询",
            messages=[],
            current_content="根因是缺复合索引，已给出 DDL",
            tool_results=[],
            safety=SimpleNamespace(verifier=verifier),
            verifier_state=SimpleNamespace(rework_count=0, rework_feedback=""),
            meta=full,
            turn_index=3,
            bus=SimpleNamespace(emit=_no_emit),
        )

    @pytest.mark.asyncio
    async def test_pure_text_pass_work_turn_not_nominated(self, env: Any) -> None:
        """纯文本 pass 工作轮不提名（显著性门控——寒暄/无工具证据无沉淀价值）。"""
        from bandi.agent.base import TerminalReason
        from bandi.agent.states import on_reflecting

        verifier = self._Verifier(required=False)
        ctx = self._ctx(verifier)
        await on_reflecting(ctx)

        assert verifier.judge_calls == 0, "纯文本轮无工具证据可核，不应花 judge 的钱"
        assert env.pending() == []
        assert ctx.terminal_reason is TerminalReason.VERIFIED

    @pytest.mark.asyncio
    async def test_pure_text_multi_step_success_not_nominated(self, env: Any) -> None:
        """多步成功（工具≥3 零失败）属 pass 类——不提名（仅返工生成教训提名）。"""
        from bandi.agent.states import on_reflecting

        verifier = self._Verifier(required=False)
        ctx = self._ctx(verifier)
        ctx.tool_results = [object(), object(), object()]
        ctx.meta["observing_digest"] = {"tool_count": 3, "failed_tools": []}
        await on_reflecting(ctx)

        assert env.pending() == [], "pass 类（含多步成功经验）不得产生待确认提名"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("work_item_id", ["", "   ", None])
    async def test_pure_text_non_work_turn_not_nominated(self, env: Any, work_item_id: Any) -> None:
        """非工作轮（员工闲聊/群聊响应）零提名——纯文本轮的主要噪声来源。"""
        from bandi.agent.states import on_reflecting

        await on_reflecting(self._ctx(self._Verifier(required=False), work_item_id=work_item_id))
        assert env.pending() == []

    @pytest.mark.asyncio
    async def test_tool_turn_rework_still_judged_and_nominated(self, env: Any) -> None:
        """工具轮 judge 判返工→教训提名，judge 调用未被门控吞掉。"""
        from bandi.agent.states import on_reflecting

        verifier = self._Verifier(required=True, action="rework")
        await on_reflecting(self._ctx(verifier))

        assert verifier.judge_calls == 1
        pending = env.pending()
        assert len(pending) == 1
        assert "judge 判定rework" in pending[0].summary
        assert "逐条核对完成" in pending[0].summary

    @pytest.mark.asyncio
    async def test_no_team_context_nothing_leaks(self, env: Any) -> None:
        """破坏者：秘书链路（无 team_id）即便工作轮也零提名——维度不全不落盘。"""
        from bandi.agent.base import TerminalReason
        from bandi.agent.states import on_reflecting

        ctx = self._ctx(self._Verifier(required=False), team_id="")
        await on_reflecting(ctx)
        assert env.pending() == []
        assert ctx.terminal_reason is TerminalReason.VERIFIED

    @pytest.mark.asyncio
    async def test_missing_verifier_no_nomination_no_crash(self, env: Any) -> None:
        """破坏者：verifier 未装配（verification 关闭）——早退不提名也不抛。"""
        from bandi.agent.base import TerminalReason
        from bandi.agent.states import on_reflecting

        ctx = self._ctx(None)
        await on_reflecting(ctx)
        assert env.pending() == []
        assert ctx.terminal_reason is TerminalReason.VERIFIED


class TestTeamMemoryAPI:
    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        import importlib

        from fastapi.testclient import TestClient

        app_mod = importlib.import_module("bandi.api.app")
        from bandi.memory import staging as staging_mod
        from bandi.memory.team_staging import TeamMemoryStaging

        monkeypatch.setattr(staging_mod, "default_memory_store_dir", lambda: tmp_path / "pending")
        monkeypatch.setattr(app_mod, "_API_KEY", "k-test")
        headers = {"Authorization": "Bearer k-test"}
        c = TestClient(app_mod.app)
        c.headers.update(headers)
        c.staging = TeamMemoryStaging(tmp_path / "pending")
        return c

    def test_confirm_lands_to_team_partition(
        self, client: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.memory.episodic import EpisodicStore

        epi = EpisodicStore(str(tmp_path / "epi.db"))
        import bandi.memory.episodic as epi_mod

        monkeypatch.setattr(epi_mod, "get_shared_episodic", lambda: epi, raising=True)
        id8 = client.staging.propose(
            summary="团队约定：先看执行计划",
            project_id="p1",
            team_id="team-a",
            employee_id="e1",
            session_id="s1",
            turn_index=1,
        )
        r = client.post(f"/api/memory/team-pending/{id8}/confirm")
        assert r.status_code == 200 and r.json()["confirmed"] is True
        hits = epi.list_recent(project_id="p1", team_id="team-a")
        assert len(hits) == 1 and "执行计划" in hits[0].summary

    def test_confirm_missing_404(self, client: Any) -> None:
        r = client.post("/api/memory/team-pending/deadbeef/confirm")
        assert r.status_code == 404

    def test_confirm_with_create_skill_writes_skill_md(
        self,
        client: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """批准时勾选生成技能卡——SKILL.md 真落盘，名称/教训可读回。"""
        from bandi.memory.episodic import EpisodicStore

        epi = EpisodicStore(str(tmp_path / "epi.db"))
        import bandi.memory.episodic as epi_mod

        monkeypatch.setattr(epi_mod, "get_shared_episodic", lambda: epi, raising=True)
        monkeypatch.setenv("BANDI_SKILLS_DIR", str(tmp_path / "skills"))
        id8 = client.staging.propose(
            summary="任务：我现在告诉你7个维度的信息吧",
            project_id="p1",
            team_id="team-a",
            employee_id="e1",
            session_id="s1",
            turn_index=1,
            extra={
                "reason": "用户尚未提供7维信息，Agent误判任务完成",
                "analysis": "分析：先确认 7 项信息齐全，再进入产出。",
            },
        )
        r = client.post(
            f"/api/memory/team-pending/{id8}/confirm",
            json={"create_skill": True, "skill_name": "7 维信息采集检查清单"},
        )
        assert r.status_code == 200, r.text
        skill_path = Path(r.json()["skill_path"])
        assert skill_path.is_file()
        text = skill_path.read_text(encoding="utf-8")
        assert "7 维信息采集检查清单" in text
        assert "7维信息" in text

    def test_confirm_land_failure_keeps_pending_retryable(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """破坏者：落库面不可写（只读库/磁盘满/沙箱拦截）时——**待确认项必须仍在**。

        端到端暴露：原实现「先消费暂存项、后落库」
        落库失败即 HTTP 500 且 pending 归零 ⇒ 用户点了「批准」但记忆既没落库
        待办也消失，**不可重试**。落库必须先于消费。
        """
        import bandi.memory.episodic as epi_mod

        class _ReadOnly:
            def record(self, episode: Any) -> Any:
                raise RuntimeError("attempt to write a readonly database")

        monkeypatch.setattr(epi_mod, "get_shared_episodic", lambda: _ReadOnly(), raising=True)
        id8 = client.staging.propose(
            summary="落库会失败的一条",
            project_id="p1",
            team_id="team-a",
            employee_id="e1",
            session_id="s1",
            turn_index=1,
        )
        r = client.post(f"/api/memory/team-pending/{id8}/confirm")
        assert r.status_code >= 500, f"落库失败必须报错而非静默成功，实际 {r.status_code}"
        assert client.staging.get_pending(id8) is not None, (
            "落库失败时暂存项必须保留（否则用户点一次批准就永久丢失这条记忆）"
        )

    def test_confirm_land_is_idempotent(
        self, client: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """落库用稳定 episode_id ⇒ 重试（消费失败/进程崩溃后再点）不产生重复行。"""
        from bandi.memory.episodic import EpisodicStore

        epi = EpisodicStore(str(tmp_path / "epi.db"))
        import bandi.memory.episodic as epi_mod

        monkeypatch.setattr(epi_mod, "get_shared_episodic", lambda: epi, raising=True)
        id8 = client.staging.propose(
            summary="同一条不应落两次",
            project_id="p1",
            team_id="team-a",
            employee_id="e1",
            session_id="s1",
            turn_index=1,
        )
        # 直接复用暂存 payload 走两次落库路径（绕过消费，模拟重试）
        payload = client.staging.get_pending(id8).payload
        from bandi.memory.episodic import Episode

        for _ in range(2):
            epi.record(
                Episode(
                    episode_id=f"team-pending-{id8}",
                    summary=payload["summary"],
                    project_id=payload["project_id"],
                    team_id=payload["team_id"],
                )
            )
        hits = epi.list_recent(project_id="p1", team_id="team-a")
        assert len(hits) == 1, f"稳定 episode_id 必须幂等，实际落库 {len(hits)} 条"

    def test_report_records_advice(self, client: Any) -> None:
        id8 = client.staging.propose(
            summary="经验 X",
            project_id="p",
            team_id="t",
            employee_id="e",
            session_id="s",
            turn_index=1,
        )
        r = client.post(
            f"/api/memory/team-pending/{id8}/report",
            json={"opinion": "同意", "reporter_role": "cto"},
        )
        assert r.status_code == 200
        assert client.staging.get_pending(id8).payload["escalation"]["opinion"] == "同意"

    def test_list_and_reject(self, client: Any) -> None:
        id8 = client.staging.propose(
            summary="经验 Y",
            project_id="p",
            team_id="t",
            employee_id="e",
            session_id="s2",
            turn_index=2,
        )
        rows = client.get("/api/memory/team-pending").json()["pending"]
        assert any(r["id"] == id8 for r in rows)
        r = client.post(f"/api/memory/team-pending/{id8}/reject")
        assert r.status_code == 200 and r.json()["rejected"] is True
