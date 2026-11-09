from __future__ import annotations

from bandi.company.talent_presets import (
    BUILTIN_TALENT_TEMPLATES,
    get_all_talent_presets,
    get_talent_categories,
    get_talent_preset,
)


class TestBuiltinPresets:
    def test_non_empty(self) -> None:
        presets = get_all_talent_presets()
        assert len(presets) >= 10

    def test_each_has_required_fields(self) -> None:
        for p in BUILTIN_TALENT_TEMPLATES:
            assert p["id"], f"missing id in {p}"
            assert p["name"], f"missing name in {p['id']}"
            assert p["category"], f"missing category in {p['id']}"

    def test_get_by_id(self) -> None:
        preset = get_talent_preset("ceo-strategist")
        assert preset is not None
        assert preset["id"] == "ceo-strategist"
        assert preset["category"] == "management"

    def test_get_unknown_returns_none(self) -> None:
        assert get_talent_preset("nonexistent-id") is None

    def test_categories_sorted_unique(self) -> None:
        cats = get_talent_categories()
        assert cats == sorted(cats)
        assert len(cats) == len(set(cats))

    def test_contains_expected_categories(self) -> None:
        cats = get_talent_categories()
        assert "engineering" in cats
        assert "management" in cats
