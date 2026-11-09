# 功能：技能数据模型（SkillDef/匹配结果）
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillDef:
    name: str
    description: str
    triggers: list[str]
    instructions: str
    allowed_tools: list[str] | None = None
    constraints: list[str] = field(default_factory=list)
    resource_paths: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    always: bool = False
    team_id: str = ""  # 功能：团队归属（空 = 全局技能，对全部团队可见）
    # 功能：技能增强元数据——子代理执行 / 模型与思考档位覆盖
    fork: bool = False
    model: str = ""
    effort: str = ""
    enabled: bool = True


@dataclass
class SkillMatchResult:
    matched: list[SkillDef]
    level: int
