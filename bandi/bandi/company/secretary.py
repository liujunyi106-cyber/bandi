# 功能：Secretary 秘书——策略持久化 + 会话记忆 + LLM 编排 + Skill 导入

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import yaml

from bandi.core.exceptions import BandiError
from bandi.core.message import LLMResponse
from bandi.skills.importer import SkillImporter, SkillImportError
from bandi.skills.path_guard import validate_path_segment

_TURNS_LOCK = threading.Lock()
_POLICY_LOCK = threading.Lock()


class SecretaryLLM(Protocol):
    # 功能：Secretary 使用的 LLM 异步补全契约
    async def complete(self, messages: list[dict[str, str]]) -> LLMResponse: ...


class WorkItemCreator(Protocol):
    # 功能：create_work_item 回调契约——接收 payload 返回工作项 id
    def __call__(self, payload: dict[str, Any]) -> str: ...


class PolicyType:
    PREFERENCE = "preference"
    STRATEGY = "strategy"
    RULE = "rule"
    CONSTRAINT = "constraint"

    _VALID: frozenset[str] = frozenset({PREFERENCE, STRATEGY, RULE, CONSTRAINT})

    @classmethod
    def validate(cls, policy_type: str) -> str:
        value = str(policy_type or "").strip().lower()
        if value not in cls._VALID:
            raise ValueError(f"invalid policy_type: {policy_type!r}")
        return value


class PolicyScope:
    GLOBAL = "global"
    PROJECT = "project"
    SESSION = "session"


@dataclass
class PolicyRecord:
    id: str
    policy_type: str
    content: str
    source: str
    created_at: datetime
    scope: str = PolicyScope.GLOBAL
    project_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "policy_type": self.policy_type,
            "content": self.content,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "scope": self.scope,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyRecord:
        created_at = datetime.now(UTC)
        raw_created = data.get("created_at")
        if isinstance(raw_created, str):
            try:
                created_at = datetime.fromisoformat(raw_created)
            except ValueError:
                created_at = datetime.now(UTC)
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            id=str(data.get("id") or f"policy_{uuid4().hex}"),
            policy_type=str(data.get("policy_type") or "rule"),
            content=str(data.get("content") or ""),
            source=str(data.get("source") or "unknown"),
            created_at=created_at,
            scope=str(data.get("scope") or PolicyScope.GLOBAL),
            project_id=data.get("project_id"),
            session_id=data.get("session_id"),
            metadata=metadata,
        )


class SecretaryStore:
    # 功能：Secretary 策略 YAML 文件持久化（global/project/session 三层）+ turns 会话记录
    def __init__(self, base_dir: str | Path, max_cached_sessions: int = 50) -> None:
        self._base_dir = Path(base_dir)
        self._global_path = self._base_dir / "global.yaml"
        self._projects_dir = self._base_dir / "projects"
        self._sessions_dir = self._base_dir / "sessions"
        self._turns_dir = self._base_dir / "turns"
        self._max_cached_sessions = max_cached_sessions
        self._turns_cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    def _load_yaml(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or []

    def _save_yaml(self, path: Path, payload: Any) -> None:
        content = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        self._atomic_write_text(path, content)

    def load(
        self,
        scope: str,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> list[PolicyRecord]:
        path = self._path_for(scope, project_id, session_id)
        return [
            PolicyRecord.from_dict(item) for item in self._load_yaml(path) if isinstance(item, dict)
        ]

    def save(
        self,
        scope: str,
        records: list[PolicyRecord],
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        path = self._path_for(scope, project_id, session_id)
        self._save_yaml(path, [record.to_dict() for record in records])

    def load_turns(self, session_id: str) -> list[dict[str, Any]]:
        cached = self._turns_cache.get(session_id)
        if cached is not None:
            self._turns_cache.move_to_end(session_id)
            return [dict(turn) for turn in cached]
        path = self._turns_path(session_id)
        turns = [turn for turn in self._load_yaml(path) if isinstance(turn, dict)]
        self._cache_turns(session_id, [dict(turn) for turn in turns])
        return turns

    def save_turns(self, session_id: str, turns: list[dict[str, Any]]) -> None:
        path = self._turns_path(session_id)
        self._save_yaml(path, turns)
        self._cache_turns(session_id, [dict(turn) for turn in turns])

    def _cache_turns(self, session_id: str, turns: list[dict[str, Any]]) -> None:
        # 功能：缓存写入 + 超限淘汰最久未访问（LRU）
        self._turns_cache[session_id] = turns
        self._turns_cache.move_to_end(session_id)
        while len(self._turns_cache) > self._max_cached_sessions:
            self._turns_cache.popitem(last=False)

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def list_sessions(self, project_id: str | None = None) -> list[str]:
        if not self._turns_dir.is_dir():
            return []
        result: list[str] = []
        for path in sorted(self._turns_dir.glob("*.yaml")):
            session_id = path.stem
            if project_id is not None and not self._session_has_project(session_id, project_id):
                continue
            result.append(session_id)
        return result

    def _session_has_project(self, session_id: str, project_id: str) -> bool:
        return any(turn.get("project_id") == project_id for turn in self.load_turns(session_id))

    def _path_for(
        self,
        scope: str,
        project_id: str | None,
        session_id: str | None,
    ) -> Path:
        if scope == PolicyScope.PROJECT:
            return self._safe_path(self._projects_dir, project_id, "project_id")
        if scope == PolicyScope.SESSION:
            return self._safe_path(self._sessions_dir, session_id, "session_id")
        if scope == PolicyScope.GLOBAL:
            return self._global_path
        raise ValueError(f"unknown policy scope: {scope!r}")

    def _turns_path(self, session_id: str) -> Path:
        return self._safe_path(self._turns_dir, session_id, "session_id")

    @staticmethod
    def _safe_path(directory: Path, identifier: str | None, label: str) -> Path:
        if not identifier:
            raise ValueError(f"{label} is required for scoped path")
        base = directory.resolve()
        target = (base / f"{identifier}.yaml").resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"invalid {label}: {identifier!r}")
        return target


class SecretaryService:
    # 功能：Secretary 长期策略捕获——YAML 持久化 + global/project/session 多层合并 + 会话记忆 + Skill 导入
    def __init__(
        self,
        base_dir: str | Path | None = None,
        llm: SecretaryLLM | None = None,
        skills_dir: str | None = None,
        allowed_skill_import_roots: list[str] | None = None,
        org_engine: Any | None = None,
        recruiter: Any | None = None,
        template_store: Any | None = None,
        profile_manager: Any | None = None,
    ) -> None:
        resolved = (
            Path(base_dir)
            if base_dir is not None
            else Path.home() / ".bandi" / "company" / "secretary"
        )
        self._store = SecretaryStore(resolved)
        self._llm = llm
        self._skills_dir = skills_dir
        # 草案经 confirm_org_build 显式确认后批量落地到该引擎；None = fail-closed skip
        self._org_engine = org_engine
        self._recruiter = recruiter
        self._template_store = template_store
        self._profile_manager = profile_manager
        self._org_drafts_dir = resolved / "org_drafts"
        if allowed_skill_import_roots is None:
            self._allowed_skill_import_roots = None
        else:
            self._allowed_skill_import_roots = [
                str(Path(root).expanduser().resolve()) for root in allowed_skill_import_roots
            ]

    def capture_policy(
        self,
        policy_type: str,
        content: str,
        source: str,
        scope: str = PolicyScope.GLOBAL,
        project_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PolicyRecord:
        # 功能：策略捕获（锁串行化读-改-写，防并发丢记录，对齐 record_turn）
        validated_type = PolicyType.validate(policy_type)
        with _POLICY_LOCK:
            records = self._store.load(scope, project_id, session_id)
            record = PolicyRecord(
                id=f"policy_{uuid4().hex}",
                policy_type=validated_type,
                content=content,
                source=source,
                created_at=datetime.now(UTC),
                scope=scope,
                project_id=project_id,
                session_id=session_id,
                metadata=metadata or {},
            )
            records.append(record)
            self._store.save(scope, records, project_id, session_id)
            return record

    def list_policies(
        self,
        policy_type: str | None = None,
        scope: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> list[PolicyRecord]:
        records = self._load_scope(scope, project_id, session_id)
        if policy_type:
            records = [r for r in records if r.policy_type == policy_type]
        return records

    def get_policy(
        self,
        policy_id: str,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> PolicyRecord | None:
        # 只查 global 层，project/session 策略永远找不到
        for record in self.load_merged(project_id=project_id, session_id=session_id):
            if record.id == policy_id:
                return record
        return None

    def search_policies(self, query: str) -> list[PolicyRecord]:
        lower = str(query or "").lower()
        return [
            r
            for r in self.load_merged()
            if lower in str(r.content or "").lower() or lower in str(r.source or "").lower()
        ]

    def load_merged(
        self,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> list[PolicyRecord]:
        # 功能：global → project → session 逐层合并，同 id 后者覆盖（session 优先级最高）
        merged: dict[str, PolicyRecord] = {r.id: r for r in self._store.load(PolicyScope.GLOBAL)}
        if project_id:
            merged.update(
                {r.id: r for r in self._store.load(PolicyScope.PROJECT, project_id=project_id)}
            )
        if session_id:
            merged.update(
                {r.id: r for r in self._store.load(PolicyScope.SESSION, session_id=session_id)}
            )
        return list(merged.values())

    def _load_scope(
        self,
        scope: str | None,
        project_id: str | None,
        session_id: str | None,
    ) -> list[PolicyRecord]:
        if scope == PolicyScope.PROJECT:
            return self._store.load(PolicyScope.PROJECT, project_id=project_id)
        if scope == PolicyScope.SESSION:
            return self._store.load(PolicyScope.SESSION, session_id=session_id)
        if scope == PolicyScope.GLOBAL:
            return self._store.load(PolicyScope.GLOBAL)
        if scope is None:
            return self.load_merged(project_id=project_id, session_id=session_id)
        raise ValueError(f"unknown policy scope: {scope!r}")

    # -- 会话记忆（长期记忆 turns 持久化）--

    def record_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        # 功能：会话轮次持久化（锁串行化读-改-写，防并发丢 turns）
        with _TURNS_LOCK:
            turns = self._store.load_turns(session_id)
            turns.append(
                {
                    "role": role,
                    "content": content,
                    "project_id": project_id,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            self._store.save_turns(session_id, turns)
            return turns[-1]

    def list_turns(self, session_id: str) -> list[dict[str, Any]]:
        return self._store.load_turns(session_id)

    def list_sessions(self, project_id: str | None = None) -> list[str]:
        return self._store.list_sessions(project_id=project_id)

    def build_session_context(self, session_id: str, limit: int = 10) -> str:
        turns = self._store.load_turns(session_id)
        if limit <= 0:
            turns = []
        elif limit < len(turns):
            turns = turns[-limit:]
        if not turns:
            return ""
        return "\n".join(
            f"{turn.get('role', 'unknown')}: {turn.get('content', '')}"
            for turn in turns
            if str(turn.get("content", "")).strip()
        )

    # -- 策略管理 --

    def describe_policies(
        self, project_id: str | None = None, session_id: str | None = None
    ) -> str:
        records = self.load_merged(project_id=project_id, session_id=session_id)
        lines = [
            f"- [{r.policy_type}] ({r.scope}) {r.content}"
            for r in records
            if str(r.content).strip()
        ]
        return "\n".join(lines)

    # -- 对话编排 --

    async def handle_message(
        self,
        content: str,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        llm: SecretaryLLM | None = None,
        on_create_work_item: WorkItemCreator | None = None,
    ) -> dict[str, Any]:
        resolved_session = session_id or f"secretary_{uuid4().hex}"
        await asyncio.to_thread(
            self.record_turn, resolved_session, "user", content, project_id=project_id
        )

        provider = llm if llm is not None else self._llm
        if provider is None:
            reply = content
            applied_actions: list[str] = []
        else:
            prompt = self._build_prompt(content, project_id=project_id, session_id=resolved_session)
            reply, applied_actions = await self._call_llm(
                provider,
                prompt,
                project_id=project_id,
                on_create_work_item=on_create_work_item,
            )

        await asyncio.to_thread(
            self.record_turn, resolved_session, "assistant", reply, project_id=project_id
        )
        return {
            "response": reply,
            "session_id": resolved_session,
            "applied_actions": applied_actions,
        }

    def _build_prompt(self, content: str, project_id: str | None, session_id: str) -> str:
        policy_summary = self.describe_policies(project_id=project_id, session_id=session_id)
        session_history = self.build_session_context(session_id)
        return (
            f"Secretary policy summary:\n{policy_summary or '(none)'}\n\n"
            f"Session history:\n{session_history or '(none)'}\n\n"
            f"User message: {content}"
        )

    async def _call_llm(
        self,
        provider: SecretaryLLM,
        prompt: str,
        project_id: str | None,
        on_create_work_item: WorkItemCreator | None = None,
    ) -> tuple[str, list[str]]:
        try:
            from bandi.llm.accounted import accounted_complete

            response = await accounted_complete(
                provider,
                [
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": prompt},
                ],
                label="secretary",
                session_id="secretary",
            )
            parsed = self._parse_response(response.content)
        except (KeyError, TypeError, OSError, ValueError, AttributeError):
            parsed = {"response": prompt, "actions": []}
        reply = str(parsed.get("response", "")).strip() or prompt
        applied_actions = await self._apply_actions(
            parsed.get("actions", []),
            project_id=project_id,
            on_create_work_item=on_create_work_item,
        )
        return reply, applied_actions

    @staticmethod
    def _system_prompt() -> str:
        # 与实现漂移）。原写占位符 ["optional","tool","tags"] ⇒ LLM 无从得知合法值
        from bandi.tools.registry import VALID_ROLE_TOOL_TAGS

        tag_enum = "|".join(sorted(VALID_ROLE_TOOL_TAGS))
        return (
            "You are the long-term secretary of the Bandi system.\n"
            "Return strict JSON only.\n"
            'JSON schema: {"response": "assistant reply", "actions": ['
            '{"kind": "import_skill", "source": "path", "path": "/path/to/skill/folder"}, '
            '{"kind": "create_work_item", "title": "task title", "turn_type": "execute|review|aggregate", '
            '"assignee_role": "role_id", "description": "optional description"}, '
            '{"kind": "register_role", "role_id": "role_id", "name": "Role Name", '
            '"responsibility": "optional", "reports_to": "optional parent role_id", '
            '"team_id": "optional home team id for multi-team isolation", '
            f'"tool_tags": ["{tag_enum}"]}}, '
            '{"kind": "register_employee", "name": "Employee Name", "role_id": "registered role_id", '
            '"employee_id": "optional id", "description": "optional", '
            '"llm_config": {"default_model": "optional model override"}, '
            '"execution_agent": "optional native|codex|claude_code|cursor|opencode"}, '
            '{"kind": "use_team_template", "template_id": "team template id from the library", '
            '"team_id": "optional home team override"}]}\n'
            f"register_role.tool_tags (optional): each array item MUST be exactly one of these "
            f"categories — {tag_enum}. "
            "OMIT the whole field when unsure; do NOT invent tag names (a role whose tool_tags "
            "contain unknown values leaves its employees unable to use ANY tool, because each "
            "value is matched against the tool's own category tag).\n"
            "Organization building rule (autonomy level B): register_role, "
            "register_employee and use_team_template are SAVED AS DRAFTS and never applied "
            "directly; the user must explicitly confirm afterwards — when the user confirms, "
            'emit {"kind": "confirm_org_build"} (draft_id optional).'
        )

    def _parse_response(self, raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
        return {"response": text, "actions": []}

    async def _apply_actions(
        self,
        actions: list[Any],
        project_id: str | None,
        on_create_work_item: Any | None = None,
    ) -> list[str]:
        applied: list[str] = []
        # 拆散成多文件会让 confirm 按 uuid 随机序落地，破坏 role→employee 依赖序
        org_build_actions: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = str(action.get("kind", "")).strip()
            if kind == "import_skill":
                path = str(action.get("path", "")).strip()
                if not path:
                    applied.append("skipped skill import because no skill path was provided")
                    continue
                resolved = Path(path).expanduser().resolve()
                if self._allowed_skill_import_roots is not None and not any(
                    resolved.is_relative_to(root) for root in self._allowed_skill_import_roots
                ):
                    applied.append(f"skipped skill import outside allowed roots: {path}")
                    continue
                if not resolved.is_dir():
                    applied.append(f"skipped skill import because path is not a directory: {path}")
                    continue
                try:
                    result = self.import_skill(str(resolved))
                    summary = f"imported skill `{result.skill_name}` from {resolved}"
                    if result.enabled_domains:
                        summary += f"; auto-injected for {', '.join(result.enabled_domains)}"
                    applied.append(summary)
                except SkillImportError as exc:
                    applied.append(f"skill import failed: {exc}")
                continue
            if kind == "create_work_item":
                applied.append(self._create_work_item_from_action(action, on_create_work_item))
                continue
            # 跨实例存活），confirm_org_build 显式确认后才批量落地
            if kind in ("register_role", "register_employee", "use_team_template"):
                org_build_actions.append(action)
                continue
            if kind == "confirm_org_build":
                outcome = self._confirm_org_build(action)
                if isinstance(outcome, list):
                    applied.extend(outcome)
                else:
                    applied.append(outcome)
                continue
        if org_build_actions:
            applied.append(self._save_org_draft(org_build_actions))
        return applied

    # -- 组织构建（招聘助手 MVP） --

    def _save_org_draft(self, actions: list[dict[str, Any]]) -> str:
        if self._org_engine is None:
            return "skipped org build because no org engine is configured"
        try:
            self._org_drafts_dir.mkdir(parents=True, exist_ok=True)
            draft_id = uuid4().hex[:8]
            draft_path = self._org_drafts_dir / f"{draft_id}.json"
            temp_path = draft_path.with_suffix(draft_path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(
                    {"draft_id": draft_id, "actions": actions}, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            temp_path.replace(draft_path)
            summary = " + ".join(
                f"{a.get('kind')}({a.get('role_id') or a.get('employee_id') or a.get('template_id') or a.get('name') or '?'})"
                for a in actions
            )
            return (
                f"org build draft `{draft_id}` saved ({len(actions)} action(s): {summary}) — "
                "awaiting confirmation (reply confirm to apply)"
            )
        except OSError as exc:
            return f"org build draft save failed: {exc}"

    def _confirm_org_build(self, action: dict[str, Any]) -> list[str] | str:
        """确认落地：draft_id 指定单个草案；'latest' 或缺省 = 全部待确认草案。

        批量逐条 fail-closed——单条失败不中断其余条目；落地后草案文件删除。
        """
        if self._org_engine is None:
            return "skipped org build confirmation because no org engine is configured"
        try:
            self._org_drafts_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"org build confirmation failed: {exc}"
        draft_id = str(action.get("draft_id", "")).strip()
        if draft_id and draft_id != "latest":
            try:
                safe_id = validate_path_segment(draft_id, label="draft_id", error_cls=ValueError)
            except ValueError as exc:
                return f"org build confirmation failed: {exc}"
            paths = [self._org_drafts_dir / f"{safe_id}.json"]
        else:
            paths = sorted(self._org_drafts_dir.glob("*.json"))
        if not paths:
            return "no pending org build drafts to confirm"
        results: list[str] = []
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                results.append(f"draft {path.stem} load failed: {exc}")
                continue
            if not isinstance(data, dict):
                results.append(f"draft {path.stem} load failed: 结构非法（顶层非对象）")
                continue
            actions = data.get("actions")
            if not isinstance(actions, list):
                results.append(f"draft {path.stem} load failed: actions 字段非法")
                continue
            for item in actions:
                if isinstance(item, dict):
                    results.append(self._apply_org_build_action(item))
            path.unlink(missing_ok=True)
        return results

    def _apply_org_build_action(self, action: dict[str, Any]) -> str:
        kind = str(action.get("kind", "")).strip()
        engine = self._org_engine
        try:
            if kind == "register_role":
                role_id = str(action.get("role_id", "")).strip()
                name = str(action.get("name", "")).strip()
                if not role_id or not name:
                    return "register_role failed: role_id and name are required"
                from bandi.company.employee_lifecycle import EmployeeTombstoneStore

                if EmployeeTombstoneStore().get(role_id) is not None:
                    return f"register_role failed: IDENTITY_RESERVED ({role_id})"
                engine.register_role(
                    role_id,
                    name,
                    responsibility=str(action.get("responsibility", "") or ""),
                    reports_to=action.get("reports_to") or None,
                    tool_tags=[str(t) for t in action.get("tool_tags", []) or []],
                    team_id=str(action.get("team_id", "") or ""),
                )
                return f"registered role `{role_id}`"
            if kind == "register_employee":
                from bandi.company.employee_lifecycle import EmployeeTombstoneStore
                from bandi.company.org_engine import OrgEmployee
                from bandi.company.staffing import find_active_role_employee

                name = str(action.get("name", "")).strip()
                role_id = str(action.get("role_id", "")).strip()
                if not name or not role_id:
                    return "register_employee failed: name and role_id are required"
                if engine.get_role(role_id) is None:
                    return (
                        f"register_employee failed: role `{role_id}` is not registered "
                        "(fail-closed — register the role first)"
                    )
                employee_id = str(action.get("employee_id", "") or name).strip()
                tombstones = EmployeeTombstoneStore()
                if tombstones.get(employee_id) is not None or tombstones.get(role_id) is not None:
                    return f"register_employee failed: IDENTITY_RESERVED ({employee_id})"
                occupant = find_active_role_employee(engine, role_id)
                if occupant is not None:
                    return (
                        f"register_employee failed: role `{role_id}` already staffed by "
                        f"{occupant.employee_id}"
                    )
                llm_config = action.get("llm_config")
                employee = OrgEmployee(
                    employee_id=employee_id,
                    name=name,
                    role_id=role_id,
                    description=str(action.get("description", "") or ""),
                    llm_config=dict(llm_config)
                    if isinstance(llm_config, dict) and llm_config
                    else None,
                    metadata=(
                        {"selected_execution_agent": str(action["execution_agent"])}
                        if action.get("execution_agent")
                        else {}
                    ),
                )
                engine.register_employee(employee)
                return f"registered employee `{employee_id}` (role={role_id})"
            if kind == "use_team_template":
                from bandi.company.team_template_store import (
                    TeamTemplateStore,
                    build_org_from_template,
                )

                template_id = str(action.get("template_id", "")).strip()
                if not template_id:
                    return "use_team_template failed: template_id is required"
                store = self._template_store or TeamTemplateStore()
                template = store.get_template(template_id)
                if template is None:
                    return f"use_team_template failed: template `{template_id}` not found"
                override = str(action.get("team_id", "") or "").strip()
                if override:
                    template = template.model_copy(update={"team_id": override})
                report = build_org_from_template(
                    template,
                    engine,
                    recruiter=self._recruiter,
                    profile_manager=self._profile_manager,
                )
                return (
                    f"instantiated team `{report.team_id}`: "
                    f"{len(report.created_role_ids)} roles / "
                    f"{len(report.created_employee_ids)} employees"
                )
        except (KeyError, TypeError, ValueError, OSError, BandiError) as exc:
            return f"{kind} failed: {exc}"
        return f"unknown org build action kind: {kind!r}"

    def _create_work_item_from_action(
        self, action: dict[str, Any], on_create_work_item: WorkItemCreator | None
    ) -> str:
        if on_create_work_item is None:
            return "skipped work item creation because no creator was provided"
        title = str(action.get("title", "")).strip()
        if not title:
            return "skipped work item creation because no title was provided"
        payload = {
            "title": title,
            "turn_type": str(action.get("turn_type", "execute") or "execute").strip(),
            "assignee_role": str(action.get("assignee_role", "") or "").strip(),
            "description": str(action.get("description", "") or "").strip(),
        }
        try:
            work_item_id = on_create_work_item(payload)
            return f"created work item `{work_item_id}`"
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return f"work item creation failed: {exc}"

    # -- Skill 导入 --

    def import_skill(self, path: str):
        importer = SkillImporter(skills_dir=self._skills_dir)
        return importer.import_skill(path)
