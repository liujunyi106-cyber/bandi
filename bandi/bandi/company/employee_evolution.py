# 功能：员工演进——经验追踪 / 经验评分 / 自演进事件档案

"""Employee experience tracking and scoring.

部分成功/失败计数 + 角色/领域/模式维度，提供经验评分与自演进事件档案；
技能沉淀走手动导入（``SkillImporter``），不再从项目反思自动晋升。

适配：
- OpenOPC ``PreferenceManager``/``SkillLibrary`` → Bandi ``SkillImporter``
  （技能库写入）+ 无偏好写入（evolution 状态独立于 memory/*.md）；
- ``employee_assignment`` metadata 语义保留（OrgEmployee 招募时写入）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_EXPERIENCE_SCHEMA_VERSION = 1
_EVENTS_LIMIT = 100


# 组织经验档案的缺省组织 id——本地单组织产品口径（与 session_scoping 的
# "default" 归属约定一致）；多组织部署时由调用方显式传入。
DEFAULT_ORGANIZATION_ID = "default"


def _quality_dimensions_prompt(*, verdict: str, reason: str, rework: str, failure: str) -> str:
    """功能：质量维度抽取提示词——只回 JSON（strengths/weaknesses/rationale）。"""
    return (
        "你是员工经验评审助手。根据以下评审/返工/失败信号，抽取经验质量维度。\n"
        f"评审结论：{verdict or '无'}\n评审理由：{reason or '无'}\n"
        f"返工反馈：{rework or '无'}\n失败原因：{failure or '无'}\n"
        '只回 JSON：{"strengths": ["..."], "weaknesses": ["..."], "rationale": "一句话"}'
    )


def _parse_quality_dimensions(raw: Any) -> dict[str, Any] | None:
    """功能：解析 LLM 质量维度 JSON——缺字段/非法一律 None（回落规则抽取）。"""
    import json as _json

    text = str(raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = _json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    strengths = [str(x).strip() for x in (data.get("strengths") or []) if str(x).strip()]
    weaknesses = [str(x).strip() for x in (data.get("weaknesses") or []) if str(x).strip()]
    return {
        "strengths": strengths[:6],
        "weaknesses": weaknesses[:6],
        "feedback_summary": str(data.get("rationale") or "").strip()[:200],
        "rationale": str(data.get("rationale") or "llm").strip()[:200],
    }


def extract_quality_dimensions(
    metadata: dict[str, Any] | None, *, llm: Callable[[str], str] | None = None
) -> dict[str, Any]:
    """功能：从评审/返工/失败信号抽取经验质量维度（strengths/weaknesses/rationale）。

    llm 注入接口（同步 callable，返回 JSON 文本）优先，失败回落规则抽取——信号源
    本身就是评审结论（structured_review_verdict.reason / rework_feedback /
    failure_reason），规则路径零延迟、零调用成本。
    """
    meta = dict(metadata or {})
    verdict_info = meta.get("structured_review_verdict")
    verdict = ""
    reason = ""
    if isinstance(verdict_info, dict):
        verdict = str(verdict_info.get("verdict") or "").strip()
        reason = str(verdict_info.get("reason") or "").strip()
    rework_feedback = str(meta.get("rework_feedback") or "").strip()
    failure_reason = str(meta.get("failure_reason") or "").strip()

    if llm is not None:
        try:
            parsed = _parse_quality_dimensions(
                llm(
                    _quality_dimensions_prompt(
                        verdict=verdict,
                        reason=reason,
                        rework=rework_feedback,
                        failure=failure_reason,
                    )
                )
            )
            if parsed is not None:
                return parsed
        except Exception:
            logger.debug("quality dimension llm extraction failed", exc_info=True)

    strengths = [reason] if verdict == "approve" and reason else []
    weaknesses: list[str] = []
    for item in ((reason if verdict == "rework" else ""), rework_feedback, failure_reason):
        if item and item not in weaknesses:
            weaknesses.append(item)
    if verdict:
        rationale = f"review:{verdict}"
    elif rework_feedback:
        rationale = "rework"
    elif failure_reason:
        rationale = "failure"
    else:
        rationale = ""
    return {
        "strengths": strengths[:6],
        "weaknesses": weaknesses[:6],
        "feedback_summary": (reason or rework_feedback or failure_reason)[:200],
        "rationale": rationale,
    }


def _resolve_task_project_id(task: Any) -> str:
    """功能：解析任务的项目归属——属性优先，回落 `metadata["project_id"]`。

    注意：`WorkItem` **没有** `project_id` 属性（项目归属由 `metadata["project_id"]`
    承载，见 `company/work_item.py`），只用 `getattr` 会恒空转。
    """
    direct = str(getattr(task, "project_id", "") or "").strip()
    if direct:
        return direct
    metadata = dict(getattr(task, "metadata", None) or {})
    return str(metadata.get("project_id") or "").strip()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _normalize_outcome(outcome: str) -> str:
    normalized = str(outcome or "").strip().lower()
    if normalized in {"success", "approved", "fully_approved", "complete_success", "done"}:
        return "success"
    if normalized in {"failure", "failed", "rejected", "fully_rejected", "cancelled"}:
        return "failure"
    return "partial_success"


def _safe_employee_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-")
    return safe or "employee"


def _bounded_float(value: Any, *, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _string_list(value: Any, *, limit: int = 8) -> list[str]:
    items = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    unique: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in unique:
            unique.append(text)
    return unique


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        elif isinstance(value, list) and isinstance(merged.get(key), list):
            existing = list(merged[key])
            for item in value:
                if item not in existing:
                    existing.append(item)
            merged[key] = existing
        else:
            merged[key] = value
    return merged


class EmployeeEvolutionManager:
    """追踪员工经验与经验评分。"""

    def __init__(self, home_dir: str | Path | None = None, skills_dir: str | None = None) -> None:
        self.home = Path(home_dir) if home_dir is not None else Path.home()
        self._skills_dir = skills_dir

    # -- 存储路径 --

    def evolution_profile_path(self, project_id: str | None = None) -> Path:
        """全局或项目级演进状态路径（evolution 状态独立于 memory/*.md）。"""
        project = str(project_id or "").strip()
        if project:
            return self.home / "projects" / project / "employee_evolution.json"
        return self.home / "company" / "evolution" / "employees.json"

    def employee_experience_path(self, organization_id: str, employee_id: str) -> Path:
        org_id = _safe_employee_filename(organization_id)
        safe_id = _safe_employee_filename(employee_id)
        return (
            self.home / "company" / "company_state" / org_id / "employee_experience"
        ) / f"{safe_id}.json"

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            # 静默返回 {}，后续追加+覆盖写回会把全部历史清空
            try:
                corrupt = path.with_name(path.name + ".corrupt")
                os.replace(str(path), str(corrupt))
                logger.warning("corrupt profile quarantined: %s -> %s", path, corrupt)
            except OSError:
                logger.exception("failed to quarantine corrupt profile %s", path)
            return {}

    def _save_json(self, path: Path, data: dict[str, Any]) -> None:
        # 截断 JSON（截断文件会在下次加载时触发上面的隔离路径）
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(dict(data or {}), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(str(tmp_path), str(path))

    # -- 档案读写 --

    def load_evolution_profile(self, project_id: str | None = None) -> dict[str, Any]:
        return self._load_json(self.evolution_profile_path(project_id))

    def save_evolution_profile(
        self, profile: dict[str, Any], project_id: str | None = None
    ) -> None:
        self._save_json(self.evolution_profile_path(project_id), profile)

    def load_employee_experience(self, organization_id: str, employee_id: str) -> dict[str, Any]:
        return self._load_json(self.employee_experience_path(organization_id, employee_id))

    def save_employee_experience(
        self, organization_id: str, employee_id: str, profile: dict[str, Any]
    ) -> None:
        self._save_json(self.employee_experience_path(organization_id, employee_id), profile)

    def get_employee_profile(
        self, employee_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        global_profile = self.load_evolution_profile()
        project_profile = self.load_evolution_profile(project_id) if project_id else {}
        global_data = dict(global_profile.get("employees", {}).get(employee_id, {}))
        project_data = dict(project_profile.get("employees", {}).get(employee_id, {}))
        return _deep_merge(global_data, project_data)

    def get_learned_skill_refs(self, employee_id: str, project_id: str | None = None) -> list[str]:
        profile = self.get_employee_profile(employee_id, project_id)
        refs: list[str] = []
        for ref in list(profile.get("learned_skill_refs", [])):
            if ref and ref not in refs:
                refs.append(ref)
        return refs

    def sync_org_employee_metadata(
        self,
        org_engine: Any,
        employee_id: str,
        *,
        project_id: str | None = None,
        organization_id: str = DEFAULT_ORGANIZATION_ID,
    ) -> bool:
        """功能：把经验分与学到的技能写回组织员工 metadata——招募读取
        （`experience_score`/`learned_skill_refs`）与前端档案（`to_dict`）同源。

        员工不存在/引擎不支持返回 False；写盘经 `register_employee`（引擎持久化
        回调）。学到的技能仅在非空时写入（不覆盖手工维护值）。
        """
        getter = getattr(org_engine, "get_employee", None)
        register = getattr(org_engine, "register_employee", None)
        if not callable(getter) or not callable(register):
            return False
        employee = getter(str(employee_id or "").strip())
        if employee is None:
            return False
        role_id = str(getattr(employee, "role_id", "") or "")
        domains = list(getattr(employee, "domains", None) or [])
        score = self.get_experience_score(
            employee_id,
            role_id,
            domains=domains,
            project_id=project_id,
            organization_id=organization_id,
        )
        refs = self.get_learned_skill_refs(employee_id, project_id)
        metadata = dict(getattr(employee, "metadata", None) or {})
        metadata["experience_score"] = round(float(score), 3)
        if refs:
            metadata["learned_skill_refs"] = refs
        employee.metadata = metadata
        register(employee)
        return True

    def build_employee_delta_context(
        self,
        employee_id: str,
        project_id: str | None = None,
        organization_id: str | None = None,
    ) -> str:
        """渲染员工 delta 档案为提示上下文（strengths/adjustments/watchouts 等）。"""
        profile = self.get_employee_profile(employee_id, project_id)
        delta = dict(profile.get("delta_profile", {}))
        experience_profile: dict[str, Any] = {}
        experience_delta: dict[str, Any] = {}
        if organization_id:
            experience_profile = self.load_employee_experience(organization_id, employee_id)
            experience_delta = dict(experience_profile.get("delta_profile", {}) or {})
        if not delta and not experience_delta:
            return ""

        parts: list[str] = []
        evolution_count = int(
            experience_profile.get("evolution_count", 0)
            or len(experience_profile.get("events", []) or [])
        )
        if evolution_count:
            parts.append(f"Self-evolution reviews: {evolution_count}")
        projects_reflected = int(profile.get("projects_reflected", 0))
        if projects_reflected:
            parts.append(f"Reflected projects: {projects_reflected}")

        for title, key in (
            ("Self-Evolved Strengths", "strengths"),
            ("Self-Evolved Adjustments", "adjustments"),
            ("Self-Evolved Watchouts", "avoid_next_time"),
            ("Self-Evolved Routing Notes", "routing_notes"),
        ):
            values = [
                str(item).strip()
                for item in list(experience_delta.get(key, []) or [])
                if str(item).strip()
            ]
            if values:
                parts.append(f"## {title}\n" + "\n".join(f"- {item}" for item in values[:6]))

        for title, key in (
            ("Working Patterns", "working_patterns"),
            ("Default Checklists", "default_checklists"),
            ("Reviewer Preferences", "reviewer_preferences"),
            ("Risk Watchouts", "risk_watchouts"),
            ("Tool Preferences", "tool_preferences"),
            ("Fit Domains", "fit_domains"),
            ("Avoid Domains", "avoid_domains"),
        ):
            values = [str(item).strip() for item in delta.get(key, []) if str(item).strip()]
            if values:
                parts.append(f"## {title}\n" + "\n".join(f"- {item}" for item in values[:6]))

        return "\n\n".join(parts)

    # -- 经验评分 --

    def get_experience_score(
        self,
        employee_id: str,
        role_id: str,
        domains: list[str] | None = None,
        project_id: str | None = None,
        organization_id: str | None = None,
    ) -> float:
        """经验评分：全局 + 角色 + 领域 + learned skill + 反思 + 自演进事件。"""
        profile = self.get_employee_profile(employee_id, project_id)
        total_successes = int(profile.get("successes", 0))
        total_partials = int(profile.get("partial_successes", 0))
        total_failures = int(profile.get("failures", 0))
        role = dict(profile.get("roles", {}).get(role_id, {}) or {})
        role_successes = int(role.get("successes", 0))
        role_partials = int(role.get("partial_successes", 0))
        role_failures = int(role.get("failures", 0))
        learned_bonus = 2 * len(profile.get("learned_skill_refs", []))
        reflection_bonus = min(4, int(profile.get("projects_reflected", 0)))
        domain_bonus = 0.0
        for domain in domains or []:
            domain_record = dict(profile.get("domains", {}).get(domain, {}) or {})
            domain_bonus += int(domain_record.get("successes", 0))
            domain_bonus += 0.5 * int(domain_record.get("partial_successes", 0))
            domain_bonus -= 0.25 * int(domain_record.get("failures", 0))
        score = (
            total_successes
            + (0.5 * total_partials)
            - (0.25 * total_failures)
            + (2 * role_successes)
            + role_partials
            - (0.5 * role_failures)
            + domain_bonus
            + learned_bonus
            + reflection_bonus
        )
        if organization_id:
            experience_profile = self.load_employee_experience(organization_id, employee_id)
            events = [
                item
                for item in list(experience_profile.get("events", []) or [])
                if isinstance(item, dict)
            ]
            score += min(8.0, float(len(events)))
            for event in events[-8:]:
                if str(event.get("role_id", "") or "").strip() == role_id:
                    score += 0.5
        return float(max(0.0, score))

    # -- 工作项完成记录 --

    def record_work_item_completion(
        self,
        task: Any,
        result_content: str,
        *,
        outcome: str = "success",
        feedback_summary: str = "",
        strengths: list[str] | None = None,
        weaknesses: list[str] | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        """记录员工完成工作项的经验（全局 + 项目双写）。"""
        metadata = dict(getattr(task, "metadata", {}) or {})
        assignment = dict(metadata.get("employee_assignment", {}) or {})
        employee_id = str(assignment.get("employee_id", "")).strip()
        if not employee_id:
            return {}

        role_id = str(
            assignment.get("role_id")
            or getattr(task, "assignee_role", "")
            or getattr(task, "assigned_to", "")
            or metadata.get("work_item_role_id", "")
        ).strip()
        domains = list(assignment.get("domains") or getattr(task, "tags", []) or [])
        project_id = (
            _resolve_task_project_id(task)
            or str(assignment.get("project_id") or "").strip()
            or None
        )
        summary = str(
            metadata.get("work_item_summary_for_downstream", "") or result_content
        ).strip()
        pattern_key = self._pattern_key(role_id, domains)
        normalized_outcome = _normalize_outcome(outcome)
        base_payload = {
            "employee_id": employee_id,
            "employee_name": assignment.get("name", ""),
            "role_id": role_id,
            "template_id": assignment.get("template_id", ""),
            "category": assignment.get("category", ""),
        }

        global_profile = self.load_evolution_profile()
        self._record_in_profile(
            global_profile,
            base_payload=base_payload,
            role_id=role_id,
            domains=domains,
            pattern_key=pattern_key,
            summary=summary,
            outcome=normalized_outcome,
            feedback_summary=feedback_summary,
            strengths=list(strengths or []),
            weaknesses=list(weaknesses or []),
            rationale=rationale,
        )
        self.save_evolution_profile(global_profile)

        if project_id:
            project_profile = self.load_evolution_profile(project_id)
            self._record_in_profile(
                project_profile,
                base_payload=base_payload,
                role_id=role_id,
                domains=domains,
                pattern_key=pattern_key,
                summary=summary,
                outcome=normalized_outcome,
                feedback_summary=feedback_summary,
                strengths=list(strengths or []),
                weaknesses=list(weaknesses or []),
                rationale=rationale,
            )
            self.save_evolution_profile(project_profile, project_id)

        return {
            "employee_id": employee_id,
            "project_id": project_id or "",
            "pattern_key": pattern_key,
            "outcome": normalized_outcome,
        }

    # -- 自演进事件 --

    def apply_employee_evolution_patch(
        self,
        *,
        organization_id: str,
        patch: dict[str, Any],
        source: dict[str, Any] | None = None,
        allowed_employee_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """记录员工自演进事件（checkpoint 反思产出），event_id 幂等去重。"""
        source_payload = dict(source or {})
        raw_events = list(patch.get("patches", []) or [])
        allowed = {
            str(item).strip() for item in (allowed_employee_ids or set()) if str(item).strip()
        }
        recorded: list[dict[str, Any]] = []

        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                continue
            employee_id = str(raw_event.get("employee_id", "") or "").strip()
            if not employee_id or (allowed and employee_id not in allowed):
                continue
            event = self._normalize_self_evolution_event(raw_event, source_payload)
            if not event:
                continue

            profile = self.load_employee_experience(organization_id, employee_id)
            if not profile:
                profile = {
                    "schema_version": _EXPERIENCE_SCHEMA_VERSION,
                    "kind": "company_employee_experience",
                    "organization_id": organization_id,
                    "employee_id": employee_id,
                    "events": [],
                    "delta_profile": {},
                    "evolution_count": 0,
                }
            profile["schema_version"] = _EXPERIENCE_SCHEMA_VERSION
            profile["kind"] = "company_employee_experience"
            profile["organization_id"] = organization_id
            profile["employee_id"] = employee_id
            events = [
                item for item in list(profile.get("events", []) or []) if isinstance(item, dict)
            ]
            event_id = str(event.get("event_id", "") or "").strip()
            if event_id and any(
                str(item.get("event_id", "") or "").strip() == event_id for item in events
            ):
                continue
            events.append(event)
            profile["events"] = events[-_EVENTS_LIMIT:]
            profile["evolution_count"] = int(profile.get("evolution_count", 0) or 0) + 1
            profile["updated_at"] = _utc_now()
            profile["delta_profile"] = self._build_self_evolution_delta(profile["events"])
            self.save_employee_experience(organization_id, employee_id, profile)
            recorded.append(
                {
                    "employee_id": employee_id,
                    "event_id": event_id,
                    "status": "recorded",
                    "path": str(self.employee_experience_path(organization_id, employee_id)),
                }
            )

        return recorded

    def _normalize_self_evolution_event(
        self,
        event: dict[str, Any],
        source: dict[str, Any],
    ) -> dict[str, Any]:
        employee_id = str(event.get("employee_id", "") or "").strip()
        if not employee_id:
            return {}
        event_id = str(event.get("event_id", "") or "").strip()
        if not event_id:
            checkpoint_id = str(source.get("checkpoint_id", "") or "").strip()
            role_id = str(event.get("role_id", "") or "").strip()
            nonce = json.dumps(event, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:12]
            event_id = _safe_employee_filename(
                f"{checkpoint_id or _utc_now()}-{employee_id}-{role_id}-{digest}"
            )
        return {
            "event_id": event_id,
            "employee_id": employee_id,
            "role_id": str(event.get("role_id", "") or "").strip(),
            "summary": str(event.get("summary", "") or "").strip(),
            "strengths": _string_list(event.get("strengths", []), limit=8),
            "adjustments": _string_list(event.get("adjustments", event.get("adjust", [])), limit=8),
            "avoid_next_time": _string_list(
                event.get("avoid_next_time", event.get("avoid", [])), limit=8
            ),
            "routing_notes": _string_list(event.get("routing_notes", []), limit=8),
            "evidence_task_ids": _string_list(event.get("evidence_task_ids", []), limit=12),
            "confidence": _bounded_float(event.get("confidence", 0.7), default=0.7),
            "source": dict(source or {}),
            "created_at": _utc_now(),
        }

    def _build_self_evolution_delta(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        counts: dict[str, dict[str, int]] = {
            "strengths": {},
            "adjustments": {},
            "avoid_next_time": {},
            "routing_notes": {},
        }
        for event in events:
            if not isinstance(event, dict):
                continue
            for key in counts:
                for item in _string_list(event.get(key, []), limit=20):
                    counts[key][item] = int(counts[key].get(item, 0)) + 1
        return {
            "strengths": self._rank_items(counts["strengths"], limit=8),
            "adjustments": self._rank_items(counts["adjustments"], limit=8),
            "avoid_next_time": self._rank_items(counts["avoid_next_time"], limit=8),
            "routing_notes": self._rank_items(counts["routing_notes"], limit=8),
        }

    # -- 内部工具 --

    def _record_in_profile(
        self,
        profile: dict[str, Any],
        *,
        base_payload: dict[str, Any],
        role_id: str,
        domains: list[str],
        pattern_key: str,
        summary: str,
        outcome: str,
        feedback_summary: str,
        strengths: list[str],
        weaknesses: list[str],
        rationale: str,
    ) -> None:
        employees = profile.setdefault("employees", {})
        record = employees.setdefault(base_payload["employee_id"], {})
        record.update(
            {
                "employee_name": base_payload.get("employee_name", ""),
                "role_id": role_id,
                "template_id": base_payload.get("template_id", ""),
                "category": base_payload.get("category", ""),
                "updated_at": _utc_now(),
            }
        )
        self._increment_outcome_counts(record, outcome)
        record["last_outcome"] = outcome
        if feedback_summary:
            record["last_feedback_summary"] = feedback_summary
        if rationale:
            record["last_feedback_rationale"] = rationale

        roles = record.setdefault("roles", {})
        role_record = roles.setdefault(
            role_id, {"successes": 0, "partial_successes": 0, "failures": 0}
        )
        self._increment_outcome_counts(role_record, outcome)
        role_record["last_outcome"] = outcome

        domain_records = record.setdefault("domains", {})
        for domain in domains:
            domain_record = domain_records.setdefault(
                domain, {"successes": 0, "partial_successes": 0, "failures": 0}
            )
            self._increment_outcome_counts(domain_record, outcome)
            domain_record["last_outcome"] = outcome

        patterns = record.setdefault("patterns", {})
        pattern = patterns.setdefault(pattern_key, self._base_pattern_record(role_id, domains))
        self._increment_outcome_counts(pattern, outcome)
        pattern["last_summary"] = summary
        pattern["last_outcome"] = outcome
        pattern["last_feedback_summary"] = feedback_summary
        pattern["last_feedback_rationale"] = rationale
        if outcome == "success":
            pattern["last_success_at"] = _utc_now()
        elif outcome == "partial_success":
            pattern["last_partial_success_at"] = _utc_now()
        else:
            pattern["last_failure_at"] = _utc_now()
        self._increment_counts(record, "working_pattern_counts", strengths)
        self._increment_counts(record, "risk_watchout_counts", weaknesses)
        self._increment_counts(pattern, "working_pattern_counts", strengths)
        self._increment_counts(pattern, "risk_watchout_counts", weaknesses)
        record["delta_profile"] = self._build_delta_profile(record)
        record.setdefault("learned_skill_refs", [])

    # -- .7：技能版本化更新与回滚 ----------------------------------------

    def _skills_root(self) -> Path:
        if self._skills_dir:
            return Path(self._skills_dir).expanduser()
        from bandi.skills.importer import default_skills_dir

        return Path(default_skills_dir())

    def _history_root(self) -> Path:
        """历史版本目录——放技能库之外（SkillImporter 原子替换会整目录重建）。"""
        return self._skills_root().parent / "skill_history"

    @staticmethod
    def _skill_version(text: str) -> int:
        match = re.search(r"(?m)^version:\s*(\d+)\s*$", text or "")
        return int(match.group(1)) if match else 1

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        match = re.match(r"^\ufeff?\s*---\s*\n.*?\n---\s*\n?", text, re.DOTALL)
        return text[match.end() :] if match else text

    @classmethod
    def _merge_skill_content(
        cls, existing_text: str, candidate_body: str, next_version: int
    ) -> str | None:
        """功能：合并式补丁——候选正文中不在既有技能里的行，作为 Revision 节追加；
        无新增返回 None（幂等不重写）。"""
        existing_body = cls._strip_frontmatter(existing_text)
        existing_lines = {line.strip() for line in existing_body.splitlines() if line.strip()}
        seen: set[str] = set()
        new_lines: list[str] = []
        for line in candidate_body.splitlines():
            stripped = line.strip()
            if not stripped or stripped in existing_lines or stripped in seen:
                continue
            seen.add(stripped)
            new_lines.append(line.rstrip())
        if not new_lines:
            return None
        revision = f"## Revision v{next_version} (reflection update)"
        return existing_body.rstrip() + f"\n\n{revision}\n" + "\n".join(new_lines) + "\n"

    def _update_skill_md(
        self, skill_name: str, candidate_body: str, metadata: dict[str, Any] | None = None
    ) -> bool:
        """功能：同名技能合并式更新——备份旧版本到 history、version 递增后原子替换。"""
        skill_md = self._skills_root() / skill_name / "SKILL.md"
        if not skill_md.exists():
            # 文件被外部删除：按新技能重建（保持名称稳定）
            self._save_skill_md(
                skill_name=skill_name, content=candidate_body, metadata=metadata, version=1
            )
            self._record_skill_change(
                action="reflection_create",
                skill_name=skill_name,
                before="",
                after=self._compose_skill_md(skill_name, candidate_body, 1),
            )
            return True
        try:
            existing = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        old_version = self._skill_version(existing)
        merged_body = self._merge_skill_content(existing, candidate_body, old_version + 1)
        if merged_body is None:
            return False
        backup = self._history_root() / skill_name / f"SKILL.v{old_version}.md"
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(existing, encoding="utf-8")
        except OSError:
            return False
        self._save_skill_md(
            skill_name=skill_name,
            content=merged_body,
            metadata=metadata,
            version=old_version + 1,
        )
        self._record_skill_change(
            action="reflection_update",
            skill_name=skill_name,
            before=existing,
            after=self._compose_skill_md(skill_name, merged_body, old_version + 1),
        )
        return True

    def _record_skill_change(
        self, *, action: str, skill_name: str, before: str, after: str
    ) -> None:
        """功能：反思沉淀技能落盘记审计账本（before/after sha256）。"""
        from bandi.skills.audit_ledger import record_skill_change

        record_skill_change(
            action=action,
            skill_id=skill_name,
            before=before,
            after=after,
            source="employee_evolution",
        )

    def rollback_skill(self, skill_name: str, version: int) -> bool:
        """功能：回滚技能到历史版本（9.7 配套回滚面）——缺失版本显式 False。"""
        try:
            backup = self._history_root() / skill_name / f"SKILL.v{int(version)}.md"
        except (TypeError, ValueError):
            return False
        if not backup.is_file():
            return False
        try:
            text = backup.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        from bandi.skills.importer import SkillImporter, SkillImportError

        with tempfile.TemporaryDirectory(prefix="bandi-evolution-") as tmp:
            skill_dir = Path(tmp) / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
            try:
                SkillImporter(skills_dir=self._skills_dir).import_skill(str(skill_dir))
            except SkillImportError:
                return False
        return True

    @staticmethod
    def _compose_skill_md(skill_name: str, content: str, version: int = 1) -> str:
        """功能：生成带 frontmatter 的 SKILL.md 全文（直写与暂存共用同一文本）。"""
        frontmatter_lines = ["---", f"name: {skill_name}", f"version: {version}", "---"]
        return "\n".join(frontmatter_lines) + "\n\n" + content

    def _save_skill_md(
        self,
        *,
        skill_name: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        version: int = 1,
    ) -> None:
        """生成带 frontmatter 的 SKILL.md 经 SkillImporter 导入技能库。"""
        from bandi.skills.importer import SkillImporter

        skill_md = self._compose_skill_md(skill_name, content, version)
        with tempfile.TemporaryDirectory(prefix="bandi-evolution-") as tmp:
            skill_dir = Path(tmp) / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
            importer = SkillImporter(skills_dir=self._skills_dir)
            importer.import_skill(str(skill_dir))

    def _attach_learned_skill(
        self,
        profile: dict[str, Any],
        employee_id: str,
        pattern_key: str,
        skill_name: str,
    ) -> None:
        employees = profile.setdefault("employees", {})
        record = employees.setdefault(employee_id, {})
        refs = record.setdefault("learned_skill_refs", [])
        if skill_name not in refs:
            refs.append(skill_name)
        patterns = record.setdefault("patterns", {})
        pattern = patterns.setdefault(pattern_key, {})
        pattern["learned_skill_ref"] = skill_name
        record["updated_at"] = _utc_now()
        record.setdefault("delta_profile", {})

    def _base_pattern_record(self, role_id: str, domains: list[str]) -> dict[str, Any]:
        return {
            "role_id": role_id,
            "domains": list(domains),
            "successes": 0,
            "partial_successes": 0,
            "failures": 0,
            "learned_skill_ref": "",
            "reflection_count": 0,
        }

    def _increment_outcome_counts(self, container: dict[str, Any], outcome: str) -> None:
        normalized = _normalize_outcome(outcome)
        if normalized == "success":
            container["successes"] = int(container.get("successes", 0)) + 1
        elif normalized == "failure":
            container["failures"] = int(container.get("failures", 0)) + 1
        else:
            container["partial_successes"] = int(container.get("partial_successes", 0)) + 1

    def _increment_counts(self, container: dict[str, Any], key: str, items: list[str]) -> None:
        counts = container.setdefault(key, {})
        for item in _dedupe_preserve_order(items):
            counts[item] = int(counts.get(item, 0)) + 1

    def _build_delta_profile(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "working_patterns": self._rank_items(record.get("working_pattern_counts", {}), limit=5),
            "default_checklists": self._rank_items(record.get("checklist_counts", {}), limit=6),
            "reviewer_preferences": self._rank_items(
                record.get("reviewer_preference_counts", {}), limit=5
            ),
            "risk_watchouts": self._rank_items(record.get("risk_watchout_counts", {}), limit=5),
            "tool_preferences": self._rank_items(record.get("tool_preference_counts", {}), limit=4),
            "fit_domains": self._rank_items(record.get("fit_domain_counts", {}), limit=4),
            "avoid_domains": self._rank_items(record.get("avoid_domain_counts", {}), limit=4),
        }

    def _rank_items(self, counts: dict[str, Any], *, limit: int = 6) -> list[str]:
        ranked = sorted(
            (
                (str(item).strip(), int(count))
                for item, count in dict(counts).items()
                if str(item).strip()
            ),
            key=lambda pair: (-pair[1], pair[0].lower()),
        )
        return [item for item, _count in ranked[:limit]]

    def _pattern_key(self, role_id: str, domains: list[str]) -> str:
        domain_key = ",".join(
            sorted({domain.strip().lower() for domain in domains if domain.strip()})
        )
        return f"{role_id}|{domain_key or 'general'}"
