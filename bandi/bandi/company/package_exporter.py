# 功能：市场包体系 — 从当前 org 导出为 OPCPackage

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .package_format import OPCPackage, OPCPackageManifest, PackageAuthor, PackageContents

_PACKAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_package_id(package_id: str, base_dir: Path) -> str:
    """package_id 双重校验：正则拒绝
    非法字符 + resolve 后 within 判定兜底。"""
    normalized = str(package_id or "").strip()
    if not _PACKAGE_ID_RE.match(normalized):
        raise ValueError(f"Invalid package id: {package_id!r}")
    base = base_dir.resolve()
    target = (base / normalized).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"Package id escapes target directory: {package_id!r}")
    return normalized


class PackageExporter:
    """将当前组织配置导出为自包含的 .opcpkg 目录。"""

    def __init__(self, org_engine: Any, talent_market: Any, home: Path) -> None:
        self.org_engine = org_engine
        self.talent_market = talent_market
        self.home = home

    def export_current(
        self,
        package_id: str,
        name: str,
        description: str = "",
        version: str = "1.0.0",
        author_name: str = "",
        author_github: str = "",
    ) -> OPCPackage:
        """从当前 org 构建 OPCPackage。"""
        roles = [
            {
                "id": r.id,
                "name": r.name,
                "reports_to": r.reports_to,
                "can_spawn": list(r.can_spawn),
                "responsibility": r.responsibility,
                "permissions": dict(r.permissions),
            }
            for r in self.org_engine.list_agents()
        ]
        templates = self.talent_market.list_templates()
        talent_dicts = [
            {
                "id": t.id or t.name,
                "name": t.name,
                "category": t.category,
                "description": t.description,
                "skills": list(t.skills),
                "system_prompt": t.system_prompt,
                "domains": list(t.domains),
                "prompt_ref": t.prompt_ref,
                "team_id": getattr(t, "team_id", ""),
            }
            for t in templates
        ]
        employees = [
            {
                "employee_id": e.employee_id,
                "name": e.name,
                "role_id": e.role_id,
                "template_id": e.template_id,
                "status": e.status,
            }
            for e in self.org_engine.list_employees()
        ]

        manifest = OPCPackageManifest(
            id=package_id,
            name=name,
            description=description,
            version=version,
            author=PackageAuthor(name=author_name, github=author_github),
            contents=PackageContents(
                roles=len(roles),
                talent_templates=len(talent_dicts),
            ),
        )

        return OPCPackage(
            manifest=manifest,
            roles=roles,
            talent_templates=talent_dicts,
            employees=employees,
            readme=self._generate_readme(manifest, roles),
        )

    def write_to_path(
        self, package: OPCPackage, out_dir: Path, package_id: str | None = None
    ) -> Path:
        """将 OPCPackage 写出为 .opcpkg 目录（package_id 显式校验防穿越）。"""
        normalized = _validate_package_id(package_id or package.manifest.id, out_dir)
        pkg_dir = out_dir / f"{normalized}.opcpkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        with open(pkg_dir / "manifest.yaml", "w", encoding="utf-8") as f:
            yaml.dump(
                {
                    "id": package.manifest.id,
                    "name": package.manifest.name,
                    "description": package.manifest.description,
                    "version": package.manifest.version,
                    "author": {
                        "name": package.manifest.author.name,
                        "github": package.manifest.author.github,
                    },
                    "license": package.manifest.license,
                    "category": package.manifest.category,
                    "tags": package.manifest.tags,
                    "team_size": package.manifest.team_size,
                    "contents": {
                        "roles": package.manifest.contents.roles,
                        "talent_templates": package.manifest.contents.talent_templates,
                    },
                },
                f,
                default_flow_style=False,
                allow_unicode=True,
            )

        org_data: dict = {
            "roles": package.roles,
            "talent_templates": package.talent_templates,
            "employees": package.employees,
        }
        with open(pkg_dir / "org_config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(org_data, f, default_flow_style=False, allow_unicode=True)

        if package.prompt_contents:
            prompts_dir = pkg_dir / "prompts"
            prompts_dir.mkdir(exist_ok=True)
            for filename, content in package.prompt_contents.items():
                # 仅保留安全字符，丢弃 Windows 保留名和空字符
                safe_name = "".join(c for c in filename if c.isalnum() or c in "-_.")
                safe_name = safe_name.replace("..", "")
                if safe_name and safe_name.upper() not in {
                    "CON",
                    "PRN",
                    "AUX",
                    "NUL",
                    "COM1",
                    "COM2",
                    "COM3",
                    "COM4",
                    "COM5",
                    "COM6",
                    "COM7",
                    "COM8",
                    "COM9",
                    "LPT1",
                    "LPT2",
                    "LPT3",
                    "LPT4",
                    "LPT5",
                    "LPT6",
                    "LPT7",
                    "LPT8",
                    "LPT9",
                }:
                    with open(prompts_dir / safe_name, "w", encoding="utf-8") as f:
                        f.write(content)

        with open(pkg_dir / "README.md", "w", encoding="utf-8") as f:
            f.write(package.readme)

        return pkg_dir

    def _generate_readme(self, manifest: OPCPackageManifest, roles: list[dict]) -> str:
        lines = [
            f"# {manifest.name}",
            "",
            manifest.description or "A Bandi architecture package.",
            "",
            f"- **Version**: {manifest.version}",
            f"- **Category**: {manifest.category}",
            f"- **Roles**: {manifest.contents.roles}",
            f"- **Talent Templates**: {manifest.contents.talent_templates}",
            "",
            "## Roles",
            "",
        ]
        for r in roles:
            rid = r.get("id", "?")
            rname = r.get("name", rid)
            resp = r.get("responsibility", "")
            lines.append(f"- **{rname}** (`{rid}`): {resp}")
        return "\n".join(lines) + "\n"
