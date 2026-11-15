# 功能：LLM JSON 提取公共模块契约（y3 抽取 + y-fix 单行 fence 回归修复）

from bandi.utils.llm_json import extract_llm_json, parse_llm_json_strict


def test_raw_json() -> None:
    assert extract_llm_json('{"a": 1}') == {"a": 1}


def test_marked_block_beats_plain_block() -> None:
    """#17 语义保留：```json 标记块是权威输出，优先于先出现的无标记块。"""
    text = '```\n{"score": 0.0}\n```\n```json\n{"score": 0.9}\n```'
    assert extract_llm_json(text) == {"score": 0.9}


def test_single_line_fence_supported() -> None:
    """y-fix 回归：单行 fence（无换行）必须可解析——原 judge/hallucination
    split 链支持该形态，抽取公共版不得回退。"""
    assert extract_llm_json('前言 ```json{"a": 1}``` 后记') == {"a": 1}
    assert extract_llm_json('```\n{"b": 2}\n```') == {"b": 2}


def test_brace_fallback_and_none() -> None:
    assert extract_llm_json('结果是 {"c": 3} 请查收') == {"c": 3}
    assert extract_llm_json("完全没有 json") is None
    assert extract_llm_json("") is None


def test_non_dict_json_skipped() -> None:
    """数组等非 object 合法 JSON 不作为命中返回（调用方契约均为 dict）。"""
    text = '[1, 2, 3]\n{"d": 4}'
    assert extract_llm_json(text) == {"d": 4}


def test_strict_mode_rejects_partial() -> None:
    import json

    import pytest

    with pytest.raises(json.JSONDecodeError):
        parse_llm_json_strict('垃圾前缀 {"a": 1}')
    assert parse_llm_json_strict('```json\n{"a": 1}\n```') == {"a": 1}
