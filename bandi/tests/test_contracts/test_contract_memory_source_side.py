# 功能：契约长期记忆不得把助手回复原文当作用户事实存下
#
# 行为不变式（钉行为，不钉实现）：
#   1. 兜底写入的内容源必须是**用户输入**（要记住的是用户说过的事实），不是助手回复
# 2. 用户说「记住…」时显式触达仍要入库
#   3. 无显式触达且用户输入不含可记事实时（纯寒暄/能力询问），不得落库
#   4. 破坏者视角：助手回复里即使含偏好词（"你喜欢…"），也不能成为记忆内容

from __future__ import annotations

from bandi.core.agent_runner import _long_term_source_text


class TestLongTermSourceIsUserInput:
    def test_plain_greeting_yields_no_memory(self) -> None:
        """功能：纯寒暄不落库——「你好呀，你能帮我做什么？」不该产生任何长期记忆。

        修复前：把助手 241 字能力清单当 task_state 落库（entities 为空、progress
        直接截取回复正文），并被召回过一次。
        """
        user_input = "你好呀，你能帮我做什么？"
        reply = "\n\n老板好！我能帮你做这些：\n\n**日常事务**\n- 检索信息\n- 日程管理\n"

        assert _long_term_source_text(user_input, reply, explicit_remember=False) is None

    def test_capability_answer_not_stored_as_user_fact(self) -> None:
        """功能：破坏者视角——助手回复含偏好词也不得成为记忆内容。"""
        user_input = "现在几点了"
        reply = "你 favorite 88，请按你的喜好来调整。"

        assert _long_term_source_text(user_input, reply, explicit_remember=False) is None

    def test_explicit_remember_still_records_user_input(self) -> None:
        """功能：显式触达仍要落库，且内容是用户原话（不是助手确认语）。"""
        user_input = "我特别喜欢数字 88 和靛蓝色，记住啦！"
        reply = "好的老板，我已经记住了！"

        got = _long_term_source_text(user_input, reply, explicit_remember=True)

        assert got == user_input
        assert "已经记住了" not in (got or "")


class TestUserFactsStillRecorded:
    def test_user_declared_fact_is_recorded(self) -> None:
        """功能：无回归——用户陈述事实时仍要落库（哪怕没说「记住」）。"""
        user_input = "我们项目的部署端口是 7421，上线时间定在周三"

        got = _long_term_source_text(user_input, "记下了。", explicit_remember=False)

        assert got == user_input

    def test_user_preference_statement_recorded(self) -> None:
        """功能：用户陈述偏好仍要落库。"""
        user_input = "我不吃香菜，以后点餐都帮我备注一下"

        got = _long_term_source_text(user_input, "好的。", explicit_remember=False)

        assert got == user_input


class TestEmptyInputGuard:
    def test_blank_user_input_returns_none(self) -> None:
        """功能：用户输入为空时不得落库（不得回退到助手回复）。"""
        assert _long_term_source_text("", "助手说了很多话", explicit_remember=True) is None
        assert _long_term_source_text("   ", "助手说了很多话", explicit_remember=False) is None
