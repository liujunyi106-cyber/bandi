"""功能：实体归一（同名不同写 / 不同名同物）——粗筛 + LLM 精判两阶段。

datafoundry DataLink 的两阶段归一（`services/datalink/src/datalink/mapper/
llm_mapper.py:594-609`）：

 第一阶段 粗筛：把 O(n²) 的候选对压到 O(k)——datafoundry 用 embedding 余弦
 ≥0.75，我们**没有向量设施**，改用中文 2-gram
 词元重叠 + 子串规则做同一件事。角色相同：廉价地把候选缩小，不做判定。
 第二阶段 精判：只把候选对喂给 LLM 逐对确认「是不是同一个东西」。

关键纪律：**相似度高不等于该合并**（datafoundry prompt 原文第 6 条："High
similarity does NOT guarantee they should be merged"）。判定权在 LLM，相似度
只负责省 token——本模块任何一处都不用相似度阈值直接下合并结论。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# 功能：粗筛阈值——低于此相似度连候选都不进，省掉 LLM 调用。
_CANDIDATE_THRESHOLD = 0.34

# 功能：单批交给 LLM 判定的候选上限——控制 token 与延迟。
_MAX_CANDIDATES = 24


@dataclass(frozen=True)
class MergeCandidate:
    """功能：一条待判定的归一候选（new = 本轮新名字，existing = 已有名字）。"""

    new: str
    existing: str
    similarity: float
    reason: str = ""


@dataclass
class MergePlan:
    """功能：归一执行计划——只含 LLM 明确确认的配对。"""

    merges: list[tuple[str, str]] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    rejected_candidates: list[MergeCandidate] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    """功能：中文 2-gram + ASCII 词元——与检索侧分词口径一致。"""
    from bandi.memory.episodic import tokenize_cn

    raw = str(text or "").strip()
    if not raw:
        return set()
    grams = set(tokenize_cn(raw))
    ascii_words = {w.lower() for w in raw.replace("-", " ").split() if len(w) >= 2}
    return grams | ascii_words or {raw.lower()}


def _similarity(left: str, right: str) -> tuple[float, str]:
    """功能：粗筛相似度——Jaccard 为主，包含关系与规范化相等单独加分。"""
    a, b = str(left or "").strip(), str(right or "").strip()
    if not a or not b:
        return 0.0, ""
    if a == b:
        return 1.0, "完全相同"
    norm_a = a.replace(" ", "").replace("的", "").lower()
    norm_b = b.replace(" ", "").replace("的", "").lower()
    if norm_a == norm_b:
        return 0.98, "去空格/助词后相同"
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0, ""
    jaccard = len(ta & tb) / len(ta | tb)
    if norm_a in norm_b or norm_b in norm_a:
        jaccard = max(jaccard, 0.75)
        return jaccard, "包含关系"
    return jaccard, "词元重叠"


def candidate_pairs(
    new_names: list[str],
    existing_names: list[str],
    *,
    threshold: float = _CANDIDATE_THRESHOLD,
    max_candidates: int = _MAX_CANDIDATES,
) -> list[MergeCandidate]:
    """功能：粗筛——列出值得交给 LLM 判定的归一候选（相似度降序）。

    这一步**不做合并决策**：分数只决定「值不值得问 LLM」。
    """
    scored: list[MergeCandidate] = []
    for new in new_names:
        for existing in existing_names:
            if str(new or "").strip() == str(existing or "").strip():
                continue
            sim, reason = _similarity(new, existing)
            if sim >= threshold:
                scored.append(
                    MergeCandidate(new=new, existing=existing, similarity=sim, reason=reason)
                )
    scored.sort(key=lambda c: (-c.similarity, c.new, c.existing))
    return scored[:max_candidates]


# 功能：交给 LLM 的判定提示词——judge 收到的是候选对，不是全量名称列表。
JUDGE_PROMPT = """你在做实体归一：判断下面每一对名称是否指向**同一个现实事物**。

规则：
1. 「明远科技」与「明远科技公司」→ 同一个
2. 「订单服务」与「支付服务」→ 不同（都是服务但是两个东西）
3. 高相似度**不保证**该合并，必须按语义判断
4. 不确定时判 false——宁可少合并，也不要把两个东西混成一个

候选对（格式：序号 | 新名称 | 已有名称 | 粗筛相似度 | 粗筛理由）：
{pairs}

逐对输出 JSON 数组，每项 {{"index": 序号, "same": true/false, "reason": "一句话理由"}}。
只输出 JSON。"""


def build_judge_payload(candidates: list[MergeCandidate]) -> str:
    """功能：把候选对渲染成判定载荷。"""
    lines = [
        f"{i} | {c.new} | {c.existing} | {c.similarity:.2f} | {c.reason}"
        for i, c in enumerate(candidates, 1)
    ]
    return JUDGE_PROMPT.format(pairs="\n".join(lines))


def parse_judgement(raw: str, candidates: list[MergeCandidate]) -> MergePlan:
    """功能：解析 LLM 判定结果——只认明确判 true 的配对，其余一律保留。

    破坏者视角：解析失败 / 缺项 / 非法 JSON 一律降级为「不合并」，
    绝不做「默认合并」——误合并会把两个事实永久粘在一起，不可逆。
    """
    import json
    import re

    plan = MergePlan()
    if not raw or not candidates:
        plan.kept = [c.new for c in candidates]
        return plan
    text = str(raw).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        plan.kept = [c.new for c in candidates]
        return plan
    try:
        items = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        plan.kept = [c.new for c in candidates]
        return plan
    by_index: dict[Any, Any] = {}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                by_index[item.get("index")] = item
    matched: set[int] = set()
    for i, candidate in enumerate(candidates, 1):
        item = by_index.get(i)
        if item is None and isinstance(by_index.get(str(i)), dict):
            item = by_index[str(i)]
        if item is None:
            plan.kept.append(candidate.new)
            plan.rejected_candidates.append(candidate)
            continue
        if item.get("same") is True:
            plan.merges.append((candidate.new, candidate.existing))
            matched.add(i)
        else:
            plan.kept.append(candidate.new)
            plan.rejected_candidates.append(candidate)
    # LLM 完全没提到的候选——同样按「不合并」处理
    for i, candidate in enumerate(candidates, 1):
        if i not in matched and candidate.new not in plan.kept:
            plan.kept.append(candidate.new)
            plan.rejected_candidates.append(candidate)
    del re
    return plan


async def normalize_entities(
    new_names: list[str],
    existing_names: list[str],
    *,
    llm: Callable[[str], Any],
) -> MergePlan:
    """功能：两阶段归一编排——粗筛 → LLM 精判 → 合并计划。

    `llm` 接收判定提示词、返回模型原文；注入式设计便于无网络单测，同步/异步均可。
    """
    candidates = candidate_pairs(new_names, existing_names)
    if not candidates:
        return MergePlan(kept=list(new_names))
    payload = build_judge_payload(candidates)
    raw = llm(payload)
    if inspect.isawaitable(raw):
        raw = await raw
    plan = parse_judgement(raw, candidates)
    # 非候选的新名字一律保留——粗筛没放它们进候选，不等于它们该被合并
    merged_new = {new for new, _ in plan.merges}
    kept = list(plan.kept)
    for name in new_names:
        if name not in merged_new and name not in kept:
            kept.append(name)
    plan.kept = kept
    return plan
