from __future__ import annotations

from bandi.company.package_format import (
    ConflictReport,
    InstalledPackageInfo,
    OPCPackage,
    OPCPackageManifest,
    PackageAuthor,
    PackageCategory,
    PackageContents,
    SandboxReport,
)


class TestPackageAuthor:
    def test_defaults_empty(self) -> None:
        author = PackageAuthor()
        assert author.name == ""
        assert author.github == ""

    def test_with_values(self) -> None:
        author = PackageAuthor(name="Alice", github="github/alice")
        assert author.name == "Alice"
        assert author.github == "github/alice"

    def test_validate_empty_name_warns(self) -> None:
        author = PackageAuthor(name="", github="gh/x")
        warnings = author.validate()
        assert len(warnings) == 1
        assert "empty" in warnings[0].lower()

    def test_validate_nonempty_name_clean(self) -> None:
        author = PackageAuthor(name="Bob")
        warnings = author.validate()
        assert warnings == []


class TestPackageContents:
    def test_defaults_zero(self) -> None:
        contents = PackageContents()
        assert contents.roles == 0
        assert contents.talent_templates == 0
        assert contents.skills == 0

    def test_with_counts(self) -> None:
        contents = PackageContents(roles=5, talent_templates=3, skills=2)
        assert contents.roles == 5
        assert contents.talent_templates == 3
        assert contents.skills == 2


class TestPackageCategory:
    def test_enum_values(self) -> None:
        values = [c.value for c in PackageCategory]
        assert "engineering" in values
        assert "analytics" in values
        assert "general" in values

    def test_is_str_enum(self) -> None:
        assert isinstance(PackageCategory.ENGINEERING, str)


class TestOPCPackageManifest:
    def test_minimal(self) -> None:
        manifest = OPCPackageManifest(id="my-pkg", name="My Package")
        assert manifest.id == "my-pkg"
        assert manifest.name == "My Package"
        assert manifest.version == "1.0.0"
        assert manifest.category == "general"
        assert manifest.contents.roles == 0

    def test_full(self) -> None:
        author = PackageAuthor(name="Bob", github="gh/bob")
        contents = PackageContents(roles=2, talent_templates=1)
        manifest = OPCPackageManifest(
            id="full-pkg",
            name="Full",
            description="A full package",
            version="2.0.0",
            author=author,
            category="engineering",
            tags=["tag1", "tag2"],
            contents=contents,
        )
        assert manifest.id == "full-pkg"
        assert manifest.author.name == "Bob"
        assert manifest.contents.roles == 2
        assert manifest.tags == ["tag1", "tag2"]

    def test_industry_and_use_cases_fields(self) -> None:
        manifest = OPCPackageManifest(
            id="ind-pkg", name="Industry", industry="engineering", use_cases=["web", "api"]
        )
        assert manifest.industry == "engineering"
        assert manifest.use_cases == ["web", "api"]

    def test_validate_empty_id_warns(self) -> None:
        manifest = OPCPackageManifest(id="", name="NoId")
        warnings = manifest.validate()
        assert any("id" in w.lower() for w in warnings)

    def test_validate_invalid_category_warns(self) -> None:
        manifest = OPCPackageManifest(id="x", name="X", category="unknown_cat")
        warnings = manifest.validate()
        assert any("category" in w.lower() for w in warnings)


class TestInstalledPackageInfo:
    def test_from_manifest(self) -> None:
        manifest = OPCPackageManifest(
            id="pkg-1", name="Pkg", industry="analytics", use_cases=["reporting"]
        )
        info = InstalledPackageInfo.from_manifest("pkg-1", manifest)
        assert info.package_id == "pkg-1"
        assert info.industry == "analytics"
        assert info.use_cases == ["reporting"]

    def test_from_manifest_copies_use_cases(self) -> None:
        manifest = OPCPackageManifest(id="m", name="M", use_cases=["a", "b"])
        info = InstalledPackageInfo.from_manifest("m", manifest)
        assert info.use_cases == ["a", "b"]
        # Mutating info.use_cases should not affect manifest
        info.use_cases.append("c")
        assert manifest.use_cases == ["a", "b"]


class TestOPCPackage:
    def test_empty(self) -> None:
        manifest = OPCPackageManifest(id="e", name="Empty")
        pkg = OPCPackage(manifest=manifest)
        assert pkg.roles == []
        assert pkg.talent_templates == []
        assert pkg.prompt_contents == {}
        assert pkg.readme == ""

    def test_with_data(self) -> None:
        manifest = OPCPackageManifest(id="d", name="Data")
        pkg = OPCPackage(
            manifest=manifest,
            roles=[{"id": "r1", "name": "R1"}],
            talent_templates=[{"id": "t1"}],
            prompt_contents={"foo.md": "content"},
            readme="# README",
        )
        assert len(pkg.roles) == 1
        assert pkg.roles[0]["id"] == "r1"
        assert pkg.prompt_contents["foo.md"] == "content"


class TestSandboxReport:
    def test_passed_by_default(self) -> None:
        report = SandboxReport()
        assert report.passed is True
        assert report.warnings == []
        assert report.errors == []

    def test_with_errors(self) -> None:
        report = SandboxReport(passed=False, errors=["bad id"], warnings=["suspicious"])
        assert report.passed is False
        assert report.errors == ["bad id"]
        assert report.warnings == ["suspicious"]


class TestConflictReport:
    def test_no_conflicts(self) -> None:
        report = ConflictReport()
        assert report.has_conflicts is False

    def test_with_conflicts(self) -> None:
        report = ConflictReport(role_conflicts=["r1"], template_conflicts=["t1"])
        assert report.has_conflicts is True
        assert report.role_conflicts == ["r1"]
        assert report.template_conflicts == ["t1"]

    def test_empty_lists_not_conflicts(self) -> None:
        report = ConflictReport(role_conflicts=[], template_conflicts=[])
        assert report.has_conflicts is False
