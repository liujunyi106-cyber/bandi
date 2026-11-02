# 功能：契约 提示词常量是唯一真相源——YAML 覆盖机制已移除
#
# 背景：原实现把同一段提示词写两遍（artifacts.py 常量 + prompts/*.yaml），
# PromptLoader 优先读 YAML、常量仅作兜底。两份漂移会导致「改了常量但跑的是
# 另一份」——//修复时即踩此坑（改常量后契约全红）。
#
# 行为不变式：
#   1. 提示词只有一份定义（常量），不存在等价的 YAML 影子
#   2. PromptLoader 不再接受 prompt_dir，也不再依赖 yaml 库
#   3. 各 getter 直接返回常量，语义与此前「YAML 生效」时一致

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from bandi.agent.prompt_harness import artifacts
from bandi.agent.prompt_harness.prompt_loader import PromptLoader


class TestSingleSourceOfTruth:
    def test_no_yaml_shadow_files(self) -> None:
        """功能：仓库内不得残留提示词 YAML 影子文件。

        破坏者视角：新增 prompts/*.yaml 会让常量不再是唯一真相源。
        """
        prompts_dir = Path(artifacts.__file__).parent / "prompts"

        leftovers = (
            sorted(p.name for p in prompts_dir.glob("*.yaml")) if prompts_dir.exists() else []
        )

        assert leftovers == [], f"提示词 YAML 影子仍在：{leftovers}"

    def test_loader_module_does_not_import_yaml(self) -> None:
        """功能：loader 不得再依赖 yaml 解析（覆盖机制已移除）。"""
        source = Path(
            PromptLoader.__module__ and importlib.import_module(PromptLoader.__module__).__file__
        ).read_text(encoding="utf-8")

        assert "import yaml" not in source
        assert "_load_yaml" not in source

    def test_loader_takes_no_prompt_dir(self) -> None:
        """功能：构造签名不得再暴露 prompt_dir（无 YAML 覆写口子）。"""
        import inspect

        params = inspect.signature(PromptLoader.__init__).parameters

        assert "prompt_dir" not in params


class TestGettersReturnConstants:
    def test_identity_matches_constant(self) -> None:
        assert PromptLoader().get_identity() == artifacts.IDENTITY_PROMPT

    def test_framework_matches_constant(self) -> None:
        assert PromptLoader().get_framework() == artifacts.FRAMEWORK_PROMPT

    def test_safety_matches_constant(self) -> None:
        assert PromptLoader().get_safety() == artifacts.SAFETY_PROMPT

    def test_output_control_matches_constant(self) -> None:
        assert PromptLoader().get_output_control() == artifacts.OUTPUT_CONTROL_PROMPT

    def test_user_experience_matches_constant(self) -> None:
        assert PromptLoader().get_user_experience() == artifacts.USER_EXPERIENCE_PROMPT

    def test_mode_prompts_match_constant(self) -> None:
        assert PromptLoader().get_mode_prompts() == artifacts.MODE_PROMPTS

    def test_fallback_matches_constant(self) -> None:
        assert PromptLoader().get_fallback_template() == artifacts.FALLBACK_PROMPT_TEMPLATE

    def test_secretary_identity_present(self) -> None:
        """功能：秘书身份仍要有独立模板（普通会话走它）。"""
        assert PromptLoader().get_secretary_identity().strip()


class TestIdentityCapabilityContractStillHolds:
    @pytest.mark.parametrize(
        ("getter", "needle"),
        [
            ("get_identity", "Bandi 助手"),
            ("get_framework", "暂不支持"),
            ("get_framework", "字符数"),
        ],
    )
    def test_constraints_present(self, getter: str, needle: str) -> None:
        """功能：//的提示约束在单一真相源下仍然生效。"""
        assert needle in getattr(PromptLoader(), getter)()


def test_framework_prompt_documents_bash_redirect_and_write_append() -> None:
    """契约：工具事实必须写清——bash 支持重定向但写入目标必须在工作区；
    write 支持 append；不得让员工把路径越界误说成「bash 不支持重定向」。"""
    prompt = artifacts.FRAMEWORK_PROMPT
    assert "bash 支持重定向" in prompt
    assert 'mode="append"' in prompt
    assert "工作区" in prompt
    assert "不要解释为「bash 不支持重定向」" in prompt
    assert "不要用 Markdown 引用块" in prompt
