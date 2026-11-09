# 功能：记忆字段规则抽取——纯正则/确定性（零 LLM），单轮 O(len)
from __future__ import annotations

import re

from bandi.memory.memory_types import MemoryExtract, MemoryType

# 功能：类型判定词表（设计冻结顺序：偏好 → 过程结论 → 任务状态 → 程序性 → 业务事实）
# 身份/称呼与稳定偏好同类：都属于「换会话、换项目都不该丢」的用户画像，
# 落到全局层（见 agent_runner._long_term_scope_for）。
_PREFERENCE_WORDS = (
    "喜欢",
    "偏好",
    "以后都",
    "默认",
    "习惯",
    "请不要",
    "优先选",
    "叫我",
    "称呼我",
    "我的名字",
    "以后叫",
)
_CONCLUSION_WORDS = ("结论", "决定", "因此", "方案是", "教训", "采用")
_STATE_WORDS = (
    "进行中",
    "待办",
    "TODO",
    "todo",
    "下一步",
    "已完成",
    "未完成",
    "待处理",
    "处理中",
    "已解决",
    "阻塞",
)
_PROCEDURAL_WORDS = ("步骤", "流程", "SOP", "sop")

_SENTENCE_BOUNDS = "。！？!?；;\n"

_QUOTED_RE = re.compile(r"[“”\"'「」『』《》]([^“”\"'「」『』《》\n]{1,40})[“”\"'「」『』《》]")
_FILE_RE = re.compile(r"[\w./\\-]{2,}\.[A-Za-z]{1,6}\b")
_HYPHEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+\b")
_CAMEL_RE = re.compile(
    r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b"
)
_TEMPORAL_RE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{4}/\d{1,2}/\d{1,2}"
    r"|\d{1,2}月\d{1,2}[日号]"
    r"|昨天|今天|明天|后天|前天"
)

_MAX_ENTITIES = 8
_MAX_PROGRESS_CHARS = 80
_MAX_OBJECT_CHARS = 60

# 功能：谓词词表——三元组的中间项。命中即以命中处切主客体，
# 顺序即优先级（先出现的更具体）。
_PREDICATE_WORDS: tuple[tuple[str, str], ...] = (
    ("最喜欢", "偏好"),
    ("喜欢", "偏好"),
    ("偏好", "偏好"),
    ("讨厌", "排斥"),
    ("不要", "排斥"),
    ("禁止", "禁止"),
    ("必须", "要求"),
    ("约定", "约定"),
    ("决定", "决策"),
    ("结论", "结论"),
    ("教训", "教训"),
    ("负责", "负责"),
    ("属于", "归属"),
    ("归属", "归属"),
    ("依赖", "依赖"),
    ("位于", "位于"),
    ("续约", "续约"),
    ("负责", "负责"),
)

# 功能：主语缺省——中文个性化记忆九成主语是用户本人。
_DEFAULT_SUBJECT = "用户"

# 功能：主语不落实体时的兜底——谓词前那段短文本本身就是主语
# （「项目 Alpha 属于产品线 Beta」的主语是 Alpha，而 Alpha 既非引号也非驼峰，
# 实体抽取抓不到，靠这条兜住）。代词不作主语；过长的分句回落「用户」。
_PRONOUN_HEADS = ("我", "你", "您", "他", "她", "它", "咱", "我们", "你们", "他们")

# 功能：主语长度上限——超过即视为分句而非名词短语（「今天完成了订单服务的重构」）。
_MAX_SUBJECT_CHARS = 12


def _pick_subject(head: str, entities: list[str]) -> str:
    """功能：选主语——优先谓词前的实体，其次短文本兜底，最后默认「用户」。"""
    for entity in entities:
        if entity in head:
            return entity
    candidate = head.strip(" 　，。；：:、的")
    if not candidate or candidate.startswith(_PRONOUN_HEADS):
        return _DEFAULT_SUBJECT
    if len(candidate) > _MAX_SUBJECT_CHARS:
        return _DEFAULT_SUBJECT
    return candidate


# 功能：客体清洗要剥掉的引导词，留在断言里没有信息量。
_OBJECT_LEAD_NOISE = ("是", "为", "：", ":", "的", "，", "。", "、")


def _extract_triple(text: str, entities: list[str]) -> tuple[str | None, str | None, str | None]:
    """功能：切出 (主体, 谓词, 客体)——切不出谓词就返回 (None, None, None) 由调用方回退。

    保守优先：谓词必须命中词表；主体取谓词之前的第一个实体（或默认「用户」）；
    客体取谓词之后到句末的核心片段。宁可留原句，也不产出错的断言。
    """
    hit: tuple[int, str, str] | None = None
    for word, label in _PREDICATE_WORDS:
        idx = text.find(word)
        if idx >= 0 and (hit is None or idx < hit[0]):
            hit = (idx, word, label)
    if hit is None:
        return None, None, None
    idx, word, label = hit

    head = text[:idx]
    subject = _pick_subject(head, entities)

    tail = text[idx + len(word) :]
    stop = min(
        (tail.find(bound) for bound in _SENTENCE_BOUNDS if tail.find(bound) >= 0),
        default=len(tail),
    )
    obj = tail[:stop]
    for noise in _OBJECT_LEAD_NOISE:
        while obj.startswith(noise):
            obj = obj[len(noise) :]
    obj = obj.strip(" 　\"'“”「」")
    if not obj or len(obj) > _MAX_OBJECT_CHARS:
        return subject, label, None
    return subject, label, obj


def _iter_entity_spans(text: str):
    """功能：按来源产出 (位置, 候选实体)——引号内内容 / 路径文件名 / 连字符 / 驼峰。"""
    for match in _QUOTED_RE.finditer(text):
        yield match.start(1), match.group(1)
    for pattern in (_FILE_RE, _HYPHEN_RE, _CAMEL_RE):
        for match in pattern.finditer(text):
            yield match.start(), match.group(0)


def _classify(text: str) -> MemoryType:
    """功能：按词表顺序判定记忆类型（首个命中优先）。"""
    if any(word in text for word in _PREFERENCE_WORDS):
        return MemoryType.PREFERENCE
    if any(word in text for word in _CONCLUSION_WORDS):
        return MemoryType.PROCESS_CONCLUSION
    if any(word in text for word in _STATE_WORDS):
        return MemoryType.TASK_STATE
    if any(word in text for word in _PROCEDURAL_WORDS) or ("先" in text and "再" in text):
        return MemoryType.PROCEDURAL
    return MemoryType.BUSINESS_FACT


def _extract_progress(text: str) -> str | None:
    """功能：从最早出现的状态句截取进度描述（≤80 字）。"""
    earliest: tuple[int, str] | None = None
    for word in _STATE_WORDS:
        idx = text.find(word)
        if idx >= 0 and (earliest is None or idx < earliest[0]):
            earliest = (idx, word)
    if earliest is None:
        return None
    idx = earliest[0]
    start = max((text.rfind(bound, 0, idx) for bound in _SENTENCE_BOUNDS), default=-1) + 1
    ends = [text.find(bound, idx) for bound in _SENTENCE_BOUNDS]
    end = min((value for value in ends if value >= 0), default=len(text))
    sentence = text[start:end].strip()
    if not sentence:
        return None
    return sentence[:_MAX_PROGRESS_CHARS]


def _extract_entities(text: str) -> list[str]:
    """功能：抽取实体（按出现位置排序、去重、上限 8）。

    只认有明确字面边界的实体：引号内内容 / 路径文件名 / 连字符 / 驼峰。
    中文自由文本的实体识别不在此列——六家参考框架无一例用正则抽散文实体：
    参考实现 走 LLM tool-call 显式给三元组、参考实现 与 参考实现 让 LLM 直接写
    结构化正文、datafoundry 只对表列等结构化输入做语义映射。正则抽中文散文实测
    把「两者都要部署到」当实体、真实体「Alpha」反而抓不到。"""
    spans = sorted(_iter_entity_spans(text), key=lambda pair: pair[0])
    entities: list[str] = []
    for _, value in spans:
        candidate = value.strip()
        if candidate and candidate not in entities:
            entities.append(candidate)
        if len(entities) >= _MAX_ENTITIES:
            break
    return entities


def extract_memory_fields(
    text: str, *, user_input: str = "", source: str = "agent_turn"
) -> MemoryExtract:
    """功能：纯规则抽取记忆字段（确定性、零 LLM）——类型/实体/时间/进度/三元组。

    ``user_input`` 为调用上下文保留参数（本轮用户输入），不参与规则判定。
    """
    raw = text or ""
    temporal_match = _TEMPORAL_RE.search(raw)
    entities = _extract_entities(raw)
    subject, predicate, obj = _extract_triple(raw, entities)
    return MemoryExtract(
        memory_type=_classify(raw),
        entities=entities,
        temporal=temporal_match.group(0) if temporal_match else None,
        source=source,
        progress=_extract_progress(raw),
        subject=subject,
        predicate=predicate,
        object_value=obj,
    )
