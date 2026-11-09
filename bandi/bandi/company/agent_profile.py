# 功能：数字员工能力档案模型——AgentProfile（per-Employee 能力档案，四类资源
# 绑定即校验）+ CapabilityContext（单机裁剪版调用上下文）+ CapabilityRegistry（能力提供方
# 注册 / seal / sha256 快照）。
# 四类 resource_type：skill/general_skill/knowledge_base/tool）+ backend/app/capabilities/
# contracts.py（CapabilityContext + 三 Protocol）+ capabilities/registry.py（register/seal/
# snapshot/rehydrate）——490 行全读。
# ① 模块落 company 域（与 OrgEngine 同层），独立 AgentProfile 以 employee_id 引用组织层；
# ② 绑定即校验：skill→SkillCard 落库卡、general_skill→SKILL.md 轨、tool→ToolRegistry
# knowledge_base→KnowledgeStore（注入时校验 base 存在；未注入时只存引用，兼容环境）；
# ④ Registry seal + sha256 快照 id，不rehydrate（无 provider deployment 概念）；
# ⑤ 模型绑定（default/router/step 等 5 角色）不做：Bandi 已有 skill_model_config 派生
# 预算链，避免双轨（=A）。
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from bandi.core.exceptions import BandiError
from bandi.skills.card import SkillCard
from bandi.skills.path_guard import validate_path_segment
from bandi.skills.serializer import load_card

if TYPE_CHECKING:
    from bandi.knowledge.store import KnowledgeStore
    from bandi.tools.registry import ToolRegistry

_PROFILE_SUFFIX = ".profile.json"
_TEMP_PREFIX = ".{profile_id}.tmp-{pid}-{token}"
MAX_PROFILE_ID_LENGTH = 64
DEFAULT_PROFILES_DIR = "~/.bandi/agents/profiles"
DEFAULT_SKILLS_DIR = "~/.bandi/skills"


class AgentProfileError(BandiError):
    """档案操作失败（参数非法 / 文件读写失败 / 绑定状态非法）。"""


class AgentProfileNotFoundError(AgentProfileError):
    """档案不存在。"""


class AgentProfileDuplicateError(AgentProfileError):
    """档案已存在（1:1 于 employee_id，重复建档拒绝）。"""


class AgentResourceInvalidError(AgentProfileError):
    """资源绑定校验失败（不存在 / 未知类型 / 未绑定）。"""


class AgentResourceDuplicateError(AgentProfileError):
    """同 (resource_type, resource_id) 重复绑定。"""


class CapabilityRegistryError(AgentProfileError):
    """能力注册表操作失败（重复注册 / seal 后变更 / 契约版本不支持 / 未配置）。"""


class AgentResourceType(StrEnum):
    """功能：可绑定资源类型。"""

    SKILL = "skill"  # 功能：SkillCard 图卡
    GENERAL_SKILL = "general_skill"  # 功能：SKILL.md 技能包（importer 轨）
    KNOWLEDGE_BASE = "knowledge_base"  # 功能：知识库引用
    TOOL = "tool"  # 功能：工具注册表内的工具名


class AgentResourceBinding(BaseModel):
    """功能：一条能力资源绑定（档案内唯一键 = resource_type + resource_id）。"""

    model_config = ConfigDict(extra="forbid")

    resource_type: AgentResourceType  # 功能：资源类型
    resource_id: str  # 功能：资源标识（卡 id / 技能名 / 工具名 / 知识库 id）
    status: Literal["active", "inactive"] = "active"  # 功能：绑定状态
    metadata: dict[str, Any] = Field(default_factory=dict)  # 功能：绑定级扩展信息
    created_at: str  # 功能：绑定时刻（ISO 8601）
    updated_at: str  # 功能：最近变更时刻


class AgentProfile(BaseModel):
    """功能：数字员工能力档案（1:1 于 employee_id，组织层解耦——=A）。"""

    model_config = ConfigDict(extra="forbid")

    profile_id: str  # 功能：档案标识（= employee_id，1:1）
    employee_id: str  # 功能：归属组织层员工（引用 OrgEmployee.employee_id）
    name: str  # 功能：档案显示名
    description: str = ""  # 功能：档案描述
    persona_prompt: str = ""  # 功能：人设提示词（服务风格）
    status: Literal["active", "archived"] = "active"  # 功能：档案状态
    resources: list[AgentResourceBinding] = Field(default_factory=list)  # 功能：能力绑定集
    metadata: dict[str, Any] = Field(default_factory=dict)  # 功能：档案级扩展信息
    created_at: str  # 功能：建档时刻
    updated_at: str  # 功能：最近变更时刻


@dataclass(frozen=True)
class CapabilityContext:
    """功能：能力调用上下文（裁剪版——=A）。

    裁掉的 tenant_id/channel/turn_id/deadline_at
    保留 agent/session/request/trace/attempt 五字段；必填字段非空 + attempt 正整数。"""

    agent_id: str
    session_id: str
    request_id: str
    trace_id: str | None = None
    attempt: int = 1

    def __post_init__(self) -> None:
        required = {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
        }
        missing = [
            name
            for name, value in required.items()
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            raise ValueError("CapabilityContext requires non-empty fields: " + ", ".join(missing))
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 1:
            raise ValueError("CapabilityContext attempt must be a positive integer")


@dataclass(frozen=True)
class DurableCapabilityBinding:
    """功能：可序列化的绑定身份（刻意不含 live provider 实例）。"""

    capability: str
    provider_id: str
    provider_deployment_id: str
    service_contract_version: str


@dataclass(frozen=True)
class CapabilityBinding:
    """功能：能力 → 提供方绑定（注册是配置行为，非运行时热切换）。"""

    capability: str
    provider_id: str
    provider_deployment_id: str
    service_contract_version: str
    provider: Any = None

    def durable(self) -> DurableCapabilityBinding:
        return DurableCapabilityBinding(
            capability=self.capability,
            provider_id=self.provider_id,
            provider_deployment_id=self.provider_deployment_id,
            service_contract_version=self.service_contract_version,
        )


@dataclass(frozen=True)
class CapabilitySnapshot:
    """功能：一次 Agent 回合内固定的提供方选择（不可变）。"""

    bindings: MappingProxyType  # 功能：capability -> CapabilityBinding 只读视图
    snapshot_id: str  # 功能：规范化绑定的 sha256（审计/回放锚点）
    durable_bindings: tuple[DurableCapabilityBinding, ...]  # 功能：可序列化身份集

    def get(self, capability: str) -> CapabilityBinding | None:
        return self.bindings.get(capability)

    def require(self, capability: str) -> CapabilityBinding:
        binding = self.get(capability)
        if binding is None:
            raise CapabilityRegistryError(f"capability provider is not configured: {capability}")
        return binding


class CapabilityRegistry:
    """功能：显式能力注册表——register / seal / snapshot。"""

    def __init__(self) -> None:
        self._bindings: dict[str, CapabilityBinding] = {}
        self._sealed = False

    def seal(self) -> None:
        """功能：标记启动期注册完成；seal 后禁止再变更。"""
        self._sealed = True

    def register(self, binding: CapabilityBinding) -> None:
        if self._sealed:
            raise CapabilityRegistryError("capability registry is sealed")
        if (
            not binding.capability
            or not binding.provider_id
            or not binding.provider_deployment_id
            or not binding.service_contract_version
        ):
            raise CapabilityRegistryError(
                "capability, provider_id, provider_deployment_id and contract_version are required"
            )
        if binding.capability in self._bindings:
            raise CapabilityRegistryError(f"capability already registered: {binding.capability}")
        self._bindings[binding.capability] = binding

    def snapshot(
        self,
        requested: set[str] | None = None,
        *,
        supported_contracts: dict[str, set[str]] | None = None,
    ) -> CapabilitySnapshot:
        selected = {
            key: value
            for key, value in self._bindings.items()
            if requested is None or key in requested
        }
        if supported_contracts is not None:
            unsupported = [
                f"{key}={binding.service_contract_version}"
                for key, binding in selected.items()
                if binding.service_contract_version not in supported_contracts.get(key, set())
            ]
            if unsupported:
                raise CapabilityRegistryError(
                    "unsupported capability contract: " + ", ".join(sorted(unsupported))
                )
        durable_bindings = tuple(binding.durable() for _, binding in sorted(selected.items()))
        canonical = [
            {
                "capability": key,
                "provider_id": binding.provider_id,
                "provider_deployment_id": binding.provider_deployment_id,
                "service_contract_version": binding.service_contract_version,
            }
            for key, binding in sorted(selected.items())
        ]
        snapshot_id = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()
        return CapabilitySnapshot(MappingProxyType(selected), snapshot_id, durable_bindings)


class AgentProfileManager:
    """功能：能力档案管理器——建档 / 绑定（即校验）/ 快照上下文，原子落盘。"""

    def __init__(
        self,
        *,
        profiles_dir: str = DEFAULT_PROFILES_DIR,
        skills_dir: str = DEFAULT_SKILLS_DIR,
        tool_registry: ToolRegistry | None = None,
        knowledge_store: KnowledgeStore | None = None,
    ) -> None:
        self.profiles_dir = profiles_dir
        self.skills_dir = skills_dir
        self._tool_registry = tool_registry
        self._knowledge_store = knowledge_store

    # ---------------------------------------------------------------- 建档 / 读取 / 更新

    def create_profile(
        self,
        employee_id: str,
        *,
        name: str = "",
        description: str = "",
        persona_prompt: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AgentProfile:
        """功能：建档（profile_id = employee_id，1:1；重复建档拒绝）。"""
        profile_id = _validate_profile_id(employee_id)
        if self.get_profile(profile_id) is not None:
            raise AgentProfileDuplicateError(f"档案已存在: {profile_id}")
        now = _now()
        profile = AgentProfile(
            profile_id=profile_id,
            employee_id=profile_id,
            name=name or profile_id,
            description=description,
            persona_prompt=persona_prompt,
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        self._persist(profile)
        return profile

    def get_profile(self, profile_id: str) -> AgentProfile | None:
        """功能：读档案；不存在返回 None，内容非法显式失败。"""
        path = self._profile_path(profile_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AgentProfileError(f"无法读取档案: {path}: {exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AgentProfileError(f"档案文件非法 JSON: {path}: {exc}") from exc
        try:
            return AgentProfile(**payload)
        except (TypeError, ValueError) as exc:
            raise AgentProfileError(f"档案 Schema 校验失败: {path}: {exc}") from exc

    def require_profile(self, profile_id: str) -> AgentProfile:
        profile = self.get_profile(profile_id)
        if profile is None:
            raise AgentProfileNotFoundError(f"档案不存在: {profile_id}")
        return profile

    def delete_profile(self, profile_id: str) -> bool:
        """功能：删除单个员工档案文件（不存在返回 False；失败显式抛出）。"""
        path = self._profile_path(profile_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise AgentProfileError(f"档案删除失败: {path}: {exc}") from exc
        return True

    def list_profiles(self) -> list[AgentProfile]:
        """功能：列出全部档案（按 profile_id 字母序）。"""
        base = Path(self.profiles_dir).expanduser()
        if not base.exists():
            return []
        profiles = []
        for entry in sorted(base.glob(f"*{_PROFILE_SUFFIX}")):
            profiles.append(self.require_profile(entry.name[: -len(_PROFILE_SUFFIX)]))
        return profiles

    def update_profile(
        self,
        profile_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        persona_prompt: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentProfile:
        """功能：更新档案字段（至少给一个字段，全空拒绝）。"""
        profile = self.require_profile(profile_id)
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if persona_prompt is not None:
            updates["persona_prompt"] = persona_prompt
        if status is not None:
            if status not in ("active", "archived"):
                raise AgentProfileError(f"status 仅允许 active|archived: {status!r}")
            updates["status"] = status
        if metadata is not None:
            updates["metadata"] = dict(metadata)
        if not updates:
            raise AgentProfileError("update_profile 至少需要一个待更新字段")
        updates["updated_at"] = _now()
        return self._persist(profile.model_copy(update=updates))

    # ---------------------------------------------------------------- 绑定（即校验）

    def bind_resource(
        self,
        profile_id: str,
        resource_type: AgentResourceType | str,
        resource_id: str,
        *,
        metadata: dict[str, Any] | None = None,
        status: str = "active",
    ) -> AgentProfile:
        """功能：绑定能力资源——绑定时即校验存在性（=A）。

        skill → SkillCard 落库卡；general_skill → `<skills_dir>/<id>/SKILL.md`；
        tool → ToolRegistry.get；knowledge_base → KnowledgeStore.has_base（未注入存储时只存引用）。
        """
        profile = self.require_profile(profile_id)
        res_type = _coerce_resource_type(resource_type)
        res_id = _validate_resource_id(res_type, resource_id)
        self._validate_resource(res_type, res_id)
        if any(
            item.resource_type == res_type and item.resource_id == res_id
            for item in profile.resources
        ):
            raise AgentResourceDuplicateError(f"资源已绑定: {res_type.value}/{res_id}")
        now = _now()
        binding = AgentResourceBinding(
            resource_type=res_type,
            resource_id=res_id,
            status=status,
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        return self._persist(
            profile.model_copy(
                update={"resources": [*profile.resources, binding], "updated_at": now}
            )
        )

    def unbind_resource(
        self,
        profile_id: str,
        resource_type: AgentResourceType | str,
        resource_id: str,
    ) -> AgentProfile:
        """功能：解绑资源（未绑定显式失败）。"""
        profile = self.require_profile(profile_id)
        res_type = _coerce_resource_type(resource_type)
        res_id = _validate_resource_id(res_type, resource_id)
        remaining = [
            item
            for item in profile.resources
            if not (item.resource_type == res_type and item.resource_id == res_id)
        ]
        if len(remaining) == len(profile.resources):
            raise AgentResourceInvalidError(f"资源未绑定: {res_type.value}/{res_id}")
        return self._persist(
            profile.model_copy(update={"resources": remaining, "updated_at": _now()})
        )

    def set_resource_status(
        self,
        profile_id: str,
        resource_type: AgentResourceType | str,
        resource_id: str,
        *,
        status: str,
    ) -> AgentProfile:
        """功能：切换绑定状态（active | inactive）。"""
        profile = self.require_profile(profile_id)
        res_type = _coerce_resource_type(resource_type)
        res_id = _validate_profile_id(resource_id)
        if status not in ("active", "inactive"):
            raise AgentProfileError(f"绑定 status 仅允许 active|inactive: {status!r}")
        found = False
        resources: list[AgentResourceBinding] = []
        for item in profile.resources:
            if item.resource_type == res_type and item.resource_id == res_id:
                found = True
                resources.append(item.model_copy(update={"status": status, "updated_at": _now()}))
            else:
                resources.append(item)
        if not found:
            raise AgentResourceInvalidError(f"资源未绑定: {res_type.value}/{res_id}")
        return self._persist(
            profile.model_copy(update={"resources": resources, "updated_at": _now()})
        )

    # ---------------------------------------------------------------- 上下文桥

    def build_context(
        self,
        profile_id: str,
        *,
        session_id: str,
        request_id: str,
        trace_id: str | None = None,
        attempt: int = 1,
    ) -> CapabilityContext:
        """功能：从档案构建能力调用上下文（agent_id = profile_id）。"""
        self.require_profile(profile_id)
        return CapabilityContext(
            agent_id=profile_id,
            session_id=session_id,
            request_id=request_id,
            trace_id=trace_id,
            attempt=attempt,
        )

    # ---------------------------------------------------------------- 内部

    def _profile_path(self, profile_id: str) -> Path:
        validated = _validate_profile_id(profile_id)
        return Path(self.profiles_dir).expanduser() / f"{validated}{_PROFILE_SUFFIX}"

    def _persist(self, profile: AgentProfile) -> AgentProfile:
        final_path = self._profile_path(profile.profile_id)
        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            raise AgentProfileError(f"无法创建档案目录: {final_path.parent}: {exc}") from exc
        tmp_path = final_path.parent / _TEMP_PREFIX.format(
            profile_id=profile.profile_id, pid=os.getpid(), token=uuid.uuid4().hex[:8]
        )
        try:
            data = json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise AgentProfileError(f"档案含不可 JSON 序列化字段: {exc}") from exc
        try:
            tmp_path.write_text(data, encoding="utf-8")
            tmp_path.replace(final_path)
        except (OSError, ValueError) as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise AgentProfileError(f"档案写入失败: {final_path}: {exc}") from exc
        return profile

    def _validate_resource(self, res_type: AgentResourceType, res_id: str) -> None:
        """功能：绑定即校验——按资源类型解析存在性（knowledge_base 显式豁免）。"""
        if res_type is AgentResourceType.SKILL:
            path = Path(self.skills_dir).expanduser() / f"{res_id}.card.json"
            try:
                card: SkillCard | None = load_card(path)
            except Exception as exc:
                raise AgentResourceInvalidError(f"技能卡不存在或非法: {res_id}: {exc}") from exc
            if card is None:
                raise AgentResourceInvalidError(f"技能卡不存在: {res_id}")
            return
        if res_type is AgentResourceType.GENERAL_SKILL:
            skill_md = Path(self.skills_dir).expanduser() / res_id / "SKILL.md"
            if not skill_md.is_file():
                raise AgentResourceInvalidError(f"SKILL.md 技能不存在: {res_id}")
            return
        if res_type is AgentResourceType.TOOL:
            if self._tool_registry is None:
                from bandi.tools.registry import ToolRegistry

                self._tool_registry = ToolRegistry()
            if self._tool_registry.get(res_id) is None:
                raise AgentResourceInvalidError(f"工具未注册: {res_id}")
            return
        # KNOWLEDGE_BASE：注入 KnowledgeStore 时校验 base 存在；未注入时只存引用。
        if self._knowledge_store is None:
            return
        if not self._knowledge_store.has_base(res_id):
            raise AgentResourceInvalidError(f"知识库不存在: {res_id}")


# 功能：引用型资源 id 的字符口径——只是键（不落文件名），允许命名空间冒号与点
# （知识库 base id 形如 `kb:design-org`，工具名形如 `fs.read`）。`\w` 含下划线与中文。
_RESOURCE_ID_RE = re.compile(rf"^[\w.:\-]{{1,{MAX_PROFILE_ID_LENGTH}}}$")


def _validate_resource_id(res_type: AgentResourceType, resource_id: str) -> str:
    """功能：资源 id 按类型校验——落文件名的技能保持严格，纯引用型放宽到允许冒号。

    SKILL / GENERAL_SKILL 的 id 会拼成 `<skills_dir>/<id>.card.json` 或 `<id>/SKILL.md`，
    必须仍是安全路径片段；TOOL / KNOWLEDGE_BASE 只是引用键，冒号与点都必须放行，
    否则 `kb:design-org` 这类命名空间 id 永远绑不上员工。
    """
    raw = str(resource_id or "")
    if res_type in (AgentResourceType.SKILL, AgentResourceType.GENERAL_SKILL):
        return _validate_profile_id(raw)
    if not _RESOURCE_ID_RE.fullmatch(raw):
        raise AgentResourceInvalidError(
            f"resource_id 含不安全字符（仅允许字母数字/下划线/连字符/点/冒号）: {raw!r}"
        )
    return raw


def _validate_profile_id(profile_id: str) -> str:
    """功能：档案/资源片段安全校验（委托 path_guard，Unicode 口径允许中文档案名）。"""
    return validate_path_segment(
        profile_id,
        label="profile_id",
        error_cls=AgentProfileError,
        max_length=MAX_PROFILE_ID_LENGTH,
    )


def _coerce_resource_type(value: AgentResourceType | str) -> AgentResourceType:
    if isinstance(value, AgentResourceType):
        return value
    try:
        return AgentResourceType(str(value))
    except ValueError as exc:
        raise AgentResourceInvalidError(f"未知资源类型: {value!r}") from exc


def _now() -> str:
    return datetime.now(UTC).isoformat()
