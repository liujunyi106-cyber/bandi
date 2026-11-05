# 功能：记忆类型与抽取结果——长期记忆入库的轻量类型系统
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class MemoryType(StrEnum):
    """功能：记忆类型（入库分类单源）。"""

    PREFERENCE = "preference"
    BUSINESS_FACT = "business_fact"
    PROCESS_CONCLUSION = "process_conclusion"
    TASK_STATE = "task_state"
    PROCEDURAL = "procedural"


# 功能：一句话索引长度上限——够判断相关性，又不至于变成正文。
_DESCRIPTION_MAX_CHARS = 80


@dataclass
class MemoryExtract:
    """功能：一条记忆的规则抽取结果（类型 + 实体 + 时间 + 来源 + 进度 + 三元组）。"""

    memory_type: MemoryType
    entities: list[str] = field(default_factory=list)
    temporal: str | None = None
    source: str = "agent_turn"
    progress: str | None = None
    # 结构化三元组——「原句不等于记忆，断言才是」。切不出谓词时为 None，
    # 调用方据此回退原句，宁可保留原句也不产出错误的断言。
    subject: str | None = None
    predicate: str | None = None
    object_value: str | None = None

    def to_metadata(self) -> dict:
        """功能：转为 MemoryItem.metadata 保留键（不改表结构的落库形态）。"""
        return {
            "memory_type": str(self.memory_type),
            "entities": list(self.entities),
            "temporal": self.temporal,
            "source": self.source,
            "progress": self.progress,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object_value,
        }

    def render_fact(self, evidence: str = "") -> str:
        """功能：渲染结构化断言——记忆的主形态是断言，原句只作证据留底。

        断言形态「主体 ·谓词· 客体（时间）」：检索命中时直接可用，不必回读原句；
        切不出谓词、或切不出客体时回退原句——只切出「主体·谓词」的半截断言
        等于没写内容：正文被模板占满后不可检索，且不同记忆会渲染成同一串
        模板而被内容去重误并成一条。
        """
        if not (self.subject and self.predicate and self.object_value):
            return evidence
        head = f"{self.subject}·{self.predicate} {self.object_value}"
        tail = f"（{self.temporal}）" if self.temporal else ""
        body = f"[{self.memory_type}] {head}{tail}"
        if self.progress:
            body += f"；进度：{self.progress}"
        return body

    def render_description(self, evidence: str = "") -> str:
        """功能：渲染一句话索引——判断「这条记忆和当前问题相关吗」用。

        移植 cc-haha 的 `description` 字段（`src/memdir/memoryTypes.ts:265`），
        原文注释是「used to decide relevance in future conversations, so be specific」：
        它不是给人读的解释，而是**给检索排序看的索引**——断言形态天然就是好索引，
        无谓词时取原句首句并截断。
        """
        labels = {
            MemoryType.PREFERENCE: "偏好",
            MemoryType.BUSINESS_FACT: "事实",
            MemoryType.PROCESS_CONCLUSION: "结论",
            MemoryType.TASK_STATE: "进度",
            MemoryType.PROCEDURAL: "做法",
        }
        label = labels.get(self.memory_type, "记忆")
        if self.subject and self.predicate:
            core = f"{self.subject}{self.predicate}"
            if self.object_value:
                core += self.object_value
            return f"{label}·{core}"[:_DESCRIPTION_MAX_CHARS]
        head = str(evidence or "").strip().replace("\n", " ")
        for bound in "。！？!?；;":
            if bound in head:
                head = head.split(bound, 1)[0]
                break
        return f"{label}·{head}"[:_DESCRIPTION_MAX_CHARS]
