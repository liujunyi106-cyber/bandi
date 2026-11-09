from __future__ import annotations

from pathlib import Path

import pytest

from bandi.skills.importer import SkillImporter, SkillImportError, SkillImportResult


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "src" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A test skill\n---\n\n# My Skill\n\nUse it wisely.\n",
        encoding="utf-8",
    )
    (skill / "helper.py").write_text("print('hello')\n", encoding="utf-8")
    return skill


@pytest.fixture
def target_dir(tmp_path: Path) -> Path:
    return tmp_path / "skills"


class TestSkillImporter:
    def test_import_skill_copies_skill_md(self, source_dir: Path, target_dir: Path) -> None:
        result = SkillImporter(skills_dir=str(target_dir)).import_skill(str(source_dir))
        assert isinstance(result, SkillImportResult)
        assert result.skill_name == "my-skill"
        assert result.enabled_domains == []
        target = target_dir / "my-skill" / "SKILL.md"
        assert target.exists()
        assert "Use it wisely" in target.read_text(encoding="utf-8")

    def test_import_skill_from_skill_md_file(self, source_dir: Path, target_dir: Path) -> None:
        md_file = source_dir / "SKILL.md"
        result = SkillImporter(skills_dir=str(target_dir)).import_skill(str(md_file))
        assert result.skill_name == "my-skill"
        assert (target_dir / "my-skill" / "SKILL.md").exists()

    def test_import_skill_idempotent_overwrite(self, source_dir: Path, target_dir: Path) -> None:
        importer = SkillImporter(skills_dir=str(target_dir))
        importer.import_skill(str(source_dir))
        (source_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: Updated\n---\n\n# Updated\n",
            encoding="utf-8",
        )
        importer.import_skill(str(source_dir))
        content = (target_dir / "my-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "Updated" in content

    def test_import_skill_missing_sk_md(self, tmp_path: Path, target_dir: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(target_dir)).import_skill(str(empty))

    def test_import_skill_missing_path(self, target_dir: Path, tmp_path: Path) -> None:
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(target_dir)).import_skill(str(tmp_path / "nope"))

    def test_import_skill_rejects_traversal_name(self, tmp_path: Path, target_dir: Path) -> None:
        evil = tmp_path / "evil"
        evil.mkdir()
        (evil / "SKILL.md").write_text(
            "---\nname: ../../evil\ndescription: traversal\n---\n# Evil\n",
            encoding="utf-8",
        )
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(target_dir)).import_skill(str(evil))
        assert not (tmp_path.parent / "evil" / "SKILL.md").exists()

    def test_import_skill_bad_frontmatter(self, tmp_path: Path, target_dir: Path) -> None:
        skill = tmp_path / "bad"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: [unclosed\n---\n\nbody\n", encoding="utf-8")
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(target_dir)).import_skill(str(skill))

    def test_import_skill_returns_path_and_domains(
        self, source_dir: Path, target_dir: Path
    ) -> None:
        result = SkillImporter(skills_dir=str(target_dir)).import_skill(str(source_dir))
        assert result.skill_path == str(target_dir / "my-skill" / "SKILL.md")
        assert result.message


class TestSkillImporterRobustness:
    def test_import_skill_null_domains_ok(self, tmp_path: Path, target_dir: Path) -> None:
        skill = tmp_path / "null-domains"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: null-domains\ndomains:\n---\n# Body\n",
            encoding="utf-8",
        )
        result = SkillImporter(skills_dir=str(target_dir)).import_skill(str(skill))
        assert result.enabled_domains == []

    def test_import_skill_dot_name_rejected(self, tmp_path: Path, target_dir: Path) -> None:
        skill = tmp_path / "dot-name"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: .\n---\n# Body\n", encoding="utf-8")
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(target_dir)).import_skill(str(skill))

    def test_import_skill_bom_frontmatter_parsed(self, tmp_path: Path, target_dir: Path) -> None:
        skill = tmp_path / "bom-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_bytes(
            b"\xef\xbb\xbf---\nname: bom-skill\ndomains:\n  - coding\n---\n# Body\n"
        )
        result = SkillImporter(skills_dir=str(target_dir)).import_skill(str(skill))
        assert result.skill_name == "bom-skill"
        assert result.enabled_domains == ["coding"]

    def test_import_skill_leading_blank_lines_frontmatter(
        self, tmp_path: Path, target_dir: Path
    ) -> None:
        skill = tmp_path / "leading-blank"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "\n\n---\nname: leading-blank\n---\n# Body\n",
            encoding="utf-8",
        )
        result = SkillImporter(skills_dir=str(target_dir)).import_skill(str(skill))
        assert result.skill_name == "leading-blank"

    def test_import_error_family_under_bandi_error(self) -> None:
        """契约：SkillImportError 必须归 BandiError 错误族
        （项目错误继承纪律——调用方 except BandiError 兜底可覆盖）。"""
        from bandi.core.exceptions import BandiError

        assert issubclass(SkillImportError, BandiError)
