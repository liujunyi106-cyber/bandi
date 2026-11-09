# 功能：同类记忆语义合并（Jaccard 分组 + 确定性合并内容 + 批次原子落库）
# 参考：Hermes tools/memory_tool_store.py apply_batch（全批原子 + 最终态预算口径）
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

from bandi.memory.episodic import tokenize_cn
from bandi.memory.long_term import (
    RELIABILITY_DEFAULT,
    LongTermMemory,
    MemoryItem,
    _as_float,
    suppress_scope_miss,
)
from bandi.memory.structured import STRUCTURED_KINDS, is_weak_key

# 默认记忆类型（metadata 缺失 memory_type 时按 business_fact 归组）
DEFAULT_MEMORY_TYPE = "business_fact"
# 合并内容上限（字符）——与注入 snippet 预算解耦的硬上限
MERGE_CONTENT_MAX_CHARS = 2000
_SCAN_PAGE_SIZE = 500
# 审计/迁移前缀排除。`turn:` **不在排除表里**：逐轮对话快照是冗余的主要来源
# （每轮都写一条），正是合并要收拾的对象——但它另有两道保护（见 `_turn_merge_eligible`）。
# `consolidated:` 同理：整合后的长期条目与逐轮快照是同一份事实的两次落库，
# 保留两份会让「记忆×草稿去重」预期落空。
# `compaction:` 是压缩快照的内部产物，语义上不是可复用事实，仍排除。
EXCLUDED_ID_PREFIXES = ("compaction:",)
# `turn:` 记忆参与合并的最小沉淀天数——新近对话可能仍被后续轮次引用，过早合并
# 会把不同轮次的重要上下文糊成一条。
MIN_TURN_MERGE_AGE_DAYS = 7.0

_ASCII_WORD_RE = re.compile(r"[0-9A-Za-z_]+")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")
# 数值 token（含小数/百分号）——模板相同仅数值不同 = 实质差异，不得自动合并
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
# 中文数词字符（含大写与「两」）——连续两字以上才视为数词 token（排除「一起」等单字假阳性）
_CN_NUMERAL_CHARS = "零一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟"
# 中文数字 token：百分之X / 连续数词（五十、一万）/ X成（三成、八成）
_CN_NUMBER_RE = re.compile(
    rf"百分之[{_CN_NUMERAL_CHARS}]+"
    rf"|[{_CN_NUMERAL_CHARS}]{{2,}}"
    rf"|[{_CN_NUMERAL_CHARS}]+成"
)
# 成对引号内的专名（中英引号）——专名不同 = 实质差异，不得自动合并
_QUOTED_NAME_RE = re.compile(
    "「([^」]{1,64})」|『([^』]{1,64})』|“([^”]{1,64})”|‘([^’]{1,64})’"
    "|\"([^\"]{1,64})\"|'([^']{1,64})'"
)
# 合并时丢弃的瞬时 metadata 键（检索排序产物，不随合并落库）
_TRANSIENT_META_KEYS = frozenset({"_fts_rank"})


@dataclass
class MergeSuggestion:
    target_id: str
    source_ids: list[str]
    merged_content: str
    memory_type: str
    member_hashes: dict[str, str] = field(default_factory=dict)


def _content_hash(content: str) -> str:
    """功能：成员内容哈希（TOCTOU 快照比较）。"""
    return hashlib.sha1((content or "").encode("utf-8")).hexdigest()


_WHITESPACE_RE = re.compile(r"\s+")


def _squeeze(text: str) -> str:
    """功能：压掉全部空白——用于判断两条记忆是否只是排版差异。

    合并去重先前只做完全子串比对，于是「部署端口 7421」与「部署端口7421」会被当成
    两条不同陈述并列写进同一条记忆。中文里空白不承载语义，压掉后比较即可消掉
    这类纯排版变体。
    """
    return _WHITESPACE_RE.sub("", text or "")


def _number_tokens(content: str) -> tuple[str, ...]:
    """功能：数字 token 集合（去重排序）——ASCII 数字/百分号与中文数词形态；
    同一批数字的改写不构成实质差异，数字不同则拒绝自动合并。"""
    text = content or ""
    tokens = set(_NUMBER_RE.findall(text)) | set(_CN_NUMBER_RE.findall(text))
    return tuple(sorted(tokens))


def _quoted_names(content: str) -> tuple[str, ...]:
    """功能：成对引号内专名集合（去重排序）。"""
    names: list[str] = []
    for groups in _QUOTED_NAME_RE.findall(content or ""):
        names.extend(group for group in groups if group)
    return tuple(sorted(set(names)))


def _same_substance(left: str, right: str) -> bool:
    """功能：实质差异保护——数字 token 与引号专名集合任一不同即拒绝合并
    （模板相同仅数值替换/专名替换 = 语义不同，自动合并删源会丢事实）。"""
    return _number_tokens(left) == _number_tokens(right) and _quoted_names(left) == _quoted_names(
        right
    )


def _tokens(content: str) -> set[str]:
    """功能：相似度 token 集合——中文复用 tokenize_cn n-gram + ASCII 词（小写）。"""
    return {token.lower() for token in tokenize_cn(content)} | {
        word.lower() for word in _ASCII_WORD_RE.findall(content)
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    """功能：Jaccard 相似度（任一为空视为 0，不制造空集合并）。"""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _entity_names(metadata: dict) -> list[str]:
    """功能：metadata.entities 规整为去重排序字符串表（非列表形态忽略）。"""
    raw = metadata.get("entities")
    if not isinstance(raw, (list, tuple, set)):
        return []
    return sorted({str(name).strip() for name in raw if str(name).strip()})


def _merge_group_content(group: list[MemoryItem]) -> str:
    """功能：确定性合并内容——最长成员为底，其余不重复句子/实体名追加，总长 ≤2000。"""
    ordered = sorted(group, key=lambda item: item.id)
    base = min(ordered, key=lambda item: (-len(item.content), item.id))
    joined = base.content.strip()

    def _try_add(chunk: str) -> bool:
        nonlocal joined
        chunk = chunk.strip()
        if len(chunk) < 2:
            return False
        if _squeeze(chunk) in _squeeze(joined):
            return False
        if len(joined) + 1 + len(chunk) > MERGE_CONTENT_MAX_CHARS:
            return True
        joined = f"{joined}\n{chunk}"
        return False

    reached_cap = False
    for item in ordered:
        if item.id == base.id:
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(item.content):
            if _try_add(sentence):
                reached_cap = True
                break
        if reached_cap:
            break
    if not reached_cap:
        names = sorted({name for item in ordered for name in _entity_names(item.metadata)})
        for name in names:
            if _try_add(name):
                break
    return joined[:MERGE_CONTENT_MAX_CHARS]


# 写入侧去重键的 kind 前缀（与 structured.kind 同名，便于日志排查）
_DEDUPE_KEY_PREFIX = "struct"
_DEDUPE_FINGERPRINT_PREFIX = "fp"


def _structured_of(item: MemoryItem) -> dict:
    """功能：取条目的 structured 断言（非 dict 形态视为缺失）。"""
    raw = (item.metadata or {}).get("structured")
    return raw if isinstance(raw, dict) else {}


def dedupe_key(item: MemoryItem) -> str:
    """功能：写入侧去重键——有结构化断言时取 `normalize(kind + key)`，否则内容指纹。

    强 key（`export_format`/`port` 这类具体属性）即属性本身，足以判定「同一条
    事实的两次表述」。**弱 key**（`preference`/`fact`——属性词未命中时的兜底键，
    见 `structured.WEAK_KEYS`）不参与判定：它们只说明「这是条偏好/事实」，
    不说明是关于什么的，「偏好简洁」与「偏好详细」同属 `preference` 却是两回事，
    仅凭 kind+key 合并会吞掉一条事实——这类退回内容指纹。
    """
    structured = _structured_of(item)
    kind = str(structured.get("kind") or "").strip()
    key = str(structured.get("key") or "").strip().lower()
    if kind in STRUCTURED_KINDS and key and not is_weak_key(key):
        return f"{_DEDUPE_KEY_PREFIX}:{kind}:{key}"
    fingerprint = _content_hash(_squeeze(item.content or ""))
    return f"{_DEDUPE_FINGERPRINT_PREFIX}:{fingerprint}"


def _merge_evidence(existing: MemoryItem, incoming: MemoryItem) -> list:
    """功能：证据并集（去重保序，兼容字符串/列表两种历史形态）。"""
    collected: list[str] = []

    def _add(value: object) -> None:
        if isinstance(value, (list, tuple, set)):
            for element in value:
                text = str(element).strip()
                if text and text not in collected:
                    collected.append(text)
        else:
            text = str(value).strip()
            if text and text not in collected:
                collected.append(text)

    _add((existing.metadata or {}).get("evidence"))
    _add((incoming.metadata or {}).get("evidence"))
    return collected


def merge_on_write(
    long_term: LongTermMemory,
    incoming: MemoryItem,
    key: str,
    *,
    project_id: str | None = None,
    employee_id: str | None = None,
) -> MemoryItem | None:
    """功能：写入侧按去重键合并——命中同键已有条目时合并进它并返回该条目，
    无同键条目（或查询失败）返回 None，由调用方按正常写入处理。

    合并语义：importance 取高、内容以更长者为底并补入新陈述、证据并集、
    `merged_from` 记入新条目 id（与事后合并 `_merge_metadata` 同一谱系）。
    """
    try:
        existing_items = long_term.list_items(
            project_id=project_id, employee_id=employee_id, limit=_SCAN_PAGE_SIZE
        )
    except Exception:  # noqa: BLE001 — 去重是增强，查不到不得吞掉正常写入
        return None
    matches = [item for item in existing_items if dedupe_key(item) == key]
    if len(matches) != 1:
        # 0 条=无重复；≥2 条=库内本身已有重复，本次不猜合并目标（避免误并）
        return None
    target = matches[0]
    metadata = dict(target.metadata or {})
    metadata["memory_type"] = metadata.get("memory_type") or str(
        (incoming.metadata or {}).get("memory_type") or DEFAULT_MEMORY_TYPE
    )
    evidence = _merge_evidence(target, incoming)
    if evidence:
        metadata["evidence"] = evidence
    lineage = sorted(
        {*(metadata.get("merged_from") or []), *((target.metadata or {}).get("merged_from") or [])}
    )
    if target.id != incoming.id:
        lineage.append(incoming.id)
    if lineage:
        metadata["merged_from"] = sorted(set(lineage))
    merged = MemoryItem(
        item_id=target.id,
        content=_merge_group_content([target, incoming]),
        metadata=metadata,
        timestamp=target.timestamp,
        importance=max(target.importance, incoming.importance),
    )
    with suppress_scope_miss():
        long_term.store(merged, project_id=project_id, employee_id=employee_id)
    return merged


def _target_id(member_ids: list[str], kind: str = "other") -> str:
    """功能：稳定合并目标 id——成员 id 排序串的 sha1 前 12 位；kind 继承成员
    （非 other 时用 `<kind>:merged:` 前缀，保持分区语义）。"""
    digest = hashlib.sha1("|".join(sorted(member_ids)).encode("utf-8")).hexdigest()
    if kind != "other":
        return f"{kind}:merged:{digest[:12]}"
    return f"merged:{digest[:12]}"


def _list_all_items(
    long_term: LongTermMemory,
    *,
    project_id: str | None,
    employee_id: str | None,
) -> list[MemoryItem]:
    """功能：offset 翻页列全量候选——合并扫描面不得被 list_items 单页 limit 截断。"""
    items: list[MemoryItem] = []
    offset = 0
    while True:
        page = long_term.list_items(
            project_id=project_id,
            employee_id=employee_id,
            limit=_SCAN_PAGE_SIZE,
            offset=offset,
        )
        items.extend(page)
        if len(page) < _SCAN_PAGE_SIZE:
            return items
        offset += _SCAN_PAGE_SIZE


def _turn_merge_eligible(item: MemoryItem) -> bool:
    # 功能：`turn:` 记忆的合并资格——「未被召回 + 够老」双条件（其余记忆直接放行）。
    # 已被召回（metadata.recall_count > 0）说明它被读过/引用过，合并会破坏引用者上下文；
    # 未够老（距今 < MIN_TURN_MERGE_AGE_DAYS）可能仍被后续轮次引用。
    if not str(item.id).startswith("turn:"):
        return True
    recalled = _as_float((item.metadata or {}).get("recall_count"), 0.0)
    if recalled > 0:
        return False
    if item.timestamp is None:
        return True
    age_days = (time.time() - float(item.timestamp)) / 86400.0
    return age_days >= MIN_TURN_MERGE_AGE_DAYS


def suggest_merges(
    long_term: LongTermMemory,
    *,
    project_id: str | None = None,
    employee_id: str | None = None,
    similarity_threshold: float = 0.6,
    limit: int = 50,
) -> list[MergeSuggestion]:
    """功能：同 scope 同 memory_type 候选 Jaccard 分组，产出确定性合并建议；
    审计/迁移前缀（turn/compaction/consolidated）排除，数字与引号专名实质不同
    的候选拒绝入组。"""
    items = _list_all_items(long_term, project_id=project_id, employee_id=employee_id)
    by_type: dict[str, list[MemoryItem]] = {}
    for item in items:
        if not (item.content or "").strip():
            continue
        if item.id.startswith(EXCLUDED_ID_PREFIXES):
            continue
        if not _turn_merge_eligible(item):
            continue
        memory_type = str(item.metadata.get("memory_type") or DEFAULT_MEMORY_TYPE)
        by_type.setdefault(memory_type, []).append(item)

    suggestions: list[MergeSuggestion] = []
    for memory_type in sorted(by_type):
        members = sorted(by_type[memory_type], key=lambda item: item.id)
        token_sets = {item.id: _tokens(item.content) for item in members}
        parent = {item.id: item.id for item in members}

        def _find(member_id: str) -> str:
            while parent[member_id] != member_id:
                parent[member_id] = parent[parent[member_id]]
                member_id = parent[member_id]
            return member_id

        for index, left in enumerate(members):
            for right in members[index + 1 :]:
                if not token_sets[left.id] or not token_sets[right.id]:
                    continue
                if _jaccard(token_sets[left.id], token_sets[right.id]) < similarity_threshold:
                    continue
                if not _same_substance(left.content, right.content):
                    continue
                root_left, root_right = _find(left.id), _find(right.id)
                if root_left != root_right:
                    parent[max(root_left, root_right)] = min(root_left, root_right)

        groups: dict[str, list[MemoryItem]] = {}
        for item in members:
            groups.setdefault(_find(item.id), []).append(item)
        for root in sorted(groups):
            group = groups[root]
            if len(group) < 2:
                continue
            member_ids = sorted(item.id for item in group)
            kinds = {LongTermMemory._kind_of(item.id) for item in group}
            kind = kinds.pop() if len(kinds) == 1 else "other"
            suggestions.append(
                MergeSuggestion(
                    target_id=_target_id(member_ids, kind),
                    source_ids=member_ids,
                    merged_content=_merge_group_content(group),
                    memory_type=memory_type,
                    member_hashes={item.id: _content_hash(item.content) for item in group},
                )
            )
    suggestions.sort(key=lambda suggestion: suggestion.target_id)
    return suggestions[: max(0, int(limit))]


def _merge_metadata(members: list[MemoryItem], memory_type: str) -> dict:
    """功能：成员 metadata 并集——普通键按 id 序后者覆盖；entities 并集去重
    merged_from 递归展开去重、reliability 取 max、recall_count 求和
    last_recalled_at 取最新。"""
    metadata: dict = {}
    for member in sorted(members, key=lambda item: item.id):
        for key, value in (member.metadata or {}).items():
            if key in _TRANSIENT_META_KEYS:
                continue
            metadata[key] = value
    metadata["memory_type"] = memory_type

    entities: set[str] = set()
    for member in members:
        entities.update(_entity_names(member.metadata))
    if entities:
        metadata["entities"] = sorted(entities)

    lineage: set[str] = set()
    for member in members:
        lineage.add(member.id)
        raw = (member.metadata or {}).get("merged_from")
        if isinstance(raw, (list, tuple, set)):
            lineage.update(str(parent).strip() for parent in raw if str(parent).strip())
    metadata["merged_from"] = sorted(lineage)

    reliabilities = [
        _as_float((member.metadata or {}).get("reliability"), RELIABILITY_DEFAULT)
        for member in members
        if (member.metadata or {}).get("reliability") is not None
    ]
    if reliabilities:
        metadata["reliability"] = max(reliabilities)

    counted = [
        member for member in members if (member.metadata or {}).get("recall_count") is not None
    ]
    if counted:
        metadata["recall_count"] = sum(
            max(0, int(_as_float((member.metadata or {}).get("recall_count"), 0.0)))
            for member in counted
        )

    recalled = [
        str((member.metadata or {}).get("last_recalled_at"))
        for member in members
        if (member.metadata or {}).get("last_recalled_at")
    ]
    if recalled:
        metadata["last_recalled_at"] = max(recalled)
    return metadata


def apply_merges(
    long_term: LongTermMemory,
    suggestions: list[MergeSuggestion],
    *,
    stale_targets: list[str] | None = None,
) -> int:
    """功能：应用合并建议（target 写入 + 源删除，走 apply_batch 单事务）；源全部
    消失的建议直接跳过（重复应用无副作用）；成员内容与建议快照（member_hashes）
    不一致（TOCTOU）则跳过并计入 stale_targets，返回实际写入条数。"""
    batches: dict[tuple[str | None, str | None], tuple[list[MemoryItem], list[str]]] = {}
    for suggestion in suggestions:
        members: list[MemoryItem] = []
        stale = False
        for source_id in suggestion.source_ids:
            member = long_term.get(source_id)
            if member is None:
                continue
            expected = suggestion.member_hashes.get(source_id)
            if expected is not None and _content_hash(member.content) != expected:
                stale = True
                break
            members.append(member)
        if stale:
            if stale_targets is not None:
                stale_targets.append(suggestion.target_id)
            continue
        if not members:
            continue
        scope = long_term.scope_of(members[0].id)
        if scope is None:
            continue
        merged_item = MemoryItem(
            item_id=suggestion.target_id,
            content=suggestion.merged_content,
            metadata=_merge_metadata(members, suggestion.memory_type),
            timestamp=max(member.timestamp for member in members),
            importance=max(member.importance for member in members),
        )
        upserts, deletes = batches.setdefault(scope, ([], []))
        upserts.append(merged_item)
        deletes.extend(member.id for member in members)

    applied = 0
    for (project_id, employee_id), (upserts, deletes) in batches.items():
        with suppress_scope_miss():
            long_term.apply_batch(
                upserts=upserts,
                deletes=deletes,
                project_id=project_id,
                employee_id=employee_id,
            )
        applied += len(upserts)
    return applied


def is_semantic_duplicate(
    long_term: LongTermMemory,
    content: str,
    item_id: str,
    *,
    project_id: str | None = None,
    employee_id: str | None = None,
    similarity_threshold: float = 0.6,
    window: int = 50,
) -> bool:
    # 功能：跨轮语义去重——与同 scope 近期记忆高度相似即视为重复（写入侧防线）。
    # 与检索去重（读时省 token）、合并（事后收拾）互补：从源头少写重复条目。
    # 比对窗口 = 最近 window 条（timestamp DESC）；同 id（自身重放）跳过；
    # 取不到 store / 异常一律按「不重复」放行（去重失败不得吞掉正常写入）。
    # 截断到合并内容上限——tokenize 成本随文本线性增长，超长轮次的去重检查
    # 会把写入路径拖慢一个量级
    new_tokens = _tokens((content or "")[:MERGE_CONTENT_MAX_CHARS])
    if not new_tokens:
        return False
    try:
        recent = long_term.list_items(project_id=project_id, employee_id=employee_id, limit=window)
    except Exception:  # noqa: BLE001 — 去重是增强，失败不阻断写入
        return False
    new_len = len(new_tokens)
    for other in recent:
        if other.id == item_id:
            continue
        other_tokens = _tokens((other.content or "")[:MERGE_CONTENT_MAX_CHARS])
        # 量级预筛：Jaccard 上界 = min(len)/max(len)，差异过大直接跳过
        # （省掉大部分完整交集运算——超长轮次下的主要开销）
        if not other_tokens:
            continue
        if min(new_len, len(other_tokens)) / max(new_len, len(other_tokens)) < similarity_threshold:
            continue
        if _jaccard(new_tokens, other_tokens) >= similarity_threshold:
            return True
    return False


def merge_similar_memories(
    long_term: LongTermMemory,
    *,
    project_id: str | None = None,
    employee_id: str | None = None,
    similarity_threshold: float = 0.6,
    limit: int = 50,
    dry_run: bool = False,
) -> list[MergeSuggestion]:
    """功能：先建议后自动的合并入口——dry_run=True 只返回建议；否则应用并返回
    实际已应用的（target 已落库的）建议列表。"""
    suggestions = suggest_merges(
        long_term,
        project_id=project_id,
        employee_id=employee_id,
        similarity_threshold=similarity_threshold,
        limit=limit,
    )
    if dry_run or not suggestions:
        return suggestions
    applied = apply_merges(long_term, suggestions)
    if applied == len(suggestions):
        return suggestions
    return [
        suggestion for suggestion in suggestions if long_term.get(suggestion.target_id) is not None
    ]


__all__ = [
    "DEFAULT_MEMORY_TYPE",
    "MERGE_CONTENT_MAX_CHARS",
    "MergeSuggestion",
    "apply_merges",
    "is_semantic_duplicate",
    "merge_similar_memories",
    "suggest_merges",
]
