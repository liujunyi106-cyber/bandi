# 功能：记忆结构化 schema——metadata["structured"] 的规则抽取与键归一（零 LLM 零延迟）
#
# 统一 schema（写死字段）：
#   metadata["structured"] = {kind, key, value?, subject?, predicate?, object?}
#   - kind ∈ STRUCTURED_KINDS；key 为 snake_case 规范键（合并/更新的稳定依据）
#   - predicate 出现时必须在 tbox.RELATION_TERMS 白名单内（记忆关系谓词单源）
from __future__ import annotations

import re

from bandi.memory.extraction import extract_memory_fields
from bandi.memory.memory_types import MemoryType

# 功能：structured.kind 词表——三档语义 + episode 兜底档（降级保证 kind/key 恒在）。
STRUCTURED_KINDS = ("profile", "preference", "fact", "episode")

# 功能：kind 的中文展示标签（消息中心草稿摘要前缀用）——落库键保持英文稳定键，
# 只在渲染层翻译；未知 kind 原样展示（不硬造标签）。
STRUCTURED_KIND_LABELS: dict[str, str] = {
    "profile": "档案",
    "preference": "偏好",
    "fact": "事实",
    "episode": "经历",
}

# 功能：迁移标注档——存量条目规则抽不出时的显式留底（不冒充结构化内容）。
EVIDENCE_ONLY_KIND = "evidence-only"

# 功能：属性词表——口语属性 → 规范 snake_case key（顺序即优先，先具体后宽泛）。
# 覆盖：交付格式/财务口径/人物联系/身份称呼/语言地区/作息/健康忌口/视觉审美/
# 工具技术栈/流程规范/时间计划/数量规格/渠道通知。
ATTRIBUTE_TERMS: tuple[tuple[str, str], ...] = (
    # 交付格式
    ("导出格式", "export_format"),
    ("文件格式", "file_format"),
    ("报表格式", "report_format"),
    ("输出格式", "output_format"),
    ("编码", "encoding"),
    ("格式", "format"),
    # 财务口径
    ("预算口径", "budget_caliber"),
    ("预算", "budget"),
    ("口径", "caliber"),
    ("币种", "currency"),
    ("结算", "settlement"),
    ("价格", "price"),
    ("成本", "cost"),
    ("折扣", "discount"),
    # 人物联系
    ("对接联系人", "contact"),
    ("对接人", "contact"),
    ("联系人", "contact"),
    ("联系电话", "phone"),
    ("电话", "phone"),
    ("邮箱", "email"),
    ("地址", "address"),
    ("公司", "company"),
    ("部门", "department"),
    ("团队", "team"),
    # 身份称呼
    ("称呼", "preferred_name"),
    ("姓名", "preferred_name"),
    ("名字", "preferred_name"),
    ("职位", "title"),
    ("角色", "role"),
    # 语言地区
    ("语言", "language"),
    ("时区", "timezone"),
    ("地区", "region"),
    ("城市", "city"),
    ("国家", "country"),
    # 作息与时间
    ("上班时间", "work_hours"),
    ("工作时间", "work_hours"),
    ("截止", "deadline"),
    ("周期", "cycle"),
    ("频率", "frequency"),
    # 健康忌口
    ("忌口", "dietary"),
    ("口味", "taste"),
    ("过敏", "allergy"),
    ("不吃", "dietary"),
    ("作息", "routine"),
    # 视觉审美
    ("配色", "color"),
    ("颜色", "color"),
    ("字号", "font_size"),
    ("字体", "font"),
    ("主题", "theme"),
    ("布局", "layout"),
    # 工具技术栈
    ("编辑器", "preferred_editor"),
    ("数据库", "database"),
    ("语言环境", "runtime"),
    ("框架", "framework"),
    ("工具", "preferred_tool"),
    ("软件", "preferred_tool"),
    ("插件", "plugin"),
    ("浏览器", "browser"),
    ("模型", "model"),
    # 流程规范（具体项在前，泛化项「流程/步骤」在后）
    ("规范", "convention"),
    ("审批", "approval"),
    ("权限", "permission"),
    ("命名", "naming"),
    ("部署", "deployment"),
    ("端口", "port"),
    ("上线", "release"),
    ("备份", "backup"),
    ("流程", "procedure"),
    ("步骤", "procedure"),
    # 数量规格
    ("数量", "quantity"),
    ("规格", "spec"),
    ("尺寸", "size"),
    ("容量", "capacity"),
    ("并发", "concurrency"),
    ("超时", "timeout"),
    ("重试", "retry"),
    # 渠道通知
    ("通知", "notification"),
    ("渠道", "channel"),
    ("同步", "sync"),
    ("推送", "push"),
)

# 功能：弱 key——规则回退兜底键（属性词未命中时）。同 kind+弱 key 的两条记忆
# 属性未知，合并时不得只凭 key 视为同一条（交由内容指纹判实质差异，B3 消费）。
WEAK_KEYS = frozenset({"preference", "fact"})

# 功能：谓词 label → 规范 key（extraction._PREDICATE_WORDS 的 label 全集镜像）。
PREDICATE_KEYS: dict[str, str] = {
    "偏好": "preference",
    "排斥": "avoidance",
    "禁止": "prohibition",
    "要求": "requirement",
    "约定": "agreement",
    "决策": "decision",
    "结论": "conclusion",
    "教训": "lesson",
    "负责": "ownership",
    "归属": "belonging",
    "依赖": "dependency",
    "位于": "location",
    "续约": "renewal",
}

# 功能：谓词 label → tbox 白名单 id——只有白名单谓词允许出现在 structured.predicate。
TBOX_PREDICATE_MAP: dict[str, str] = {
    "归属": "belongs_to",
    "负责": "owned_by",
    "依赖": "depends_on",
}

_KEY_SANITIZE_RE = re.compile(r"[^a-z0-9]+")
_KEY_MAX_CHARS = 40
_MAX_VALUE_CHARS = 120


def normalize_key(raw: str) -> str:
    """功能：key 归一为 snake_case——非 ASCII（中文等）无效返回空串，由调用方降级。"""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    normalized = _KEY_SANITIZE_RE.sub("_", text).strip("_")
    return normalized[:_KEY_MAX_CHARS].strip("_")


def is_weak_key(key: str) -> bool:
    """功能：弱 key 判定（属性词未命中时的兜底键）——B3 去重据此退回内容指纹，
    不得只凭 kind+弱 key 把两条不同属性的记忆当成同一条合并。"""
    return str(key or "") in WEAK_KEYS


def _attribute_key(text: str) -> str:
    """功能：扫属性词表取规范 key（未命中返回空串）。"""
    for word, key in ATTRIBUTE_TERMS:
        if word in text:
            return key
    return ""


def build_structured(text: str) -> dict[str, str] | None:
    """功能：规则抽取结构化字段（零延迟）——偏好/事实 → {kind,key,...}；抽不出 None。

    偏好类：属性词命中优先，其次谓词规范名，兜底 "preference"（偏好语义明确，
    弱 key 不会从结构化面消失）；value 取切出的客体。
    事实类：白名单谓词 + 主客体齐全 → 三元组形态（predicate ∈ RELATION_TERMS）；
    否则 key:value 形态（属性词/谓词规范名）；两者都不成立 → None
    （调用方按「抽不出仅 content」处理，不阻塞写入）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    fields = extract_memory_fields(raw)
    if fields.memory_type == MemoryType.PREFERENCE:
        key = _attribute_key(raw) or PREDICATE_KEYS.get(fields.predicate or "", "") or "preference"
        data = {"kind": "preference", "key": key}
        if fields.object_value:
            data["value"] = fields.object_value[:_MAX_VALUE_CHARS]
        return data
    tbox_id = TBOX_PREDICATE_MAP.get(fields.predicate or "")
    if tbox_id and fields.subject and fields.object_value:
        return {
            "kind": "fact",
            "key": tbox_id,
            "subject": fields.subject[:_MAX_VALUE_CHARS],
            "predicate": tbox_id,
            "object": fields.object_value[:_MAX_VALUE_CHARS],
        }
    key = _attribute_key(raw) or PREDICATE_KEYS.get(fields.predicate or "", "")
    if key:
        data = {"kind": "fact", "key": key}
        value = fields.object_value or fields.progress or raw
        data["value"] = str(value)[:_MAX_VALUE_CHARS]
        return data
    return None


def entailed_structured(entries: list[dict]) -> list[dict]:
    """功能：按 tbox 谓词特征从已断言的三元组推导隐含三元组——只读派生，永不落盘。

    记忆侧的三元组来自 `build_structured` 的 fact 形态（subject/predicate/object
    齐全且 predicate ∈ tbox 白名单）。本函数只做入口过滤与 via 标注，闭包统一
    委托 `entailed_edges`（对称/传递/逆关系的单一实现）——撤回前提，派生结论
    随之消失，无需反向清理。

    派生项带 `inferred` 与 `derived_via` 标记，使其无法与断言事实混淆（否则
    巩固会把推出来的东西当新事实再写一遍，推论就会自我放大）。
    """
    from bandi.memory.tbox import RELATION_TERMS, entailed_edges, relation_for

    triples: list[tuple[str, str, str]] = []
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("kind") != "fact":
            continue
        subject = str(entry.get("subject") or "").strip()
        predicate = str(entry.get("predicate") or "").strip()
        obj = str(entry.get("object") or "").strip()
        if not (subject and predicate and obj):
            continue
        if relation_for(predicate) is None:
            continue  # 谓词不在白名单，无从判断特征
        triples.append((subject, predicate, obj))

    if not triples:
        return []
    asserted = set(triples)
    inverse_sources = {
        term.inverse_of: term.id for term in RELATION_TERMS.values() if term.inverse_of
    }
    derived: list[dict] = []
    for triple in entailed_edges(list(triples)):
        subject, predicate, obj = triple
        term = relation_for(predicate)
        if term is not None and term.symmetric and (obj, predicate, subject) in asserted:
            via = "symmetric"
        elif (
            predicate in inverse_sources and (obj, inverse_sources[predicate], subject) in asserted
        ):
            via = "inverse"
        else:
            via = "transitive"
        derived.append(_derived_structured(triple, via))
    return derived


def _derived_structured(triple: tuple[str, str, str], via: str) -> dict[str, str | bool]:
    """功能：派生三元组条目——带 inferred 标记与推理来源。"""
    subject, predicate, obj = triple
    return {
        "kind": "fact",
        "key": predicate,
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "inferred": True,
        "derived_via": via,
    }


def structured_preview(content: str, structured: object, *, max_chars: int = 80) -> str:
    """功能：渲染消息中心摘要——中文 kind 标签前缀 + 断言句截断（不含对话原文包装）。"""
    kind = "memory"
    if isinstance(structured, dict) and structured.get("kind"):
        kind = str(structured["kind"])
    label = STRUCTURED_KIND_LABELS.get(kind, kind)
    return f"[{label}] {str(content or '')[:max_chars]}"


__all__ = [
    "ATTRIBUTE_TERMS",
    "entailed_structured",
    "EVIDENCE_ONLY_KIND",
    "PREDICATE_KEYS",
    "STRUCTURED_KINDS",
    "TBOX_PREDICATE_MAP",
    "WEAK_KEYS",
    "build_structured",
    "is_weak_key",
    "normalize_key",
    "structured_preview",
]
