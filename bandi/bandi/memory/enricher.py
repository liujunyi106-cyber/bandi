# 功能：记忆本体增强——实体抽取 + 图谱只读事实/矛盾查询
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# 功能：三类实体抽取正则——中文引号专名 / 路径 / 英文驼峰
_QUOTED_RE = re.compile(r"[「『“\"]([^「『“\"」』”\n]{1,60})[」』”\"]")
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s，。；;、「」'\"“”]{1,200}")
_POSIX_PATH_RE = re.compile(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+")
_CAMEL_RE = re.compile(r"\b[A-Za-z][a-z0-9]*(?:[A-Z][A-Za-z0-9]*)+\b")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_WHITESPACE_RE = re.compile(r"\s+")

_MAX_CONTRADICTION_CANDIDATES = 50
_MAX_CONTRADICTIONS = 5
_MIN_CONTRADICTION_CHARS = 20
_MAX_CONTRADICTION_JACCARD = 0.15


@dataclass
class EntityRef:
    """功能：记忆文本中抽取的实体引用（图谱只读查询的最小入参）。"""

    name: str
    entity_type: str = ""
    entity_id: str = ""


@runtime_checkable
class MemoryEnricher(Protocol):
    """功能：记忆增强只读接口——实现方只允许读，不得写任何存储。"""

    def extract_entities(self, text: str) -> list[EntityRef]: ...

    def find_facts(self, entities: list[EntityRef], *, limit: int = 5) -> list[str]: ...

    def find_contradictions(self, candidates: list[str]) -> list[tuple[str, str]]: ...


def _token_set(text: str) -> set[str]:
    """功能：相似度 token 集合——中文走 n-gram，英文/数字按词切分（纯符号为空）。"""
    from bandi.memory.episodic import tokenize_cn

    return {t.lower() for t in tokenize_cn(text)} | {w.lower() for w in _WORD_RE.findall(text)}


class EntityGraphEnricher:
    """功能：本体只读适配（`OntologyEntityLookup`）的包装——仅调用白名单只读 API（不写图、不建图）。"""

    def __init__(
        self,
        graph: Any,
        *,
        project_id: str | None = None,
        find_entities: Callable[..., Any] | None = None,
    ) -> None:
        self._graph = graph
        self._project_id = project_id
        self._find_entities = find_entities or getattr(graph, "find_entities", None)

    def extract_entities(self, text: str) -> list[EntityRef]:
        """功能：纯文本抽取三类实体（引号专名/路径/英文驼峰）——不访问图。"""
        if not isinstance(text, str):
            return []
        found: list[tuple[int, str]] = []
        for match in _QUOTED_RE.finditer(text):
            name = match.group(1).strip()
            if name:
                found.append((match.start(1), name))
        for regex in (_WINDOWS_PATH_RE, _POSIX_PATH_RE):
            for match in regex.finditer(text):
                found.append((match.start(), match.group(0).strip()))
        for match in _CAMEL_RE.finditer(text):
            found.append((match.start(), match.group(0)))
        refs: list[EntityRef] = []
        seen: set[str] = set()
        for _, name in sorted(found, key=lambda item: item[0]):
            key = _WHITESPACE_RE.sub("", name)
            if not key or key in seen:
                continue
            seen.add(key)
            refs.append(EntityRef(name=name))
        return refs

    def find_facts(self, entities: list[EntityRef], *, limit: int = 5) -> list[str]:
        """功能：实体名→图内事实文本（name 优先、description 回退），去重后总上限 limit；
        project_id 非空时经图查询透传作用域（默认 None 零行为）。"""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return []
        if not isinstance(entities, (list, tuple)) or self._find_entities is None:
            return []
        facts: list[str] = []
        seen: set[str] = set()
        for ref in entities:
            name = str(getattr(ref, "name", "") or "").strip()
            if not name:
                continue
            try:
                rows = self._query_entities(name, limit)
            except Exception as exc:  # noqa: BLE001 — 单实体查询失败不阻断增强
                logger.warning("enricher_find_entities_failed [query=%s]: %s", name, exc)
                continue
            if not isinstance(rows, (list, tuple)):
                continue
            for row in rows:
                text = self._row_text(row)
                if not text or text in seen:
                    continue
                seen.add(text)
                facts.append(text)
                if len(facts) >= limit:
                    return facts
        return facts

    def _query_entities(self, name: str, limit: int) -> Any:
        """功能：实体查询——作用域非空时透传 project_id（None 保持既有调用形态）。"""
        if self._project_id is None:
            return self._find_entities(query=name, limit=limit) or []
        return self._find_entities(query=name, limit=limit, project_id=self._project_id) or []

    @staticmethod
    def _row_text(row: Any) -> str:
        """功能：单行实体→事实文本（name 优先、description 回退；脏类型安全跳过）。"""
        for attr in ("name", "description"):
            text = str(getattr(row, attr, "") or "").strip()
            if text:
                return text
        return ""

    def find_contradictions(self, candidates: list[str]) -> list[tuple[str, str]]:
        """功能：共享实体但内容低相似（token Jaccard < 0.15）的对——上限 5 对。"""
        if not isinstance(candidates, (list, tuple)):
            return []
        texts = [
            str(candidate or "") for candidate in (candidates or [])[:_MAX_CONTRADICTION_CANDIDATES]
        ]
        entity_sets = [{ref.name for ref in self.extract_entities(text)} for text in texts]
        token_sets = [_token_set(text) for text in texts]
        pairs: list[tuple[str, str]] = []
        for i, left in enumerate(texts):
            if len(left) < _MIN_CONTRADICTION_CHARS or not entity_sets[i]:
                continue
            for j in range(i + 1, len(texts)):
                right = texts[j]
                if len(right) < _MIN_CONTRADICTION_CHARS:
                    continue
                if not entity_sets[i] & entity_sets[j]:
                    continue
                union = token_sets[i] | token_sets[j]
                if not union:
                    continue
                jaccard = len(token_sets[i] & token_sets[j]) / len(union)
                if jaccard < _MAX_CONTRADICTION_JACCARD:
                    pairs.append((left, right))
                    if len(pairs) >= _MAX_CONTRADICTIONS:
                        return pairs
        return pairs
