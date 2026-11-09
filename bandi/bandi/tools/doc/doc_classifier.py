# 功能：DocClassifierTool — 文档分类（rules/llm/hybrid 三法 + 置信度）

from __future__ import annotations

import re
from typing import Any

from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.doc._doc_utils import lazy_provider as _lazy_provider
from bandi.tools.fs_tool import FsTool

DOCUMENT_TYPES = [
    "invoice",
    "contract",
    "report",
    "resume",
    "letter",
    "memo",
    "proposal",
    "specification",
    "manual",
    "receipt",
    "form",
    "certificate",
    "agreement",
    "presentation",
    "spreadsheet",
    "other",
]

RULE_PATTERNS: dict[str, list[tuple[str, float]]] = {
    "invoice": [
        (r"(?i)\b(invoice|发票|bill\s+to|amount\s+due|total\s+amount)\b", 0.8),
        (r"(?i)\b(qty|quantity|unit\s+price|subtotal)\b", 0.5),
        (r"(?i)\b(payment\s+terms|due\s+date|invoice\s+(no|number|#))\b", 0.7),
    ],
    "contract": [
        (r"(?i)\b(contract|agreement|合同|hereby\s+agree|terms\s+and\s+conditions)\b", 0.8),
        (r"(?i)\b(party|whereas|witnesseth|甲方|乙方)\b", 0.6),
        (r"(?i)\b(effective\s+date|termination|breach|indemnify)\b", 0.5),
    ],
    "report": [
        (r"(?i)\b(report|报告|analysis|findings|executive\s+summary)\b", 0.7),
        (r"(?i)\b(conclusion|recommendation|methodology|abstract)\b", 0.5),
        (r"(?i)\b(figure\s+\d|table\s+\d|appendix)\b", 0.4),
    ],
    "resume": [
        (r"(?i)\b(resume|curriculum\s+vitae|cv|简历)\b", 0.9),
        (r"(?i)\b(education|experience|skills|objective)\b", 0.6),
        (r"(?i)\b(work\s+history|references|certifications)\b", 0.5),
    ],
    "letter": [
        (r"(?i)\b(dear\s+\w+|sincerely|regards|尊敬的|此致)\b", 0.7),
        (r"(?i)^(to|from|date|subject):\s*", 0.5),
    ],
    "memo": [
        (r"(?i)\b(memo|memorandum|备忘录)\b", 0.9),
        (r"(?i)^(to|from|date|subject|re):\s*", 0.6),
    ],
    "proposal": [
        (r"(?i)\b(proposal|提案|proposed|we\s+propose)\b", 0.8),
        (r"(?i)\b(scope\s+of\s+work|deliverables|timeline|budget)\b", 0.6),
    ],
    "specification": [
        (r"(?i)\b(specification|spec|规格|requirements)\b", 0.8),
        (r"(?i)\b(version|revision|dimensions|tolerance)\b", 0.5),
    ],
    "manual": [
        (r"(?i)\b(manual|guide|handbook|手册|instructions)\b", 0.8),
        (r"(?i)\b(step\s+\d|warning|caution|note:)\b", 0.5),
    ],
    "receipt": [
        (r"(?i)\b(receipt|收据|transaction|paid|thank\s+you)\b", 0.8),
        (r"(?i)\b(cash|card|change|subtotal)\b", 0.5),
    ],
    "form": [
        (r"(?i)\b(form|表格|please\s+fill|signature|checkbox)\b", 0.7),
        (r"(?i)_{3,}|\.{3,}", 0.4),
    ],
    "certificate": [
        (r"(?i)\b(certificate|certify|证书|awarded|completed)\b", 0.8),
        (r"(?i)\b(hereby|授予|valid\s+until|expiry)\b", 0.5),
    ],
    "agreement": [
        (r"(?i)\b(agreement|协议|mutual|parties\s+agree)\b", 0.8),
        (r"(?i)\b(terms|conditions|obligations|rights)\b", 0.5),
    ],
}

_LLM_CLASSIFICATION_PROMPT = """Classify the following document into one of these categories:
{categories}

Document text (first 2000 characters):
---
{text}
---

Respond with ONLY the category name in lowercase, nothing else.
If unsure, respond with "other".

Category:"""

_RULE_TEXT_CHARS = 5000
_LLM_TEXT_CHARS = 2000
_LLM_MAX_TOKENS = 50
_DEFAULT_CONFIDENCE_THRESHOLD = 0.6


class DocClassifierTool(FsTool):
    name: str = "doc_classifier"
    description: str = (
        "Classify documents into categories (invoice, contract, report, resume, letter, "
        "memo, proposal, specification, manual, receipt, form, certificate, agreement, "
        "presentation, spreadsheet, other) using rule-based patterns, LLM analysis, "
        "or hybrid (rules first, LLM when uncertain)."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Document text to classify"},
            "file_path": {
                "type": "string",
                "description": "Path to a text file to classify (alternative to text)",
            },
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "method": {
                "type": "string",
                "enum": ["rules", "llm", "hybrid"],
                "description": "Classification method: rules (patterns), llm (AI), hybrid (rules first, LLM if uncertain). Default hybrid",
            },
            "confidence_threshold": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Minimum confidence for rules (default 0.6)",
            },
            "custom_categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Custom category list to use instead of defaults (LLM method)",
            },
        },
        "required": [],
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = True
    risk_level: str = "low"

    def __init__(self, provider: Any | None = None) -> None:
        super().__init__()
        self._provider = provider

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        try:
            text = params.get("text")
            file_path = params.get("file_path")
            method = params.get("method", "hybrid")
            threshold = float(params.get("confidence_threshold", _DEFAULT_CONFIDENCE_THRESHOLD))
            custom_categories = params.get("custom_categories")

            if not text and not file_path:
                return {"success": False, "error": "either text or file_path must be provided"}
            if file_path:
                path = self._resolve_safe_path(file_path)
                if self._is_sensitive(path):
                    return {"success": False, "error": f"sensitive path rejected: {file_path}"}
                if not path.is_file():
                    return {"success": False, "error": f"file not found: {file_path}"}
                text = path.read_text(encoding="utf-8", errors="ignore")

            if not text or not str(text).strip():
                return {
                    "success": True,
                    "category": "other",
                    "confidence": 0.0,
                    "method_used": method,
                    "warning": "empty or unreadable document",
                }

            categories = custom_categories or DOCUMENT_TYPES

            if method == "rules":
                return self._classify_with_rules(str(text), threshold)
            if method == "llm":
                return await self._classify_with_llm(str(text), categories)
            rules_result = self._classify_with_rules(str(text), threshold)
            if rules_result["confidence"] >= threshold:
                rules_result["method_used"] = "rules"
                return rules_result
            provider = _lazy_provider(self._provider)
            if provider is None or str(type(provider)).endswith("MockProvider"):
                rules_result["method_used"] = "rules (fallback)"
                rules_result["warning"] = (
                    "rule-based confidence below threshold and no LLM available; using best rule-based guess"
                )
                return rules_result
            llm_result = await self._classify_with_llm(str(text), categories)
            llm_result["rules_suggestion"] = rules_result["category"]
            llm_result["rules_confidence"] = rules_result["confidence"]
            return llm_result
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}

    def _classify_with_rules(self, text: str, threshold: float) -> dict[str, Any]:
        sample = text[:_RULE_TEXT_CHARS]
        scores: dict[str, float] = {cat: 0.0 for cat in RULE_PATTERNS}
        matches: dict[str, list[str]] = {cat: [] for cat in RULE_PATTERNS}

        for category, patterns in RULE_PATTERNS.items():
            for pattern, weight in patterns:
                found = re.findall(pattern, sample)
                if found:
                    scores[category] += weight * min(len(found), 3) / 3
                    matches[category].extend(found[:3])

        if not any(scores.values()):
            return {
                "success": True,
                "category": "other",
                "confidence": 0.0,
                "method_used": "rules",
                "scores": scores,
            }

        max_score = max(scores.values())
        if max_score > 0:
            scores = {k: v / (max_score * 1.2) for k, v in scores.items()}
        best_category = max(scores, key=lambda k: scores[k])
        confidence = min(scores[best_category], 1.0)
        top_categories = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:3]

        result: dict[str, Any] = {
            "success": True,
            "category": best_category,
            "confidence": round(confidence, 3),
            "method_used": "rules",
            "top_matches": [
                {"category": cat, "score": round(score, 3)} for cat, score in top_categories
            ],
        }
        if matches.get(best_category):
            result["matched_keywords"] = list(set(matches[best_category]))[:5]
        return result

    async def _classify_with_llm(self, text: str, categories: list[str]) -> dict[str, Any]:
        provider = _lazy_provider(self._provider)
        if provider is None:
            return {
                "success": True,
                "category": "other",
                "confidence": 0.0,
                "method_used": "llm (failed)",
                "error": "LLM provider not available",
            }
        prompt = _LLM_CLASSIFICATION_PROMPT.format(
            categories=", ".join(categories), text=text[:_LLM_TEXT_CHARS]
        )
        try:
            from bandi.llm.accounted import accounted_complete

            response = await accounted_complete(
                provider,
                [{"role": "user", "content": prompt}],
                label="doc_classify",
                session_id="doc_classify",
                max_tokens=_LLM_MAX_TOKENS,
            )
            predicted = (response.content or "").strip().lower()
            if predicted not in categories:
                for cat in categories:
                    if cat in predicted:
                        predicted = cat
                        break
                else:
                    predicted = "other"
            return {
                "success": True,
                "category": predicted,
                "confidence": 0.85,
                "method_used": "llm",
            }
        except Exception as e:  # noqa: BLE001
            return {
                "success": True,
                "category": "other",
                "confidence": 0.0,
                "method_used": "llm (failed)",
                "error": str(e),
            }
