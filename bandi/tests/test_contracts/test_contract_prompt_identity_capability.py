# 功能：契约（BC-007 / BC-003 / BC-008）系统提示的身份与能力诚实约束
#
# 行为不变式（钉模型可见的行为规格，不钉实现细节）：
#   1. BC-007 身份层给出自称口径，并禁止自曝底层模型名与厂商
#   2. BC-003 框架层要求能力清单以工具面为准，清单外一律答「暂不支持」
#   3. BC-008 框架层定义「N 个字」= N 个字符（含中文标点）
#
# 三条都是评测 L1-G01-01 / L1-G03-01 直接判出的失败，提示层是唯一修复面。

from __future__ import annotations

import pytest

from bandi.agent.prompt_harness.prompt_loader import PromptLoader


@pytest.fixture(scope="module")
def identity() -> str:
    return PromptLoader().get_identity()


@pytest.fixture(scope="module")
def framework() -> str:
    return PromptLoader().get_framework()


@pytest.fixture(scope="module")
def secretary_identity() -> str:
    return PromptLoader().get_secretary_identity()


class TestIdentitySelfNaming:
    def test_identity_declares_self_name(self, identity: str) -> None:
        """功能：BC-007——身份层必须给出自称口径，否则模型改用底层模型身份回答。"""
        assert "Bandi 助手" in identity

    def test_identity_forbids_underlying_model_name(self, identity: str) -> None:
        """功能：BC-007——禁止在回复里提及底层模型名/厂商（破坏者视角：模型知道自己是谁）。"""
        text = identity.replace(" ", "")
        assert "不要提及底层模型" in text or "不得提及底层模型" in text
        assert "厂商" in identity

    def test_secretary_identity_also_constrained(self, secretary_identity: str) -> None:
        """功能：秘书身份（is_secretary=True）走另一份identity，同样不得漏约束。"""
        assert "Bandi 助手" in secretary_identity


class TestCapabilityHonesty:
    def test_framework_requires_tool_grounded_capability_list(self, framework: str) -> None:
        """功能：BC-003——能力清单必须以工具面为唯一依据。"""
        assert "能力清单以本次列出的工具为唯一依据" in framework
        assert "暂不支持" in framework

    def test_framework_forbids_fabricating_capabilities(self, framework: str) -> None:
        """功能：BC-003——禁止凭想象列举能力（门禁项「未编造不存在的能力」）。"""
        assert "不要凭想象" in framework or "不得凭想象" in framework


class TestNumericConstraintSemantics:
    def test_framework_defines_zi_as_characters(self, framework: str) -> None:
        """功能：BC-008——「N 个字」口径必须定义为字符数（含标点），否则门禁判据无法判定。"""
        assert "字符数" in framework
        assert "标点" in framework


class TestLanguageSlotPreserved:
    def test_identity_keeps_language_placeholder(self, identity: str) -> None:
        """功能：回归护栏——身份层须保留 {language} 占位（否则语言注入失效）。"""
        assert "{language}" in identity

    def test_identity_formats_language(self) -> None:
        """功能：{language} 可被格式化，不抛异常。"""
        out = PromptLoader().get_identity().format(language="简体中文")

        assert "简体中文" in out
        assert "{language}" not in out
