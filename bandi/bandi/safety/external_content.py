# 功能：外部不可信内容安全包装（EXTERNAL_UNTRUSTED_CONTENT 边界 + 特殊 token/零宽清洗，6.14）
"""外部内容（网页/搜索/MCP 远端返回）进上下文前统一包装：

- 随机 id 边界标记，模型可识别「不可信外部内容」区间（对齐 openclaw
  `security/external-content.ts` 的 EXTERNAL_UNTRUSTED_CONTENT 语义）；
- ChatML / 伪系统标签中和（防外部内容伪造系统消息）；
- 零宽与双向控制字符清除（同形/隐藏指令向量）。
中文等正文内容原样保留（不做 NFKC 全量归一，避免破坏中文标点）。
"""

from __future__ import annotations

import re
import secrets
from functools import lru_cache

EXTERNAL_CONTENT_PREFIX = "<<<EXTERNAL_UNTRUSTED_CONTENT"
EXTERNAL_CONTENT_SUFFIX = "<<<END_EXTERNAL_UNTRUSTED_CONTENT"

# ChatML / 特殊 token（<|im_start|> 等）
_CHATML_TOKEN_RE = re.compile(r"<\|[A-Za-z0-9_]+\|>")
# 伪系统标签（[INST]/[SYSTEM]/[/INST] 等）
_FAKE_TAG_RE = re.compile(r"\[/?(?:INST|SYSTEM|ASSISTANT|USER)\]", re.IGNORECASE)
# 伪造边界标记（<<<EXTERNAL_UNTRUSTED_CONTENT ...>>> / <<<END_...>>>）
# ——整段（含伪造 id）中和后可防外部内容伪造 footer 提前闭合可信区间
_MARKER_LITERAL_RE = re.compile(
    r"<<<(?:END_)?EXTERNAL_UNTRUSTED_CONTENT(?:[^>\n]*>>>)?", re.IGNORECASE
)
# 零宽 + 双向控制字符（隐藏指令/同形攻击向量）
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")


@lru_cache(maxsize=1)
def _injection_matchers() -> tuple[tuple[re.Pattern[str], str], ...]:
    # 功能：——复用 prompt_guard 注入模式集（模块级编译一次）
    # 外部内容命中时在边界声明 suspicious 属性（可观测 + 模型警觉，不阻断）
    from bandi.safety.prompt_guard import DEFAULT_INJECTION_PATTERNS

    return tuple(
        (re.compile(pattern), category) for pattern, category in DEFAULT_INJECTION_PATTERNS
    )


def suspicious_categories(text: str) -> list[str]:
    # 功能：外部内容命中的注入模式类别（去重保序；空=无疑似）
    if not text:
        return []
    categories: list[str] = []
    for regex, category in _injection_matchers():
        if regex.search(text) and category not in categories:
            categories.append(category)
    return categories


def sanitize_external_text(text: str) -> str:
    # 功能：外部内容清洗——伪造边界标记/特殊 token/伪系统标签中和、不可见控制字符清除
    text = _MARKER_LITERAL_RE.sub("[external-marker-removed]", text)
    text = _CHATML_TOKEN_RE.sub("[external-token-removed]", text)
    text = _FAKE_TAG_RE.sub("[external-tag-removed]", text)
    text = _INVISIBLE_RE.sub("", text)
    return text


def wrap_external_content(text: str, source: str = "") -> str:
    # 功能：以随机 id 边界标记包裹外部不可信内容（工具输出唯一出口消费）；
    # 命中注入模式（对原文检测，清洗前）时在 header 声明 suspicious 属性
    raw = text or ""
    categories = suspicious_categories(raw)
    cleaned = sanitize_external_text(raw)
    token = secrets.token_hex(8)
    suspicious_attr = f' suspicious="{"|".join(categories)}"' if categories else ""
    header = (
        f'{EXTERNAL_CONTENT_PREFIX} id={token} source="{source or "unknown"}"{suspicious_attr}>>>'
    )
    footer = f"{EXTERNAL_CONTENT_SUFFIX} id={token}>>>"
    return f"{header}\n{cleaned}\n{footer}"


__all__ = [
    "EXTERNAL_CONTENT_PREFIX",
    "EXTERNAL_CONTENT_SUFFIX",
    "sanitize_external_text",
    "suspicious_categories",
    "wrap_external_content",
]
