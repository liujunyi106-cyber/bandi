from __future__ import annotations

import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .package_format import (
    ConflictReport,
    InstalledPackageInfo,
    OPCPackage,
)

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)
_PACKAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class PackageLoader:
    """从磁盘加载 .opcpkg 包并安装/卸载。"""

    def __init__(self, home: Path) -> None:
        self.home = home

    def load_from_path(self, pkg_path: Path) -> OPCPackage:
        """解析 .opcpkg 目录为 OPCPackage。"""
        pkg_path = pkg_path.expanduser().resolve()
        if not pkg_path.is_dir():
            raise FileNotFoundError(f"Package directory not found: {pkg_path}")

        manifest_path = pkg_path / "manifest.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Missing manifest.yaml in {pkg_path}")
        with open(manifest_path, encoding="utf-8") as f:
            manifest_data = yaml.safe_load(f) or {}

        org_path = pkg_path / "org_config.yaml"
        org_data: dict[str, Any] = {}
        if org_path.exists():
            with open(org_path, encoding="utf-8") as f:
                org_data = yaml.safe_load(f) or {}

        prompt_contents: dict[str, str] = {}
        prompts_dir = pkg_path / "prompts"
        if prompts_dir.is_dir():
            for p in sorted(prompts_dir.rglob("*.md")):
                try:
                    prompt_contents[p.name] = p.read_text(encoding="utf-8")
                except OSError:
                    logger.debug("Failed to read prompt %s", p)

        readme = ""
        readme_path = pkg_path / "README.md"
        if readme_path.exists():
            readme = readme_path.read_text(encoding="utf-8")

        return OPCPackage(
            manifest=type(
                "Manifest",
                (),
                {
                    "id": manifest_data.get("id", ""),
                    "name": manifest_data.get("name", ""),
                    "version": manifest_data.get("version", "1.0.0"),
                },
            )(),
            roles=org_data.get("roles", []),
            talent_templates=org_data.get("talent_templates", []),
            employees=org_data.get("employees", []),
            prompt_contents=prompt_contents,
            readme=readme,
        )

    def detect_conflicts(self, package: OPCPackage) -> ConflictReport:
        """检测包与当前 org 的 ID 冲突。"""
        report = ConflictReport()
        existing_role_ids = {r.id for r in getattr(self.org_engine, "list_agents", lambda: [])()}
        for role in package.roles:
            rid = role.get("id", "")
            if rid and rid in existing_role_ids:
                report.role_conflicts.append(rid)
        return report

    def install(
        self,
        package: OPCPackage,
        strategy: str = "namespace",
    ) -> InstalledPackageInfo:
        """安装包到当前 org。"""
        pkg_id = package.manifest.id
        if not _PACKAGE_ID_RE.match(pkg_id):
            raise ValueError(f"Invalid package id: {pkg_id!r}")

        from bandi.company.sandbox_checker import SandboxChecker

        report = SandboxChecker().validate(package)
        if not report.passed:
            raise ValueError(f"Package sandbox check failed: {'; '.join(report.errors)}")

        prefix = f"{pkg_id}:" if strategy == "namespace" else ""
        self._write_prompts(pkg_id, package.prompt_contents)

        org_engine = getattr(self, "org_engine", None)
        role_ids: list[str] = []
        template_ids: list[str] = []

        if org_engine is not None:
            # 第一遍全部不带 reports_to 注册并收集待补关系，第二遍补上级；
            # 逆拓扑序（子角色定义在前）不再崩溃，未知上级降级为顶层+告警
            pending_reports: dict[str, Any] = {}
            for role_data in package.roles:
                original_id = role_data.get("id", "")
                new_id = f"{prefix}{original_id}" if original_id else original_id
                role_data = dict(role_data)
                role_data["id"] = new_id
                if prefix:
                    if role_data.get("reports_to") and role_data["reports_to"] != "owner":
                        role_data["reports_to"] = f"{prefix}{role_data['reports_to']}"
                    if role_data.get("can_spawn"):
                        role_data["can_spawn"] = [f"{prefix}{s}" for s in role_data["can_spawn"]]
                org_engine.register_role(
                    role_id=new_id,
                    name=str(role_data.get("name", new_id)),
                    responsibility=str(role_data.get("responsibility", "")),
                    reports_to=None,
                    can_spawn=role_data.get("can_spawn", []),
                )
                pending_reports[new_id] = role_data.get("reports_to")
                role_ids.append(new_id)
            for new_id, reports_to in pending_reports.items():
                if not reports_to or reports_to == new_id:
                    continue
                try:
                    org_engine.set_reports_to(new_id, str(reports_to))
                except ValueError as exc:
                    logger.warning(
                        "package role %s reports_to unresolved (%s); kept as top-level",
                        new_id,
                        exc,
                    )

            # talent templates → filesystem
            for tmpl_data in package.talent_templates:
                original_id = str(tmpl_data.get("id", "") or "").strip()
                new_id = f"{prefix}{original_id}" if original_id else original_id
                template_ids.append(new_id)

            installed = getattr(org_engine, "installed_packages", None)
            if installed is None:
                org_engine.installed_packages = []
                installed = org_engine.installed_packages
            installed.append(
                InstalledPackageInfo(
                    package_id=pkg_id,
                    name=package.manifest.name,
                    version=package.manifest.version,
                    installed_at=datetime.now(UTC).isoformat(),
                    source_path=pkg_id,
                    role_ids=role_ids,
                    template_ids=template_ids,
                )
            )

        logger.info(
            "Installed package %s: %d roles, %d templates", pkg_id, len(role_ids), len(template_ids)
        )
        return InstalledPackageInfo(
            package_id=pkg_id,
            name=package.manifest.name,
            version=package.manifest.version,
            installed_at=datetime.now(UTC).isoformat(),
            source_path=pkg_id,
            role_ids=role_ids,
            template_ids=template_ids,
        )

    def uninstall(self, package_id: str) -> bool:
        """从 org 卸载已安装包。"""
        if not _PACKAGE_ID_RE.match(package_id):
            raise ValueError(f"Invalid package id: {package_id!r}")

        org_engine = getattr(self, "org_engine", None)
        if org_engine is None:
            return False

        installed = getattr(org_engine, "installed_packages", [])
        target = None
        for pkg in installed:
            pid = (
                pkg.package_id
                if isinstance(pkg, InstalledPackageInfo)
                else pkg.get("package_id", "")
            )
            if pid == package_id:
                target = pkg
                break
        if target is None:
            return False

        if isinstance(target, InstalledPackageInfo):
            role_ids = set(target.role_ids)
        else:
            role_ids = set(target.get("role_ids", []))

        org_engine._roles = {k: v for k, v in org_engine._roles.items() if k not in role_ids}
        org_engine._employees = {
            k: v for k, v in org_engine._employees.items() if v.role_id not in role_ids
        }
        org_engine.installed_packages = [
            p
            for p in org_engine.installed_packages
            if (p.package_id if isinstance(p, InstalledPackageInfo) else p.get("package_id", ""))
            != package_id
        ]

        prompts_dir = self.home / "prompts" / "market" / package_id
        if prompts_dir.exists():
            shutil.rmtree(prompts_dir, ignore_errors=True)

        talent_prompts_dir = self.home / "talent_prompts" / package_id
        if talent_prompts_dir.exists():
            shutil.rmtree(talent_prompts_dir, ignore_errors=True)

        logger.info("Uninstalled package %s", package_id)
        return True

    def _write_prompts(self, package_id: str, prompt_contents: dict[str, str]) -> None:
        if not prompt_contents:
            return
        target_dir = self._market_prompts_dir(package_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        base = target_dir.resolve()
        for filename, content in prompt_contents.items():
            dest = target_dir / filename
            resolved = dest.resolve()
            if resolved != base and resolved.parent != base:
                raise ValueError(f"Prompt filename escapes package directory: {filename!r}")
            dest.write_text(content, encoding="utf-8")

    def _market_prompts_dir(self, package_id: str) -> Path:
        normalized = str(package_id or "").strip()
        if not _PACKAGE_ID_RE.match(normalized):
            raise ValueError(f"Invalid package id: {package_id!r}")
        return self.home / "prompts" / "market" / normalized
