# 功能：契约——群聊意图四态：
# confirm=确认执行（顶层接单出计划，turn_type=INTAKE）/ adjust=调整返工 /
# query=咨询问答（chat_kind=query——右栏不出发计划）/ task=执行指令（无标记）。
# 钉行为：分类词表命中与短句限制 / 卡的 chat_kind 标记与 turn_type 映射 /
# 破坏者：长文含确认词不误判 / task 不标记。
from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.api.app import _classify_chat_intent


class TestClassifyChatIntent:
    def test_confirm_short_phrases(self) -> None:
        for text in ("行，就这个方案吧", "开始吧", "确认执行", "就这么办", "动手吧"):
            assert _classify_chat_intent(text) == "confirm", text

    def test_confirm_requires_short_sentence(self) -> None:
        """破坏者：长文末尾夹带确认词不得误判 confirm（≤24 字限制）。"""
        long_text = "我们讨论了目标市场与品类方向以及供应链的一切细节之后决定开始吧请帮助推进"
        assert _classify_chat_intent(long_text) != "confirm"

    def test_adjust_phrases(self) -> None:
        for text in ("视觉方案要调整一下", "把前端方案重做", "这个改成蓝色"):
            assert _classify_chat_intent(text) == "adjust", text

    def test_query_phrases(self) -> None:
        for text in ("这个app应该采用什么风格", "前端技术栈我们应该用什么", "我应该选哪个方案？"):
            assert _classify_chat_intent(text) == "query", text

    def test_idea_statements_are_query(self) -> None:
        """想法陈述（我想做 X）是讨论而非执行指令。"""
        for text in ("我想做一个宠物智能硬件产品", "我打算进入北美市场", "准备做一个新项目"):
            assert _classify_chat_intent(text) == "query", text

    def test_task_default(self) -> None:
        for text in ("帮我做一个app", "写一份北美市场调研报告"):
            assert _classify_chat_intent(text) == "task", text

    def test_priority_confirm_over_adjust_query(self) -> None:
        """优先级：confirm > adjust > 想法/query > task。"""
        assert _classify_chat_intent("就这个方案吧，不用改") == "confirm"
        assert _classify_chat_intent("调整这个方案") == "adjust"
        assert _classify_chat_intent("我想调整一下方案") == "adjust"


class TestHandleChatMessageIntent:
    def _run(self, tmp_path: Path, intent: str, mode: str = "") -> Any:
        from bandi.api.company_chat import handle_chat_message
        from bandi.company.org_engine import OrgEngine
        from bandi.company.store import WorkItemStore

        store = WorkItemStore(str(tmp_path / "c.db"))
        org = OrgEngine()
        org.register_role("pm", "产品经理", responsibility="需求", team_id="t1")
        comms_dir = tmp_path / "comms"
        comms_dir.mkdir()

        class _Comms:
            def send_dm(self, **kwargs: Any) -> None:
                return None

        created = handle_chat_message(
            content="写一份产品需求文档",
            sender_role="user",
            communication=_Comms(),
            store=store,
            org_engine=org,
            project_id="",
            session_id="s1",
            mode=mode,
            default_role="pm",
            member_role_ids={"pm"},
            intent=intent,
        )
        return created

    def test_query_marks_chat_kind(self, tmp_path: Path) -> None:
        created = self._run(tmp_path, "query")
        assert created and created[0].metadata.get("chat_kind") == "query"
        assert created[0].turn_type.value == "execute"

    def test_confirm_uses_intake_turn_type(self, tmp_path: Path) -> None:
        created = self._run(tmp_path, "confirm")
        assert created and created[0].turn_type.value == "intake"

    def test_task_has_no_chat_kind(self, tmp_path: Path) -> None:
        created = self._run(tmp_path, "task")
        assert created and "chat_kind" not in created[0].metadata

    def test_adjust_marks_chat_kind(self, tmp_path: Path) -> None:
        created = self._run(tmp_path, "adjust")
        assert created and created[0].metadata.get("chat_kind") == "adjust"
