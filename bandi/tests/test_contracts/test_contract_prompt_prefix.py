"""
  前缀指纹：比对相邻两次请求的 system 区字节一致性，指出变化落在哪个 `## 段`。

破坏者断言：
- 只比 system 区——状态块/本轮输入挂在 user 侧，变化不得被误报为前缀变化；
- 首次请求不产生变化记录（没有可比对象）；
- system 完全一致时不得产生变化记录（否则日志噪音淹没真实信号）。
"""

from types import SimpleNamespace
from typing import Any


def _ctx(session_id: str = "s1") -> Any:
    from bandi.agent.states import _PREFIX_STATE

    _PREFIX_STATE.clear()
    return SimpleNamespace(meta={"session_id": session_id})


def _messages(system: str, user: str = "hi") -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def test_first_request_records_no_change() -> None:
    from bandi.agent.states import _track_prompt_prefix

    ctx = _ctx()
    _track_prompt_prefix(ctx, _messages("## 角色\n你是助手"))
    assert "_prompt_prefix_change" not in ctx.meta


def test_same_system_records_no_change() -> None:
    from bandi.agent.states import _track_prompt_prefix

    ctx = _ctx()
    _track_prompt_prefix(ctx, _messages("## 角色\n你是助手"))
    _track_prompt_prefix(ctx, _messages("## 角色\n你是助手"))
    assert "_prompt_prefix_change" not in ctx.meta


def test_changed_system_reports_section() -> None:
    from bandi.agent.states import _track_prompt_prefix

    ctx = _ctx()
    _track_prompt_prefix(ctx, _messages("## 角色\n你是助手"))
    _track_prompt_prefix(ctx, _messages("## 角色\n你是助手\n\n## 上下文参考\n- 记忆A"))

    detail = ctx.meta["_prompt_prefix_change"]
    assert detail["changed_section"] == "上下文参考"
    assert detail["chars"] > detail["prev_chars"]


def test_user_side_change_is_not_prefix_change() -> None:
    """状态块与当前输入挂在 user 侧——它们的每轮变化不得计入前缀指纹。"""
    from bandi.agent.states import _track_prompt_prefix

    ctx = _ctx()
    _track_prompt_prefix(ctx, _messages("## 角色\n你是助手", user="第一轮"))
    _track_prompt_prefix(
        ctx, _messages("## 角色\n你是助手", user="[任务状态]\n当前目标: X\n\n第二轮")
    )
    assert "_prompt_prefix_change" not in ctx.meta


def test_fingerprint_survives_new_context_per_turn() -> None:
    """每轮 ctx 都是新对象——指纹必须按会话跨 ctx 比较，否则永远只报首次。"""
    from bandi.agent.states import _PREFIX_STATE, _track_prompt_prefix

    _PREFIX_STATE.clear()
    first = SimpleNamespace(meta={"session_id": "s-turn"})
    _track_prompt_prefix(first, _messages("## 角色\n助手"))
    second = SimpleNamespace(meta={"session_id": "s-turn"})
    _track_prompt_prefix(second, _messages("## 角色\n助手\n\n## 上下文参考\n- 记忆A"))

    assert second.meta["_prompt_prefix_change"]["changed_section"] == "上下文参考"


def test_sessions_do_not_share_prefix_baseline() -> None:
    from bandi.agent.states import _PREFIX_STATE, _track_prompt_prefix

    _PREFIX_STATE.clear()
    a = SimpleNamespace(meta={"session_id": "s-a"})
    b = SimpleNamespace(meta={"session_id": "s-b"})
    _track_prompt_prefix(a, _messages("## 角色\nA"))
    _track_prompt_prefix(b, _messages("## 角色\n完全不同的 B"))

    assert "_prompt_prefix_change" not in b.meta
