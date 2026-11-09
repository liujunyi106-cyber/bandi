"""契约：输出契约校验接受中英文同义必填键，且只把行首围栏当作未闭合判定依据。

必填键——report 的 summary、review 的 findings/verdict 在中文正文里写作
摘要/总结/结论、发现/问题/评审发现、裁决/结论/判定 时必须判合法；
空正文与无任何小节名的寒暄仍判缺失。

代码围栏——只有行首（允许缩进）出现的围栏标记参与配对，行内提及的反引号不参与；
开标记之后须遇到同长度或更长的闭标记才算闭合，仍未闭合才判格式错。
"""

from __future__ import annotations

import pytest

from bandi.company.output_contract import (
    KEY_SYNONYMS,
    OutputContract,
    _infer_contract,
    validate_output,
)
from bandi.company.work_item import WorkItemTurnType, create_work_item

CHINESE_REVIEW = """## 评审发现
- 鉴权中间件缺少来源校验
- 日志会带出完整 token

## 裁决
驳回，先补来源校验再合并。

## 风险
上线前必须回归登录链路。
"""

ENGLISH_REVIEW = """## Findings
- Missing origin check in auth middleware
- Log line leaks the full token

## Verdict
Request changes.
"""


def _contract(turn_type: WorkItemTurnType) -> OutputContract:
    return _infer_contract(create_work_item(title="t", turn_type=turn_type))


def _markdown_contract() -> OutputContract:
    return OutputContract(format_spec="markdown")


class TestReviewKeysAcceptChinese:
    def test_review_contract_requires_findings_and_verdict(self) -> None:
        contract = _contract(WorkItemTurnType.REVIEW)
        assert contract.required_keys == ("findings", "verdict")

    def test_chinese_review_body_is_valid(self) -> None:
        report = validate_output(CHINESE_REVIEW, _contract(WorkItemTurnType.REVIEW))
        assert report.valid, report.missing_keys
        assert report.missing_keys == []
        assert report.format_errors == []

    def test_english_review_body_is_valid(self) -> None:
        report = validate_output(ENGLISH_REVIEW, _contract(WorkItemTurnType.REVIEW))
        assert report.valid, report.missing_keys

    def test_structured_json_review_is_valid(self) -> None:
        report = validate_output(
            '{"review_verdict":"approve","summary":"ok"}',
            _contract(WorkItemTurnType.REVIEW),
        )
        assert report.valid, report.missing_keys

    def test_chinese_report_summary_is_valid(self) -> None:
        contract = _contract(WorkItemTurnType.REPORT)
        assert contract.required_keys == ("summary",)
        report = validate_output("## 结论\n本次交付已完成，产物在输出目录。", contract)
        assert report.valid, report.missing_keys


class TestEmptyOutputStillViolates:
    @pytest.mark.parametrize(
        "output",
        [
            "",
            "   \n\n  ",
            "你好，我是后端工程师，稍后开始处理。",
        ],
    )
    def test_review_without_any_key_is_invalid(self, output: str) -> None:
        report = validate_output(output, _contract(WorkItemTurnType.REVIEW))
        assert not report.valid
        assert sorted(report.missing_keys) == ["findings", "verdict"]

    def test_report_without_summary_is_invalid(self) -> None:
        report = validate_output("任务已处理完毕。", _contract(WorkItemTurnType.REPORT))
        assert not report.valid
        assert report.missing_keys == ["summary"]

    def test_unrelated_heading_does_not_satisfy_key(self) -> None:
        report = validate_output("## 结论\n正文", OutputContract(required_keys=("risks",)))
        assert not report.valid
        assert report.missing_keys == ["risks"]


class TestKeySynonyms:
    @pytest.mark.parametrize(
        ("key", "alias"),
        [(key, alias) for key, aliases in KEY_SYNONYMS.items() for alias in aliases],
    )
    def test_alias_satisfies_required_key(self, key: str, alias: str) -> None:
        report = validate_output(f"## {alias}\n正文", OutputContract(required_keys=(key,)))
        assert report.valid, report.missing_keys

    def test_full_width_and_case_are_normalized(self) -> None:
        contract = OutputContract(required_keys=("summary", "next_steps"))
        report = validate_output("## ＮＥＸＴ ＳＴＥＰＳ\n1. 回归\n## Summary\n完成", contract)
        assert report.valid, report.missing_keys

    def test_separator_variant_accepted(self) -> None:
        report = validate_output(
            "## Next Steps\n1. 回归", OutputContract(required_keys=("next_steps",))
        )
        assert report.valid, report.missing_keys


class TestMarkdownFenceDetection:
    def test_inline_fence_mention_is_not_unclosed(self) -> None:
        text = "## 结论\n正文里用 ``` 包裹示例代码，注意行首缩进；行内提及不算围栏。\n"
        report = validate_output(text, _markdown_contract())
        assert report.valid, report.format_errors

    def test_double_inline_mention_is_not_unclosed(self) -> None:
        text = "## 结论\n行内写 ``` 表示围栏，正文里再提一次 ``` 也不产生未闭合。\n"
        report = validate_output(text, _markdown_contract())
        assert report.valid, report.format_errors

    def test_paired_fence_is_valid(self) -> None:
        text = "## 结论\n\n示例：\n\n```python\nprint(1)\n```\n\n以上。\n"
        report = validate_output(text, _markdown_contract())
        assert report.valid, report.format_errors

    def test_indented_paired_fence_is_valid(self) -> None:
        text = "## 结论\n\n  ```bash\n  npm run build\n  ```\n"
        report = validate_output(text, _markdown_contract())
        assert report.valid, report.format_errors

    def test_single_line_fence_block_is_closed(self) -> None:
        text = "## 结论\n```bash npm run build```\n"
        report = validate_output(text, _markdown_contract())
        assert report.valid, report.format_errors

    def test_line_start_unclosed_fence_is_invalid(self) -> None:
        text = "## 结论\n这里开始写代码：\n\n```python\nprint(1)\n"
        report = validate_output(text, _markdown_contract())
        assert not report.valid
        assert any("unclosed" in error for error in report.format_errors)

    def test_shorter_closing_fence_keeps_block_open(self) -> None:
        text = "## 结论\n\n````python\nprint(1)\n```\n"
        report = validate_output(text, _markdown_contract())
        assert not report.valid
        assert any("unclosed" in error for error in report.format_errors)

    def test_code_format_uses_same_fence_rule(self) -> None:
        inline = validate_output("用 ``` 包裹即可", OutputContract(format_spec="code"))
        assert any("no code fences" in warning for warning in inline.warnings)
        assert not any("unclosed" in warning for warning in inline.warnings)
        unclosed = validate_output("```python\nprint(1)", OutputContract(format_spec="code"))
        assert any("unclosed" in warning for warning in unclosed.warnings)
