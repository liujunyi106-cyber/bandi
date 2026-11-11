"""功能：契约 — 异常文案兜底（空 error/静默失败专项）

钉的行为：
- error_text：空消息异常 → 类名（不得产生空串）；有消息 → 原样；
- 用户可见终态接线（agent 循环 / CLI / API / 公司链）以 error_text 兜底——
  接线由 wiring-check + 场景验证。
"""

from __future__ import annotations

import pytest

from bandi.agent.states import _PROMPT_TOO_LONG_MESSAGE
from bandi.utils.errors import error_text


class _SilentError(Exception):
    pass


class TestErrorText:
    def test_empty_message_falls_back_to_type_name(self) -> None:
        assert error_text(_SilentError()) == "_SilentError"

    def test_message_preserved(self) -> None:
        assert error_text(ValueError("boom")) == "boom"

    @pytest.mark.parametrize(
        "exc",
        [_SilentError(), ValueError(""), RuntimeError(), KeyError("k"), OSError()],
    )
    def test_never_empty(self, exc: BaseException) -> None:
        assert error_text(exc), "error_text 绝不得为空（空回复/空错误面无信息）"


# ==================== overflow_terminal_shape ====================


class TestOverflowTerminalShape:
    def test_message_non_empty(self) -> None:
        assert _PROMPT_TOO_LONG_MESSAGE.strip(), "溢出终态文案不得为空（准则 2 可诊断性）"

    def test_message_carries_actionable_guidance(self) -> None:
        assert any(k in _PROMPT_TOO_LONG_MESSAGE for k in ("开新会话", "减小输入", "减小规模")), (
            "必须给出可行动指引（准则 1 形状同构 + 准则 2）"
        )
