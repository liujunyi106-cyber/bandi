# 功能：团队模板数据模型（TeamTemplate：org 拓扑 + 任务图 + skills/SOP 绑定 + 模型配置 +
# 项目制关联）。构造即校验（拓扑引用完整 / id 唯一 / reports_to 无环 / 任务图无环）
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bandi.core.exceptions import BandiError
from bandi.skills.path_guard import validate_path_segment


class TeamTemplateError(BandiError):
    """团队模板操作失败（参数非法 / 落盘读写失败）。"""


class TeamTemplateNotFoundError(TeamTemplateError):
    """模板不存在（导出等必须有产物的入口）。"""


class TeamTemplateSaveError(TeamTemplateError):
    """模板写盘失败。"""


class TeamTemplateLoadError(TeamTemplateError):
    """模板读取失败：文件非法 JSON / schema 校验不符（与缺失区分）。"""


class TeamRoleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # 功能：角色标识（模板内唯一；路径安全——与 profile/员工/session id 口径合一）
    name: str  # 功能：角色显示名
    responsibility: str = ""  # 功能：职责描述
    reports_to: str | None = None  # 功能：模板内上级角色 id（None = 顶层）
    can_spawn: list[str] = Field(default_factory=list)  # 功能：可派生角色 id 列表
    tool_tags: list[str] = Field(default_factory=list)  # 功能：角色工具面标签（空 = 不限）
    is_final_decider: bool = False  # 功能：是否团队最终决策角色
    fs_scopes: list[str] = Field(default_factory=list)  # 功能：角色落盘目录白名单
    skill_refs: list[str] = Field(default_factory=list)  # 功能：角色技能绑定
    prompt_refs: list[str] = Field(default_factory=list)  # 功能：角色提示词要点
    llm_config: dict[str, Any] = Field(default_factory=dict)  # 功能：per-role 模型配置

    @field_validator("id")
    @classmethod
    def _validate_role_id(cls, value: str) -> str:
        # 与 template_id 同口径：含冒号/空白/穿越会让 prefix 后 id 在
        # profile 建档（path_guard）静默失败——角色/员工/profile 四 id 口径合一
        try:
            return validate_path_segment(value, label="role id", error_cls=TeamTemplateError)
        except TeamTemplateError as exc:
            raise ValueError(str(exc)) from exc


class TeamTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str  # 功能：模板标识（= 库文件名，路径安全校验）
    name: str  # 功能：模板显示名
    description: str = ""  # 功能：模板描述
    team_id: str = ""  # 功能：实例化后角色 home team（空 = 实例化时取 template_id）
    roles: list[TeamRoleSpec]  # 功能：角色集（非空）
    sop_refs: list[str] = Field(default_factory=list)  # 功能：团队级 SOP/SkillCard 引用
    project_id: str = ""  # 功能：项目制关联（项目标识）
    created_at: str = ""  # 功能：首次落盘时刻（store 维护）
    updated_at: str = ""  # 功能：最近落盘时刻（store 维护）

    @field_validator("template_id")
    @classmethod
    def _validate_template_id(cls, value: str) -> str:
        try:
            return validate_path_segment(value, label="template_id", error_cls=TeamTemplateError)
        except TeamTemplateError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not str(value or "").strip():
            raise ValueError("模板 name 不能为空")
        return value

    @model_validator(mode="after")
    def _validate_topology(self) -> TeamTemplate:
        errors: list[str] = []
        if not self.roles:
            errors.append("roles 不能为空")
        roles_by_id: dict[str, TeamRoleSpec] = {}
        for role in self.roles:
            if role.id in roles_by_id:
                errors.append(f"角色 id 重复: {role.id}")
            else:
                roles_by_id[role.id] = role
        for role in self.roles:
            parent = role.reports_to
            if parent is not None:
                if parent == role.id:
                    errors.append(f"角色 {role.id} 不能以自身为上级")
                elif parent not in roles_by_id:
                    errors.append(f"角色 {role.id} 的上级 {parent} 不在模板内")
            for spawn_id in role.can_spawn:
                if spawn_id not in roles_by_id:
                    errors.append(f"角色 {role.id} 的 can_spawn 引用 {spawn_id} 不在模板内")

        cycle_at = _find_reports_cycle(roles_by_id)
        if cycle_at is not None:
            errors.append(f"reports_to 环: {cycle_at}")
        if errors:
            raise ValueError("; ".join(errors))
        return self


def _find_reports_cycle(roles_by_id: dict[str, TeamRoleSpec]) -> str | None:
    """功能：沿 reports_to 链检环，返回环上角色 id（引用缺失视作链终止）。"""
    for role_id in roles_by_id:
        seen: set[str] = set()
        current: str | None = role_id
        while current:
            if current in seen:
                return current
            seen.add(current)
            role = roles_by_id.get(current)
            current = role.reports_to if role else None
    return None


def builtin_templates() -> list[TeamTemplate]:
    """功能：内置模板清单——5 个架构蓝图转换 + 选品团队 + 产品开发团队（开箱即用，不落盘）。"""
    from bandi.company.architecture_registry import ARCHITECTURE_PRESETS

    templates = [_from_blueprint(blueprint) for blueprint in ARCHITECTURE_PRESETS]
    templates.append(_selection_team())
    templates.append(_product_team())
    return templates


def _from_blueprint(blueprint: Any) -> TeamTemplate:
    """功能：架构蓝图 → 团队模板（reports_to="owner" 归一为顶层 None）。"""
    roles: list[TeamRoleSpec] = []
    for raw in blueprint.roles:
        reports_to = raw.get("reports_to")
        roles.append(
            TeamRoleSpec(
                id=str(raw["id"]),
                name=str(raw.get("name", raw["id"])),
                responsibility=str(raw.get("responsibility", "")),
                reports_to=None if reports_to in (None, "", "owner") else str(reports_to),
                can_spawn=[str(s) for s in raw.get("can_spawn", [])],
            )
        )
    return TeamTemplate(
        template_id=str(blueprint.id),
        name=str(blueprint.name),
        description=str(blueprint.description),
        roles=roles,
    )


def _selection_team() -> TeamTemplate:
    """功能：选品团队内置模板（用户核心场景：调研 → 选品 → 上架 → 售后闭环）。"""
    roles = [
        TeamRoleSpec(
            id="selection_lead",
            name="选品经理",
            responsibility="选品决策、供应商筛选与上架最终审批",
            can_spawn=["market_researcher", "supply_chain", "designer", "customer_service"],
            is_final_decider=True,
        ),
        TeamRoleSpec(
            id="market_researcher",
            name="市场调研",
            responsibility="市场趋势、竞品与需求分析",
            reports_to="selection_lead",
        ),
        TeamRoleSpec(
            id="supply_chain",
            name="供应链",
            responsibility="供应商对接、库存与物流管理",
            reports_to="selection_lead",
        ),
        TeamRoleSpec(
            id="designer",
            name="美工设计",
            responsibility="商品主图、详情页与店铺视觉",
            reports_to="selection_lead",
        ),
        TeamRoleSpec(
            id="customer_service",
            name="客服",
            responsibility="售前咨询与售后反馈收集",
            reports_to="selection_lead",
        ),
    ]
    return TeamTemplate(
        template_id="selection-team",
        name="选品团队",
        description="电商选品与上架子团队：调研 → 选品 → 铺货 → 上架评审 → 售后反馈闭环",
        roles=roles,
    )


def _product_team() -> TeamTemplate:
    """功能：产品开发团队内置模板（需求 → 信息架构/UI → 前后端并行 → 测试验收）。"""
    roles = [
        TeamRoleSpec(
            id="product_lead",
            name="产品经理",
            responsibility="需求规格、信息架构决策与交付评审",
            can_spawn=["designer", "frontend_dev", "backend_dev", "qa"],
            is_final_decider=True,
        ),
        TeamRoleSpec(
            id="designer",
            name="UI 设计",
            responsibility="信息架构、界面与交互方案",
            reports_to="product_lead",
        ),
        TeamRoleSpec(
            id="frontend_dev",
            name="前端工程师",
            responsibility="前端实现与联调",
            reports_to="product_lead",
        ),
        TeamRoleSpec(
            id="backend_dev",
            name="后端工程师",
            responsibility="后端接口与数据实现",
            reports_to="product_lead",
        ),
        TeamRoleSpec(
            id="qa",
            name="测试工程师",
            responsibility="功能验收与回归测试",
            reports_to="product_lead",
        ),
    ]
    return TeamTemplate(
        template_id="product-team",
        name="产品开发团队",
        description="产品开发闭环：需求规格 → 信息架构/UI 设计 → 前后端并行开发 → 测试验收（产品经理评审）",
        roles=roles,
    )
