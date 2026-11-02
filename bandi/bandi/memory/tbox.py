# 功能：记忆本体词汇表（TBox）——谓词必须先声明才能用
#
# 自 `` 的 `ontology_define` / `ontology_assert` 两段式
# 该项目的核心设计是 **validate, not parse**——LLM 通过 tool-call 显式给三元组，
# 代码侧不解析散文，只校验三元组是否落在已声明的词汇上。
#
# 我们此前是「谓词词表硬编码在抽取函数里 + 关系类型任意字符串」
# "rel_type 无校验——类型枚举随 退役，无白名单来源"）。本模块就是那个
# 缺失的白名单来源：关系类型集中声明、附带 domain/range 约束、违规时给出
# **写给模型看的可操作错误消息**
# "One reason an input was rejected, phrased for the model that must fix it"）。
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RelationTerm:
    """功能：一个可声明的关系词条——谓词 + 适用主体/客体 + 逻辑特征。

    字段对齐 `RelationTerm`（`src/types.ts:47-66`）的子集
    我们不需要 domain/range 全套描述逻辑，够用即可。
    """

    id: str
    label: str
    #: 允许的主体记忆类型（空 = 不约束）
    subject_types: tuple[str, ...] = ()
    #: 允许的客体记忆类型（空 = 不约束）
    object_types: tuple[str, ...] = ()
    #: 是否对称关系（A depends_on B ⇒ B depends_on A）
    symmetric: bool = False
    #: 是否传递关系（A r B, B r C ⇒ A r C）
    transitive: bool = False
    #: 是否函数关系（同一主体至多一个客体——第二个会被判冲突）
    functional: bool = False
    #: 是否允许反向表示（用于给出「你是不是想写 X」的提示）
    inverse_of: str = ""
    description: str = ""


# 功能：关系词表单源——谓词必须在此声明才能写入记忆三元组。
# 取值刻意保守：只收「跨轮稳定、语义单向明确」的谓词。像「续约」这种一次性
# 商业事件不进关系表——它属于记忆内容，不是记忆之间的关系。
RELATION_TERMS: dict[str, RelationTerm] = {
    term.id: term
    for term in (
        RelationTerm(
            id="depends_on",
            label="依赖",
            symmetric=False,
            transitive=True,
            description="A 的成立以 B 为前提（B 变了 A 也要复核）",
        ),
        RelationTerm(
            id="belongs_to",
            label="属于",
            symmetric=False,
            transitive=True,
            description="A 在组织上归属于 B",
        ),
        RelationTerm(
            id="owned_by",
            label="归属",
            symmetric=False,
            description="负责人/所属方视角的归属表达",
        ),
        RelationTerm(
            id="blocks",
            label="阻塞",
            symmetric=False,
            transitive=True,
            description="A 未完成会卡住 B",
        ),
        RelationTerm(
            id="assigned_to",
            label="分配给",
            subject_types=("task_state",),
            object_types=("business_fact",),
            description="任务分派给了谁",
        ),
        RelationTerm(
            id="references",
            label="引用",
            symmetric=False,
            description="A 引用了 B 的内容",
        ),
        RelationTerm(
            id="supersedes",
            label="取代",
            symmetric=False,
            description="A 是 B 的更新版本（B 已过时）",
        ),
        RelationTerm(
            id="contradicts",
            label="冲突",
            symmetric=True,
            description="A 与 B 不能同时为真——必须人工裁决",
        ),
        RelationTerm(
            id="relates_to",
            label="关联",
            symmetric=True,
            description="弱关联，没有更精确的关系词时的兜底",
        ),
    )
}


# 功能：词汇表缺项时给模型的补全提示——的做法是
# "A rejected fact tells you exactly which constraint failed — fix the input or
# extend the vocabulary with ontology_define, do not work around it"。
# 预留：记忆关系词表 prompt 注入；启用时机 记忆关系补全提示接入提示词时
def relation_vocabulary_prompt() -> str:
    """功能：渲染关系词表进提示词——让模型知道能写哪些谓词。"""
    lines = ["## 记忆关系词汇表（memory_assert 可用的谓词）", ""]
    for term in RELATION_TERMS.values():
        flags = []
        if term.symmetric:
            flags.append("对称")
        if term.transitive:
            flags.append("传递")
        if term.functional:
            flags.append("函数")
        suffix = f"（{'、'.join(flags)}）" if flags else ""
        lines.append(f"- {term.id}（{term.label}）{suffix}：{term.description}")
    return "\n".join(lines)


def relation_for(predicate: str) -> RelationTerm | None:
    """功能：按谓词取词条——未声明则返回 None（调用方必须拒绝写入）。"""
    return RELATION_TERMS.get(str(predicate or "").strip())


def suggest_relations(predicate: str) -> list[str]:
    """功能：按标签/描述找相近谓词——给模型「你是不是想写 X」的提示。"""
    key = str(predicate or "").strip().lower()
    if not key:
        return []
    return [
        term.id
        for term in RELATION_TERMS.values()
        if key in term.label or key in term.id or key in term.description
    ]


@dataclass
class Violation:
    """功能：一条校验失败原因。

    措辞面向**必须修复它的那个模型**，不是给人看的日志
    `src/types.ts:120-126`：错误消息要能直接驱动下一轮自我修正。
    """

    code: str
    message: str


def validate_fact(
    *,
    subject: str,
    predicate: str,
    object_value: str,
    subject_type: str = "",
    object_type: str = "",
) -> list[Violation]:
    """功能：校验一个三元组是否落在已声明词汇上——validate, not parse。

    返回违规列表（空 = 通过）。调用方遇到非空必须**拒绝写入并把消息回给模型**，
    不允许绕过、不允许自动改写——这是 的核心纪律。
    """
    violations: list[Violation] = []
    term = relation_for(predicate)
    if term is None:
        near = suggest_relations(predicate)
        hint = f"；你是否想写 {' / '.join(near)}？" if near else ""
        return [
            Violation(
                "unknown-relation",
                f"关系 `{predicate}` 不在已声明的词汇表里{hint}"
                f"可用谓词：{', '.join(RELATION_TERMS)}。请改用已有谓词，不要绕过。",
            )
        ]
    if not str(subject or "").strip():
        violations.append(Violation("missing-subject", "主体不能为空"))
    if not str(object_value or "").strip():
        violations.append(Violation("missing-object", f"关系 `{term.id}` 的客体不能为空"))
    if term.subject_types and subject_type and subject_type not in term.subject_types:
        violations.append(
            Violation(
                "domain-mismatch",
                f"`{subject}` 的类型是 {subject_type}，不允许作为 `{term.id}` 的主体"
                f"（只允许 {'/'.join(term.subject_types)}）",
            )
        )
    if term.object_types and object_type and object_type not in term.object_types:
        violations.append(
            Violation(
                "range-mismatch",
                f"`{object_value}` 的类型是 {object_type}，不允许作为 `{term.id}` 的客体"
                f"（只允许 {'/'.join(term.object_types)}）",
            )
        )
    return violations


def entailed_edges(
    edges: list[tuple[str, str, str]], max_rounds: int = 8
) -> list[tuple[str, str, str]]:
    """功能：按对称/传递/逆关系特征推导隐含边——只读推导，永不落库。

    `entail`（`src/ontology.ts:481-519`）的核心：推论是
    read-time 的派生物，撤回前提即自动消失，不留过期推理残留。
    """
    known = {tuple(e) for e in edges}
    derived: list[tuple[str, str, str]] = []
    for _ in range(max_rounds):
        grew = False
        snapshot = sorted(known)
        for subject, predicate, obj in snapshot:
            term = relation_for(predicate)
            if term is None:
                continue
            if term.symmetric:
                candidate = (obj, predicate, subject)
                if candidate not in known:
                    known.add(candidate)
                    derived.append(candidate)
                    grew = True
            if term.inverse_of:
                candidate = (obj, term.inverse_of, subject)
                if candidate not in known:
                    known.add(candidate)
                    derived.append(candidate)
                    grew = True
            if term.transitive:
                for s2, p2, o2 in snapshot:
                    if p2 != predicate or s2 != obj or o2 == subject:
                        continue
                    candidate = (subject, predicate, o2)
                    if candidate not in known:
                        known.add(candidate)
                        derived.append(candidate)
                        grew = True
        if not grew:
            break
    return derived
