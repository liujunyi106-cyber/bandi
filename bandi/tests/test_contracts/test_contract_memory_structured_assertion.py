"""契约：长期记忆的主形态是结构化断言，原句只作证据留底。

原实现把 `content` 直接写用户/助手原句，导致：
- 检索命中返回的是自然语言段落，合并时按字面相似度比对（「我特别喜欢 A」
  与「我喜欢 A」判为不同 → 同一偏好存两条）
- 冲突检测同样按字面比对，改一个字的偏好变动会被当成全新事实
- 下游无法按主体/谓词/客体做结构化查询

本契约钉：能切出谓词 → content 是断言且 evidence 存原句；切不出 → 老实回退原句。
"""

from __future__ import annotations

from pathlib import Path

from bandi.memory.extraction import extract_memory_fields


class TestStructuredAssertion:
    def test_preference_becomes_assertion(self) -> None:
        raw = "我特别喜欢数字 88 和靛蓝色"
        f = extract_memory_fields(raw)
        assert f.predicate == "偏好"
        assert f.subject == "用户"
        fact = f.render_fact(raw)
        assert fact.startswith("[preference]")
        assert "靛蓝色" in fact
        assert raw not in fact, "content 不应再是原句"

    def test_entity_is_subject_when_present(self) -> None:
        raw = "项目 Alpha 属于产品线 Beta"
        f = extract_memory_fields(raw)
        assert f.subject == "项目 Alpha"
        assert f.predicate == "归属"
        assert "产品线" in (f.object_value or "")

    def test_temporal_renders_into_assertion(self) -> None:
        raw = "今天完成了订单服务的重构，结论是拆成两个服务"
        f = extract_memory_fields(raw)
        assert f.temporal == "今天"
        assert "（今天）" in f.render_fact(raw)

    def test_progress_appended_when_task_state(self) -> None:
        raw = "登录接口联调进行中"
        f = extract_memory_fields(raw)
        assert f.memory_type == "task_state"
        assert f.progress

    def test_metadata_carries_triple_and_evidence_key(self) -> None:
        f = extract_memory_fields("我喜欢深色主题")
        meta = f.to_metadata()
        for key in (
            "subject",
            "predicate",
            "object",
            "memory_type",
            "entities",
            "temporal",
            "source",
        ):
            assert key in meta, f"metadata 缺 {key}"

    def test_metadata_is_json_serialisable(self) -> None:
        import json

        f = extract_memory_fields("我喜欢深色主题")
        json.dumps(f.to_metadata(), ensure_ascii=False)

    def test_no_predicate_falls_back_to_raw(self) -> None:
        """破坏者视角：切不出谓词时必须回退原句，不产出错误断言。"""
        raw = "老板，该文件不存在。fixtures 目录下没有 missing_file_xyz.txt"
        f = extract_memory_fields(raw)
        assert f.predicate is None
        assert f.render_fact(raw) == raw

    def test_evidence_kept_alongside_assertion(self) -> None:
        """断言用于检索/合并，原句用于追溯与纠错——两者都要在。"""
        raw = "我特别喜欢数字 88 和靛蓝色"
        f = extract_memory_fields(raw)
        fact = f.render_fact(raw)
        assert fact != raw
        assert raw, "原句须由调用方存入 metadata.evidence"

    def test_overlong_object_rejected(self) -> None:
        """破坏者视角：客体过长说明切错了，宁可不要客体也不要错误断言。"""
        raw = "禁止" + "很长的内容" * 30
        f = extract_memory_fields(raw)
        assert f.object_value is None

    def test_deterministic(self) -> None:
        """同一输入两次抽取必须完全一致——去重/合并依赖确定性。"""
        raw = "我特别喜欢数字 88 和靛蓝色"
        a, b = extract_memory_fields(raw), extract_memory_fields(raw)
        assert a.to_metadata() == b.to_metadata()
        assert a.render_fact(raw) == b.render_fact(raw)


class TestAgentRunnerUsesAssertion:
    def test_runner_writes_assertion_as_content(self) -> None:
        """写入路径：content 是断言、evidence 存原句。"""
        import ast

        src = (
            Path(__file__).resolve().parents[2] / "bandi" / "core" / "agent_runner.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "render_fact"
        ]
        assert calls, "agent_runner 必须用 render_fact 生成 content"
        assert '"evidence"' in src, "metadata 必须保留 evidence 原句"
