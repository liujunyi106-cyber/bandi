# 功能：多选项卡生产链路契约
#
# 钉行为不钉实现：
# 1. 工具 schema 暴露 choices/multi_select/allow_custom（LLM 看得见才会传）
# 2. execute 透传选项：多问 → ask_questions payload.questions[].choices；
# 单问 → ask_input(choices=...)（前端单选卡读顶层 payload.choices）
from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.prompt_harness.artifacts import MODE_PROMPTS
from bandi.tools.user_input import UserInputTool


class _FakeUI:
    """功能：记录挂起 payload 的交互通道替身。"""

    def __init__(self) -> None:
        self.input_calls: list[dict[str, Any]] = []
        self.question_calls: list[list[dict[str, Any]]] = []

    async def ask_input(self, prompt: str, **kwargs: Any) -> str:
        self.input_calls.append({"prompt": prompt, **kwargs})
        return "已选：B"

    async def ask_questions(self, questions: list[dict[str, Any]]) -> dict[str, str]:
        self.question_calls.append(questions)
        return {str(q.get("id")): "已选：A" for q in questions}


class TestSchema:
    def test_schema_exposes_choice_fields(self) -> None:
        item_props = UserInputTool.parameters["properties"]["questions"]["items"]["properties"]
        assert "choices" in item_props, "schema 未暴露 choices → LLM 不会传 → 选项卡永不触发"
        assert "multi_select" in item_props
        assert "allow_custom" in item_props

    def test_reason_is_optional(self) -> None:
        # → 提问卡永不出现。reason 是元信息，不得成为提问前置条件。
        assert "required" not in UserInputTool.parameters
        assert "reason" in UserInputTool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_questions_only_call_succeeds(self) -> None:
        ui = _FakeUI()
        tool = UserInputTool(ui=ui)
        result = await tool.execute(
            {"questions": [{"question": "先做哪一步？", "choices": ["A", "B"]}]}
        )
        assert "error" not in result
        assert ui.input_calls, "无 reason 也应正常挂起提问"

    @pytest.mark.asyncio
    async def test_empty_call_returns_actionable_error(self) -> None:
        tool = UserInputTool(ui=_FakeUI())
        result = await tool.execute({})
        assert "error" in result and "questions" in str(result["error"])

    def test_description_guides_llm_to_supply_choices(self) -> None:
        assert "choices" in UserInputTool.description


class TestChoicePassthrough:
    @pytest.mark.asyncio
    async def test_multi_questions_keep_choices(self) -> None:
        ui = _FakeUI()
        tool = UserInputTool(ui=ui)
        result = await tool.execute(
            {
                "reason": "需要确认范围",
                "questions": [
                    {
                        "id": "q1",
                        "question": "先做哪一步？",
                        "choices": ["数据核对", "归因框架", "补数据"],
                    },
                    {"id": "q2", "question": "报告给谁？", "choices": ["我", "团队"]},
                ],
            }
        )
        assert len(ui.question_calls) == 1
        items = ui.question_calls[0]
        assert items[0]["choices"] == ["数据核对", "归因框架", "补数据"]
        assert items[1]["choices"] == ["我", "团队"]
        assert result["answers"]

    @pytest.mark.asyncio
    async def test_single_question_uses_input_choices(self) -> None:
        ui = _FakeUI()
        tool = UserInputTool(ui=ui)
        await tool.execute(
            {
                "reason": "选一个方案",
                "questions": [
                    {
                        "id": "q1",
                        "question": "选哪个方案？",
                        "choices": ["方案 A", "方案 B"],
                        "allow_custom": True,
                    }
                ],
            }
        )
        # 单问走 ask_input（前端单选卡读顶层 payload.choices）
        assert len(ui.input_calls) == 1
        call = ui.input_calls[0]
        assert call["choices"] == ["方案 A", "方案 B"]
        assert call["allow_custom"] is True

    @pytest.mark.asyncio
    async def test_single_choice_is_dropped(self) -> None:
        # 破坏者视角：只有 1 个选项时不算选项卡（应退化为纯文本）
        ui = _FakeUI()
        tool = UserInputTool(ui=ui)
        await tool.execute(
            {"reason": "r", "questions": [{"question": "继续？", "choices": ["好"]}]}
        )
        assert ui.input_calls[0].get("choices") in (None, [])

    @pytest.mark.asyncio
    async def test_legacy_ui_without_choices_keyword_still_works(self) -> None:
        class _LegacyUI:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def ask_input(self, prompt: str) -> str:  # 无 choices 关键字
                self.calls.append(prompt)
                return "答案"

        ui = _LegacyUI()
        tool = UserInputTool(ui=ui)
        result = await tool.execute(
            {"reason": "r", "questions": [{"question": "继续吗？", "choices": ["A", "B"]}]}
        )
        assert ui.calls, "旧通道应被调用（TypeError 退化）"
        assert result["answers"]


class TestUnansweredNote:
    """：超时/未答也要把结果回给 LLM，让它判断决策倾向与是否追问。"""

    @pytest.mark.asyncio
    async def test_timeout_returns_note_for_llm(self) -> None:
        class _TimeoutUI(_FakeUI):
            async def ask_input(self, prompt: str, **kwargs: Any) -> str:
                self.input_calls.append({"prompt": prompt, **kwargs})
                return ""  # 超时 → 空答复

            async def ask_questions(self, questions: list[dict[str, Any]]) -> dict[str, str]:
                self.question_calls.append(questions)
                return {}

        ui = _TimeoutUI()
        tool = UserInputTool(ui=ui)
        result = await tool.execute(
            {"questions": [{"id": "q1", "question": "先做哪一步？", "choices": ["A", "B"]}]}
        )
        # 单问：键保留、值为空（超时未答）
        assert result["answers"] == {"q1": ""}
        assert result["unanswered"] == 1
        assert "未答" in str(result["note"]) or "未答复" in str(result["note"])

    @pytest.mark.asyncio
    async def test_partial_answers_report_missing_count(self) -> None:
        class _PartialUI(_FakeUI):
            async def ask_questions(self, questions: list[dict[str, Any]]) -> dict[str, str]:
                self.question_calls.append(questions)
                return {"q1": "已选 A"}  # q2 未答（用户跳过）

        ui = _PartialUI()
        tool = UserInputTool(ui=ui)
        result = await tool.execute(
            {
                "questions": [
                    {"id": "q1", "question": "环境？"},
                    {"id": "q2", "question": "回滚策略？"},
                ]
            }
        )
        assert result["unanswered"] == 1
        assert "q1" in result["answers"] and "q2" not in result["answers"]

    @pytest.mark.asyncio
    async def test_fully_answered_has_no_note(self) -> None:
        ui = _FakeUI()
        tool = UserInputTool(ui=ui)
        result = await tool.execute(
            {"questions": [{"id": "q1", "question": "环境？", "choices": ["prod", "staging"]}]}
        )
        assert "note" not in result and "unanswered" not in result

    @pytest.mark.asyncio
    async def test_note_tells_model_to_ask_in_prose_not_reopen_card(self) -> None:
        """（决策反转）：信息不足时**不要再弹选项卡**。

        原实现 note 引导「优先再次调用 request_user_input 弹出选项卡」
        但纯自然语言触发率仅 1/3 且频繁弹卡打断对话——
        用户明确要求「信息不足直接在回复内容里问就行」。故本契约反转：
        note 必须引导模型在**回复正文**里追问，并显式禁止再为此弹卡。
        """

        class _PartialUI(_FakeUI):
            async def ask_questions(self, questions: list[dict[str, Any]]) -> dict[str, str]:
                self.question_calls.append(questions)
                return {"q1": "已选 A"}  # q2 跳过 → 触发 note 分支

        tool = UserInputTool(ui=_PartialUI())
        result = await tool.execute(
            {"questions": [{"id": "q1", "question": "环境？"}, {"id": "q2", "question": "回滚？"}]}
        )
        note = str(result.get("note", ""))
        assert "回复正文" in note, "note 必须引导模型在回复正文里向用户提问"
        assert "不要再" in note and "选项卡" in note, "note 必须显式禁止再弹选项卡"
        # 引导语不得挤掉原有语义：缺额 + 禁止臆测仍须在
        assert result["unanswered"] == 1
        assert "臆测" in note


class TestPlanPrompt:
    def test_plan_prompt_drops_time_estimate(self) -> None:
        prompt = MODE_PROMPTS["plan"]
        assert "预期耗时" not in prompt, "用户真机反馈：耗时标注对用户无价值"

    def test_plan_prompt_forbids_mode_self_reference(self) -> None:
        prompt = MODE_PROMPTS["plan"]
        assert "禁止" in prompt
        assert "耗时" in prompt
