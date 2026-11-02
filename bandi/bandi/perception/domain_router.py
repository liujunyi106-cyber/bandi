# 功能：DomainRouter — 领域路由引擎（两级匹配 + 置信度评分）

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIDENCE_AUTO = 0.85
CONFIDENCE_CONFIRM = 0.60
_EXACT_MATCH_SCORE_CAP = 0.95
_EXACT_MATCH_SCORE_BASE = 0.7
_EXACT_MATCH_SCORE_INCREMENT = 0.05
_FUZZY_DISCOUNT_FACTOR = 0.8


@dataclass
class DomainDef:
    key: str
    label: str
    entity_types: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    synonyms: dict[str, str] = field(default_factory=dict)
    no_tools: bool = False


@dataclass
class DomainRoute:
    domain: DomainDef | None
    confidence: float
    action: str  # auto | confirm | clarify
    matched_keywords: list[str] = field(default_factory=list)
    is_level1: bool = False
    domain_key: str = ""


class YamlDomainConfigProvider:
    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = Path(__file__).parent / "domains.yaml"
        self._path = Path(path)
        self._domains: dict[str, DomainDef] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._domains = {}
            return
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, ValueError) as e:
            import logging

            logging.getLogger(__name__).warning(
                "failed to load domains.yaml: %s, using empty config", e
            )
            self._domains = {}
            return
        self._domains = {}
        for key, cfg in raw.get("domains", {}).items():
            self._domains[key] = DomainDef(
                key=key,
                label=cfg.get("label", key),
                entity_types=cfg.get("entity_types", []),
                allowed_tools=cfg.get("allowed_tools", []),
                keywords=cfg.get("keywords", []),
                synonyms=cfg.get("synonyms", {}),
                no_tools=cfg.get("no_tools", False),
            )

    def get_domains(self) -> dict[str, DomainDef]:
        return dict(self._domains)

    def get_domain(self, key: str) -> DomainDef | None:
        return self._domains.get(key)

    def reload(self) -> None:
        self._load()


def _levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[len(s2)]


def _fuzzy_keyword_score(kw: str, text: str) -> float:
    kw_lower = kw.lower()
    text_lower = text.lower()
    if len(kw_lower) < 2:
        return 0.0
    kw_len = len(kw_lower)
    best_sim = 0.0
    for i in range(max(1, len(text_lower) - kw_len + 1)):
        end = min(i + kw_len, len(text_lower))
        window = text_lower[i:end]
        if len(window) < 2:
            continue
        dist = _levenshtein_distance(kw_lower, window)
        max_len = max(kw_len, len(window))
        sim = max(0.0, 1.0 - dist / max_len) if max_len > 0 else 0.0
        best_sim = max(best_sim, sim)
    return best_sim


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", text.lower())


class DomainRouter:
    def __init__(self, config_provider: YamlDomainConfigProvider | None = None) -> None:
        self._provider = config_provider or YamlDomainConfigProvider()

    @property
    def config_provider(self) -> YamlDomainConfigProvider:
        return self._provider

    def route(self, text: str) -> DomainRoute:
        if not text or not text.strip():
            return DomainRoute(domain=None, confidence=0.0, action="clarify", domain_key="")

        domains = self._provider.get_domains()
        if not domains:
            return DomainRoute(domain=None, confidence=0.0, action="clarify", domain_key="")

        text_lower = text.lower()

        best_domain: DomainDef | None = None
        best_confidence = 0.0
        best_keywords: list[str] = []
        is_level1 = False

        for dd in domains.values():
            hits: list[str] = []
            all_kw = list(dd.keywords) + list(dd.synonyms.keys())
            for kw in all_kw:
                kw_lower = kw.lower()
                if kw_lower in text_lower:
                    hits.append(kw)
            if hits:
                score = min(
                    _EXACT_MATCH_SCORE_CAP,
                    _EXACT_MATCH_SCORE_BASE + _EXACT_MATCH_SCORE_INCREMENT * len(hits),
                )
                if score > best_confidence:
                    best_domain = dd
                    best_confidence = score
                    best_keywords = hits
                    is_level1 = True

        if not is_level1:
            for dd in domains.values():
                best_kw_sim = 0.0
                best_kw = ""
                for kw in dd.keywords:
                    sim = _fuzzy_keyword_score(kw, text_lower)
                    if sim > best_kw_sim:
                        best_kw_sim = sim
                        best_kw = kw
                if best_kw_sim > 0:
                    fuzzy_score = best_kw_sim * _FUZZY_DISCOUNT_FACTOR
                    if fuzzy_score > best_confidence:
                        best_domain = dd
                        best_confidence = fuzzy_score
                        best_keywords = [best_kw] if best_kw else []
                        is_level1 = False

        if best_confidence >= CONFIDENCE_AUTO:
            action = "auto"
        elif best_confidence >= CONFIDENCE_CONFIRM:
            action = "confirm"
        else:
            action = "clarify"

        if best_domain is None:
            return DomainRoute(domain=None, confidence=0.0, action="clarify", domain_key="")

        return DomainRoute(
            domain=best_domain,
            confidence=round(best_confidence, 2),
            action=action,
            matched_keywords=best_keywords,
            is_level1=is_level1,
            domain_key=best_domain.key,
        )
