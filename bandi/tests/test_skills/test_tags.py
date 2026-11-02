from __future__ import annotations

from pathlib import Path

from bandi.skills.loader import SkillLoader
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


def _skill(**kwargs: object) -> SkillDef:
    defaults: dict[str, object] = {
        "name": "demo",
        "description": "demo skill",
        "triggers": [],
        "instructions": "",
        "tags": [],
    }
    defaults.update(kwargs)
    return SkillDef(**defaults)  # type: ignore[arg-type]


class TestSkillDefTags:
    def test_tags_field_default_empty(self) -> None:
        assert _skill().tags == []

    def test_tags_field_roundtrip(self) -> None:
        s = _skill(tags=["coding", "python"])
        assert s.tags == ["coding", "python"]


class TestLoaderTags:
    def _write(self, root: Path, name: str, fm: str) -> Path:
        skill = root / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(f"---\n{fm}\n---\n# Body\n", encoding="utf-8")
        return skill

    def test_parse_tags_from_frontmatter(self, tmp_path: Path) -> None:
        self._write(tmp_path, "tagged", "name: tagged\ntags:\n  - coding\n  - python")
        loaded = SkillLoader(str(tmp_path)).load_all()
        assert loaded[0].tags == ["coding", "python"]

    def test_parse_domains_as_tags_fallback(self, tmp_path: Path) -> None:
        self._write(tmp_path, "dom", "name: dom\ndomains:\n  - data\n  - analysis")
        loaded = SkillLoader(str(tmp_path)).load_all()
        assert loaded[0].tags == ["data", "analysis"]

    def test_no_tags_returns_empty(self, tmp_path: Path) -> None:
        self._write(tmp_path, "plain", "name: plain")
        loaded = SkillLoader(str(tmp_path)).load_all()
        assert loaded[0].tags == []

    def test_tags_precedence_over_domains(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "both",
            "name: both\ntags:\n  - explicit\ndomains:\n  - domain_tag",
        )
        loaded = SkillLoader(str(tmp_path)).load_all()
        assert loaded[0].tags == ["explicit"]


class TestRegistryListByTags:
    def test_list_by_tags_matches_any(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill(name="a", tags=["coding", "python"]))
        reg.register(_skill(name="b", tags=["data"]))
        reg.register(_skill(name="c", tags=[]))
        assert {s.name for s in reg.list_by_tags(["python"])} == {"a"}
        assert {s.name for s in reg.list_by_tags(["coding", "data"])} == {"a", "b"}

    def test_list_by_tags_no_match(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill(name="a", tags=["coding"]))
        assert reg.list_by_tags(["missing"]) == []

    def test_list_by_tags_empty_request(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill(name="a", tags=["coding"]))
        assert reg.list_by_tags([]) == []

    def test_list_by_tags_works_with_skill_no_tags_field(self) -> None:
        reg = SkillRegistry()
        reg.register(SkillDef(name="legacy", description="", triggers=[], instructions=""))
        assert reg.list_by_tags(["anything"]) == []
