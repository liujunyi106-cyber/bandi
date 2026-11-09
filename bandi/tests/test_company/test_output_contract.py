from __future__ import annotations

from datetime import UTC, datetime

from bandi.company.output_contract import (
    OUTPUT_CONTRACT_KEYS,
    PATH_BLOCKER_TOKENS,
    OutputContract,
    is_path_relative_to,
    normalize_path_text,
    output_alias_map,
    output_contract_metadata,
    render_output_contract_context,
    replace_output_aliases,
    text_has_path_blocker,
    validate_output,
    validate_work_item_output,
)
from bandi.company.phase import Phase
from bandi.company.work_item import WorkItem, WorkItemTurnType


class TestOutputContract:
    def test_empty_contract_passes_any_output(self):
        contract = OutputContract()
        report = validate_output("anything", contract)
        assert report.valid

    def test_required_key_check(self):
        contract = OutputContract(required_keys=("summary", "steps"))
        report = validate_output("here is my summary", contract)
        assert not report.valid
        assert "steps" in report.missing_keys
        assert "summary" not in report.missing_keys

    def test_required_key_chinese_suffix(self):
        """中文 key 后接“如下/：”等正文时不得因缺词边界被判缺失。"""
        contract = OutputContract(required_keys=("分工与执行链",))
        report = validate_output("## 分工与执行链如下\n- Lead → Worker", contract)
        assert report.valid, report.missing_keys

    def test_required_key_word_boundary(self):
        contract = OutputContract(required_keys=("sum",))
        report = validate_output("summary of work", contract)
        assert not report.valid
        assert report.missing_keys == ["sum"]
        report = validate_output("the sum is five", contract)
        assert report.valid

    def test_min_length_violation_is_warning_not_error(self):
        contract = OutputContract(min_length=50)
        report = validate_output("short", contract)
        assert report.valid
        assert len(report.warnings) == 1

    def test_max_length_violation_is_error(self):
        contract = OutputContract(max_length=10)
        report = validate_output("this is too long output", contract)
        assert not report.valid
        assert len(report.format_errors) == 1

    def test_markdown_format_check(self):
        contract = OutputContract(format_spec="markdown")
        report = validate_output("plain text without heading or fences", contract)
        assert report.valid
        assert any("no headings" in w for w in report.warnings)

    def test_markdown_unclosed_fence(self):
        contract = OutputContract(format_spec="markdown")
        report = validate_output("some text\n```\nunclosed", contract)
        assert not report.valid
        assert any("unclosed" in e for e in report.format_errors)

    def test_json_format_check(self):
        contract = OutputContract(format_spec="json")
        report = validate_output('{"key": "value"}', contract)
        assert report.valid

    def test_json_format_invalid(self):
        contract = OutputContract(format_spec="json")
        report = validate_output("not json", contract)
        assert not report.valid
        assert any("json" in e for e in report.format_errors)

    def test_code_format_no_fence_warning(self):
        contract = OutputContract(format_spec="code")
        report = validate_output("print('hello')", contract)
        assert any("no code fences" in w for w in report.warnings)

    def test_code_format_unclosed_fence_warning(self):
        contract = OutputContract(format_spec="code")
        report = validate_output("```python\nprint('hi')", contract)
        assert any("unclosed code fence" in w for w in report.warnings)
        assert report.valid

    def test_validate_work_item_output_infers_contract(self):
        item = WorkItem(
            id="wi1",
            title="intake",
            description="",
            phase=Phase.READY,
            turn_type=WorkItemTurnType.INTAKE,
            assignee_role=None,
            parent_id=None,
            children_ids=[],
            links={},
            metadata={"allowed_delegate_role_ids": ["visual"]},
            runtime_metadata={},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        item.runtime_metadata["_last_output"] = "my plan"
        report = validate_work_item_output(item)
        assert not report.valid
        assert "分工与执行链" in report.missing_keys


class TestOutputPathContract:
    def test_contract_keys_constant(self):
        assert OUTPUT_CONTRACT_KEYS == (
            "workspace_root",
            "output_root",
            "target_output_dir",
            "requested_output_root",
            "output_root_aliases",
            "output_root_alias_map",
        )

    def test_path_blocker_tokens_constant(self):
        assert "operation not permitted" in PATH_BLOCKER_TOKENS
        assert "sandbox" in PATH_BLOCKER_TOKENS
        assert "not writable" in PATH_BLOCKER_TOKENS

    def test_normalize_path_text_empty(self):
        assert normalize_path_text(None) == ""
        assert normalize_path_text("  ") == ""

    def test_normalize_path_text_resolves(self):
        from pathlib import Path

        assert normalize_path_text("a/b") == str(Path("a/b").expanduser().resolve(strict=False))

    def test_normalize_path_text_garbage_returns_raw(self):
        raw = "\x00invalid\x00"
        assert normalize_path_text(raw) == raw or normalize_path_text(raw) == raw.strip()

    def test_is_path_relative_to_inside(self):
        from pathlib import Path

        root = Path("tmp_root").resolve()
        assert is_path_relative_to(root / "sub" / "f.txt", root)

    def test_is_path_relative_to_outside(self):
        assert not is_path_relative_to("D:/elsewhere", "C:/root")

    def test_output_contract_metadata_empty(self):
        assert output_contract_metadata(None) == {}

    def test_output_contract_metadata_output_root_only(self):
        contract = output_contract_metadata({"output_root": "D:/out"})
        assert contract["output_root"] == normalize_path_text("D:/out")
        assert contract["target_output_dir"] == contract["output_root"]

    def test_output_contract_metadata_workspace_root(self):
        contract = output_contract_metadata({"workspace_root": "D:/ws"})
        assert contract["workspace_root"] == normalize_path_text("D:/ws")

    def test_output_contract_metadata_comms_workspace_fallback(self):
        contract = output_contract_metadata({"comms_workspace_root": "D:/comms"})
        assert contract["workspace_root"] == normalize_path_text("D:/comms")

    def test_output_contract_metadata_requested_alias(self):
        contract = output_contract_metadata(
            {"output_root": "D:/out", "requested_output_root": "D:/alias"}
        )
        assert contract["requested_output_root"] == normalize_path_text("D:/alias")
        assert normalize_path_text("D:/alias") in contract["output_root_aliases"]

    def test_output_contract_metadata_aliases_dedup(self):
        contract = output_contract_metadata(
            {"output_root": "D:/out", "output_root_aliases": ["D:/a", "D:/a", "D:/b"]}
        )
        assert contract["output_root_aliases"].count(normalize_path_text("D:/a")) == 1

    def test_output_contract_metadata_alias_map(self):
        contract = output_contract_metadata(
            {"output_root": "D:/out", "output_root_alias_map": {"D:/old": "D:/out"}}
        )
        assert contract["output_root_alias_map"][
            normalize_path_text("D:/old")
        ] == normalize_path_text("D:/out")

    def test_output_contract_metadata_full(self):
        contract = output_contract_metadata(
            {
                "workspace_root": "D:/ws",
                "output_root": "D:/out",
                "requested_output_root": "D:/req",
                "output_root_aliases": ["D:/a"],
                "output_root_alias_map": {"D:/old": "D:/out"},
            }
        )
        assert contract["workspace_root"] == normalize_path_text("D:/ws")
        assert contract["output_root"] == normalize_path_text("D:/out")
        assert "requested_output_root" in contract
        assert "output_root_aliases" in contract
        assert "output_root_alias_map" in contract


class TestOutputAliasHelpers:
    def test_output_alias_map_empty(self):
        assert output_alias_map(None) == {}

    def test_output_alias_map_requested(self):
        mapping = output_alias_map({"output_root": "D:/out", "requested_output_root": "D:/req"})
        assert mapping["D:/req"] == normalize_path_text("D:/out")

    def test_output_alias_map_aliases(self):
        mapping = output_alias_map(
            {"output_root": "D:/out", "output_root_aliases": ["D:/a", "D:/b"]}
        )
        assert mapping["D:/a"] == normalize_path_text("D:/out")
        assert mapping["D:/b"] == normalize_path_text("D:/out")

    def test_output_alias_map_agrees_with_contract_metadata(self):
        source = {
            "output_root": "D:/out",
            "requested_output_root": "D:/req",
            "output_root_aliases": ["D:/a"],
            "output_root_alias_map": {"D:/old": "D:/out"},
        }
        mapping = output_alias_map(source)
        contract = output_contract_metadata(source)
        for alias, target in contract["output_root_alias_map"].items():
            if alias in mapping:
                assert mapping[alias] == target

    def test_output_alias_map_raw_map_empty_target_falls_back_to_root(self):
        mapping = output_alias_map(
            {"output_root": "D:/out", "output_root_alias_map": {"D:/old": ""}}
        )
        assert mapping["D:/old"] == normalize_path_text("D:/out")

    def test_replace_output_aliases_string_longest_first(self):
        source = {
            "output_root": "D:/out",
            "output_root_alias_map": {"a": "D:/out", "ab": "D:/other"},
        }
        assert (
            replace_output_aliases("use ab now", source)
            == f"use {normalize_path_text('D:/other')} now"
        )

    def test_replace_output_aliases_list_tuple_dict(self):
        source = {"output_root": "D:/out", "output_root_alias_map": {"a": "D:/out"}}
        target = normalize_path_text("D:/out")
        assert replace_output_aliases(["a", "x"], source) == [target, "x"]
        assert replace_output_aliases(("a",), source) == (target,)
        assert replace_output_aliases({"k": "a"}, source) == {"k": target}

    def test_replace_output_aliases_non_string(self):
        assert (
            replace_output_aliases(
                42, {"output_root": "D:/out", "output_root_alias_map": {"a": "D:/out"}}
            )
            == 42
        )

    def test_replace_output_aliases_skips_alias_inside_own_target(self):
        source = {"output_root": "D:/out", "output_root_aliases": ["out"]}
        assert replace_output_aliases("write to D:/out now", source) == "write to D:/out now"

    def test_replace_output_aliases_resolves_nested_chain(self):
        source = {
            "output_root": "D:/out",
            "output_root_alias_map": {"x": "ab", "ab": "D:/out"},
        }
        assert (
            replace_output_aliases("use x now", source)
            == f"use {normalize_path_text('D:/out')} now"
        )

    def test_text_has_path_blocker_hit(self):
        assert text_has_path_blocker("permission denied writing file")

    def test_text_has_path_blocker_miss_and_empty(self):
        assert not text_has_path_blocker("all good here")
        assert not text_has_path_blocker(None)


class TestRenderOutputContractContext:
    def test_render_empty_source(self):
        assert render_output_contract_context(None) == ""

    def test_render_heading_and_canonical(self):
        rendered = render_output_contract_context({"output_root": "D:/out"})
        assert "## Runtime Output Contract" in rendered
        assert f"Canonical writable output root: {normalize_path_text('D:/out')}" in rendered

    def test_render_workspace_root_hidden(self):
        rendered = render_output_contract_context(
            {"workspace_root": "D:/ws", "output_root": "D:/out"}, include_workspace_root=False
        )
        assert "Workspace root" not in rendered

    def test_render_alias_map_capped(self):
        aliases = {f"D:/a{i}": "D:/out" for i in range(10)}
        rendered = render_output_contract_context(
            {"output_root": "D:/out", "output_root_alias_map": aliases}
        )
        assert rendered.count("->") == 6
        assert "write and verify against the canonical output root" in rendered


class TestPlanDiscussionValidation:
    def _plan_item(self, *, source: str, output: str, metadata: dict | None = None) -> WorkItem:
        meta = {"source": source} if source else {}
        meta.update(metadata or {})
        return WorkItem(
            id="wi-plan",
            title="plan",
            description="",
            phase=Phase.READY,
            turn_type=WorkItemTurnType.PLAN,
            assignee_role=None,
            parent_id=None,
            children_ids=[],
            links={},
            metadata=meta,
            runtime_metadata={"_last_output": output},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def test_plan_discussion_reply_valid_without_dispatch_keys(self) -> None:
        """plan 讨论轮：缺分工小节不判失败（讨论输出即合法产出）。"""
        report = validate_work_item_output(
            self._plan_item(source="chat_dm", output="你好，老板！我是后端工程师。")
        )
        assert report.valid
        assert report.missing_keys == []

    def test_plan_long_reply_still_valid(self) -> None:
        report = validate_work_item_output(self._plan_item(source="chat_dm", output="很长" * 300))
        assert report.valid

    def test_plan_format_error_still_strict(self) -> None:
        """破坏者：讨论轮也不豁免真格式错（未闭合代码围栏仍判失败）。"""
        report = validate_work_item_output(
            self._plan_item(source="chat_dm", output="说明\n```\nunclosed")
        )
        assert not report.valid
        assert report.format_errors

    def test_slot_answered_plan_requires_dispatch_keys(self) -> None:
        """槽位口径确认后的 plan 续跑轮：要求分工小节（与合约同源信号）。"""
        report = validate_work_item_output(
            self._plan_item(source="", output="my plan", metadata={"slot_answered": True})
        )
        assert not report.valid
        assert "分工与执行链" in report.missing_keys
