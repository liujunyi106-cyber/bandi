"""功能：失败必须给用户可读文案、能力清单必须与真实工具面一致。

真机缺陷（两条都出自 L1 评测）：

① `ProviderChainExhaustedError` 时，`final_message` 直接取 `str(exc)`，
   用户看到的是「LLM call failed (candidates disabled): last_llm_error=...」。
   它还非空、不超时，于是评测把它当成一次成功回复——既误导用户，也让评测失真。

② 系统提示一边要求「清单里没有的能力一律回答暂不支持」，一边又把
   「演示文稿/图片生成/OCR/浏览器自动化」写成固定分档条目。模型只好先报高、
   下一轮再自我纠正，用户连续收到两条互相矛盾的答案。
"""

from __future__ import annotations

from bandi.core.exceptions import LLMUnavailableError
from bandi.agent.loop import _user_facing_failure


def test_llm_unavailable_gives_readable_text() -> None:
    """功能：模型不可用时的用户文案不得含内部异常串。"""
    exc = LLMUnavailableError(
        "LLM call failed (candidates disabled): "
        "last_llm_error=ProviderChainExhaustedError: All capability candidates failed"
    )
    text = _user_facing_failure(exc)
    for leak in ("LLM call failed", "ProviderChainExhaustedError", "candidates disabled"):
        assert leak not in text, f"内部串泄漏给用户: {leak}"
    assert text.strip(), "必须给用户一句可读说明，不能空"


def test_generic_error_keeps_reason() -> None:
    """功能：普通异常保留原因（用户能据此行动），但仍走同一个人口。"""
    text = _user_facing_failure(ValueError("目录不存在"))
    assert "目录不存在" in text


def test_empty_exception_falls_back_to_type_name() -> None:
    """功能：异常无文本时用类名兜底，不得返回空串。"""
    text = _user_facing_failure(RuntimeError())
    assert text.strip()


def test_capability_prompt_has_no_unbacked_entries() -> None:
    """功能：能力分档不得列出工具面不存在的条目（否则模型只能先报高再纠正）。"""
    from bandi.agent.prompt_harness import artifacts

    # 只查「能力分档条目」本身，不查全文：
    # 说明文字里引用这些词是为了记录为什么要删，不是能力承诺。
    import re as _re

    text = open(artifacts.__file__, encoding="utf-8").read()  # noqa: SIM115
    # 取「能力分档」那段：从「被问你能做什么」到空行结尾
    # 只取编号条目（1. … 5. ）本身，不含后面的说明文字
    entries = _re.findall(r"^\s*\d+\.\s*([^\n]+)$", text, _re.M)
    assert entries, "找不到能力分档条目"
    block_text = "\n".join(entries)
    banned = ("演示文稿", "图片生成", "OCR", "浏览器自动化")
    for entry in banned:
        assert entry not in block_text, f"能力分档仍承诺不存在的能力: {entry}"


def test_capability_prompt_still_keeps_team_ability() -> None:
    """功能：核心能力「组建 AI 员工团队」不能因为清理而丢失。"""
    from bandi.agent.prompt_harness import artifacts

    text = open(artifacts.__file__, encoding="utf-8").read()  # noqa: SIM115
    assert "组建 AI 员工团队" in text
