"""本体系统提示词注入段（复刻 dsh-ontology `src/prompt.ts`，57 行）。

模型需要知道「图存在、里面已经用了什么词汇」而不必先花一次工具调用去查——
故把词表常驻进系统提示词。

刻意只放词汇不放实例：TBox 小、慢变、便宜；实例数据大、快变，属于
`ontology_query` 的职责。空图给不同的段落——一份解释图是干什么的引导语，
因为列出「什么都没有」教不会模型任何东西。
"""

from __future__ import annotations

from bandi.ontology.engine import render_tbox, stats_of
from bandi.ontology.types import GraphView

# 词表摘要的词数上限（计划 §4.2-6）
MAX_TBOX_TERMS = 60

_EMPTY_GUIDANCE = (
    "一个持久、有类型的知识图谱可用，且当前为空。它跨会话存在，不像本对话。"
    "当你确立值得留存的结构性事实时——系统的组成与彼此依赖、人与人的角色、"
    "某领域的实体及其关系——用 `ontology_define` 声明词汇，用 `ontology_assert` 记录实例，"
    "而不只是在回复里陈述。\n\n"
    "不要把它当瞬时事实的草稿纸，也不要创建你不会用来断言的词汇。"
)


def render_ontology_section(graph: GraphView, max_terms: int = MAX_TBOX_TERMS) -> str:
    """功能：为一次装配渲染本体段落。

    非空图：计数 + 词表清单 + 读/扩展指引；空图：引导语。
    """
    stats = stats_of(graph)
    lines = ["# 本体"]

    if stats.classes == 0 and stats.relations == 0:
        lines.append("")
        lines.append(_EMPTY_GUIDANCE)
        return "\n".join(lines)

    lines.append("")
    lines.append(
        f"一个持久、有类型的知识图谱跨会话存在：{stats.classes} 个类、"
        f"{stats.relations} 个关系、{stats.entities} 个实体、{stats.facts} 条事实。"
        "它已声明的词汇："
    )
    lines.append("")
    lines.extend(f"- {line}" for line in render_tbox(graph, max_terms))
    lines.append("")
    lines.append(
        "假设这些你都不了解之前，先用 `ontology_query` 读；用 `ontology_define`（词汇）"
        "与 `ontology_assert`（实例）扩展它。断言是对照词汇校验的，所以被拒意味着"
        "该主张不符合已声明的模型——要么主张错了，要么模型需要扩充。"
        "复用既有词汇，不要新造近义重复项。"
    )
    return "\n".join(lines)


__all__ = ["MAX_TBOX_TERMS", "render_ontology_section"]
