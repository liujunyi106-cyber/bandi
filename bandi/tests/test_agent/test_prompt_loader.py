from bandi.agent.prompt_harness.prompt_loader import PromptLoader


class TestPromptLoader:
    def test_identity_loaded(self) -> None:
        loader = PromptLoader()
        tpl = loader.get_identity()
        assert "{language}" in tpl

    def test_framework_loaded(self) -> None:
        loader = PromptLoader()
        tpl = loader.get_framework()
        assert "五种实体" in tpl

    def test_mode_prompts_loaded(self) -> None:
        loader = PromptLoader()
        modes = loader.get_mode_prompts()
        assert "execute" in modes
        assert "plan" in modes
        assert "ask" not in modes, "双模式化：ask 模式段必须删除"
        assert "思考" in modes["execute"]

    def test_safety_loaded(self) -> None:
        loader = PromptLoader()
        tpl = loader.get_safety()
        assert "安全约束" in tpl

    def test_output_control_loaded(self) -> None:
        loader = PromptLoader()
        tpl = loader.get_output_control()
        assert "输出规则" in tpl

    def test_user_experience_loaded(self) -> None:
        loader = PromptLoader()
        tpl = loader.get_user_experience()
        assert "用户体验" in tpl

    def test_fallback_template_loaded(self) -> None:
        loader = PromptLoader()
        tpl = loader.get_fallback_template()
        assert "{degraded_list}" in tpl

    def test_prompt_harness_builds_with_loader(self) -> None:
        from bandi.agent.prompt_harness.builder import PromptHarness
        from bandi.perception.context_loader import ContextBundle

        harness = PromptHarness()
        result = harness.build("execute", ContextBundle())
        assert "Bandi" in result
        assert "执行模式" in result

    def test_prompt_harness_plan_mode(self) -> None:
        from bandi.agent.prompt_harness.builder import PromptHarness
        from bandi.perception.context_loader import ContextBundle

        harness = PromptHarness()
        result = harness.build("plan", ContextBundle())
        assert "计划模式" in result
