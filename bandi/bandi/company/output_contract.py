# 功能：输出契约——交付物校验 + 路径合约 + alias 替换 + Runtime Artifact 工件（content_hash/delta）

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from bandi.company.work_item import WorkItem

_T = TypeVar("_T")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutputContract:
    deliverable_type: str = "general"
    required_keys: tuple[str, ...] = ()
    format_spec: str = "free"
    min_length: int = 0
    max_length: int = 0


# 功能：契约键同义词表——同一必填小节接受中英文等价写法
KEY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "summary": ("摘要", "总结", "结论"),
    "findings": ("发现", "问题", "评审发现"),
    "verdict": ("裁决", "结论", "判定"),
    "risks": ("风险",),
    "next_steps": ("下一步", "后续"),
}


@dataclass
class ValidationReport:
    valid: bool
    missing_keys: list[str] = field(default_factory=list)
    format_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _normalize_key_text(value: str) -> str:
    # 功能：键与正文比对前的归一——全半角折叠 + 大小写折叠
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _key_forms(key: str) -> tuple[str, ...]:
    # 功能：一个必填键连同其同义词的全部可接受写法
    normalized = _normalize_key_text(key).strip()
    if not normalized:
        return ()
    related = [normalized]
    for canonical, aliases in KEY_SYNONYMS.items():
        group = {_normalize_key_text(canonical), *(_normalize_key_text(item) for item in aliases)}
        if normalized in group:
            related.extend(sorted(group))
    forms: list[str] = []
    for form in related:
        for candidate in (form, form.replace("_", " ")):
            if candidate and candidate not in forms:
                forms.append(candidate)
    return tuple(forms)


def _text_has_key(normalized_text: str, form: str) -> bool:
    # 功能：单个写法是否出现在正文中——ASCII 按标识符边界、中文按包含判定
    if not form:
        return False
    if form.isascii():
        pattern = rf"(?<![a-z0-9_]){re.escape(form)}(?![a-z0-9_])"
        return re.search(pattern, normalized_text) is not None
    return form in normalized_text


def validate_output(output_text: str, contract: OutputContract) -> ValidationReport:
    missing_keys: list[str] = []
    format_errors: list[str] = []
    warnings: list[str] = []

    output_text = output_text or ""

    # 长度不对称是有意设计：min_length 是提示（短输出可接受）
    # max_length 是硬限（超长视为无效）——契约语义区分强弱约束。
    if contract.min_length > 0 and len(output_text) < contract.min_length:
        warnings.append(f"output length {len(output_text)} < min {contract.min_length}")

    if contract.max_length > 0 and len(output_text) > contract.max_length:
        format_errors.append(f"output length {len(output_text)} > max {contract.max_length}")

    if contract.required_keys:
        normalized_text = _normalize_key_text(output_text)
        for key in contract.required_keys:
            forms = _key_forms(key)
            if not any(_text_has_key(normalized_text, form) for form in forms):
                missing_keys.append(key)
        # 评审卡允许单行 JSON 裁决（review_bridge 的解析契约）作为等价交付形态，
        # 避免“契约要求 findings/verdict 小节、解析器却只认 JSON”互相判死。
        if contract.deliverable_type == "review" and _has_review_verdict(output_text):
            missing_keys = []

    if contract.format_spec == "markdown":
        _check_markdown(output_text, format_errors, warnings)
    elif contract.format_spec == "json":
        _check_json(output_text, format_errors)
    elif contract.format_spec == "code":
        _check_code_blocks(output_text, warnings)

    valid = not missing_keys and not format_errors
    return ValidationReport(
        valid=valid, missing_keys=missing_keys, format_errors=format_errors, warnings=warnings
    )


_FENCE_LINE_RE = re.compile(r"^[ \t]*(?P<run>`{3,})(?P<rest>.*)$")


def _fence_markers(text: str) -> list[int]:
    # 功能：收集行首缩进处的围栏标记长度，行内提及的反引号不计入
    markers: list[int] = []
    for line in text.splitlines():
        match = _FENCE_LINE_RE.match(line)
        if match is None:
            continue
        run = len(match.group("run"))
        if re.search(rf"`{{{run},}}", match.group("rest")):
            # 功能：同一行内既开又闭的单行围栏块按已闭合处理
            continue
        markers.append(run)
    return markers


def _has_unclosed_fence(markers: list[int]) -> bool:
    # 功能：围栏按出现顺序配对，开标记需遇到同长度或更长的闭标记
    open_length = 0
    for run in markers:
        if open_length == 0:
            open_length = run
        elif run >= open_length:
            open_length = 0
    return open_length != 0


def _check_markdown(text: str, errors: list[str], warnings: list[str]) -> None:
    if not text.strip():
        return
    lines = text.strip().split("\n")
    has_heading = any(line.strip().startswith("#") for line in lines)
    if not has_heading:
        warnings.append("markdown: no headings found")
    if _has_unclosed_fence(_fence_markers(text)):
        errors.append("markdown: unclosed code fence")


def _check_json(text: str, errors: list[str]) -> None:
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        errors.append(f"json: {e}")


def _has_review_verdict(text: str) -> bool:
    """功能：识别 JSON 裁决或结构化 verdict 行（评审契约的等价形态）。"""
    raw = str(text or "")
    for candidate in (raw.strip(), _json_object_slice(raw)):
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and str(
            payload.get("review_verdict") or ""
        ).strip().lower() in {
            "approve",
            "reject",
        }:
            return True
    return bool(
        re.search(
            r"(?im)^\s*(?:#{1,6}\s*)?(?:review[_ ])?verdict\s*[:：]?\s*(approve|reject)\b",
            raw,
        )
    )


def _json_object_slice(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return ""
    return raw[start : end + 1]


def _check_code_blocks(text: str, warnings: list[str]) -> None:
    markers = _fence_markers(text)
    if not markers:
        warnings.append("code: no code fences found")
        return
    if _has_unclosed_fence(markers):
        warnings.append("code: unclosed code fence")


def validate_work_item_output(
    item: WorkItem, contract: OutputContract | None = None
) -> ValidationReport:
    if contract is None:
        contract = _infer_contract(item)
    output_text = item.runtime_metadata.get("_last_output", "")
    return validate_output(output_text, contract)


def _infer_contract(item: WorkItem) -> OutputContract:
    turn_type = item.turn_type.value if hasattr(item.turn_type, "value") else ""
    from bandi.company.company_runtime_contract import _is_smalltalk_work_item, is_dispatch_ready

    if _is_smalltalk_work_item(
        {
            "title": item.title,
            "description": item.description,
            "metadata": dict(item.metadata or {}),
        }
    ):
        return OutputContract(format_spec="markdown")

    if is_dispatch_ready({"turn_type": turn_type, "metadata": dict(item.metadata or {})}):
        # 功能：派发轮（确定方案后、需要执行前）要求分工交付格式
        return OutputContract(
            deliverable_type="dispatch_plan",
            required_keys=("分工与执行链", "锁定目标与交付物"),
            format_spec="markdown",
        )
    contract_map: dict[str, OutputContract] = {
        # 功能：plan/intake（讨论/澄清轮）为轻契约——不因缺分工小节判失败
        "plan": OutputContract(deliverable_type="plan", format_spec="markdown"),
        "intake": OutputContract(deliverable_type="plan", format_spec="markdown"),
        "execute": OutputContract(deliverable_type="execution_result", min_length=1),
        "report": OutputContract(
            deliverable_type="report", required_keys=("summary",), format_spec="markdown"
        ),
        "review": OutputContract(
            deliverable_type="review", required_keys=("findings", "verdict"), format_spec="markdown"
        ),
        "deliver": OutputContract(deliverable_type="deliverable", format_spec="markdown"),
    }
    return contract_map.get(turn_type, OutputContract())


OUTPUT_CONTRACT_KEYS: tuple[str, ...] = (
    "workspace_root",
    "output_root",
    "target_output_dir",
    "requested_output_root",
    "output_root_aliases",
    "output_root_alias_map",
)

PATH_BLOCKER_TOKENS: tuple[str, ...] = (
    "operation not permitted",
    "permission denied",
    "sandbox",
    "not writable",
    "read-only",
    "readonly",
    "missing required path",
    "required root",
    "required path",
)

MAX_ALIAS_DISPLAY: int = 6


def normalize_path_text(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve(strict=False))
    except Exception as e:  # noqa: BLE001 - 路径规范化尽力而为，失败保留原样
        logger.debug("path normalize failed, keep raw: %s", e)
        return raw


def is_path_relative_to(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).expanduser().resolve(strict=False).relative_to(
            Path(root).expanduser().resolve(strict=False)
        )
        return True
    except ValueError:
        # 不相对或无法解析：视为不在 root 内。
        return False


def _collect_alias_entries(
    data: dict[str, Any], *, include_normalized: bool, output_root: str
) -> list[str]:
    collected: list[str] = []
    for item in list(data.get("output_root_aliases", []) or []):
        raw_alias = str(item or "").strip()
        normalized_alias = normalize_path_text(raw_alias)
        candidates = [normalized_alias, raw_alias] if include_normalized else [raw_alias]
        for alias in candidates:
            if alias and alias not in collected:
                collected.append(alias)
    requested = str(data.get("requested_output_root") or "").strip()
    normalized_requested = normalize_path_text(requested)
    candidates = [normalized_requested, requested] if include_normalized else [requested]
    for alias in candidates:
        if alias and alias != output_root and alias not in collected:
            collected.append(alias)
    return collected


def output_contract_metadata(source: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(source or {})
    output_root = normalize_path_text(data.get("output_root") or data.get("target_output_dir"))
    target_output_dir = normalize_path_text(data.get("target_output_dir") or output_root)
    workspace_root = normalize_path_text(
        data.get("workspace_root") or data.get("comms_workspace_root")
    )

    aliases = _collect_alias_entries(data, include_normalized=True, output_root=output_root)
    requested_output_root = normalize_path_text(
        str(data.get("requested_output_root") or "").strip()
    )

    alias_map: dict[str, str] = {}
    raw_map = data.get("output_root_alias_map", {})
    if isinstance(raw_map, dict):
        for key, value in raw_map.items():
            raw_alias = str(key or "").strip()
            alias = normalize_path_text(key)
            target = normalize_path_text(value)
            if raw_alias and target:
                alias_map[raw_alias] = target
            if alias and target:
                alias_map[alias] = target
    if output_root:
        for alias in aliases:
            if alias and alias != output_root:
                alias_map.setdefault(alias, output_root)

    contract: dict[str, Any] = {}
    if workspace_root:
        contract["workspace_root"] = workspace_root
    if output_root:
        contract["output_root"] = output_root
        contract["target_output_dir"] = target_output_dir or output_root
    elif target_output_dir:
        contract["target_output_dir"] = target_output_dir
    if requested_output_root:
        contract["requested_output_root"] = requested_output_root
    if aliases:
        contract["output_root_aliases"] = list(dict.fromkeys(aliases))
    if alias_map:
        contract["output_root_alias_map"] = alias_map
    return contract


def output_alias_map(source: dict[str, Any] | None) -> dict[str, str]:
    data = dict(source or {})
    contract = output_contract_metadata(data)
    aliases = dict(contract.get("output_root_alias_map", {}) or {})
    output_root = str(
        contract.get("output_root") or contract.get("target_output_dir") or ""
    ).strip()
    if not output_root:
        return aliases

    for raw_alias in _collect_alias_entries(
        data, include_normalized=False, output_root=output_root
    ):
        aliases.setdefault(raw_alias, output_root)

    raw_map = data.get("output_root_alias_map", {})
    if isinstance(raw_map, dict):
        for key, value in raw_map.items():
            raw_alias = str(key or "").strip()
            raw_target = str(value or "").strip()
            target = normalize_path_text(raw_target) or output_root
            if raw_alias and target:
                aliases.setdefault(raw_alias, target)
    return aliases


def replace_output_aliases(value: _T, source: dict[str, Any] | None) -> _T:
    aliases = output_alias_map(source)
    if not aliases:
        return value
    if isinstance(value, str):
        updated = value
        active = sorted(
            (
                (alias, target)
                for alias, target in aliases.items()
                if alias and target and alias != target and alias not in target
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        # 循环替换直到稳定：嵌套 alias（x → ab → D:/out）需多轮收敛。
        # 上限防循环 alias（x→y, y→x）振荡。
        for _ in range(MAX_ALIAS_DISPLAY + 1):
            changed = False
            for alias, target in active:
                if alias in updated:
                    updated = updated.replace(alias, target)
                    changed = True
            if not changed:
                break
        return updated
    if isinstance(value, list):
        return [replace_output_aliases(item, source) for item in value]
    if isinstance(value, tuple):
        return tuple(replace_output_aliases(item, source) for item in value)
    if isinstance(value, dict):
        return {key: replace_output_aliases(item, source) for key, item in value.items()}
    return value


def text_has_path_blocker(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(token in text for token in PATH_BLOCKER_TOKENS)


def render_output_contract_context(
    source: dict[str, Any] | None,
    *,
    heading: str = "## Runtime Output Contract",
    include_workspace_root: bool = True,
) -> str:
    contract = output_contract_metadata(source)
    output_root = str(contract.get("output_root", "") or "").strip()
    workspace_root = str(contract.get("workspace_root", "") or "").strip()
    requested = str(contract.get("requested_output_root", "") or "").strip()
    alias_map = dict(contract.get("output_root_alias_map", {}) or {})
    visible_workspace_root = workspace_root if include_workspace_root else ""
    if not any([output_root, visible_workspace_root, requested, alias_map]):
        return ""

    lines: list[str] = [heading]
    if output_root:
        lines.append(f"Canonical writable output root: {output_root}")
    if visible_workspace_root:
        lines.append(f"Workspace root: {visible_workspace_root}")
    if requested and requested != output_root:
        lines.append(f"Requested output alias: {requested}")
    if alias_map:
        lines.append("Path aliases:")
        for alias, target in list(alias_map.items())[:MAX_ALIAS_DISPLAY]:
            lines.append(f"- {alias} -> {target}")
        lines.append(
            "When old instructions mention an alias path, write and verify against the canonical output root."
        )
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------

RUNTIME_ARTIFACT_HEADER = "## Runtime Artifact:"
RUNTIME_ARTIFACT_DELTA_HEADER = "## Runtime Artifact Delta:"

ARTIFACT_TYPE_TOOL_SURFACE = "tool_surface"
ARTIFACT_TYPE_SKILLS = "skills"

# 功能：工件标题单源——渲染进 system 文本，上下文用量按标题归因（两处必须同值）
ARTIFACT_TITLE_TOOL_SURFACE = "可用工具"
ARTIFACT_TITLE_SKILLS = "技能"


@dataclass
class RuntimeArtifact:
    artifact_type: str
    title: str
    content: str
    scope: str = "runtime"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_record(self, *, content_hash: str) -> dict[str, Any]:
        return {
            "type": self.artifact_type,
            "title": self.title,
            "content": self.content,
            "scope": self.scope,
            "content_hash": content_hash,
            "metadata": dict(self.metadata),
        }


def artifact_content_hash(content: str, metadata: dict[str, Any] | None = None) -> str:
    raw = json.dumps(
        {
            "content": str(content or ""),
            "metadata": dict(metadata or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_runtime_artifact_manifest(
    artifacts: Iterable[RuntimeArtifact],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    manifest: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    for artifact in artifacts:
        content_hash = artifact_content_hash(artifact.content, artifact.metadata)
        manifest.append(artifact.to_record(content_hash=content_hash))
        hashes[artifact.artifact_type] = content_hash
    return manifest, hashes


def changed_artifacts(
    artifacts: Iterable[RuntimeArtifact],
    previous_hashes: dict[str, str] | None = None,
) -> list[tuple[RuntimeArtifact, bool]]:
    previous = dict(previous_hashes or {})
    changed: list[tuple[RuntimeArtifact, bool]] = []
    for artifact in artifacts:
        embedded = str(artifact.metadata.get("content_hash", "") or "")
        content_hash = embedded or artifact_content_hash(artifact.content, artifact.metadata)
        is_delta = bool(previous.get(artifact.artifact_type)) and (
            previous.get(artifact.artifact_type) != content_hash
        )
        changed.append((artifact, is_delta))
    return changed


def render_runtime_artifact_messages(
    artifacts: Iterable[RuntimeArtifact],
    *,
    previous_hashes: dict[str, str] | None = None,
    emit_delta_messages: bool = True,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for artifact, is_delta in changed_artifacts(artifacts, previous_hashes):
        header = (
            RUNTIME_ARTIFACT_DELTA_HEADER
            if emit_delta_messages and is_delta
            else RUNTIME_ARTIFACT_HEADER
        )
        messages.append(
            {
                "role": "system",
                "content": f"{header} {artifact.title}\n{artifact.content}".strip(),
            }
        )
    return messages
