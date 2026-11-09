# 功能：契约——槽位门答复解析：clarify 通道提交的是 JSON 字符串（前端 JSON.stringify(answers)），
# 执行侧必须解析还原为 answers 映射并写回任务描述（否则答复被静默丢弃、口径丢失、
# 门形同虚设——2026-10-19 验证实锤：多问卡答完执行侧拿到的仍是空答复）。
from __future__ import annotations

import importlib
import json

import pytest


def _app_module():
    return importlib.import_module("bandi.api.app")


class _FakeStore:
    def __init__(self, decision: object) -> None:
        self._decision = decision

    async def get_decision(self, _interaction_id: str, timeout: float = 0.0) -> object:
        return self._decision


@pytest.mark.asyncio
async def test_slot_answer_provider_parses_json_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """破坏者视角：前端 wire 形态是字符串——不解析即静默丢答复（本契约防回归）。"""
    app_module = _app_module()
    answers = {"market": "美国（Amazon 主渠道）", "category": "只做猫用智能硬件"}
    monkeypatch.setattr(
        app_module,
        "_get_interaction_store",
        lambda: _FakeStore(json.dumps(answers, ensure_ascii=False)),
    )
    assert await app_module._slot_answer_provider("iid", 1.0) == answers


@pytest.mark.asyncio
async def test_slot_answer_provider_accepts_dict_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    app_module = _app_module()
    answers = {"market": "美国"}
    monkeypatch.setattr(app_module, "_get_interaction_store", lambda: _FakeStore(answers))
    assert await app_module._slot_answer_provider("iid", 1.0) == answers


@pytest.mark.asyncio
async def test_slot_answer_provider_unwraps_nested_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    app_module = _app_module()
    answers = {"market": "美国"}
    monkeypatch.setattr(
        app_module, "_get_interaction_store", lambda: _FakeStore({"answers": answers})
    )
    assert await app_module._slot_answer_provider("iid", 1.0) == answers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [None, "", "not-json{", ["a", "b"], 42, json.dumps(["a"]), json.dumps("txt")],
)
async def test_slot_answer_provider_rejects_garbage(
    monkeypatch: pytest.MonkeyPatch, decision: object
) -> None:
    """空/坏/非映射输入一律 None（禁止把垃圾写进任务描述）。"""
    app_module = _app_module()
    monkeypatch.setattr(app_module, "_get_interaction_store", lambda: _FakeStore(decision))
    assert await app_module._slot_answer_provider("iid", 1.0) is None


@pytest.mark.asyncio
async def test_clarify_json_string_wire_reaches_provider_as_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端（生产 wire）：session_clarify 收到 JSON 字符串 → resolve →
    _slot_answer_provider 返回 dict —— 与前端 InteractionCard 提交形态逐字一致。"""
    from bandi.core.user_interaction import InteractionStore

    app_module = _app_module()
    store = InteractionStore()
    monkeypatch.setattr(app_module, "_get_interaction_store", lambda: store)
    item = store.register(
        session_id="s-slot", kind="input", payload={"questions": []}, ttl_seconds=60
    )
    answers = {"market": "美国", "category": "猫用"}
    await app_module.session_clarify(
        "s-slot",
        {"interaction_id": item.interaction_id, "answer": json.dumps(answers, ensure_ascii=False)},
    )
    assert await app_module._slot_answer_provider(item.interaction_id, 1.0) == answers
