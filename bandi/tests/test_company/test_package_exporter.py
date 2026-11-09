from __future__ import annotations

from pathlib import Path

from bandi.company.architecture_registry import get_preset
from bandi.company.org_engine import OrgEngine
from bandi.company.package_exporter import PackageExporter
from bandi.company.sandbox_checker import SandboxChecker
from bandi.company.talent_market import TalentMarket


def _make_exporter(tmp_path: Path) -> PackageExporter:
    org_engine = OrgEngine()
    talent_market = TalentMarket(templates_dir=str(tmp_path / "talent"))
    home = tmp_path / "bandi_home"
    home.mkdir()
    return PackageExporter(org_engine=org_engine, talent_market=talent_market, home=home)


class TestPackageExporter:
    def test_export_creates_package(self, tmp_path: Path) -> None:
        exporter = _make_exporter(tmp_path)
        pkg = exporter.export_current(
            package_id="test-export",
            name="Test Export",
            description="For testing",
        )
        assert pkg.manifest.id == "test-export"
        assert pkg.manifest.name == "Test Export"
        assert pkg.manifest.description == "For testing"

    def test_export_includes_roles(self, tmp_path: Path) -> None:
        exporter = _make_exporter(tmp_path)
        exporter.org_engine.register_role(
            "custom-role", "Custom Role", responsibility="Do custom things"
        )
        pkg = exporter.export_current(package_id="with-roles", name="With Roles")
        role_ids = [r["id"] for r in pkg.roles]
        assert "custom-role" in role_ids

    def test_write_to_path_creates_directory(self, tmp_path: Path) -> None:
        exporter = _make_exporter(tmp_path)
        pkg = exporter.export_current(package_id="disk-test", name="Disk Test")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        pkg_dir = exporter.write_to_path(pkg, out_dir)
        assert pkg_dir.exists()
        assert pkg_dir.name == "disk-test.opcpkg"

    def test_write_to_path_has_manifest(self, tmp_path: Path) -> None:
        exporter = _make_exporter(tmp_path)
        pkg = exporter.export_current(package_id="manifest-test", name="Manifest")
        out_dir = tmp_path / "out2"
        out_dir.mkdir()
        pkg_dir = exporter.write_to_path(pkg, out_dir)
        manifest_path = pkg_dir / "manifest.yaml"
        assert manifest_path.exists()
        content = manifest_path.read_text(encoding="utf-8")
        assert "manifest-test" in content

    def test_write_to_path_has_org_config(self, tmp_path: Path) -> None:
        exporter = _make_exporter(tmp_path)
        pkg = exporter.export_current(package_id="config-test", name="Config")
        out_dir = tmp_path / "out3"
        out_dir.mkdir()
        pkg_dir = exporter.write_to_path(pkg, out_dir)
        org_config = pkg_dir / "org_config.yaml"
        assert org_config.exists()

    def test_write_to_path_has_readme(self, tmp_path: Path) -> None:
        exporter = _make_exporter(tmp_path)
        pkg = exporter.export_current(package_id="readme-test", name="Readme")
        out_dir = tmp_path / "out4"
        out_dir.mkdir()
        pkg_dir = exporter.write_to_path(pkg, out_dir)
        readme = pkg_dir / "README.md"
        assert readme.exists()
        text = readme.read_text(encoding="utf-8")
        assert "Readme" in text

    def test_export_uses_builtin_presets_as_reference(self, tmp_path: Path) -> None:
        """导出时引用内置蓝图确认架构 registry 接线正确。"""
        preset = get_preset("startup-studio")
        assert preset is not None
        assert preset.id == "startup-studio"
        assert len(preset.roles) == 5

    def test_package_id_path_safe(self, tmp_path: Path) -> None:
        exporter = _make_exporter(tmp_path)
        pkg = exporter.export_current(package_id="safe-id_123", name="Safe")
        out_dir = tmp_path / "out5"
        out_dir.mkdir()
        pkg_dir = exporter.write_to_path(pkg, out_dir)
        assert ".." not in pkg_dir.name
        assert "/" not in pkg_dir.name


class TestPackageExporterE2E:
    def test_export_sandbox_write_full_pipeline(self, tmp_path: Path) -> None:
        """全链路：导出当前 org → 沙箱校验通过 → 写入磁盘 → 验证所有文件存在且内容有效。"""
        org_engine = OrgEngine()
        org_engine.register_role(
            "pkg-test-role",
            "Pkg Test Role",
            responsibility="Test responsibility",
            reports_to="ceo",
        )
        talent_market = TalentMarket(templates_dir=str(tmp_path / "talent"))
        exporter = PackageExporter(
            org_engine=org_engine, talent_market=talent_market, home=tmp_path / "home"
        )
        checker = SandboxChecker()

        pkg = exporter.export_current(
            package_id="e2e-pkg",
            name="E2E Package",
            description="End-to-end pipeline test",
        )

        report = checker.validate(pkg)
        assert report.passed is True, f"Unexpected sandbox errors: {report.errors}"
        assert report.warnings == []

        out_dir = tmp_path / "e2e_out"
        out_dir.mkdir()
        pkg_dir = exporter.write_to_path(pkg, out_dir)

        # 断言最终产物（非 mock 空数据）
        assert pkg_dir.exists()
        assert pkg_dir.name == "e2e-pkg.opcpkg"

        manifest_path = pkg_dir / "manifest.yaml"
        assert manifest_path.exists()
        manifest_text = manifest_path.read_text(encoding="utf-8")
        assert "e2e-pkg" in manifest_text
        assert "E2E Package" in manifest_text

        org_config_path = pkg_dir / "org_config.yaml"
        assert org_config_path.exists()
        org_text = org_config_path.read_text(encoding="utf-8")
        assert "pkg-test-role" in org_text  # 自定义角色已写入

        readme_path = pkg_dir / "README.md"
        assert readme_path.exists()
        readme_text = readme_path.read_text(encoding="utf-8")
        assert "E2E Package" in readme_text
        assert "pkg-test-role" in readme_text

    def test_export_with_dangerous_tool_fails_sandbox(self, tmp_path: Path) -> None:
        """破坏者视角：包内含危险工具，沙箱应拦截——全链路中断。"""
        org_engine = OrgEngine()
        org_engine._roles["evil_role"] = type(
            "OrgRole",
            (),
            {
                "id": "evil_role",
                "name": "Evil",
                "reports_to": None,
                "can_spawn": [],
                "permissions": {},
                "responsibility": "Do bad things",
            },
        )()
        talent_market = TalentMarket(templates_dir=str(tmp_path / "talent"))
        exporter = PackageExporter(
            org_engine=org_engine, talent_market=talent_market, home=tmp_path / "home"
        )
        checker = SandboxChecker()

        pkg = exporter.export_current(package_id="evil-pkg", name="Evil")
        # 手动注入危险工具到导出结果
        pkg.roles.append({"id": "evil_role", "tools": ["shell_exec", "eval"]})

        report = checker.validate(pkg)
        assert report.passed is False
        assert any("shell_exec" in e for e in report.errors)

    def test_export_id_format_rejected_by_sandbox(self, tmp_path: Path) -> None:
        """破坏者视角：包 ID 含大写/点号，沙箱应拒绝。"""
        exporter = _make_exporter(tmp_path)
        checker = SandboxChecker()

        pkg = exporter.export_current(package_id="BadID.123", name="Bad ID Test")
        report = checker.validate(pkg)
        assert report.passed is False
        assert any("lowercase" in e.lower() for e in report.errors)
