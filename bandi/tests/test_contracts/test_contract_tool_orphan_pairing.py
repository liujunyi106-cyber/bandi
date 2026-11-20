"""功能：发请求前必须不存在「无前置 tool_calls」的 tool 行。

真机缺陷（网关 400，导致整轮反复失败、界面长期「正在执行…」）：

    Prefill server error (400 Bad Request):
    Messages with role 'tool' must be a response to a preceding message with 'tool_calls'

两处分层防御，语义不同，都要钉住：

1. **Message 层** `drop_orphan_tool_messages(..., extra_pending_ids=...)`：
   保留「本轮在途」豁免——结果先写、assistant(tool_calls) 行后补是正确写序，
   收紧它会打断真实时序（`test_states.py::test_appends_tool_results` 依赖此行为）。
2. **dict 层** `guard_provider_dicts()`：发请求时不再有「在途」概念，
   assistant 行必须已存在；任何孤儿都在这里剔除。

缺第 2 层就漏：轮次结束后那批 tool 行仍无配对 → 网关 400 → 上层重试整轮
→ 又撞同一伪孤儿 → 死循环。
"""

from __future__ import annotations

from bandi.agent.tool_message_guard import (
    drop_orphan_tool_messages,
    guard_provider_dicts,
)
from bandi.core.message import Message, MessageRole, ToolCall


def _tool(tcid: str) -> Message:
    return Message(role=MessageRole.TOOL, content="result", tool_call_id=tcid)


def _assistant_msg(*ids: str) -> Message:
    return Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=cid, name="t", arguments={}) for cid in ids],
    )


def _d(role: str, **kw: object) -> dict:
    return {"role": role, **kw}


# ── dict 层：发请求前的最终保证 ────────────────────────────────


def test_dict_layer_drops_orphan_tool() -> None:
    """功能：dict 层必须剔除无配对的 tool 行（网关 400 的直接来源）。"""
    out = guard_provider_dicts([_d("tool", tool_call_id="ghost", content="r")])
    assert out == [], "孤儿 tool 行会让网关拒绝整个请求"


def test_dict_layer_keeps_paired_tool() -> None:
    """功能：正常配对不得被误删（防过度收紧）。"""
    out = guard_provider_dicts(
        [
            _d("assistant", tool_calls=[{"id": "c1", "type": "function"}]),
            _d("tool", tool_call_id="c1", content="r"),
        ]
    )
    assert len(out) == 2


def test_dict_layer_invariant_holds_on_mixed_input() -> None:
    """功能：混合输入后——输出里不存在无前置 tool_calls 的 tool 行。"""
    out = guard_provider_dicts(
        [
            _d("user", content="hi"),
            _d("tool", tool_call_id="stale", content="old"),
            _d("assistant", tool_calls=[{"id": "ok", "type": "function"}]),
            _d("tool", tool_call_id="ok", content="r"),
            _d("system", content="note"),
        ]
    )
    pending: set[str] | None = None
    for msg in out:
        role = msg.get("role")
        if role == "assistant":
            pending = {str(c.get("id")) for c in (msg.get("tool_calls") or [])} or None
        elif role == "tool":
            assert pending and str(msg.get("tool_call_id")) in pending, (
                f"输出仍含孤儿 tool 行：{msg.get('tool_call_id')}"
            )
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "system"]


# ── Message 层：保留「在途」豁免（正确写序）─────────────────────


def test_message_layer_keeps_in_flight_tool() -> None:
    """功能：在途豁免必须保留——结果先写、assistant 行后补是正确时序。"""
    out = drop_orphan_tool_messages([_tool("inflight")], extra_pending_ids={"inflight"})
    assert len(out) == 1


def test_message_layer_drops_plain_orphan() -> None:
    """功能：不在途的孤儿在 Message 层仍应剔除。"""
    assert drop_orphan_tool_messages([_tool("x")]) == []


def test_message_layer_normal_pairing_unaffected() -> None:
    """功能：正常配对不受影响。"""
    out = drop_orphan_tool_messages([_assistant_msg("a1"), _tool("a1")])
    assert len(out) == 2

def test_dict_guard_is_wired_into_request_assembly() -> None:
    """功能：dict 层校验必须在请求组装处真被调用。

    单测函数本身绿不代表生产生效——本项目层出过「定义了
    防护但零调用方」的空接线。这里直接钉住唯一收口点
    `_build_provider_messages` 确实调用它。
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "bandi" / "agent" / "states.py"
    text = source.read_text(encoding="utf-8")
    assert "guard_provider_dicts(provider_messages)" in text, (
        "dict 层孤儿校验未接入 _build_provider_messages："
        "网关 400 会回来"
    )
    assert "guard_provider_dicts" in text.split("def _build_provider_messages")[1][:1200], (
        "校验必须在请求组装函数内部"
    )
