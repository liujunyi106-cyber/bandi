# 功能：用户输入工具（UserInputTool 请求用户交互，async Protocol 通道）
from __future__ import annotations

import asyncio
import json
from typing import Any

from bandi.core.user_interaction import UserInteraction
from bandi.tools.base import BaseTool, RetryPolicy, ToolCategory, ToolPermissionContext


class UserInputTool(BaseTool):
    name: str = "request_user_input"
    description: str = (
        "Pause execution and request input from the user. "
        "Use when you need missing information to continue the task. "
        "Whenever the answer can be covered by 2-4 concrete candidates, put them in "
        "`questions[].choices` so the user can pick with one click instead of typing."
    )
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why user input is needed",
            },
            "questions": {
                "type": ["array", "string"],
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "question": {"type": "string"},
                        "choices": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional candidate answers (2-4 items) rendered as one-click "
                                "options; omit to ask a free-text question. Do NOT add "
                                'catch-all items such as "Other"/"其他"/"其它"/"其他服务"/'
                                '"以上都不是" — the card already renders a free-text input '
                                "for answers outside the list, so a catch-all choice is "
                                "redundant and makes the user click instead of type."
                            ),
                        },
                        "multi_select": {
                            "type": "boolean",
                            "description": "Allow picking several options (default false)",
                        },
                        "allow_custom": {
                            "type": "boolean",
                            "description": (
                                "Allow the user to type a custom answer; defaults to true "
                                "(set false only when the options must be picked as-is)"
                            ),
                        },
                    },
                    "required": ["question"],
                },
                "description": "Questions to ask the user",
            },
        },
        # 因 'reason' is a required property 被 schema 校验拒绝 → 提问卡永不出现。
        # 「为什么要问」是元信息，不该成为提问的前置条件。
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    def __init__(
        self,
        input_fn: Any | None = None,
        ui: UserInteraction | None = None,
    ) -> None:
        super().__init__()
        # 注入的 async 交互通道（CLI/HTTP 实现 UserInteraction Protocol）——优先
        self._ui = ui
        # 退化输入源（单测/CLI 本地场景）：同步 fn 经 to_thread 包出 async
        # 避免 headless（无 stdin）下 input() 永久挂死——无 ui 无 input_fn 时
        # 显式抛错而非静默等待（fail-closed，见 _prompt_one）
        self._input_fn = input_fn

    def bind_ui(self, ui: UserInteraction) -> None:
        """运行时装配交互通道（registry 内工具由 factory 构建后 bind，沿 SubAgentTool.bind_registry 先例）。

        registry 中工具实例化时无注入通道，须在 ExecutionContext 装配时把
        run_agent_turn 收到的 user_interaction（CLI/HTTP UI）bind 到本工具
        否则真实链 LLM 调用 request_user_input 会 fail-closed 抛错而非真挂起。
        """
        self._ui = ui

    async def _prompt_one(
        self,
        text: str,
        choices: list[str] | None = None,
        multi_select: bool = False,
        allow_custom: bool = False,
    ) -> str:
        if self._ui is not None:
            try:
                answer = await self._ui.ask_input(
                    text,
                    choices=choices,
                    multi_select=multi_select,
                    allow_custom=allow_custom,
                )
            except TypeError as exc:
                # 只兜「老实现不接受 choices 等关键字」这一种情况。若把 TypeError
                # 一概兜住，ask_input 内部任何真实缺陷（如同步回调被无条件 await）
                # 都会触发重发——同一问题登记两张卡，前一张成孤儿、用户答复回错卡。
                if "unexpected keyword argument" not in str(exc):
                    raise
                answer = await self._ui.ask_input(text)
            return (answer or "").strip() if isinstance(answer, str) else ""
        if self._input_fn is None:
            raise RuntimeError(
                "request_user_input: no interactive channel available "
                "(headless mode denies by default)"
            )
        result = await asyncio.to_thread(self._input_fn, f"{text}: ")
        return (result or "").strip()

    @staticmethod
    def _clean_choices(raw: Any) -> list[str]:
        """功能：清洗选项列表（<2 项不算选项卡——单项无意义，退化为纯文本）。"""
        if not isinstance(raw, list):
            return []
        cleaned = [str(item).strip() for item in raw if str(item).strip()]
        return cleaned if len(cleaned) >= 2 else []

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        reason = params.get("reason", "")
        raw_questions = params.get("questions", [])
        # 模型常把问题数组序列化成 JSON 字符串提交——宽容解析（同 emit_ui_tree 先例）
        if isinstance(raw_questions, str):
            try:
                raw_questions = json.loads(raw_questions)
            except json.JSONDecodeError:
                raw_questions = []
        questions = raw_questions if isinstance(raw_questions, list) else []
        if not questions and not str(reason).strip():
            return {
                "reason": "",
                "answers": {},
                "error": "请提供 questions（要问的问题，可带 choices）或 reason（需要用户补充的信息）",
            }
        # 多决策点（≥2 问，-3）：一次挂起走分页通道（前端 <1/N> 切换后汇总 JSON 回传）；
        items: list[dict[str, Any]] = []
        for q in questions:
            if not isinstance(q, dict) or not str(q.get("question") or "").strip():
                continue
            item: dict[str, Any] = {
                "id": str(q.get("id") or q.get("question")),
                "question": str(q.get("question")),
            }
            # 多选题永远退化成文本框；多选项卡在生产链路无法触发）
            choices = self._clean_choices(q.get("choices"))
            if choices:
                item["choices"] = choices
                if q.get("multi_select") is True:
                    item["multi_select"] = True
                if q.get("allow_custom") is True:
                    item["allow_custom"] = True
            items.append(item)
        if len(items) > 1 and self._ui is not None and hasattr(self._ui, "ask_questions"):
            answers = await self._ui.ask_questions(items)
            return self._result_with_unanswered_note(reason, answers, len(items))
        answers: dict[str, str] = {}
        for q in questions:
            text = q.get("question", "") if isinstance(q, dict) else str(q)
            qid = q.get("id", text) if isinstance(q, dict) else text
            if text:
                answer = await self._prompt_one(
                    f"{text}: ",
                    choices=self._clean_choices(q.get("choices")) if isinstance(q, dict) else None,
                    multi_select=bool(isinstance(q, dict) and q.get("multi_select") is True),
                    allow_custom=bool(isinstance(q, dict) and q.get("allow_custom") is True),
                )
                answers[qid] = answer
        return self._result_with_unanswered_note(reason, answers, 1)

    @staticmethod
    def _result_with_unanswered_note(
        reason: str, answers: dict[str, str], asked: int
    ) -> dict[str, Any]:
        """功能：未答/超时的答复也要**带说明回传**，让 LLM 知道这是"用户没答"而非答了空。

         超时按空答复处理，但要把结果回给 LLM，由它判断用户对该决策点
        是否有倾向、是否需要继续追问（缺项本身就是"未决问题"的信号）。
        """
        result: dict[str, Any] = {"reason": reason, "answers": answers}
        answered = sum(1 for value in answers.values() if str(value or "").strip())
        if asked > 0 and answered < asked:
            result["unanswered"] = asked - answered
            # 说明频繁弹卡反而打断对话；秘书在正文里追问更自然。）
            result["note"] = (
                f"用户未答复全部问题（{answered}/{asked} 已答；其余为空）。"
                "请据此判断用户对这些决策点是否有倾向；若关键信息仍缺失，"
                "直接在回复正文里向用户提问说明当前阻塞点，"
                "不要臆测用户的选择，也不要再为此弹出选项卡。"
            )
        return result
