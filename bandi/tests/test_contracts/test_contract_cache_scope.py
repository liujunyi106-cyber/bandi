"""
契约族：缓存 key 作用域隔离。

背景：ToolResultCache.build_key 原只 hash tool_name+arguments，两个不同
agent/role/scope/mode/project/策略版本共享同一缓存时，第二个上下文会命中
第一个上下文的缓存结果 = 越权复用（data leakage across identity boundaries）。

本契约钉的行为族（全部生产调用路径）：
- ToolExecutor._cached_result（读路径）
- ToolExecutor._run_and_record（写路径）
- build_key 静态键构建（作用域前缀）
- factory 装配点传入 policy_version（策略变更自动失效）

断言「最终产物」：不同上下文执行同参数 → 各自真实执行（calls 计数递增）
不返回对方缓存；相同上下文 → 命中复用且不再执行。
"""

from __future__ import annotations

import asyncio

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.cache import ToolResultCache
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class _ScopeTool(BaseTool):
    name = "scope_op"
    description = "scope-aware read tool"
    category = ToolCategory.UTIL
    cache_ttl = 60.0
    is_read_only = True

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(self, params: dict, context=None):  # type: ignore[override]
        self.calls += 1
        return f"live:{self.calls}"


def _build(identity: dict, cache: ToolResultCache, registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry=registry, result_cache=cache, **identity)


def _run(executor: ToolExecutor, args: dict | None = None) -> ToolResult:
    return asyncio.run(
        executor.execute([ToolCall(id="c1", name="scope_op", arguments=args or {"x": 1})])
    )[0]


def test_contract_build_key_scope_prefix_separates_identities() -> None:
    """build_key 作用域前缀：同 tool+args 不同 scope 必须产出不同 key。"""
    k_a = ToolResultCache.build_key("scope_op", {"x": 1}, scope="a")
    k_b = ToolResultCache.build_key("scope_op", {"x": 1}, scope="b")
    assert k_a != k_b, "不同 scope 不得产出相同缓存 key（越权复用根因）"
    k_a2 = ToolResultCache.build_key("scope_op", {"x": 1}, scope="a")
    assert k_a == k_a2, "同 scope 同参数必须幂等产出同一 key"


def test_contract_agent_id_isolation_no_cross_reuse() -> None:
    """不同 agent_id 共享缓存：第二个 agent 必须真实执行，不得复用第一个结果。"""
    cache = ToolResultCache()
    registry = ToolRegistry()
    registry.register_instance(_ScopeTool())
    alice = _build({"agent_id": "alice"}, cache, registry)
    bob = _build({"agent_id": "bob"}, cache, registry)

    r1 = _run(alice)
    r2 = _run(bob)
    assert r1.output == "live:1", f"alice 首执应 live:1，实际 {r1.output!r}"
    assert r2.output == "live:2", f"bob 同参数应重新执行（越权复用实锤），实际 {r2.output!r}"


def test_contract_role_scope_mode_isolation() -> None:
    """role/scope 越界端到端不串缓存；mode 维观测缓存键隔离（plan 模式执行面拦截自定义工具）。"""
    for variant in (
        {"agent_role": "hr"},
        {"agent_scope": "project-a"},
    ):
        cache = ToolResultCache()
        registry = ToolRegistry()
        registry.register_instance(_ScopeTool())
        base = _build({}, cache, registry)
        alt = _build(variant, cache, registry)

        r1 = _run(base)
        r2 = _run(alt)
        assert r1.output == "live:1", f"base 首执应 live:1，实际 {r1.output!r}"
        assert r2.output == "live:2", (
            f"{variant} 同参数应重新执行（越权复用实锤），实际 {r2.output!r}"
        )

    cache = ToolResultCache()
    registry = ToolRegistry()
    registry.register_instance(_ScopeTool())
    base = _build({}, cache, registry)
    alt = _build({"mode": "plan"}, cache, registry)
    assert base.cache_scope != alt.cache_scope, "mode 变更必须产生不同缓存作用域键"


def test_contract_project_root_isolation() -> None:
    """不同 project_root 不得串缓存（工作区数据隔离）。"""
    cache = ToolResultCache()
    registry = ToolRegistry()
    registry.register_instance(_ScopeTool())
    a = _build({"project_root": "/proj/a"}, cache, registry)
    b = _build({"project_root": "/proj/b"}, cache, registry)

    r1 = _run(a)
    r2 = _run(b)
    assert r1.output == "live:1", f"project A 首执应 live:1，实际 {r1.output!r}"
    assert r2.output == "live:2", f"project B 同参数应重新执行，实际 {r2.output!r}"


def test_contract_policy_version_auto_invalidate() -> None:
    """策略版本变更自动失效：新策略上下文不得复用旧策略缓存结果。"""
    cache = ToolResultCache()
    registry = ToolRegistry()
    registry.register_instance(_ScopeTool())
    v1 = _build({"policy_version": "v1"}, cache, registry)
    v2 = _build({"policy_version": "v2"}, cache, registry)

    r1 = _run(v1)
    r2 = _run(v2)
    assert r1.output == "live:1", f"v1 首执应 live:1，实际 {r1.output!r}"
    assert r2.output == "live:2", (
        f"v2 新策略应重新执行（策略变更后缓存自动失效），实际 {r2.output!r}"
    )


def test_contract_same_scope_reuses_cache() -> None:
    """完全相同的身份上下文共享缓存：第二次命中复用，不二次执行。"""
    cache = ToolResultCache()
    registry = ToolRegistry()
    tool = _ScopeTool()
    registry.register_instance(tool)
    identity = {"agent_id": "alice", "agent_role": "dev", "policy_version": "v1"}
    a1 = _build(identity, cache, registry)
    a2 = _build(identity, cache, registry)
    r1 = _run(a1)
    r2 = _run(a2)
    assert r1.output == "live:1"
    assert r2.output == "live:1", f"同上下文应命中缓存（不再执行），实际 {r2.output!r}"
    assert tool.calls == 1, f"同上下文第二次不得真实执行，calls={tool.calls}"


def test_contract_session_id_isolation() -> None:
    """不同 session_id 不得串缓存（TMC-session 维进缓存键）。"""
    cache = ToolResultCache()
    registry = ToolRegistry()
    registry.register_instance(_ScopeTool())
    base = _build({"agent_id": "alice"}, cache, registry)
    base.bind_session_meta({"session_id": "sess-a"})
    alt = _build({"agent_id": "alice"}, cache, registry)
    alt.bind_session_meta({"session_id": "sess-b"})

    r1 = _run(base)
    r2 = _run(alt)
    assert r1.output == "live:1", f"sess-a 首执应 live:1，实际 {r1.output!r}"
    assert r2.output == "live:2", f"sess-b 同参数应重新执行（session 隔离实锤），实际 {r2.output!r}"


def test_contract_tool_version_isolation() -> None:
    """不同 tool_version 不得串缓存（工具实现变更后旧缓存不得复用）。"""
    cache = ToolResultCache()
    v1 = _ScopeTool()
    v2 = _ScopeTool()
    v2.tool_version = "v2"
    reg1 = ToolRegistry()
    reg1.register_instance(v1)
    reg2 = ToolRegistry()
    reg2.register_instance(v2)
    base = _build({}, cache, reg1)
    alt = _build({}, cache, reg2)

    r1 = _run(base)
    r2 = _run(alt)
    assert r1.output == "live:1"
    assert r2.output == "live:1", f"v2 独立首执应 live:1，实际 {r2.output!r}"
    assert v2.calls == 1, f"不同 tool_version 必须真实执行（未命中 v1 缓存），calls={v2.calls}"


def test_contract_provider_version_isolation() -> None:
    """不同 provider_version 不得串缓存（LLM provider 配置变更后旧缓存失效）。"""
    cache = ToolResultCache()
    registry = ToolRegistry()
    registry.register_instance(_ScopeTool())
    base = _build({"provider_version": "p1"}, cache, registry)
    alt = _build({"provider_version": "p2"}, cache, registry)

    r1 = _run(base)
    r2 = _run(alt)
    assert r1.output == "live:1"
    assert r2.output == "live:2", f"不同 provider_version 应重新执行，实际 {r2.output!r}"
