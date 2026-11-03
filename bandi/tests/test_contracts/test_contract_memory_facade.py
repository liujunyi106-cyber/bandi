"""
  search_unified 统一出口：切词多候选聚合 + 维度过滤 + episodic note_recall
     ——memory_source 与 prefetch 的唯一数据源
  MemoryContextSource 委托门面（不再自查库——monkeypatch 断言同源）
  fail-safe：底层存储异常 → 空结果降级不炸主链

破坏者断言：同内容不得在同请求内双段出现；异常不得向上传播。
"""

from typing import Any

import pytest

from bandi.core.message import LLMResponse, TokenUsage


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class _FakeManager:
    """可控 search_all 桩——记录调用并返回预置分类结果。"""

    def __init__(self, results: dict[str, list] | None = None, error: Exception | None = None):
        self.results = results or {}
        self.error = error
        self.calls: list[tuple[str, int]] = []
        self.recalled: list[str] = []

    def search_all(self, query: str, limit: int = 5, **kw: Any) -> dict[str, list]:
        if self.error is not None:
            raise self.error
        self.calls.append((query, limit))
        return self.results

    @property
    def episodic(self) -> Any:
        class _Epi:
            def note_recall(self, episode_id: str, query: str | None = None) -> None:
                pass

        return _Epi()


# ── search_unified 统一出口 ────────────────────────────────────────────────


def test_search_unified_aggregates_and_dedupes(tmp_path: Any) -> None:
    """契约：切词多候选聚合去重 + 维度透传——同一出口供两条通道消费。"""
    from bandi.memory.memory_provider import MemoryManager

    class _LT:
        def retrieve(self, q: str, limit: int = 5, **kw: Any) -> list:
            from bandi.memory.long_term import MemoryItem

            return [MemoryItem(item_id=f"lt-{q}", content=f"蓝鲸知识{q}")]

        def search_fts_ranked(self, q: str, limit: int = 5, **kw: Any) -> list:
            return self.retrieve(q, limit=limit, **kw)

    mgr = MemoryManager(long_term=_LT())
    out = mgr.search_unified("蓝鲸栖息地", session_id="s1")

    assert isinstance(out, list) and out, "统一出口必须返回聚合命中"
    values = " ".join(str(getattr(h, "value", "")) for h in out)
    assert "蓝鲸" in values


def test_search_unified_bumps_episodic_recall(tmp_path: Any) -> None:
    """契约（P1b）：episodic 命中必须 bump 召回计数（原 memory_source 行为收编）。"""
    from bandi.memory.episodic import Episode, EpisodicStore
    from bandi.memory.memory_provider import MemoryManager

    epi = EpisodicStore(str(tmp_path / "ep.db"))
    stored = epi.record(Episode(summary="蓝鲸是最大动物"))
    mgr = MemoryManager(episodic=epi)

    mgr.search_unified("蓝鲸", session_id="s1")

    refreshed = epi.get(stored.episode_id)
    assert refreshed is not None and refreshed.recall_count >= 1, (
        "统一出口必须保持 note_recall bump 语义"
    )


def test_search_unified_fail_safe(tmp_path: Any) -> None:
    """契约（+破坏者）：底层异常必须降级空列表，不得向上传播炸主链。"""
    from bandi.memory.memory_provider import MemoryManager

    class _Boom:
        def retrieve(self, *a: Any, **k: Any) -> list:
            raise RuntimeError("db down")

        def search_all(self, *a: Any, **k: Any) -> dict:
            raise RuntimeError("db down")

    mgr = MemoryManager(long_term=_Boom())
    assert mgr.search_unified("任意", session_id="s") == []


# ── MemoryContextSource 委托门面 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_source_delegates_to_unified() -> None:
    """契约：MemoryContextSource 必须经 search_unified 取数（单一出口）
    不再自行调 search_all 多候选循环。"""
    from bandi.context.sources.memory_source import MemoryContextSource
    from bandi.core.message import Message, MessageRole

    calls: list[str] = []

    class _DelegatingManager(_FakeManager):
        def search_unified(self, query: str, **kw: Any) -> list:
            calls.append(query)
            return []

    src = MemoryContextSource(memory_manager=_DelegatingManager())
    await src.collect(Message(role=MessageRole.USER, content="查蓝鲸"), session_id="s1")

    assert calls, "collect 必须委托 search_unified"


# ── Prefetch 委托 + session_memory 节删除 ──────────────────────────────────


@pytest.mark.asyncio
async def test_prefetch_delegates_and_drops_session_memory() -> None:
    """
    （当前轮消息重复注入）必须删除。"""
    from bandi.perception.prefetch import MemoryPrefetchProvider

    class _UnifiedOnly:
        def search_unified(self, query: str, **kw: Any) -> list:
            from bandi.memory.memory_provider import MemoryHit

            return [MemoryHit(key="k1", value="蓝鲸栖息地在海洋")]

    provider = MemoryPrefetchProvider(_UnifiedOnly(), project_id=None, employee_id=None)
    msgs = [{"role": "user", "content": "这一轮的消息原文不该被再次注入"}]

    payload = await provider("蓝鲸", "sess-1", msgs)

    assert "session_memory" not in payload, f"session_memory 节必须删除（实际 {sorted(payload)}）"
    assert "focused_memory" in payload, "focused 记忆节保留"
    assert "蓝鲸栖息地" in payload["focused_memory"]


# ── 双注消除 ────────────────────────────────────────────────────────────────


def test_inject_prefetch_skips_already_injected_lines() -> None:
    """
    不得重复注入——同请求内同内容双段出现必须消除。"""
    from bandi.agent.states import _filter_prefetch_payload

    already = "蓝鲸是地球上现存最大的动物（全文细节……）"
    payload = {"focused_memory": (f"{already}\n全新的一条未注入过的记忆内容XYZ")}
    system_texts = [f"[long_term] {already[:200]}"]

    filtered = _filter_prefetch_payload(payload, system_texts)

    assert "全新的一条未注入过的记忆内容XYZ" in filtered.get("focused_memory", ""), (
        "未注入过的新记忆必须保留"
    )
    assert already not in filtered.get("focused_memory", ""), "首轮已注入的行不得二次注入"


def test_filter_keeps_all_when_nothing_injected() -> None:
    """契约（P4b）：system 无记忆段时全部保留（个人链首轮场景不受影响）。"""
    from bandi.agent.states import _filter_prefetch_payload

    payload = {"focused_memory": "行A\n行B"}
    assert _filter_prefetch_payload(payload, []) == payload


# ── 头部插入 ────────────────────────────────────────────────────────────────


def test_inject_prefetch_inserts_after_system_block() -> None:
    """
    （OpenAI 协议 system 在头部），而非 append 到尾部。"""
    from types import SimpleNamespace

    from bandi.agent import states as states_mod
    from bandi.perception.prefetch import PrefetchHandle

    class _Handle:
        async def consume(self, timeout: float = 1.0) -> dict[str, str]:
            return {"focused_memory": "预取记忆内容Q"}

    handle = PrefetchHandle.__new__(PrefetchHandle)
    object.__setattr__(handle, "task", None)
    handle.consume = _Handle().consume  # 实例级替换——不得改类属性污染其他用例

    class _Bus:
        async def emit(self, event: Any) -> None:
            pass

    cfg = SimpleNamespace(prefetch=SimpleNamespace(enabled=True, timeout_secs=1.0))
    ctx = SimpleNamespace(
        meta={"prefetch_handle": handle},
        turn_index=2,
        config=cfg,
        bus=_Bus(),
        messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "问"},
        ],
    )

    import asyncio

    provider_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "问"},
    ]
    result = asyncio.run(states_mod._inject_prefetch(ctx, provider_messages, cfg))

    contents = [str(m.get("content")) for m in result]
    assert any("预取记忆内容Q" in c for c in contents), "prefetch block 必须被注入"
    prefetch_idx = next(i for i, c in enumerate(contents) if "预取记忆内容Q" in c)
    assert prefetch_idx == 1, (
        f"prefetch block 必须紧跟 system 区之后（位置 {prefetch_idx}，实际顺序 {contents}）"
    )


def _mk_reflect_ctx(verdict_action: str, hierarchy: Any, tool_results: list) -> Any:
    from types import SimpleNamespace

    from bandi.agent.verifier import Verdict

    class _Verifier:
        def verification_required(self, ctx: Any) -> bool:
            return True

        async def evaluate(self, packet: Any) -> Verdict:
            return Verdict(action=verdict_action, reason="验证失败原因X")

        def apply_stagnation_cap(self, state: dict, verdict: Verdict) -> Verdict:
            return verdict

    return SimpleNamespace(
        terminal_reason=None,
        final_message=None,
        raw_input="修复蓝鲸模块的导出缺陷",
        messages=[],
        current_content="执行输出",
        tool_results=tool_results,
        safety=SimpleNamespace(verifier=_Verifier()),
        verifier_state=SimpleNamespace(rework_count=0, rework_feedback=""),
        meta={
            "session_id": "s-lesson",
            "hierarchy_manager": hierarchy,
            "observing_digest": {"tool_count": len(tool_results)},
        },
        turn_index=3,
        bus=_Bus(),
    )


class _HierarchyStub:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []

    def save_memory(self, scope: str, content: str, **kw: Any) -> None:
        self.saved.append((scope, content))


@pytest.mark.asyncio
async def test_observing_fills_digest_from_tool_results() -> None:
    """契约：OBSERVING 从工具结果提炼观察摘要进 meta（原 pass 空态）。"""
    from types import SimpleNamespace

    from bandi.agent import states as states_mod
    from bandi.core.message import ToolResult

    ctx = SimpleNamespace(
        terminal_reason=None,
        meta={},
        tool_results=[
            ToolResult(
                tool_call_id="a", tool_name="read", output="ok", success=True, duration_ms=1
            ),
            ToolResult(
                tool_call_id="b",
                tool_name="write",
                output="",
                success=False,
                error="denied",
                duration_ms=1,
            ),
        ],
    )
    await states_mod.on_observing(ctx)

    digest = ctx.meta.get("observing_digest")
    assert digest is not None and digest["tool_count"] == 2, f"观察摘要必须填充（实际 {digest}）"
    assert "write" in digest["failed_tools"]


@pytest.mark.asyncio
async def test_rework_writes_lesson_to_global() -> None:
    from bandi.agent import states as states_mod

    hierarchy = _HierarchyStub()
    ctx = _mk_reflect_ctx("rework", hierarchy, [])

    await states_mod.on_reflecting(ctx)

    globals_written = [c for scope, c in hierarchy.saved if scope == "global"]
    assert globals_written, f"rework 必须写 global lesson（实际 {hierarchy.saved}）"
    blob = "\n".join(globals_written)
    assert "验证失败原因X" in blob, "lesson 必须含失败原因"
    assert "蓝鲸" not in blob, "摘要化：lesson 不得含任务描述（跨项目信息面）"


@pytest.mark.asyncio
async def test_pass_verdict_writes_no_lesson() -> None:
    """契约（+D-t3=A）：正常完成零写入——lesson 只从异常学。"""
    from bandi.agent import states as states_mod

    hierarchy = _HierarchyStub()
    ctx = _mk_reflect_ctx("pass", hierarchy, [])

    await states_mod.on_reflecting(ctx)

    assert hierarchy.saved == [], f"pass 不得写 lesson（实际 {hierarchy.saved}）"


@pytest.mark.asyncio
async def test_lesson_idempotent_per_turn() -> None:
    """契约（+破坏者）：同一 turn 双次 REFLECTING 只写一条 lesson。"""
    from bandi.agent import states as states_mod

    hierarchy = _HierarchyStub()
    ctx = _mk_reflect_ctx("fail", hierarchy, [])

    await states_mod.on_reflecting(ctx)
    await states_mod.on_reflecting(ctx)

    assert len([c for s, c in hierarchy.saved if s == "global"]) == 1, (
        f"同 turn 不得双写 lesson（实际 {len(hierarchy.saved)} 条）"
    )


@pytest.mark.asyncio
async def test_lesson_skipped_without_hierarchy() -> None:
    """契约：无 hierarchy（个人链）时 best-effort 静默跳过不炸
    且 rework 状态机正常推进（rework_count/feedback 不受影响）。"""
    from bandi.agent import states as states_mod

    ctx = _mk_reflect_ctx("rework", None, [])
    await states_mod.on_reflecting(ctx)
    assert ctx.verifier_state.rework_count == 1, "rework 推进不得因 lesson 缺席受阻"
    assert any(
        getattr(m, "role", None) is not None and "反思反馈" in str(getattr(m, "content", ""))
        for m in ctx.messages
    ), "反思反馈消息必须照常入列"


@pytest.mark.parametrize(
    ("role_name", "action", "expected"),
    [
        ("admin", "write", "ALLOW"),
        ("power_user", "read", "ALLOW"),
        ("power_user", "write", "ASK"),
        ("member", "execute", "DENY"),
        ("readonly", "write", "DENY"),
        ("readonly", "read", "ALLOW"),
    ],
)
def test_register_builtin_role_decision_semantics(
    role_name: str, action: str, expected: str
) -> None:
    """契约：四角色策略注册后决策语义与定义一致（parametrize=6 覆盖三态）。"""
    from bandi.core.permission.models import PermissionContext, Resource
    from bandi.core.permission.policy_engine import PolicyEngine
    from bandi.core.permission.roles import register_builtin_role

    engine = PolicyEngine()
    assert register_builtin_role(engine, role_name) is True

    decision = engine.check(
        Resource(type="tool", name=f"tool_{action}", action=action),
        PermissionContext(user_id="u1", role=role_name),
    )
    assert decision.decision.name == expected, (
        f"{role_name}.{action} 应为 {expected}（实际 {decision.decision.name}: {decision.reason}）"
    )


def test_register_unknown_role_returns_false() -> None:
    """契约（+破坏者）：未知角色不注册任何策略（user 默认态走权限管道单链）。"""
    from bandi.core.permission.policy_engine import PolicyEngine
    from bandi.core.permission.roles import register_builtin_role

    assert register_builtin_role(PolicyEngine(), "user") is False
    assert register_builtin_role(PolicyEngine(), "nonexistent") is False


def test_factory_wires_builtin_role_policies(tmp_path: Any) -> None:
    """契约：build_agent_context 按 agent_role 注册对应角色策略——
    readonly 会话下写工具资源被角色策略拒绝。"""
    from unittest.mock import patch

    from bandi.agent.factory import build_agent_context
    from bandi.core.config import BandiConfig
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.llm.mock import MockProvider
    from bandi.tools.registry import ToolRegistry

    provider = MockProvider()
    provider.add_response(
        LLMResponse(content="", tool_calls=[], usage=TokenUsage(1, 1), finish_reason="stop")
    )
    cfg = BandiConfig()
    with patch("pathlib.Path.home", return_value=tmp_path):
        ctx = build_agent_context(
            user_input="q",
            mode="execute",
            provider=provider,
            tool_registry=ToolRegistry(),
            bandi_config=cfg,
            agent_role="readonly",
        )

    engine = ctx.meta.get("policy_engine")
    assert engine is not None, "PolicyEngine 必须在 meta 可达"
    from bandi.core.permission.models import PermissionContext, Resource

    decision = engine.check(
        Resource(type="tool", name="file_write_probe", action="write"),
        PermissionContext(user_id="u", role="readonly"),
    )
    assert decision.decision.name == "DENY", (
        f"readonly 写必须被角色策略拒绝（实际 {decision.decision.name}）"
    )


# ── per-role llm_config（数据模型 + 路由通道）─────────────────────


def test_org_employee_llm_config_roundtrip() -> None:
    """契约：OrgEmployee.llm_config 字段存在且经序列化往返保真。"""
    from bandi.company.org_engine import OrgEmployee

    emp = OrgEmployee(
        employee_id="emp-1",
        name="工程师A",
        role_id="cto",
        llm_config={"default_model": "glm-5.2", "api_key": "k-x", "base_url": "https://b"},
    )
    assert emp.llm_config is not None and emp.llm_config["default_model"] == "glm-5.2"

    data = emp.to_dict()
    assert data.get("llm_config") == emp.llm_config, (
        f"to_dict 必须携带 llm_config（实际 keys={sorted(data)}）"
    )

    restored = OrgEmployee.from_dict(data)
    assert restored.llm_config == emp.llm_config, "from_dict 往返必须保真"


@pytest.mark.asyncio
async def test_run_agent_turn_provider_override(tmp_path: Any, monkeypatch: Any) -> None:
    """契约：provider_config 覆盖全局 LLM 配置——build_provider 收到的
    config 已带覆盖值（路由通道最终产物断言）。"""
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.config import BandiConfig
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    captured: list[Any] = []

    def fake_build_provider(cfg: BandiConfig) -> MockProvider:
        captured.append(cfg)
        p = MockProvider()
        p.add_response(LLMResponse(content="ok", usage=TokenUsage(2, 1), finish_reason="stop"))
        return p

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "s.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        with (
            patch("bandi.core.agent_runner.build_provider", side_effect=fake_build_provider),
            # 两入口同 patch——契约意图不变：provider 构建时收到的 config 必带覆盖值
            patch("bandi.agent.factory._build_provider_direct", side_effect=fake_build_provider),
        ):
            await run_agent_turn(
                user_input="路由验证",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
                provider_config={"default_model": "glm-5.2"},
            )

    assert captured, "provider 必须被构建（显式模型路径经 _build_provider_direct）"
    assert captured[0].llm.default_model == "glm-5.2", (
        f"provider_config 必须覆盖默认模型（实际 {captured[0].llm.default_model}）"
    )


@pytest.mark.asyncio
async def test_provider_override_does_not_pollute_caller_config(tmp_path: Any) -> None:
    """契约（B-t1 破坏者）：provider_config 覆盖必须作用于本 turn 副本——
    调用方持有的原 config 对象不得被原地改写（并发多员工各传不同 llm_config
    时互相污染 = 数据竞争）。"""
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.config import BandiConfig
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "s.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        original_model = cfg.llm.default_model

        with patch(
            "bandi.core.agent_runner.build_provider",
            side_effect=lambda _cfg: MockProvider(),
        ):
            await run_agent_turn(
                user_input="覆盖验证",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
                provider_config={"default_model": "kimi-k2.6"},
            )

    assert cfg.llm.default_model == original_model, (
        f"调用方原 config 不得被污染（期望 {original_model}，实际 {cfg.llm.default_model}）"
    )


def test_corrupted_org_json_llm_config_degrades_gracefully(tmp_path: Any) -> None:
    """契约（B-t2 破坏者）：损坏 org.json 中 llm_config 为非 dict 值（如字符串）时
    load_org 必须静默降级 llm_config=None——而非让 AttributeError 炸掉执行链。
    """
    import json
    from pathlib import Path

    from bandi.company.org_engine import OrgEngine
    from bandi.company.org_persistence import load_org
    from bandi.company.recruiter import Recruiter

    corrupt_org = {
        "roles": {},
        "employees": [
            {
                "employee_id": "test-corp-01",
                "name": "测试员工",
                "role_id": "test-role",
                "agent_role": "member",
                "llm_config": "garbage-string-not-dict",
            }
        ],
        "hierarchy": None,
    }
    path = str(tmp_path / "corrupt_org.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(corrupt_org, f, ensure_ascii=False)

    engine = OrgEngine()
    load_org(engine, Recruiter(), path=path)

    employees = engine.list_employees()
    assert len(employees) == 1
    emp = employees[0]
    assert emp.llm_config is None, (
        f"损坏 org.json llm_config 非 dict 必须降级 None（实际 {emp.llm_config}）"
    )
    assert emp.employee_id == "test-corp-01"
