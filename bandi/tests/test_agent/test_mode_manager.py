# 功能：ModeManager 单元测试——默认模式/切换/工具过滤/提示词

from bandi.agent.mode_manager import ModeManager


class TestModeManager:
    def test_default_mode_is_execute(self) -> None:
        mm = ModeManager()
        assert mm.mode == "execute"

    def test_switch_valid_mode(self) -> None:
        mm = ModeManager("execute")
        old = mm.switch("plan")
        assert old == "execute"
        assert mm.mode == "plan"

    def test_switch_invalid_mode_does_nothing(self) -> None:
        mm = ModeManager("execute")
        old = mm.switch("invalid")
        assert old == "execute"
        assert mm.mode == "execute"

    def test_filter_tools_ask_falls_back_execute(self) -> None:
        """双模式化：ask 不再是合法初始模式，兜底 execute 返回全量工具。"""
        mm = ModeManager("ask")
        assert mm.mode == "execute"
        tools = ["bash", "read", "write"]
        assert mm.filter_tools(tools) == tools

    def test_filter_tools_plan_returns_readonly(self) -> None:
        mm = ModeManager("plan")
        result = mm.filter_tools(["bash", "read", "web_search", "ontology_query", "write"])
        assert set(result) == {"read", "web_search", "ontology_query"}

    def test_filter_tools_execute_returns_all(self) -> None:
        mm = ModeManager("execute")
        tools = ["bash", "read", "write", "ontology_assert"]
        assert mm.filter_tools(tools) == tools

    def test_mode_label(self) -> None:
        assert ModeManager("execute").mode_label()
