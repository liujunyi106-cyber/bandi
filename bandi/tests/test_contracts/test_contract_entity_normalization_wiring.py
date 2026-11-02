"""契约：memory_assert 的实体归一接入（E 的生产接线）。

缺口：归一模块本身有粗筛+LLM 精判，但没有调用点——「明远科技」与「明远科技
公司」会算出两个三元组 ID，同一件事被存成两条且永不合并。

本契约钉住接线后的行为：归一命中时 subject 落到规范名、ID 与规范名那条一致、
原始写法留痕；归一不可用时**照常写入**（归一是增强项，不是硬前置）。
"""

from __future__ import annotations

import pytest

from bandi.memory.long_term import LongTermMemory
from bandi.tools.memory_tools import MemoryAssertTool


class _FakeLLM:
    """假判定器——按预设 JSON 回答，不调真实网络。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.reply


def _tool(tmp_path, llm) -> MemoryAssertTool:
    lt = LongTermMemory(db_path=tmp_path / "lt.db")
    return MemoryAssertTool(long_term=lt, llm=llm)


class TestNormalizationWiring:
    @pytest.mark.asyncio
    async def test_same_entity_merges_to_canonical_id(self, tmp_path) -> None:
        """同义写法必须落到同一条三元组——ID 用规范名算。"""
        merge = '[{"index": 1, "same": true, "reason": "同一家公司"}]'
        tool = _tool(tmp_path, _FakeLLM(merge))
        first = await tool.execute(
            {"subject": "明远科技", "predicate": "depends_on", "object": "支付"}
        )
        tool._llm = _FakeLLM(merge)
        second = await tool.execute(
            {"subject": "明远科技公司", "predicate": "depends_on", "object": "支付"}
        )
        assert first["asserted"] and second["asserted"]
        assert first["id"] == second["id"], "同义写法算出两个 ID = 同一件事存成两条"
        assert "明远科技公司" not in second["content"], "归一后 content 必须用规范名"

    @pytest.mark.asyncio
    async def test_original_name_is_traced(self, tmp_path) -> None:
        """归一不能抹掉原写法——留痕才可回溯。"""
        seed = _tool(tmp_path, None)
        await seed.execute({"subject": "明远科技", "predicate": "depends_on", "object": "蓝海"})
        tool = _tool(tmp_path, _FakeLLM('[{"index": 1, "same": true}]'))
        tool._long_term = seed._long_term
        out = await tool.execute(
            {"subject": "明远科技公司", "predicate": "depends_on", "object": "支付"}
        )
        assert out.get("normalized_from") == "明远科技公司"

    @pytest.mark.asyncio
    async def test_rejected_judgement_keeps_both(self, tmp_path) -> None:
        """判定为不同实体时不许合并——误合并不可逆。"""
        tool = _tool(tmp_path, _FakeLLM('[{"index": 1, "same": false}]'))
        out = await tool.execute(
            {"subject": "订单服务", "predicate": "depends_on", "object": "支付"}
        )
        assert out["asserted"]
        assert "normalized_from" not in out
        assert "订单服务" in out["content"]


class TestNormalizationNeverBlocksWrite:
    @pytest.mark.asyncio
    async def test_no_llm_still_writes(self, tmp_path) -> None:
        """破坏者视角：没有判定器时不能拒绝写入——归一不得成为硬前置。"""
        tool = _tool(tmp_path, None)
        out = await tool.execute(
            {"subject": "订单服务", "predicate": "depends_on", "object": "支付"}
        )
        assert out["asserted"] is True

    @pytest.mark.asyncio
    async def test_llm_raises_still_writes(self, tmp_path) -> None:
        async def boom(prompt: str) -> str:
            raise RuntimeError("judge down")

        tool = _tool(tmp_path, boom)
        out = await tool.execute(
            {"subject": "订单服务", "predicate": "depends_on", "object": "支付"}
        )
        assert out["asserted"] is True

    @pytest.mark.asyncio
    async def test_malformed_judgement_still_writes(self, tmp_path) -> None:
        """判定返回垃圾内容时按「不合并」处理，且写入照常。"""
        tool = _tool(tmp_path, _FakeLLM("这不是 JSON"))
        out = await tool.execute(
            {"subject": "订单服务", "predicate": "depends_on", "object": "支付"}
        )
        assert out["asserted"] is True
        assert "normalized_from" not in out


class TestNoCandidatesSkipsJudge:
    @pytest.mark.asyncio
    async def test_no_existing_names_makes_no_llm_call(self, tmp_path) -> None:
        """库里还没有任何实体名时不该白花一次 LLM 调用。"""
        llm = _FakeLLM("[]")
        tool = _tool(tmp_path, llm)
        await tool.execute({"subject": "全新实体", "predicate": "depends_on", "object": "支付"})
        assert llm.calls == []
