# 功能：IntentRouter 单元测试

from __future__ import annotations

from bandi.core.message import Message
from bandi.perception.intent_router import IntentRouter


class TestIntentRouter:
    def test_route_plan_keyword_chinese(self) -> None:
        router = IntentRouter()
        msg = Message(role="user", content="帮我规划一下学习计划")
        assert router.route(msg) == "plan"

    def test_route_plan_keyword_english(self) -> None:
        router = IntentRouter()
        msg = Message(role="user", content="make a plan for today")
        assert router.route(msg) == "plan"

    def test_route_question_returns_execute(self) -> None:
        """双模式化后问句不再是 ask——意图提示只区分 plan/execute。"""
        router = IntentRouter()
        msg = Message(role="user", content="深度学习是什么")
        assert router.route(msg) == "execute"

    def test_route_question_english_returns_execute(self) -> None:
        router = IntentRouter()
        msg = Message(role="user", content="what is ontology")
        assert router.route(msg) == "execute"

    def test_route_execute_default(self) -> None:
        router = IntentRouter()
        msg = Message(role="user", content="帮我查一下天气")
        assert router.route(msg) == "execute"

    def test_route_empty_content(self) -> None:
        router = IntentRouter()
        msg = Message(role="user", content="")
        assert router.route(msg) == "execute"

    def test_route_plan_in_sentence(self) -> None:
        router = IntentRouter()
        msg = Message(role="user", content="帮我规划一下路线")
        assert router.route(msg) == "plan"

    def test_qingwen_prefix_not_ask(self) -> None:
        """「请问如何配置环境」是执行请求，不得因含「问」误判为 ask 禁掉工具。"""
        router = IntentRouter()
        msg = Message(role="user", content="请问如何配置开发环境")
        assert router.route(msg) == "execute"

    def test_fangwen_not_ask(self) -> None:
        """「访问某网站」不得误判 ask。"""
        router = IntentRouter()
        msg = Message(role="user", content="帮我访问一下那个网页")
        assert router.route(msg) == "execute"

    def test_wenti_word_not_ask(self) -> None:
        """「这是问题所在」中的「问题」不是问句。"""
        router = IntentRouter()
        msg = Message(role="user", content="这就是问题的所在，帮我修复它")
        assert router.route(msg) == "execute"

    def test_real_question_still_execute(self) -> None:
        """真正的问句（这是什么/问个问题）不再切 ask——execute 兜底。"""
        router = IntentRouter()
        assert router.route(Message(role="user", content="这是什么")) == "execute"
        assert router.route(Message(role="user", content="问个问题：这个工具怎么用")) == "execute"
        assert router.route(Message(role="user", content="这个函数在哪里定义的")) == "execute"
