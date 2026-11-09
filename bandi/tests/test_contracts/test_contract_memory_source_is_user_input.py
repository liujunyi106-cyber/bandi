"""契约：长期记忆的内容源——直写只取用户输入两路（显式记住 / 可记事实）。

行为不变式（钉行为，不钉实现）：
1. 显式「记住…」→ 记用户原话；
2. 用户陈述事实/偏好 → 记用户原话；
3. 回复侧永不直写——助手结论不是内容源（用户只给指令、事实由助手查出时，
 结论不经直写落库）；
4. 破坏者视角：回复里含事实/偏好/数字也不得成为记忆内容。

丢弃回复侧内容源的原因：助手回复落进长期记忆会以 task_state 名义污染记忆库
。"""

from __future__ import annotations

import pytest

from bandi.core.agent_runner import _long_term_source_text


class TestUserInputPreferred:
    def test_explicit_remember_uses_user_words(self) -> None:
        src = _long_term_source_text("记住我的暗号是 pineapple-77", "已记住暗号", True)
        assert src == "记住我的暗号是 pineapple-77"

    def test_self_stated_fact_uses_user_words(self) -> None:
        src = _long_term_source_text("我特别喜欢数字 88 和靛蓝色", "好的，已记住", False)
        assert src == "我特别喜欢数字 88 和靛蓝色"

    def test_empty_user_input_writes_nothing(self) -> None:
        assert _long_term_source_text("", "助手说了很多", True) is None
        assert _long_term_source_text(None, "助手说了很多", True) is None


class TestReplyNeverDirectWrites:
    """回复侧永不直写——「源选择」不存在助手回落路径（旧实现的「长回复
    ≥50 字回落」已删除）。"""

    # 取自 L1 的真实助手结论长度（旧实现 ≥50 字会回落写入）
    CONCLUSION = (
        "结论：第一轮回答：恒星贸易项目已确认合同金额五十万元，联系人王经理，"
        "下一步确认交付日期，议价空间百分之七十五。"
    )

    @pytest.mark.parametrize(
        "user_input",
        ["记录恒星贸易项目", "帮我记录一下", "帮我看看", "现在几点了"],
        ids=["record-instructive", "record-request", "lookup", "casual"],
    )
    def test_reply_never_becomes_source(self, user_input: str) -> None:
        assert _long_term_source_text(user_input, self.CONCLUSION, explicit_remember=False) is None

    def test_explicit_remember_ignores_reply(self) -> None:
        src = _long_term_source_text("帮我看看恒星项目", self.CONCLUSION, True)
        assert src == "帮我看看恒星项目", "显式触达仍记用户原话，但绝不取回复"

    def test_reply_with_fact_markers_still_rejected(self) -> None:
        """破坏者视角：回复含偏好词/数字/结论信号也不得成为记忆内容。"""
        reply = "你 favorite 88，结论：数字 88 是靛蓝色偏好，请按你的喜好来调整。" * 3
        assert _long_term_source_text("现在几点了", reply, explicit_remember=False) is None
