from __future__ import annotations

from bandi.company.architecture_registry import (
    get_all_presets,
    get_preset,
    get_preset_categories,
)


class TestGetPreset:
    def test_get_startup_studio(self) -> None:
        preset = get_preset("startup-studio")
        assert preset is not None
        assert preset.id == "startup-studio"
        assert preset.name == "Startup Studio"
        assert len(preset.roles) == 5

    def test_get_enterprise_corp(self) -> None:
        preset = get_preset("enterprise-corp")
        assert preset is not None
        assert len(preset.roles) == 11

    def test_get_unknown_returns_none(self) -> None:
        assert get_preset("nonexistent") is None

    def test_get_creative_agency(self) -> None:
        preset = get_preset("creative-agency")
        assert preset is not None
        assert preset.category == "agency"


class TestGetAllPresets:
    def test_returns_list(self) -> None:
        presets = get_all_presets()
        assert isinstance(presets, list)
        assert len(presets) >= 5

    def test_all_have_required_fields(self) -> None:
        for preset in get_all_presets():
            assert preset.id
            assert preset.name
            assert preset.description
            assert preset.category
            assert isinstance(preset.roles, list)
            assert isinstance(preset.tags, list)


class TestGetPresetCategories:
    def test_returns_sorted_unique(self) -> None:
        categories = get_preset_categories()
        assert categories == sorted(categories)
        assert len(categories) == len(set(categories))

    def test_contains_expected(self) -> None:
        categories = get_preset_categories()
        assert "startup" in categories
        assert "engineering" in categories


class TestArchitectureBlueprintDisplay:
    def test_to_display_card(self) -> None:
        preset = get_preset("startup-studio")
        card = preset.to_display_card()
        assert card["id"] == "startup-studio"
        assert card["name"] == "Startup Studio"
        assert card["roles_count"] == 5
        assert "collaboration_pattern" in card

    def test_to_detail(self) -> None:
        preset = get_preset("startup-studio")
        detail = preset.to_detail()
        assert "roles" in detail
        assert len(detail["roles"]) == 5
        assert detail["roles"][0]["id"] == "ceo"


class TestArchitectureBlueprintIndustryUseCases:
    def test_blueprint_has_industry_field(self) -> None:
        preset = get_preset("startup-studio")
        assert hasattr(preset, "industry")
        # Built-in presets have empty industry by default
        assert preset.industry == ""

    def test_blueprint_has_use_cases_field(self) -> None:
        preset = get_preset("startup-studio")
        assert hasattr(preset, "use_cases")
        assert isinstance(preset.use_cases, list)
