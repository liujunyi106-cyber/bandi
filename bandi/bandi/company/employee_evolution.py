# 功能：员工演进——经验追踪 / 项目反思 / learned skill 自动晋升

"""Employee experience tracking, project reflections, and learned-skill distillation.

部分成功/失败计数 + 角色/领域/模式维度），项目交付后汇总项目反思
（what_worked / reusable_checklist / reviewer_preferences / risks）
反思达阈值（LEARNED_SKILL_THRESHOLD）自动晋升为 learned skill
（生成 SKILL.md 经 SkillImporter 导入 Bandi 技能库）。

适配：
``PreferenceManager``/``SkillLibrary`` → Bandi ``SkillImporter``
  （技能库写入）+ 无偏好写入（evolution 状态独立于 memory/*.md）；
- ``projection_id_for_task`` 对齐 Bandi ``work_item_identity``；
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

from bandi.company.work_item_identity import projection_id_for_task
from bandi.memory.global_store import store_write_lock
from bandi.memory.staging import ORIGIN_BACKGROUND, stage_skill_write

logger = logging.getLogger(__name__)

_LEARNED_SKILL_THRESHOLD = 2
_PENDING_SKILL_PREFIX = "pending:"
_EXPERIENCE_SCHEMA_VERSION = 1
_EVENTS_LIMIT = 100
_TERMINAL_STATUSES = ("done", "approved", "failed", "cancelled", "rejected")

# 组织经验档案的缺省组织 id——本地单组织产品口径（与 session_scoping 的
# "default" 归属约定一致）；多组织部署时由调用方显式传入。
DEFAULT_ORGANIZATION_ID = "default"


def _build_self_evolution_event(reflection: dict[str, Any]) -> dict[str, Any]:
    """功能：项目反思产出 → 自进化补丁事件——strengths/adjustments/avoid_next_time/
    """
    employee_id = str(reflection.get("employee_id") or "").strip()
    if not employee_id:
        return {}
    delivery_key = str(
        reflection.get("delivery_projection_id") or reflection.get("delivery_task_id") or ""
    ).strip()
    if not delivery_key:
        return {}
    summary = (
        f"{str(reflection.get('pattern_key') or '').strip()} "
        f"交付反思（{str(reflection.get('employee_outcome') or '').strip()}）"
    ).strip()
    return {
        "event_id": f"reflection:{delivery_key}:{employee_id}",
        "employee_id": employee_id,
        "role_id": str(reflection.get("role_id") or "").strip(),
        "summary": summary,
        "strengths": list(reflection.get("what_worked") or [])[:8],
        "adjustments": list(reflection.get("reviewer_preferences") or [])[:8],
        "avoid_next_time": list(reflection.get("mistakes_to_avoid") or [])[:8],
        "routing_notes": list(reflection.get("tool_preferences") or [])[:8],
        "evidence_task_ids": list(reflection.get("source_task_ids") or [])[:12],
        "confidence": float(reflection.get("confidence") or 0.7),
    }


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
    承载，见 `company/work_item.py`），只用 `getattr` 会让生产调用恒空转
    （`record_project_reflections` 曾在真实对象上直接 `return []`）。
    """
    direct = str(getattr(task, "project_id", "") or "").strip()
    if direct:
        return direct
    metadata = dict(getattr(task, "metadata", None) or {})
    return str(metadata.get("project_id") or "").strip()


def _task_status(task: Any) -> str:
    """功能：解析任务状态串——兼容 `Phase` 枚举（`Phase.` → `"approved"`）。

    `WorkItem.phase` 是枚举，直接 `str()` 得 `"Phase."`，会让终态过滤
    失败判定全部失配（假对象契约传字符串 `"done"`，故长期未暴露）。
    """
    raw = getattr(task, "phase", None) or getattr(task, "status", None)
    value = getattr(raw, "value", raw)
    return str(value or "").strip().lower()


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


def _normalize_skill_name(raw: str) -> str:
    normalized = str(raw or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized[:64]


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
    """追踪员工经验、项目反思与 learned skill 晋升。"""

    LEARNED_SKILL_THRESHOLD = _LEARNED_SKILL_THRESHOLD

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

    @classmethod
    def attach_approved_skill(
        cls,
        profile_path: str | Path,
        *,
        employee_id: str,
        pattern_key: str,
        skill_name: str,
    ) -> bool:
        """功能：审批通过后把待批准技能回写档案为真实技能名（幂等）。

        只更新档案中已存在的员工记录；档案缺失/损坏或员工不在档案时返回
        False（审批端点据此保留待批准记录，防技能已落盘而档案永久失联）。
        """
        path = Path(profile_path)
        employee = str(employee_id or "").strip()
        pattern = str(pattern_key or "").strip()
        skill = str(skill_name or "").strip()
        if not employee or not pattern or not skill:
            return False
        # 否则并发 approve 的两个技能引用互相覆盖（丢更新）
        with store_write_lock(path):
            if not path.is_file():
                return False
            manager = cls(home_dir=path.parent)
            profile = manager._load_json(path)
            existing = (profile.get("employees") or {}).get(employee)
            if not isinstance(existing, dict) or not existing:
                return False
            manager._attach_learned_skill(profile, employee, pattern, skill)
            manager._save_json(path, profile)
        return True

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

    # -- 项目反思 --

    def record_project_reflections(
        self,
        delivery_task: Any,
        work_item_tasks: list[Any],
        partial: bool = False,
        *,
        feedback: dict[str, Any] | None = None,
        evaluation: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """项目交付后按员工聚合反思，达阈值自动晋升 learned skill。"""
        project_id = _resolve_task_project_id(delivery_task)
        delivery_task_id = str(getattr(delivery_task, "id", "") or "").strip()
        if not project_id or not delivery_task_id:
            return []

        tasks = self._normalize_work_item_tasks(work_item_tasks, delivery_task)
        employee_groups: dict[str, list[Any]] = {}
        for task in tasks:
            status = _task_status(task)
            if status not in _TERMINAL_STATUSES:
                continue
            assignment = (
                dict(getattr(task, "metadata", {}) or {}).get("employee_assignment", {}) or {}
            )
            employee_id = str(assignment.get("employee_id", "")).strip()
            if not employee_id:
                continue
            employee_groups.setdefault(employee_id, []).append(task)

        results: list[dict[str, Any]] = []
        for employee_id, employee_tasks in employee_groups.items():
            reflection = self._build_project_reflection(
                delivery_task=delivery_task,
                employee_tasks=employee_tasks,
                project_id=project_id,
                partial=partial,
                feedback=feedback,
                evaluation=evaluation,
            )
            reflection_path = self.evolution_profile_path(project_id)

            global_profile = self.load_evolution_profile()
            self._record_project_reflection_in_profile(
                global_profile, reflection=reflection, reflection_path=reflection_path
            )
            learned_skill_ref = self._maybe_promote_reflection_skill(
                global_profile,
                reflection,
                project_id=project_id,
                profile_path=self.evolution_profile_path(),
            )
            self.save_evolution_profile(global_profile)

            project_profile = self.load_evolution_profile(project_id)
            self._record_project_reflection_in_profile(
                project_profile, reflection=reflection, reflection_path=reflection_path
            )
            if not learned_skill_ref:
                learned_skill_ref = self._maybe_promote_reflection_skill(
                    project_profile,
                    reflection,
                    project_id=project_id,
                    profile_path=self.evolution_profile_path(project_id),
                )
            if learned_skill_ref and not learned_skill_ref.startswith(_PENDING_SKILL_PREFIX):
                # 待批准技能不记入档案引用——技能尚未落盘，批准后由审批链路接管
                self._attach_learned_skill(
                    project_profile,
                    reflection["employee_id"],
                    reflection["pattern_key"],
                    learned_skill_ref,
                )
            self.save_evolution_profile(project_profile, project_id)

            # 自进化补丁流水线（A3+A4）：反思产出 → 补丁事件 → 组织经验档案
            # （event_id 按交付+员工幂等；失败不阻断反思主链）
            event = _build_self_evolution_event(reflection)
            if event:
                try:
                    self.apply_employee_evolution_patch(
                        organization_id=DEFAULT_ORGANIZATION_ID,
                        patch={"patches": [event]},
                        source={
                            "kind": "project_reflection",
                            "project_id": project_id,
                            "delivery_task_id": str(reflection.get("delivery_task_id") or ""),
                        },
                        allowed_employee_ids={employee_id},
                    )
                except Exception:
                    logger.debug("self evolution patch failed", exc_info=True)

            results.append(
                {
                    "employee_id": employee_id,
                    "reflection_path": str(reflection_path),
                    "learned_skill_ref": learned_skill_ref,
                    "status": "recorded",
                    "reflection": reflection,
                }
            )

        return results

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

    def _record_project_reflection_in_profile(
        self,
        profile: dict[str, Any],
        *,
        reflection: dict[str, Any],
        reflection_path: Path,
    ) -> None:
        employees = profile.setdefault("employees", {})
        employee_id = str(reflection.get("employee_id", "")).strip()
        if not employee_id:
            return
        record = employees.setdefault(employee_id, {})
        record.update(
            {
                "employee_name": reflection.get("employee_name", ""),
                "role_id": reflection.get("role_id", ""),
                "template_id": reflection.get("template_id", ""),
                "category": reflection.get("category", ""),
                "updated_at": _utc_now(),
                "last_reflection_at": _utc_now(),
            }
        )
        reflection_ids = record.setdefault("project_reflection_ids", [])
        if reflection["delivery_task_id"] in reflection_ids:
            return
        reflection_ids.append(reflection["delivery_task_id"])
        reflection_paths = record.setdefault("reflection_paths", [])
        reflection_paths.append(str(reflection_path))
        project_ids = record.setdefault("project_ids", [])
        if reflection["project_id"] not in project_ids:
            project_ids.append(reflection["project_id"])
        record["projects_reflected"] = len(project_ids)

        patterns = record.setdefault("patterns", {})
        pattern_key = str(reflection.get("pattern_key", ""))
        pattern = patterns.setdefault(
            pattern_key,
            self._base_pattern_record(
                str(reflection.get("role_id", "")).strip(),
                list(reflection.get("domains", [])),
            ),
        )
        pattern["role_id"] = reflection.get("role_id", "")
        pattern["domains"] = list(reflection.get("domains", []))
        pattern["reflection_count"] = int(pattern.get("reflection_count", 0)) + 1
        pattern["latest_project_summary"] = str(reflection.get("project_summary", ""))
        pattern["last_reflection_at"] = _utc_now()
        pattern_reflection_paths = pattern.setdefault("reflection_paths", [])
        pattern_reflection_paths.append(str(reflection_path))

        for key, reflection_key in (
            ("working_pattern_counts", "what_worked"),
            ("checklist_counts", "reusable_checklist"),
            ("reviewer_preference_counts", "reviewer_preferences"),
            ("tool_preference_counts", "tool_preferences"),
            ("risk_watchout_counts", "mistakes_to_avoid"),
            ("fit_domain_counts", "suitable_for"),
            ("avoid_domain_counts", "avoid_for"),
        ):
            self._increment_counts(pattern, key, reflection.get(reflection_key, []))
            self._increment_counts(record, key, reflection.get(reflection_key, []))
        record["delta_profile"] = self._build_delta_profile(record)
        record.setdefault("learned_skill_refs", [])

    def _build_project_reflection(
        self,
        *,
        delivery_task: Any,
        employee_tasks: list[Any],
        project_id: str,
        partial: bool = False,
        feedback: dict[str, Any] | None = None,
        evaluation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        first_assignment = (
            dict(getattr(employee_tasks[0], "metadata", {}) or {}).get("employee_assignment", {})
            or {}
        )
        employee_id = str(first_assignment.get("employee_id", "")).strip()
        role_id = str(
            first_assignment.get("role_id") or getattr(employee_tasks[0], "assignee_role", "") or ""
        ).strip()
        domains = self._collect_domains(
            employee_tasks, fallback=list(first_assignment.get("domains", []))
        )
        pattern_key = self._pattern_key(role_id, domains)
        task_summaries = self._collect_task_summaries(employee_tasks)
        artifacts = self._collect_items(employee_tasks, "artifacts")
        decisions = self._collect_items(employee_tasks, "decisions")
        risks = self._collect_items(employee_tasks, "risks")
        open_questions = self._collect_items(employee_tasks, "open_questions")
        preferred_agents = self._collect_preferred_agents(employee_tasks, first_assignment)
        employee_feedback = self._find_employee_feedback(employee_id, evaluation)
        historical_context = self.build_employee_delta_context(employee_id, project_id=project_id)
        feedback_summary = str(
            (evaluation or {}).get("summary", "") or (feedback or {}).get("raw_feedback", "")
        ).strip()

        what_worked = [
            "Break work into explicit deliverables and leave concise handoff summaries.",
            "Preserve reviewer-friendly artifacts so downstream validation is faster.",
        ]
        if decisions:
            what_worked.append(
                "Capture explicit implementation decisions that downstream reviewers can verify."
            )
        if artifacts:
            what_worked.append("Reference exact artifact paths or outputs in every handoff.")

        reusable_checklist = [
            "State the objective and completion summary explicitly in the handoff.",
            "Leave reviewer-friendly artifacts for the next work item.",
        ]
        if decisions:
            reusable_checklist.append("Record key decisions that affect downstream execution.")
        if artifacts:
            reusable_checklist.append("List concrete artifact references for changed outputs.")
        if any(domain in {"coding", "api", "backend", "frontend", "devops"} for domain in domains):
            reusable_checklist.append(
                "Include validation or test evidence before requesting review."
            )

        reviewer_preferences = [
            "Make decisions, risks, and artifact references explicit for review."
        ]
        if risks or open_questions:
            reviewer_preferences.append(
                "Flag unresolved risks and open questions before requesting approval."
            )
        if artifacts:
            reviewer_preferences.append("Point reviewers to exact changed files or deliverables.")

        tool_preferences = [
            f"Prefer external agent `{agent}` for similar `{role_id}` work."
            for agent in preferred_agents
        ]

        mistakes_to_avoid = list(risks[:4])
        if open_questions:
            mistakes_to_avoid.extend(
                item for item in open_questions[:2] if item not in mistakes_to_avoid
            )
        if not mistakes_to_avoid:
            mistakes_to_avoid.append(
                "Avoid handoffs that omit concrete artifacts, risks, or validation notes."
            )
        what_worked.extend(
            str(item).strip()
            for item in list(employee_feedback.get("strengths", []))
            if str(item).strip()
        )
        mistakes_to_avoid.extend(
            str(item).strip()
            for item in list(employee_feedback.get("weaknesses", []))
            if str(item).strip()
        )
        if feedback_summary:
            reviewer_preferences.append(f"Carry forward user feedback themes: {feedback_summary}")

        suitable_for = list(domains or [role_id])
        avoid_for = self._infer_avoid_domains(mistakes_to_avoid)
        confidence = round(min(0.95, 0.55 + (0.08 * len(employee_tasks))), 2)

        failures: list[dict[str, str]] = []
        for t in employee_tasks:
            status = _task_status(t)
            if status == "done" or status == "approved":
                continue
            reason = str(getattr(t, "metadata", {}) or {}).get("failure_reason", "")
            failures.append(
                {
                    "task": str(getattr(t, "title", "") or ""),
                    "status": status,
                    "reason": reason,
                }
            )
            if reason:
                mistakes_to_avoid.append(reason)

        if partial:
            confidence = round(min(confidence, 0.4), 2)

        result: dict[str, Any] = {
            "employee_id": employee_id,
            "employee_name": first_assignment.get("name", ""),
            "template_id": first_assignment.get("template_id", ""),
            "category": first_assignment.get("category", ""),
            "project_id": project_id,
            "delivery_task_id": str(getattr(delivery_task, "id", "") or ""),
            "delivery_projection_id": projection_id_for_task(delivery_task),
            "role_id": role_id,
            "domains": domains,
            "pattern_key": pattern_key,
            "project_summary": " ".join(task_summaries[:3]),
            "what_worked": _dedupe_preserve_order(what_worked),
            "mistakes_to_avoid": _dedupe_preserve_order(mistakes_to_avoid),
            "reusable_checklist": _dedupe_preserve_order(reusable_checklist),
            "reviewer_preferences": _dedupe_preserve_order(reviewer_preferences),
            "tool_preferences": _dedupe_preserve_order(tool_preferences),
            "suitable_for": _dedupe_preserve_order(suitable_for),
            "avoid_for": _dedupe_preserve_order(avoid_for),
            "confidence": confidence,
            "source_task_ids": [str(getattr(task, "id", "")) for task in employee_tasks],
            "created_at": _utc_now(),
            "employee_outcome": _normalize_outcome(
                str(employee_feedback.get("outcome", "partial_success") or "partial_success")
            ),
            "feedback_summary": feedback_summary,
            "feedback_rationale": str(employee_feedback.get("reason", "")).strip(),
        }
        if historical_context:
            result["historical_context"] = historical_context
        if feedback:
            result["user_feedback"] = {
                "label": str(feedback.get("label", "")).strip(),
                "raw_feedback": str(feedback.get("raw_feedback", "")).strip(),
                "scope": str(feedback.get("scope", "")).strip(),
            }
        if evaluation:
            result["runtime_feedback_evaluation"] = {
                "overall_outcome": str(evaluation.get("overall_outcome", "")).strip(),
                "summary": str(evaluation.get("summary", "")).strip(),
                "strengths": [
                    str(item).strip()
                    for item in list(evaluation.get("strengths", []))
                    if str(item).strip()
                ][:6],
                "weaknesses": [
                    str(item).strip()
                    for item in list(evaluation.get("weaknesses", []))
                    if str(item).strip()
                ][:6],
            }
        if failures:
            result["failures"] = failures
            result["partial"] = True
        return result

    def _maybe_promote_reflection_skill(
        self,
        profile: dict[str, Any],
        reflection: dict[str, Any],
        project_id: str | None = None,
        *,
        profile_path: str | Path | None = None,
    ) -> str:
        """反思达阈值且存在重复模式时，生成/更新 SKILL.md 并导入技能库。

        .7：同名技能不再冻结——已晋升技能出现新条目时走**合并式补丁更新**
        （version 递增 + history 备份，可回滚），而非整文件覆盖丢历史；
        无新条目保持幂等不重写。
        技能晋升审批：首次晋升不再直写技能库，改入技能暂存区等待用户批准
        （无自动批准路径），返回 `pending:<id8>` 待批准引用；同 skill_name
        重复命中由暂存区覆盖去重（supersede）。暂存 payload 携带档案定位
        （profile_path/employee_id/pattern_key）——审批通过后由审批链路回写
        真实技能名，使后续反思进入合并更新分支而非重复入队。
        """
        employees = profile.setdefault("employees", {})
        employee_id = str(reflection.get("employee_id", "")).strip()
        record = employees.get(employee_id)
        if not record:
            return ""
        pattern_key = str(reflection.get("pattern_key", "")).strip()
        pattern = dict(record.get("patterns", {}).get(pattern_key, {}) or {})
        # 功能：历史直写时代遗留的非空 learned_skill_ref 视为已批准基线（审批门
        # 上线前技能已直写技能库），允许直接走合并更新；新流程不再产生此类档案
        existing_ref = str(pattern.get("learned_skill_ref", ""))
        if not pattern:
            return existing_ref
        if int(pattern.get("reflection_count", 0)) < self.LEARNED_SKILL_THRESHOLD:
            return existing_ref

        repeated_working = self._repeated_items(pattern.get("working_pattern_counts", {}))
        repeated_checklists = self._repeated_items(pattern.get("checklist_counts", {}))
        repeated_reviewer = self._repeated_items(pattern.get("reviewer_preference_counts", {}))
        repeated_tools = self._repeated_items(pattern.get("tool_preference_counts", {}))
        repeated_risks = self._repeated_items(pattern.get("risk_watchout_counts", {}))
        if not any(
            (
                repeated_working,
                repeated_checklists,
                repeated_reviewer,
                repeated_tools,
                repeated_risks,
            )
        ):
            return existing_ref

        employee_name = str(reflection.get("employee_name", "Employee")).strip() or "Employee"
        role_id = str(reflection.get("role_id", "")).strip() or record.get("role_id", "general")
        domains = (
            list(reflection.get("domains", [])) or list(pattern.get("domains", [])) or [role_id]
        )
        primary_domain = domains[0] if domains else role_id
        display_name = f"{employee_name} {role_id} {primary_domain} playbook"
        skill_name = existing_ref or _normalize_skill_name(display_name)
        reflection_count = int(pattern.get("reflection_count", 0))

        sections = [
            f"# {display_name}",
            "",
            f"Learned playbook for **{employee_name}** in role `{role_id}` across "
            f"{', '.join(domains)}.",
            f"Distilled from {reflection_count} project reflections.",
        ]
        if repeated_working:
            sections.extend(
                ["", "## Successful Behaviors", *[f"- {item}" for item in repeated_working]]
            )
        if repeated_checklists:
            sections.extend(
                ["", "## Default Checklist", *[f"- {item}" for item in repeated_checklists]]
            )
        if repeated_reviewer:
            sections.extend(
                ["", "## Reviewer Preferences", *[f"- {item}" for item in repeated_reviewer]]
            )
        if repeated_tools:
            sections.extend(["", "## Tool Preferences", *[f"- {item}" for item in repeated_tools]])
        if repeated_risks:
            sections.extend(["", "## Risk Watchouts", *[f"- {item}" for item in repeated_risks]])
        avoid_for = self._rank_items(record.get("avoid_domain_counts", {}), limit=4)
        if avoid_for:
            sections.extend(["", "## Avoid For", *[f"- {item}" for item in avoid_for]])

        content = "\n".join(sections).strip() + "\n"
        metadata = {
            "employee_id": employee_id,
            "employee_name": employee_name,
            "role_id": role_id,
            "template_id": reflection.get("template_id", ""),
            "pattern_key": pattern_key,
            "built_from": "project_reflections",
        }
        if existing_ref:
            # 已晋升：合并式更新（无新条目 → 幂等不重写）
            self._update_skill_md(existing_ref, content, metadata)
            return existing_ref
        lesson_bits = _dedupe_preserve_order(
            [*repeated_working, *repeated_checklists, *repeated_risks]
        )
        summary = f"技能 {skill_name} 待批准（{reflection_count} 次项目反思）"
        if lesson_bits:
            summary += "：" + "；".join(lesson_bits[:2])
        location_path = (
            Path(profile_path)
            if profile_path is not None
            else self.evolution_profile_path(project_id)
        )
        record = stage_skill_write(
            skill_name=skill_name,
            content=self._compose_skill_md(skill_name, content, version=1),
            origin=ORIGIN_BACKGROUND,
            summary=summary,
            reason=f"pattern {pattern_key} reflection_count={reflection_count}",
            extra_payload={
                "profile_path": str(location_path),
                "employee_id": employee_id,
                "pattern_key": pattern_key,
            },
        )
        if record is None or not getattr(record, "persisted", False):
            return ""
        return f"{_PENDING_SKILL_PREFIX}{record.id8}"

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

    def _normalize_work_item_tasks(
        self, work_item_tasks: list[Any], delivery_task: Any
    ) -> list[Any]:
        tasks_by_id: dict[str, Any] = {}
        for task in work_item_tasks:
            task_id = str(getattr(task, "id", "") or "").strip()
            if task_id:
                tasks_by_id[task_id] = task
        delivery_task_id = str(getattr(delivery_task, "id", "") or "").strip()
        if delivery_task_id:
            tasks_by_id[delivery_task_id] = delivery_task
        return list(tasks_by_id.values())

    def _collect_domains(self, tasks: list[Any], fallback: list[str]) -> list[str]:
        collected: list[str] = []
        for task in tasks:
            assignment = (
                dict(getattr(task, "metadata", {}) or {}).get("employee_assignment", {}) or {}
            )
            for value in list(assignment.get("domains", [])) + list(
                getattr(task, "tags", []) or []
            ):
                item = str(value).strip()
                if item and item not in collected:
                    collected.append(item)
        for value in fallback:
            item = str(value).strip()
            if item and item not in collected:
                collected.append(item)
        return collected

    def _collect_task_summaries(self, tasks: list[Any]) -> list[str]:
        summaries: list[str] = []
        for task in tasks:
            task_meta = dict(getattr(task, "metadata", {}) or {})
            result_obj = getattr(task, "result", None)
            result_text = ""
            if isinstance(result_obj, dict):
                result_text = str(result_obj.get("content", "") or "")
            for candidate in (
                str(task_meta.get("work_item_summary_for_downstream", "") or ""),
                result_text,
                str(getattr(task, "title", "") or ""),
            ):
                candidate = str(candidate or "").strip()
                if candidate and candidate not in summaries:
                    summaries.append(candidate)
                    break
        return summaries

    def _collect_items(self, tasks: list[Any], key: str) -> list[str]:
        items: list[str] = []
        for task in tasks:
            for value in list((getattr(task, "metadata", {}) or {}).get(key, []) or []):
                text = str(value).strip()
                if text and text not in items:
                    items.append(text)
        return items

    def _collect_preferred_agents(self, tasks: list[Any], assignment: dict[str, Any]) -> list[str]:
        agents: list[str] = []
        assignment_agent = str(assignment.get("preferred_external_agent", "") or "").strip()
        if assignment_agent:
            agents.append(assignment_agent)
        for task in tasks:
            value = str(getattr(task, "assigned_external_agent", "") or "").strip()
            if value and value not in agents:
                agents.append(value)
        return agents

    def _infer_avoid_domains(self, risks: list[str]) -> list[str]:
        avoid: list[str] = []
        joined = " ".join(risks).lower()
        for token in ("security", "compliance", "billing", "finance", "production", "deployment"):
            if token in joined and token not in avoid:
                avoid.append(token)
        return avoid

    def _find_employee_feedback(
        self, employee_id: str, evaluation: dict[str, Any] | None
    ) -> dict[str, Any]:
        if not evaluation:
            return {}
        for item in list(evaluation.get("employees", [])):
            if str(item.get("employee_id", "")).strip() == employee_id:
                return dict(item)
        return {}

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

    def _repeated_items(self, counts: dict[str, Any], *, minimum: int = 2) -> list[str]:
        ranked = sorted(
            (
                (str(item).strip(), int(count))
                for item, count in dict(counts).items()
                if int(count) >= minimum and str(item).strip()
            ),
            key=lambda pair: (-pair[1], pair[0].lower()),
        )
        return [item for item, _count in ranked[:6]]

    def _pattern_key(self, role_id: str, domains: list[str]) -> str:
        domain_key = ",".join(
            sorted({domain.strip().lower() for domain in domains if domain.strip()})
        )
        return f"{role_id}|{domain_key or 'general'}"
