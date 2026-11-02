from __future__ import annotations

from pathlib import Path

import pytest

from bandi.skills.importer import SkillImporter, SkillImportError


@pytest.fixture
def target_dir(tmp_path: Path) -> Path:
    return tmp_path / "skills"


def _write_skill(
    root: Path, dir_name: str, skill_name: str, body: str = "# Body\n", **fm: object
) -> Path:
    skill = root / dir_name
    skill.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {skill_name}"]
    for key, value in fm.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(body)
    (skill / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return skill


class TestFrontmatterValidation:
    def test_unknown_frontmatter_keys_preserved_in_metadata(
        self, tmp_path: Path, target_dir: Path
    ) -> None:
        src = _write_skill(tmp_path, "ok-skill", "ok-skill", custom_key="kept")
        result = SkillImporter(skills_dir=str(target_dir)).import_skill(str(src))
        assert result.skill_name == "ok-skill"

    def test_name_too_long_rejected(self, tmp_path: Path, target_dir: Path) -> None:
        long_name = "a" * 65
        src = _write_skill(tmp_path, "long", long_name)
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(target_dir)).import_skill(str(src))

    def test_name_with_slash_rejected(self, tmp_path: Path, target_dir: Path) -> None:
        src = _write_skill(tmp_path, "evil", "a/b")
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(target_dir)).import_skill(str(src))

    def test_description_with_todo_rejected(self, tmp_path: Path, target_dir: Path) -> None:
        src = _write_skill(tmp_path, "todo-skill", "todo-skill", description="[todo] fill me")
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(target_dir)).import_skill(str(src))

    def test_description_too_long_rejected(self, tmp_path: Path, target_dir: Path) -> None:
        src = _write_skill(tmp_path, "long-desc", "long-desc", description="x" * 1025)
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(target_dir)).import_skill(str(src))

    def test_missing_description_ok(self, tmp_path: Path, target_dir: Path) -> None:
        src = _write_skill(tmp_path, "no-desc", "no-desc")
        result = SkillImporter(skills_dir=str(target_dir)).import_skill(str(src))
        assert result.skill_name == "no-desc"


class TestResourceCopy:
    def test_allowed_dirs_copied(self, tmp_path: Path, target_dir: Path) -> None:
        src = _write_skill(tmp_path, "with-res", "with-res")
        (src / "scripts").mkdir()
        (src / "scripts" / "run.py").write_text("x", encoding="utf-8")
        (src / "references").mkdir()
        (src / "references" / "notes.md").write_text("y", encoding="utf-8")
        SkillImporter(skills_dir=str(target_dir)).import_skill(str(src))
        dest = target_dir / "with-res"
        assert (dest / "scripts" / "run.py").read_text(encoding="utf-8") == "x"
        assert (dest / "references" / "notes.md").read_text(encoding="utf-8") == "y"

    def test_other_files_fallback_to_assets_root(self, tmp_path: Path, target_dir: Path) -> None:
        src = _write_skill(tmp_path, "fallback-skill", "fallback-skill")
        (src / "helper.py").write_text("z", encoding="utf-8")
        SkillImporter(skills_dir=str(target_dir)).import_skill(str(src))
        dest = target_dir / "fallback-skill"
        assert (dest / "assets" / "imported-root" / "helper.py").read_text(encoding="utf-8") == "z"

    def test_sk_md_not_copied_as_resource(self, tmp_path: Path, target_dir: Path) -> None:
        src = _write_skill(tmp_path, "single", "single")
        SkillImporter(skills_dir=str(target_dir)).import_skill(str(src))
        dest = target_dir / "single"
        assert (dest / "SKILL.md").exists()
        assert not (dest / "assets" / "imported-root" / "SKILL.md").exists()


class TestAtomicReplace:
    def test_replace_existing_skill_atomically(self, tmp_path: Path, target_dir: Path) -> None:
        importer = SkillImporter(skills_dir=str(target_dir))
        v1 = _write_skill(tmp_path, "v1", "same", description="first")
        importer.import_skill(str(v1))
        v2 = _write_skill(tmp_path, "v2", "same", description="second")
        importer.import_skill(str(v2))
        dest = target_dir / "same" / "SKILL.md"
        assert "second" in dest.read_text(encoding="utf-8")
        leftovers = [
            p
            for p in target_dir.iterdir()
            if p.is_dir() and (p.name.startswith(".bak") or p.name.startswith(".tmp"))
        ]
        assert leftovers == []

    def test_import_keeps_original_sk_md_intact(self, tmp_path: Path, target_dir: Path) -> None:
        src = _write_skill(tmp_path, "kept", "kept", description="orig")
        SkillImporter(skills_dir=str(target_dir)).import_skill(str(src))
        dest = target_dir / "kept" / "SKILL.md"
        assert "orig" in dest.read_text(encoding="utf-8")
