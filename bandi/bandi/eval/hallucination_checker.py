# 功能：幻觉检查（双层一致性核验 + 候选提取）
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bandi.utils.llm_json import extract_llm_json

if TYPE_CHECKING:
    from bandi.llm.base import BaseProvider

logger = logging.getLogger(__name__)

_CAPITALIZED_WORDS = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b")

_CJK_SEQ = re.compile(r"[\u4e00-\u9fff]+")

_CJK_STOPWORDS: frozenset[str] = frozenset(
    {
        "我们",
        "你们",
        "他们",
        "她们",
        "它们",
        "咱们",
        "自己",
        "别人",
        "大家",
        "这个",
        "那个",
        "这些",
        "那些",
        "什么",
        "怎么",
        "怎样",
        "如何",
        "可以",
        "能够",
        "应该",
        "需要",
        "可能",
        "已经",
        "正在",
        "曾经",
        "将要",
        "但是",
        "因为",
        "所以",
        "如果",
        "而且",
        "然后",
        "还是",
        "或者",
        "就是",
        "而是",
        "对于",
        "关于",
        "虽然",
        "即使",
        "既然",
        "只要",
        "并且",
        "同时",
        "以及",
        "因此",
        "于是",
        "例如",
        "比如",
        "其中",
        "这里",
        "那里",
        "这样",
        "那样",
        "一个",
        "一些",
        "一下",
        "一点",
        "一定",
        "一样",
        "这么",
        "那么",
        "非常",
        "比较",
        "特别",
        "主要",
        "一般",
        "很多",
        "许多",
        "为什么",
        "怎么样",
        "什么的",
        "现在",
        "目前",
        "刚才",
    }
)

_SKIP_WORDS: frozenset = frozenset(
    {
        "I",
        "You",
        "We",
        "This",
        "That",
        "These",
        "Those",
        "It",
        "He",
        "She",
        "They",
        "My",
        "Your",
        "His",
        "Her",
        "Our",
        "Their",
        "Its",
        "A",
        "An",
        "The",
        "And",
        "Or",
        "But",
        "So",
        "If",
        "Then",
        "Else",
        "When",
        "Where",
        "Why",
        "How",
        "What",
        "Who",
        "Which",
        "Yes",
        "No",
        "Not",
        "None",
        "All",
        "Some",
        "Many",
        "Few",
        "More",
        "Most",
        "Other",
        "Another",
        "Each",
        "Every",
        "Either",
        "Neither",
        "Both",
        "Hello",
        "Hi",
        "Hey",
        "Sorry",
        "Assistant",
        "User",
        "System",
        "API",
        "JSON",
        "URL",
        "SQL",
        "HTML",
        "XML",
        "MCP",
        "YAML",
        "ID",
        "Please",
        "First",
        "Second",
        "Third",
        "Next",
        "Last",
        "Finally",
        "Task",
        "Project",
        "Note",
        "Contact",
        "Knowledge",
    }
)

_L2_SYSTEM_PROMPT = (
    "你是一个严谨的事实审查员。只对确实存在的事实不一致给出较高分数；"
    "问候、自我介绍/身份说明、能力说明、执行意图等非事实性表述不计入幻觉。"
)

_L1_EN_DELTA_PER_MISSING = 0.1
_L1_CN_DELTA_WEIGHT = 0.5
_L1_MAX_CANDIDATES = 64


@dataclass
class L1Result:
    found_entities: list[str] = field(default_factory=list)
    missing_entities: list[str] = field(default_factory=list)
    confidence_delta: float = 0.0


@dataclass
class L2Result:
    hallucination_score: float = 0.0
    hallucination_spans: list[str] = field(default_factory=list)


@dataclass
class HallucinationReport:
    l1: L1Result | None = None
    l2: L2Result | None = None
    overall_score: float = 0.0
    blocked: bool = False


def extract_candidates(text: str, max_candidates: int = _L1_MAX_CANDIDATES) -> list[str]:
    candidates: set[str] = set()
    for match in _CAPITALIZED_WORDS.finditer(text):
        word = match.group().strip()
        if word not in _SKIP_WORDS and len(word) > 1:
            candidates.add(word)
    for seq in _CJK_SEQ.findall(text):
        _collect_chinese_candidates(candidates, seq)
    ordered = sorted(candidates, key=len, reverse=True)
    return ordered[:max_candidates]


def _collect_chinese_candidates(candidates: set[str], seq: str) -> None:
    """从连续中文字段收集实体候选（2~6 字滑窗，过滤纯停用词）。"""
    seq = seq.strip()
    if len(seq) < 2:
        return
    # 5/6 字实体名（如「深度学习与实践计划」）漏召回
    for size in range(2, min(len(seq), 6) + 1):
        for i in range(len(seq) - size + 1):
            gram = seq[i : i + size]
            if _is_stopword_phrase(gram):
                continue
            candidates.add(gram)


def _is_stopword_phrase(gram: str) -> bool:
    """gram 是否纯停用词组合——整串在停用词表，或可完全按 2 字停用词
    切分（「我们可以」= 我们+可以，无实体语义；「们可」等非词子串
    不参与判断）。"""
    if gram in _CJK_STOPWORDS:
        return True
    if len(gram) % 2 != 0:
        return False
    return all(gram[i : i + 2] in _CJK_STOPWORDS for i in range(0, len(gram), 2))


async def l1_check(text: str, graph: Any) -> L1Result:
    candidates = extract_candidates(text)
    if not candidates:
        return L1Result()
    found: set[str] = set()
    missing: set[str] = set()
    en_missing = 0
    cn_missing = 0
    cn_total = 0
    for candidate in candidates:
        results = graph.find_entities(query=candidate, limit=5)
        matched = any(
            candidate.lower() in e.name.lower() or e.name.lower() in candidate.lower()
            for e in results
        )
        if matched:
            found.add(candidate)
        else:
            missing.add(candidate)
            if candidate.isascii():
                en_missing += 1
            else:
                cn_missing += 1
                cn_total += 1
    delta = min(1.0, en_missing * _L1_EN_DELTA_PER_MISSING)
    if cn_total:
        delta = max(delta, min(1.0, cn_missing / cn_total * _L1_CN_DELTA_WEIGHT))
    return L1Result(
        found_entities=sorted(found),
        missing_entities=sorted(missing),
        confidence_delta=delta,
    )


_L2_JUDGE_PROMPT = """对话上下文：
{context}

模型输出：
{output}

检查输出中是否有与上下文或常识**明确矛盾/冲突**的事实性陈述。返回 JSON：
{{"hallucination_score": 0.0-1.0, "hallucination_spans": ["陈述1", "陈述2"]}}
0=无矛盾 1=完全虚构/与事实直接冲突

判分原则（重要）：本检查只抓「**明确冲突**」，不要求输出里的每个细节都能在上下文找到出处——
上下文未提及某细节 ≠ 幻觉（unsupported 不等于矛盾）。例：上下文说「做一张主图」，
输出「主图已出：main-v2.png」属于正常执行汇报，不应计分；但输出「主图已出，尺寸 4K」
而上下文/常识与之直接冲突时计高分。

以下内容**不计入**幻觉（score 不得因此升高）：
- 问候/寒暄/结尾语（如「你好」「还有什么可以帮你」）
- 自我介绍与身份说明（如「我是 Bandi 助手」「我是你的秘书」）——系统身份无法用对话上下文核对，不属于事实断言
- 能力说明与执行意图（如「我可以帮你处理文档」「我来替你检查」）
- 对用户的提问、确认请求、以及格式性文字（标题、列表引导句）
- 上下文未提及但也不冲突的执行细节、文件名、一般性说明
只对可被上下文或常识核验且**明确冲突**的具体事实断言（数字、日期、事件、外部实体属性等）评分。
"""


async def l2_check(
    text: str,
    context: str,
    provider: BaseProvider,
) -> L2Result:
    prompt = _L2_JUDGE_PROMPT.format(context=context[:4000], output=text[:2000])
    try:
        from bandi.llm.accounted import QUALITY_CHECK_BUDGET_SEC, accounted_complete

        resp = await accounted_complete(
            provider,
            messages=[
                {"role": "system", "content": _L2_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            label="hallucination_l2",
            session_id="hallucination",
            budget_sec=QUALITY_CHECK_BUDGET_SEC,
            temperature=0.0,
        )
        result = _parse_json_output(resp.content or "{}")
        score = float(result.get("hallucination_score", 0.0))
        spans = list(result.get("hallucination_spans", []))
        return L2Result(hallucination_score=max(0.0, min(1.0, score)), hallucination_spans=spans)
    except Exception:
        logger.warning("l2_check failed", exc_info=True)
        return L2Result()


def _parse_json_output(text: str) -> dict[str, Any]:
    return extract_llm_json(text) or {}


class HallucinationChecker:
    def __init__(
        self,
        graph: Any | None = None,
        provider: BaseProvider | None = None,
        l2_threshold: float = 0.3,
        block_threshold: float = 0.7,
    ):
        self._graph = graph
        self._provider = provider
        self._l2_threshold = l2_threshold
        self._block_threshold = block_threshold

    async def check(self, text: str, context: str = "") -> HallucinationReport:
        return await check_hallucination(
            text=text,
            graph=self._graph,
            provider=self._provider,
            context=context,
            l2_threshold=self._l2_threshold,
            block_threshold=self._block_threshold,
        )


async def check_hallucination(
    text: str,
    graph: Any | None = None,
    provider: BaseProvider | None = None,
    context: str = "",
    l2_threshold: float = 0.3,
    block_threshold: float = 0.7,
) -> HallucinationReport:
    l1 = None
    if graph:
        l1 = await l1_check(text, graph)
    overall = l1.confidence_delta if l1 else 0.0
    l2 = None
    need_l2 = bool(provider and (context or (l1 and l1.confidence_delta > l2_threshold)))
    if need_l2:
        l2 = await l2_check(text, context, provider)
        overall = max(overall, l2.hallucination_score)
    blocked = overall > block_threshold
    return HallucinationReport(l1=l1, l2=l2, overall_score=overall, blocked=blocked)
