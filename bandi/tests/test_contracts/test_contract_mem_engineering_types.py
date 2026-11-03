# 功能：契约——记忆类型判定 / 规则抽取字段 / metadata 保留键兼容
# 契约原则：钉行为产物（类型/实体/时间/进度/落库读取），不钉实现（不 grep 源码）。
from __future__ import annotations

import json

from bandi.memory.extraction import extract_memory_fields
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.memory_types import MemoryType


class TestMemoryTypeClassification:
    def test_preference_text_maps_to_preference(self) -> None:
        result = extract_memory_fields("我以后都喜欢用中文回复，默认简洁一点。")
        assert result.memory_type is MemoryType.PREFERENCE

    def test_conclusion_text_maps_to_process_conclusion(self) -> None:
        result = extract_memory_fields("结论：方案 A 更稳，因此决定采用。")
        assert result.memory_type is MemoryType.PROCESS_CONCLUSION

    def test_state_text_maps_to_task_state_with_progress(self) -> None:
        result = extract_memory_fields("部署进行中，下一步先跑回归测试。")
        assert result.memory_type is MemoryType.TASK_STATE
        assert result.progress
        assert len(result.progress) <= 80
        assert "进行中" in result.progress

    def test_procedural_text_maps_to_procedural(self) -> None:
        result = extract_memory_fields("发布流程：先跑测试，再打包。")
        assert result.memory_type is MemoryType.PROCEDURAL

    def test_plain_text_defaults_to_business_fact(self) -> None:
        result = extract_memory_fields("会议在第三会议室召开。")
        assert result.memory_type is MemoryType.BUSINESS_FACT


class TestEntityAndTemporalExtraction:
    def test_entities_cover_quoted_path_and_camel_forms(self) -> None:
        result = extract_memory_fields(
            '请更新 "预算模块" 里的 config.yaml，并检查 longTermMemory 类。'
        )
        assert "预算模块" in result.entities
        assert "config.yaml" in result.entities
        assert "longTermMemory" in result.entities

    def test_entity_dedup_and_cap_at_eight(self) -> None:
        quoted = "、".join(f'"{i}"' for i in range(12))
        result = extract_memory_fields(f"包含 {quoted} 这些名字。")
        assert len(result.entities) <= 8
        assert len(result.entities) == len(set(result.entities))

    def test_temporal_patterns(self) -> None:
        assert extract_memory_fields("上线。").temporal == ""
        assert extract_memory_fields("9月20日见。").temporal == "9月20日"
        assert extract_memory_fields("昨天已完成部署。").temporal == "昨天"

    def test_empty_text_is_business_fact_with_empty_fields(self) -> None:
        for raw in ("", "   ", "\n\t "):
            result = extract_memory_fields(raw)
            assert result.memory_type is MemoryType.BUSINESS_FACT
            assert result.entities == []
            assert result.temporal is None
            assert result.progress is None

    def test_extraction_is_deterministic_and_source_echoed(self) -> None:
        text = "结论：用 bandi.db 替代。"
        first = extract_memory_fields(text, user_input="x", source="agent_turn")
        second = extract_memory_fields(text, user_input="x", source="agent_turn")
        assert first == second
        assert first.source == "agent_turn"


class TestMetadataReservedKeysAndLegacy:
    def test_extract_metadata_roundtrip_via_long_term(self, tmp_path) -> None:
        mem = LongTermMemory(str(tmp_path / "lt.db"))
        field = extract_memory_fields("偏好：我以后都用 ruff 格式化。", source="agent_turn")
        item = MemoryItem(
            item_id="turn:s:1",
            content="偏好：我以后都用 ruff 格式化。",
            metadata={"user_input": "格式化", **field.to_metadata()},
        )
        mem.store(item)
        loaded = mem.get("turn:s:1")
        assert loaded is not None
        assert loaded.metadata["memory_type"] == field.memory_type.value
        assert loaded.metadata["entities"] == field.entities
        assert loaded.metadata["source"] == "agent_turn"
        assert loaded.metadata["progress"] == field.progress
        mem.close()

    def test_legacy_metadata_without_reserved_keys_is_compatible(self) -> None:
        legacy = MemoryItem.from_dict(
            {
                "id": "old",
                "content": "旧数据",
                "metadata": json.dumps({"user_input": "x"}),
                "timestamp": 1.0,
                "importance": 0.5,
            }
        )
        assert "memory_type" not in legacy.metadata
        assert legacy.metadata["user_input"] == "x"

    def test_broken_quotes_and_huge_text_do_not_crash(self) -> None:
        result = extract_memory_fields('未闭合 "引号 config.yaml')
        assert result.memory_type is MemoryType.BUSINESS_FACT
        huge = "结论：" + "预算模块 " * 20000
        assert extract_memory_fields(huge).memory_type is MemoryType.PROCESS_CONCLUSION
