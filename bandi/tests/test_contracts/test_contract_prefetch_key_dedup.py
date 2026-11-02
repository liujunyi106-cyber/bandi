"""
元数据携带：MemoryPrefetchProvider.__call__ 结果带 `_focused_memory_items`
   （隐藏元数据键，逐条 (key, value)，与 focused_memory 文本对齐）
渲染隔离：render_prefetch_block 不渲染 `_` 开头的元数据键
key 级去重：_filter_prefetch_payload(payload, texts, injected_keys=[...]) 已注入
   key 的整条记忆剔除，未注入保留并重建 focused_memory
P3b 全未注入：injected_keys 不含任何 key → 整节保留
指纹兜底：无 key 信息 / key 未注入但文本指纹命中 system → 行级剔除（回归）
兼容：injected_keys=None 时行为与旧签名一致（只做指纹去重）
记忆会在每轮被重复注入；key 级去重必须按 memory key 剔除防重复占用预算。
"""

from typing import Any, cast

import pytest


class _PrefetchCfg:
    enabled = True
    timeout_secs = 0.05


class _BandiConfig:
    prefetch = _PrefetchCfg()


class _Bus:
    async def emit(self, event: Any) -> None:
        return None


class _StubHandle:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def consume(self, timeout: float = 1.0) -> dict[str, Any]:
        return self._payload


@pytest.mark.asyncio
async def test_inject_prefetch_e2e_key_dedup_on_production_path() -> None:
    """生产端到端（破坏者场景）：_inject_prefetch 装配层——同 key 首轮已注入
    值前缀漂移（前 80 字符不同）时，第二轮必须按 memory key 剔除（指纹兜底失效
    后由 SEEN_BY_SESSION key 判定），最终 provider_messages 不得重复注入。"""
    from bandi.agent.states import _inject_prefetch
    from bandi.context.sources.memory_source import SEEN_BY_SESSION

    sid = "sess-key-dedup"
    drifted_value = "更新后的新前缀内容" + "_" * 260
    payload: dict[str, Any] = {
        "focused_memory": f"{drifted_value}\n未注入 key 的新记忆内容",
        "_focused_memory_items": [
            {"key": "user_pref/theme", "value": drifted_value},
            {"key": "proj/beta", "value": "未注入 key 的新记忆内容"},
        ],
    }
    SEEN_BY_SESSION[sid] = {"user_pref/theme": 2}

    class _Ctx:
        turn_index = 2
        meta = {"prefetch_handle": _StubHandle(payload), "session_id": sid}

    ctx = _Ctx()
    ctx.bus = _Bus()

    base = [
        {"role": "system", "content": "身份 prompt"},
        {"role": "user", "content": "alpha 项目背景再讲讲"},
    ]
    out = await _inject_prefetch(ctx, [dict(m) for m in base], _BandiConfig())

    try:
        block = next(
            (m.get("content", "") for m in out if "## Runtime Prefetch" in m.get("content", "")),
            "",
        )
        assert block, "第二轮必须注入 prefetch 块"
        assert "更新后的新前缀内容" not in block, "已注入 key 的记忆必须被 key 级剔除"
        assert "未注入 key 的新记忆内容" in block, "未注入 key 的记忆必须保留"
    finally:
        SEEN_BY_SESSION.pop(sid, None)  # 清理模块级注入计数（防跨用例污染）


class _StubMemory:
    """最小记忆门面——search_unified 返回可控结果（带 key/value）。"""

    def __init__(self) -> None:
        self._hits: list[Any] = []

    def search_unified(self, query: str, limit: int = 5, **_: Any) -> list[Any]:
        assert query
        assert limit > 0
        from bandi.memory.memory_provider import MemoryHit

        return [
            MemoryHit(key="user_pref/theme", value="偏好深色主题"),
            MemoryHit(key="proj/alpha", value="项目 alpha 关键决策"),
        ]


class TestPayloadMetadata:
    @pytest.mark.asyncio
    async def test_prefetch_provider_emits_focused_memory_items(self) -> None:
        """payload 必须携带 `_focused_memory_items` 隐藏元数据（逐条 key/value）。"""
        from bandi.perception.prefetch import MemoryPrefetchProvider

        provider = MemoryPrefetchProvider(cast(Any, _StubMemory()))
        payload = await provider("最近偏好", "s1", [{"role": "user", "content": "最近偏好"}])
        items = payload.get("_focused_memory_items", [])
        assert isinstance(items, list) and items, "payload 必须携带 _focused_memory_items"
        assert {it["key"] for it in items} == {"user_pref/theme", "proj/alpha"}


class TestRenderIsolation:
    def test_render_hides_metadata_keys(self) -> None:
        """渲染层必须跳过 `_` 开头元数据键，避免污染 prefetch 正文。"""
        from bandi.perception.prefetch import render_prefetch_block

        payload: dict[str, Any] = {
            "focused_memory": "偏好深色主题\n项目 alpha 关键决策",
            "_focused_memory_items": [
                {"key": "user_pref/theme", "value": "偏好深色主题"},
                {"key": "proj/alpha", "value": "项目 alpha 关键决策"},
            ],
        }
        block = render_prefetch_block(payload)
        assert "偏好深色主题" in block
        assert "_focused_memory_items" not in block


class TestKeyLevelDedup:
    def test_injected_key_item_removed_others_kept(self) -> None:
        """破坏者：已注入 key 的整条记忆剔除，未注入 key 保留并重建 focused_memory。"""
        from bandi.agent.states import _filter_prefetch_payload

        payload: dict[str, Any] = {
            "focused_memory": "偏好深色主题\n项目 alpha 关键决策",
            "_focused_memory_items": [
                {"key": "user_pref/theme", "value": "偏好深色主题"},
                {"key": "proj/alpha", "value": "项目 alpha 关键决策"},
            ],
        }
        kept = _filter_prefetch_payload(
            payload, existing_system_texts=["现有系统文本"], injected_keys=["user_pref/theme"]
        )
        assert "偏好深色主题" not in kept["focused_memory"], "已注入 key 记忆必须剔除"
        assert "项目 alpha 关键决策" in kept["focused_memory"], "未注入 key 记忆必须保留"
        assert [it["key"] for it in kept["_focused_memory_items"]] == ["proj/alpha"], (
            "元数据必须与重建文本对齐"
        )

    def test_none_injected_keeps_all_items(self) -> None:
        """P3b：全未注入 → 整节保留。"""
        from bandi.agent.states import _filter_prefetch_payload

        payload: dict[str, Any] = {
            "focused_memory": "偏好深色主题\n项目 alpha 关键决策",
            "_focused_memory_items": [
                {"key": "user_pref/theme", "value": "偏好深色主题"},
                {"key": "proj/alpha", "value": "项目 alpha 关键决策"},
            ],
        }
        kept = _filter_prefetch_payload(payload, existing_system_texts=[], injected_keys=[])
        assert kept["focused_memory"] == payload["focused_memory"], "无注入时不得删减内容"


class TestFingerprintFallback:
    def test_fingerprint_dedup_still_works_without_key_match(self) -> None:
        """key 未命中注入，但文本指纹已存在于 system 区 → 行级剔除。"""
        from bandi.agent.states import _filter_prefetch_payload

        shared = "跨截断同源记忆片段 " + "s" * 300
        payload: dict[str, Any] = {
            "focused_memory": f"{shared}\nalpha 尾部内容",
            "_focused_memory_items": [
                {"key": "kv/a1", "value": shared},
                {"key": "kv/a2", "value": "alpha 尾部内容"},
            ],
        }
        kept = _filter_prefetch_payload(
            payload, existing_system_texts=["前缀 " + shared[:80]], injected_keys=["kv/none"]
        )
        assert shared not in kept["focused_memory"], "指纹命中行必须剔除"
        assert "alpha 尾部内容" in kept["focused_memory"], "未命中行必须保留"

    def test_none_injected_keys_preserves_old_signature(self) -> None:
        """injected_keys=None 兼容旧签名——只做指纹去重。"""
        from bandi.agent.states import _filter_prefetch_payload

        payload: dict[str, str] = {"focused_memory": "唯一条目 " + "f" * 300}
        kept = _filter_prefetch_payload(payload, existing_system_texts=["与内容无关的前缀"])
        assert kept["focused_memory"] == payload["focused_memory"], "无指纹命中时全保留"
