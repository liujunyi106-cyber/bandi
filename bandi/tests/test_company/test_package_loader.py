from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bandi.company.org_engine import OrgEngine
from bandi.company.package_format import OPCPackage, OPCPackageManifest
from bandi.company.package_loader import PackageLoader


@pytest.fixture
def loader(tmp_path: Path) -> PackageLoader:
    return PackageLoader(home=tmp_path / "bandi_home")


@pytest.fixture
def sample_manifest() -> OPCPackageManifest:
    return OPCPackageManifest(id="test-pkg", name="Test Package")


class TestLoadFromPath:
    def test_missing_dir_raises(self, loader: PackageLoader) -> None:
        with pytest.raises(FileNotFoundError):
            loader.load_from_path(Path("/nonexistent/pkg.opcpkg"))

    def test_missing_manifest_raises(self, loader: PackageLoader, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "no-manifest"
        pkg_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            loader.load_from_path(pkg_dir)

    def test_loads_manifest_and_config(self, loader: PackageLoader, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "good-pkg.opcpkg"
        pkg_dir.mkdir()
        (pkg_dir / "manifest.yaml").write_text(
            yaml.dump({"id": "good-pkg", "name": "Good Pkg", "version": "2.0.0"}),
            encoding="utf-8",
        )
        (pkg_dir / "org_config.yaml").write_text(
            yaml.dump({"roles": [{"id": "r1", "name": "R1"}], "talent_templates": []}),
            encoding="utf-8",
        )
        pkg = loader.load_from_path(pkg_dir)
        assert pkg.manifest.id == "good-pkg"
        assert pkg.manifest.version == "2.0.0"
        assert len(pkg.roles) == 1
        assert pkg.roles[0]["id"] == "r1"

    def test_loads_prompts(self, loader: PackageLoader, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "with-prompts.opcpkg"
        pkg_dir.mkdir()
        (pkg_dir / "manifest.yaml").write_text(
            yaml.dump({"id": "wp", "name": "With Prompts"}), encoding="utf-8"
        )
        prompts_dir = pkg_dir / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "engineer.md").write_text("# Engineer prompt", encoding="utf-8")
        pkg = loader.load_from_path(pkg_dir)
        assert "engineer.md" in pkg.prompt_contents
        assert pkg.prompt_contents["engineer.md"] == "# Engineer prompt"


class TestDetectConflicts:
    def test_no_conflicts_empty_org(
        self, loader: PackageLoader, sample_manifest: OPCPackageManifest
    ) -> None:
        org_engine = type(
            "OrgEngine",
            (),
            {
                "list_agents": lambda self: [],
                "installed_packages": [],
            },
        )()
        loader.org_engine = org_engine
        pkg = OPCPackage(manifest=sample_manifest, roles=[{"id": "r1"}])
        report = loader.detect_conflicts(pkg)
        assert report.has_conflicts is False

    def test_role_conflict_detected(
        self, loader: PackageLoader, sample_manifest: OPCPackageManifest
    ) -> None:
        from bandi.company.org_engine import OrgEngine

        org = OrgEngine()
        org.register_role("r1", "R1")
        loader.org_engine = org
        pkg = OPCPackage(manifest=sample_manifest, roles=[{"id": "r1"}])
        report = loader.detect_conflicts(pkg)
        assert report.has_conflicts is True
        assert "r1" in report.role_conflicts


class TestInstall:
    def test_install_adds_roles_to_org(self, loader: PackageLoader, tmp_path: Path) -> None:
        from bandi.company.org_engine import OrgEngine

        org = OrgEngine()
        loader.org_engine = org

        pkg_dir = tmp_path / "install-pkg.opcpkg"
        pkg_dir.mkdir()
        (pkg_dir / "manifest.yaml").write_text(
            yaml.dump({"id": "ipkg", "name": "Install Pkg"}), encoding="utf-8"
        )
        (pkg_dir / "org_config.yaml").write_text(
            yaml.dump(
                {
                    "roles": [{"id": "new-role", "name": "New Role", "reports_to": None}],
                    "talent_templates": [],
                }
            ),
            encoding="utf-8",
        )
        pkg = loader.load_from_path(pkg_dir)
        info = loader.install(pkg, strategy="overwrite")
        assert info.package_id == "ipkg"
        assert "new-role" in [r.id for r in org.list_agents()]

    def test_install_namespace_prefix(self, loader: PackageLoader, tmp_path: Path) -> None:
        from bandi.company.org_engine import OrgEngine

        org = OrgEngine()
        loader.org_engine = org

        pkg_dir = tmp_path / "ns-pkg.opcpkg"
        pkg_dir.mkdir()
        (pkg_dir / "manifest.yaml").write_text(
            yaml.dump({"id": "ns-pkg", "name": "NS Pkg"}), encoding="utf-8"
        )
        (pkg_dir / "org_config.yaml").write_text(
            yaml.dump(
                {
                    "roles": [{"id": "custom", "name": "Custom", "reports_to": None}],
                }
            ),
            encoding="utf-8",
        )
        pkg = loader.load_from_path(pkg_dir)
        loader.install(pkg, strategy="namespace")
        role_ids = [r.id for r in org.list_agents()]
        assert "ns-pkg:custom" in role_ids

    def test_invalid_package_id_raises(self, loader: PackageLoader) -> None:
        from bandi.company.package_format import OPCPackage

        manifest = OPCPackageManifest(id="BadID!", name="Bad")
        pkg = OPCPackage(manifest=manifest)
        with pytest.raises(ValueError):
            loader.install(pkg)

    def test_install_write_prompt_files(self, loader: PackageLoader, tmp_path: Path) -> None:
        from bandi.company.org_engine import OrgEngine
        from bandi.company.package_format import OPCPackage

        org = OrgEngine()
        loader.org_engine = org
        manifest = OPCPackageManifest(id="ppkg", name="Prompt Pkg")
        pkg = OPCPackage(manifest=manifest, prompt_contents={"eng.md": "# Engineer"})
        loader.install(pkg)
        prompt_file = tmp_path / "bandi_home" / "prompts" / "market" / "ppkg" / "eng.md"
        assert prompt_file.exists()
        assert prompt_file.read_text(encoding="utf-8") == "# Engineer"


class TestUninstall:
    def test_uninstall_removes_roles(self, loader: PackageLoader, tmp_path: Path) -> None:
        from bandi.company.org_engine import OrgEngine

        org = OrgEngine()
        loader.org_engine = org
        # Manually add an installed package record and role
        org.installed_packages.append(
            {
                "package_id": "unpkg",
                "role_ids": ["un-role"],
                "template_ids": [],
            }
        )
        org._roles["un-role"] = type(
            "OrgRole",
            (),
            {
                "id": "un-role",
                "name": "Un",
                "reports_to": None,
                "can_spawn": [],
                "permissions": {},
                "responsibility": "",
                "role_id": "un-role",
            },
        )()
        result = loader.uninstall("unpkg")
        assert result is True
        assert "un-role" not in [r.id for r in org.list_agents()]

    def test_uninstall_nonexistent_returns_false(self, loader: PackageLoader) -> None:
        from bandi.company.org_engine import OrgEngine

        loader.org_engine = OrgEngine()
        result = loader.uninstall("no-such-pkg")
        assert result is False

    def test_uninstall_rejects_traversal_id(self, loader: PackageLoader) -> None:
        from bandi.company.org_engine import OrgEngine

        loader.org_engine = OrgEngine()
        with pytest.raises(ValueError):
            loader.uninstall("../../etc")

    def test_uninstall_cleans_talent_prompts_dir(
        self, loader: PackageLoader, tmp_path: Path
    ) -> None:
        from bandi.company.org_engine import OrgEngine
        from bandi.company.package_format import InstalledPackageInfo

        org = OrgEngine()
        loader.org_engine = org
        org.installed_packages.append(
            InstalledPackageInfo(package_id="tpkg", role_ids=[], template_ids=[])
        )
        # Create fake talent_prompts dir
        talent_dir = tmp_path / "bandi_home" / "talent_prompts" / "tpkg"
        talent_dir.mkdir(parents=True)
        (talent_dir / "engineer.md").write_text("engineer prompt", encoding="utf-8")
        result = loader.uninstall("tpkg")
        assert result is True
        assert not talent_dir.exists()

    def test_uninstall_cleans_both_prompt_dirs(self, loader: PackageLoader, tmp_path: Path) -> None:
        from bandi.company.org_engine import OrgEngine
        from bandi.company.package_format import InstalledPackageInfo

        org = OrgEngine()
        loader.org_engine = org
        org.installed_packages.append(
            InstalledPackageInfo(package_id="both", role_ids=[], template_ids=[])
        )
        # Create both talent_prompts and prompts/market dirs
        (tmp_path / "bandi_home" / "talent_prompts" / "both" / "x.md").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "bandi_home" / "talent_prompts" / "both" / "x.md").write_text("x")
        (tmp_path / "bandi_home" / "prompts" / "market" / "both" / "y.md").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "bandi_home" / "prompts" / "market" / "both" / "y.md").write_text("y")
        result = loader.uninstall("both")
        assert result is True
        assert not (tmp_path / "bandi_home" / "talent_prompts" / "both").exists()
        assert not (tmp_path / "bandi_home" / "prompts" / "market" / "both").exists()


def _make_pkg(tmp_path, roles, dangerous_tool: bool = False):
    """构造最小可安装包。dangerous_tool=True 时角色带 shell_exec 工具。"""
    import yaml

    author = {"name": "tester"}
    manifest_data = {
        "id": "testpkg",
        "name": "Test Package",
        "description": "d",
        "version": "1.0.0",
        "author": author,
        "license": "MIT",
        "category": "general",
        "tags": [],
    }
    if dangerous_tool:
        for r in roles:
            r["tools"] = ["shell_exec"]
    pkg_dir = tmp_path / "pkgsrc"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "manifest.yaml").write_text(yaml.dump(manifest_data), encoding="utf-8")
    (pkg_dir / "org_config.yaml").write_text(
        yaml.dump({"roles": roles, "employees": []}),
        encoding="utf-8",
    )
    from bandi.company.package_loader import PackageLoader

    home = tmp_path / "home"
    loader = PackageLoader(home=home)
    return loader.load_from_path(pkg_dir)


def test_contract_export_rejects_traversal_package_id(tmp_path) -> None:
    """
    双重校验——穿越值不得在目标目录外创建文件（install/uninstall 同族补齐）。"""
    from bandi.company.package_exporter import PackageExporter

    exporter = PackageExporter(org_engine=None, talent_market=None, home=tmp_path)
    evil = ".." + chr(92) + ".." + chr(92) + "evil"
    try:
        exporter.write_to_path(
            _make_pkg(tmp_path, [{"id": "r1", "name": "R"}]),
            tmp_path,
            package_id=evil,
        )
        raise AssertionError("穿越 package_id 未被拒绝")
    except (ValueError, TypeError):
        pass


def test_contract_install_runs_sandbox_check(tmp_path) -> None:
    """
    拒绝安装——fail-closed，不得静默装进组织。"""
    from bandi.company.package_loader import PackageLoader

    pkg = _make_pkg(tmp_path, [{"id": "r1", "name": "R"}], dangerous_tool=True)
    loader = PackageLoader(home=tmp_path / "home")
    loader.org_engine = OrgEngine()
    try:
        loader.install(pkg)
        raise AssertionError("危险工具包未被拦截")
    except ValueError as exc:
        assert "sandbox" in str(exc).lower() or "security" in str(exc).lower(), str(exc)


def test_contract_install_tolerates_reverse_topology(tmp_path) -> None:
    """
    reports_to 正确——两遍注册，不得半安装态崩溃。"""
    from bandi.company.package_loader import PackageLoader

    pkg = _make_pkg(
        tmp_path,
        [
            {"id": "worker_a", "name": "W", "reports_to": "lead_b"},
            {"id": "lead_b", "name": "L"},
        ],
    )
    loader = PackageLoader(home=tmp_path / "home")
    org = OrgEngine()
    loader.org_engine = org
    info = loader.install(pkg)
    assert set(info.role_ids) == {"testpkg:worker_a", "testpkg:lead_b"}
    w = org.get_role("testpkg:worker_a")
    assert w is not None and w.reports_to == "testpkg:lead_b"
