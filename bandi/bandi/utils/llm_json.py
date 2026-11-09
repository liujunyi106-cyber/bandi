# 功能：LLM 回复 JSON 提取公共模块（y3 抽取 judge/hallucination_checker/recruiter 三处重复解析链）

import json
import re
from typing import Any

_JSON_MARKED_RE = re.compile(r"```(?:json|JSON)\s*\n(.*?)```", re.DOTALL)
_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)


def _try_blocks(pattern: re.Pattern[str], text: str) -> dict[str, Any] | None:
    for block in pattern.findall(text):
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def extract_llm_json(text: str) -> dict[str, Any] | None:
    """从 LLM 回复中尽力提取 JSON object。

    优先级：raw 全文 → ```json 标记块（#17：有标记的块是权威 JSON 输出
    优先于无标记块）→ 任意 ``` 块（含单行 fence）→ 首 {..} 段。
    全部失败返回 None，由调用方决定兜底语义。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    found = _try_blocks(_JSON_MARKED_RE, text)
    if found is not None:
        return found
    # 任意 ``` 块轮：split 语义等价实现（兼容单行 fence 与未闭合 fence）
    for block in text.split("```"):
        block = block.strip()
        if block.lower().startswith("json"):
            block = block[4:].strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    brace = _BRACE_RE.search(text)
    if brace:
        try:
            parsed = json.loads(brace.group())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def parse_llm_json_strict(raw: str) -> dict[str, Any]:
    """严格模式：剥 code fence 后整体必须是合法 JSON object，否则抛 JSONDecodeError
    （供重试循环消费解析失败信号）。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("response must be a JSON object", text, 0)
    return data


__all__ = ["extract_llm_json", "parse_llm_json_strict"]
