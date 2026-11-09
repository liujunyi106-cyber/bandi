# 功能：记忆四问路由（规则分类 + 分类加权倍率）
from __future__ import annotations

# 四问标签（唯一权威）——question_hint 命中其一方可参与检索加权
MEMORY_QUESTIONS = ("preference", "project_background", "history_conclusion", "task_state")

# 关键词命中优先级（高 → 低）：task_state > history_conclusion > preference >
# project_background；中文关键词按子串匹配（要求双字以上词组，单字词不得命中）
_QUESTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "task_state": (
        "进度",
        "状态",
        "进展",
        "做到哪",
        "待办",
        "下一步",
        "完成了吗",
        "没做完",
        "任务情况",
        "todo",
        "status",
        "progress",
    ),
    "history_conclusion": (
        "历史结论",
        "结论",
        "上次",
        "之前",
        "以前",
        "曾经",
        "历史",
        "定过",
        "说过",
        "决定过",
        "conclusion",
        "decided",
        "previously",
        "last time",
    ),
    "preference": (
        "偏好",
        "喜欢",
        "习惯",
        "风格",
        "倾向",
        "prefer",
        "preference",
        "style",
    ),
    "project_background": (
        "项目背景",
        "项目介绍",
        "项目目标",
        "立项",
        "背景资料",
        "背景是",
        "背景为",
        "上下文",
        "background",
        "context",
        "project",
    ),
}

_KEYWORD_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "状态": ("心理状态", "情绪状态"),
    # 「上下文中断/丢失」是对话截断语义，不是项目背景查询
    "上下文": ("上下文中断", "上下文丢失"),
}

# 否定窗口半径（字符）——排除词与命中词区间距离超过该窗口则不否定（
# 「心理状态和任务状态都看看」不得被远处的「心理状态」拖累漏判 task_state）
_KEYWORD_EXCLUSION_WINDOW = 2

# 四问 → 跨分类融合分倍率（未命中四问返回空 dict = 零行为变化）
_ROUTE_BIAS: dict[str, dict[str, float]] = {
    "preference": {"long_term": 1.15},
    "project_background": {"project_md": 1.2, "long_term": 1.05},
    "history_conclusion": {"episodic": 1.15, "long_term": 1.05},
    "task_state": {"working": 1.2, "episodic": 1.05},
}

_KEYWORD_PRIORITY = ("task_state", "history_conclusion", "preference", "project_background")


def _near_exclusion(text: str, start: int, length: int, exclusions: tuple[str, ...]) -> bool:
    """功能：命中区间 [start, start+length) 与任一排除词区间是否落在否定窗口内。"""
    end = start + length
    for exclusion in exclusions:
        ex_start = text.find(exclusion)
        while ex_start >= 0:
            ex_end = ex_start + len(exclusion)
            if (
                start - _KEYWORD_EXCLUSION_WINDOW < ex_end
                and ex_start < end + _KEYWORD_EXCLUSION_WINDOW
            ):
                return True
            ex_start = text.find(exclusion, ex_start + 1)
    return False


def _keyword_hit(text: str, keyword: str) -> bool:
    """功能：单关键词命中判定——存在任一出现位置未被邻近排除词覆盖才算命中。"""
    exclusions = _KEYWORD_EXCLUSIONS.get(keyword, ())
    start = text.find(keyword)
    if start < 0:
        return False
    if not exclusions:
        return True
    while start >= 0:
        if not _near_exclusion(text, start, len(keyword), exclusions):
            return True
        start = text.find(keyword, start + 1)
    return False


def classify_memory_query(query: str) -> str | None:
    """功能：纯规则判断查询属于哪一问（中文优先、空/无命中返回 None）。"""
    text = (query or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for question in _KEYWORD_PRIORITY:
        if any(_keyword_hit(lowered, keyword) for keyword in _QUESTION_KEYWORDS[question]):
            return question
    return None


def route_bias(question: str) -> dict[str, float]:
    """功能：四问 → 分类加权倍率；未知/空 hint 返回空 dict（调用方零行为）。"""
    return dict(_ROUTE_BIAS.get((question or "").strip().lower(), {}))


__all__ = ["MEMORY_QUESTIONS", "classify_memory_query", "route_bias"]
