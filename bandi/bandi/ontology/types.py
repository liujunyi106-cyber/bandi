"""本体词汇表数据模型（TBox 词表 + ABox 三元组）。

复刻 `src/types.ts`（126 行）：类词表 / 关系词表 / 实体 / 断言事实 /
派生事实 / 图视图 / 违规。纯数据形状，无 IO——校验与推理都在 `engine.py`。

设计要点：
- `Term` 是判别联合（`kind` 为 `class`/`relation`），id 空间共享，故取词必须按 kind
  过滤而非直接查表（`class_of`/`relation_of`）。
- `Fact` 按三元组复合键存（`fact_key`），重复断言即幂等覆盖。
- `DerivedFact.via` 记录推理来源，使派生结论可解释、可回溯到规则。
- `Violation` 分机器码与文案两层：code 供程序判别，message 供模型自纠。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

# 标识符语法：词表/实体/谓词共用。窄语法是刻意的——id 会进复合键（`|` 连接）、
# 进 prompt 文本、进工具参数，ASCII 可读形态让三者都无需转义。
# 分隔符 `|` 不在字符类内，故复合键无歧义。
ID_PATTERN = r"[A-Za-z_][A-Za-z0-9_.:-]*"
ID_MAX_LENGTH = 128
FACT_KEY_SEPARATOR = "|"

# 关系可指向什么：其他实体，或裸字面量值
RangeKind = Literal["entity", "literal"]
# 关系的逻辑特征——每一条都会许可某种推论（functional 另带基数约束）
RelationCharacteristic = Literal["transitive", "symmetric", "functional"]
# 派生事实的来源规则
ViaKind = Literal["asserted", "inverse", "symmetric", "transitive"]


@dataclass(frozen=True)
class Violation:
    """功能：一次被拒的原因——机器码 + 面向模型的可修复文案。"""

    code: str
    message: str


@dataclass(frozen=True)
class ClassTerm:
    """功能：TBox 类词表——实体可归属的范畴；`sub_class_of` 的传递闭包即继承。"""

    kind: Literal["class"]
    id: str
    label: str | None = None
    comment: str | None = None
    sub_class_of: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class RelationTerm:
    """功能：TBox 关系词表——实体间的有向边（或字面量值的属性）。

    `domain`/`range` 为空表示**不加约束**（诚实的「尚未决定」，不是静默放行）。
    """

    kind: Literal["relation"]
    id: str
    label: str | None = None
    comment: str | None = None
    domain: tuple[str, ...] = ()
    range_kind: RangeKind = "entity"
    range: tuple[str, ...] = ()
    characteristics: tuple[RelationCharacteristic, ...] = ()
    inverse_of: str | None = None
    created_at: str = ""
    updated_at: str = ""


Term: TypeAlias = ClassTerm | RelationTerm


@dataclass(frozen=True)
class Entity:
    """功能：ABox 个体——`classes` 的传递闭包才是其有效类归属。"""

    id: str
    classes: tuple[str, ...] = ()
    label: str | None = None
    comment: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Fact:
    """功能：ABox 三元组断言——`object_kind` 决定 object 是实体引用还是字面量。

    `violations` 在 strict=True 时恒为空（违规直接拒写）；strict=False 时
    把违规记在此处，使明知不一致的事实保持可审计。
    """

    subject: str
    predicate: str
    object: str
    object_kind: RangeKind = "entity"
    source: str | None = None
    confidence: float | None = None
    violations: tuple[str, ...] = ()
    created_at: str = ""

    @property
    def key(self) -> str:
        """功能：本条断言的复合存储键。"""
        return fact_key(self.subject, self.predicate, self.object)


@dataclass(frozen=True)
class DerivedFact:
    """功能：事实 + 其获得方式——断言事实持久，派生事实读时算出不落盘。

    读时推导的意义：撤回前提，依赖它的全部结论随之消失，无需反向清理。
    """

    subject: str
    predicate: str
    object: str
    object_kind: RangeKind = "entity"
    source: str | None = None
    confidence: float | None = None
    violations: tuple[str, ...] = ()
    created_at: str = ""
    via: ViaKind = "asserted"

    @property
    def key(self) -> str:
        """功能：本条事实的复合键（与断言事实同键空间）。"""
        return fact_key(self.subject, self.predicate, self.object)


@dataclass(frozen=True)
class GraphView:
    """功能：全图只读视图——纯逻辑只吃这个，故每条规则都可脱离存储后端单测。"""

    terms: dict[str, Term] = field(default_factory=dict)
    entities: dict[str, Entity] = field(default_factory=dict)
    facts: dict[str, Fact] = field(default_factory=dict)

    @property
    def asserted_keys(self) -> frozenset[str]:
        """功能：已断言事实的键集合（推理时不被重新派生覆盖）。"""
        return frozenset(self.facts.keys())


def fact_key(subject: str, predicate: str, object: str) -> str:
    """功能：三元组的持久记录键——标识符语法排除了分隔符，故拼接无歧义。"""
    return f"{subject}{FACT_KEY_SEPARATOR}{predicate}{FACT_KEY_SEPARATOR}{object}"


def class_of(graph: GraphView, term_id: str) -> ClassTerm | None:
    """功能：取类词表项——不存在或不是类则返回 None。"""
    term = graph.terms.get(term_id)
    return term if isinstance(term, ClassTerm) else None


def relation_of(graph: GraphView, term_id: str) -> RelationTerm | None:
    """功能：取关系词表项——不存在或不是关系则返回 None。"""
    term = graph.terms.get(term_id)
    return term if isinstance(term, RelationTerm) else None


__all__ = [
    "FACT_KEY_SEPARATOR",
    "ID_MAX_LENGTH",
    "ID_PATTERN",
    "ClassTerm",
    "DerivedFact",
    "Entity",
    "Fact",
    "GraphView",
    "RangeKind",
    "RelationCharacteristic",
    "RelationTerm",
    "Term",
    "ViaKind",
    "Violation",
    "class_of",
    "fact_key",
    "relation_of",
]
