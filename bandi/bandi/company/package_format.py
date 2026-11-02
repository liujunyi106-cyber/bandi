# 功能：市场包体系 — 包格式数据模型

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class PackageCategory(StrEnum):
    """市场包分类——与 architecture_registry 蓝图 category 对齐。"""

    ENGINEERING = "engineering"
    ANALYTICS = "analytics"
    CONTENT = "content"
    DESIGN = "design"
    MANAGEMENT = "management"
    QUALITY = "quality"
    GENERAL = "general"


@dataclass
class PackageAuthor:
    name: str = ""
    github: str = ""

    def validate(self) -> list[str]:
        """返回校验警告列表（非空则 warn）。"""
        warnings: list[str] = []
        if not self.name.strip():
            warnings.append("PackageAuthor.name is empty")
        return warnings


@dataclass
class PackageContents:
    roles: int = 0
    talent_templates: int = 0
    skills: int = 0


@dataclass
class OPCPackageManifest:
    id: str
    name: str
    description: str = ""
    version: str = "1.0.0"
    author: PackageAuthor = field(default_factory=PackageAuthor)
    license: str = "MIT"
    category: str = "general"
    tags: list[str] = field(default_factory=list)
    team_size: str = ""
    contents: PackageContents = field(default_factory=PackageContents)
    industry: str = ""
    use_cases: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        """返回校验警告列表。"""
        warnings: list[str] = []
        if not self.id.strip():
            warnings.append("OPCPackageManifest.id is empty")
        if not self.name.strip():
            warnings.append("OPCPackageManifest.name is empty")
        if self.category not in {c.value for c in PackageCategory}:
            warnings.append(f"OPCPackageManifest.category '{self.category}' not in PackageCategory")
        warnings.extend(self.author.validate())
        return warnings


@dataclass
class InstalledPackageInfo:
    package_id: str
    name: str = ""
    version: str = "1.0.0"
    installed_at: str = ""
    source_path: str = ""
    role_ids: list[str] = field(default_factory=list)
    template_ids: list[str] = field(default_factory=list)
    industry: str = ""
    use_cases: list[str] = field(default_factory=list)

    @classmethod
    def from_manifest(cls, package_id: str, manifest: OPCPackageManifest) -> InstalledPackageInfo:
        """从 manifest 创建已安装记录（含 industry/use_cases 快照）。"""
        return cls(
            package_id=package_id,
            name=manifest.name,
            version=manifest.version,
            industry=manifest.industry,
            use_cases=list(manifest.use_cases),
        )


@dataclass
class OPCPackage:
    manifest: OPCPackageManifest
    roles: list[dict[str, Any]] = field(default_factory=list)
    talent_templates: list[dict[str, Any]] = field(default_factory=list)
    employees: list[dict[str, Any]] = field(default_factory=list)
    prompt_contents: dict[str, str] = field(default_factory=dict)
    readme: str = ""


@dataclass
class SandboxReport:
    passed: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ConflictReport:
    role_conflicts: list[str] = field(default_factory=list)
    template_conflicts: list[str] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.role_conflicts or self.template_conflicts)
