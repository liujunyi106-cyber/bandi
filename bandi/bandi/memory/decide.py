"""长期记忆写入的唯一判定入口（canonical）。

背景（2026-11 审计）：写入维度曾在三条分支（普通 / 员工私聊 / 团队群聊）各写一遍
「取内容 → 判价值 → 定作用域」，改一处漏一处；bug「口头称呼出不了会话」
「任务模板被当用户陈述写成记忆」都出在这里。

本模块只回答一个问题：**这一轮要不要写长期记忆、写什么、落在哪个作用域**。
写入执行（gate / 冲突检测 / 去重 / 暂存）仍由调用方负责，本模块不碰存储。"""

from __future__ import annotations

from dataclasses import dataclass

from bandi.memory.extraction import extract_memory_fields
from bandi.memory.ingest_review import (
    has_explicit_remember,
    is_rememberable_user_input,
    review_ingest,
)
from bandi.memory.memory_types import MemoryType

_MAX_CONTENT_CHARS = 2000


@dataclass(frozen=True)
class LongTermDecision:
    """一次长期记忆写入选定：写不写、写什么、落哪。"""

    write: bool
    content: str
    source_text: str
    memory_type: str
    project_id: str | None
    employee_id: str | None
    reason: str
    # 功能：本轮是否由用户显式「记住」触发——显式才允许直写落库，其余进草稿池
    explicit: bool = False
    # 功能：价值审查得分（0-1），草稿池超限时按它排序晋级审批
    value_score: float = 0.0

    def __bool__(self) -> bool:  # 便于 `if decision:` 直读
        return self.write


def long_term_source_text(
    raw_input: str, final_message: str = "", *, explicit_remember: bool | None = None
) -> str:
    """功能：长期记忆的候选原文——显式「记住」或用户陈述了可记事实时取用户原话。

    `explicit_remember` 由调用方算好可传入（None = 本函数自判），避免调用方已判过
    的情况下再判一次造成语义分叉（2026-11 实测：薄壳重判把「帮我记录一下」放行了）。
    """
    text = (raw_input or "").strip()
    if not text:
        return ""
    explicit = has_explicit_remember(text) if explicit_remember is None else explicit_remember
    if explicit or is_rememberable_user_input(text, final_message or ""):
        return text
    return ""


def scope_for(
    memory_type: MemoryType | str,
    *,
    fallback_project: str | None,
    fallback_employee: str | None = None,
    project_id: str | None = None,
) -> tuple[str | None, str | None]:
    """功能：按记忆性质决定落点——偏好/身份→全局，过程结论→项目，其余沿用调用方作用域。"""
    kind = memory_type if isinstance(memory_type, MemoryType) else MemoryType(str(memory_type))
    if kind == MemoryType.PREFERENCE:
        return None, None
    if kind == MemoryType.PROCESS_CONCLUSION and project_id:
        return str(project_id), None
    return fallback_project, fallback_employee


def decide_long_term(
    *,
    raw_input: str,
    final_message: str = "",
    source: str = "agent_turn",
    fallback_project: str | None = None,
    fallback_employee: str | None = None,
    project_id: str | None = None,
) -> LongTermDecision:
    """功能：判定本轮是否写长期记忆（唯一入口）——三条分支都调这里，不得各自判定。

    顺序固定：
    1. 取候选（用户原话；显式「记住」或用户陈述可记事实）；
    2. 抽类型与结构化字段；
    3. `review_ingest` 价值门（噪声/低价值直接拒）；
    4. 按类型定作用域（`scope_for`）。
    """
    text = long_term_source_text(raw_input, final_message)
    if not text:
        return LongTermDecision(False, "", "", "", None, None, "no_user_statement")
    explicit = has_explicit_remember(str(raw_input or ""))

    fields = extract_memory_fields(text, user_input=raw_input, source=source)
    verdict = review_ingest(
        text,
        memory_type=fields.memory_type.value,
        source=source,
        user_input=raw_input,
    )
    if verdict.decision == "reject":
        return LongTermDecision(
            False,
            "",
            text,
            fields.memory_type.value,
            None,
            None,
            verdict.reason,
            explicit=explicit,
            value_score=verdict.score,
        )

    content = fields.render_fact(text[:_MAX_CONTENT_CHARS])[:_MAX_CONTENT_CHARS]
    if not content.strip():
        return LongTermDecision(
            False,
            "",
            text,
            fields.memory_type.value,
            None,
            None,
            "empty_content",
            explicit=explicit,
            value_score=verdict.score,
        )

    target_project, target_employee = scope_for(
        fields.memory_type,
        fallback_project=fallback_project,
        fallback_employee=fallback_employee,
        project_id=project_id,
    )
    return LongTermDecision(
        write=True,
        content=content,
        source_text=text,
        memory_type=fields.memory_type.value,
        project_id=target_project,
        employee_id=target_employee,
        reason=f"{verdict.decision}:{verdict.reason}",
        explicit=explicit,
        value_score=verdict.score,
    )


__all__ = [
    "LongTermDecision",
    "decide_long_term",
    "long_term_source_text",
    "scope_for",
]
