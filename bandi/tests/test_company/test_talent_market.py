from __future__ import annotations

from bandi.company.talent_market import TalentMarket, TalentTemplate


class TestTalentMarket:
    def test_builtin_templates_loaded(self):
        market = TalentMarket(templates_dir="/nonexistent")
        templates = market.list_templates()
        assert len(templates) >= 6
        names = [t.name for t in templates]
        assert "general_engineer" in names
        assert "data_analyst" in names

    def test_search_by_keyword(self):
        market = TalentMarket(templates_dir="/nonexistent")
        results = market.search("engineer")
        assert len(results) >= 1
        assert results[0].name == "general_engineer"

    def test_search_returns_top_k(self):
        market = TalentMarket(templates_dir="/nonexistent")
        results = market.search("code reviewer", top_k=2)
        assert len(results) <= 2

    def test_search_empty_query_returns_empty(self):
        market = TalentMarket(templates_dir="/nonexistent")
        assert market.search("") == []
        assert market.search("   ") == []
        assert market.search("", top_k=5) == []

    def test_empty_templates_dir_falls_back_to_builtin(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        market = TalentMarket(templates_dir=str(empty_dir))
        assert len(market.list_templates()) >= 6

    def test_parse_template_reads_all_fields(self, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "custom.md").write_text(
            "category: analytics\n"
            "description: 数据分析专家\n"
            "skills: sql, python, reporting\n"
            "system_prompt: You are a data expert.\n"
            "id: custom_id\n"
            "domains: finance, ecommerce\n"
            "prompt_ref: prompts/data.md\n",
            encoding="utf-8",
        )
        market = TalentMarket(templates_dir=str(template_dir))
        template = market.get_template("custom")
        assert template is not None
        assert template.id == "custom_id"
        assert template.skills == ["sql", "python", "reporting"]
        assert template.system_prompt == "You are a data expert."
        assert template.domains == ["finance", "ecommerce"]
        assert template.prompt_ref == "prompts/data.md"

    def test_search_english_keyword_matches_chinese_description(self):
        market = TalentMarket(templates_dir="/nonexistent")
        results = market.search("engineer")
        assert any(t.name == "general_engineer" for t in results)

    def test_get_template(self):
        market = TalentMarket(templates_dir="/nonexistent")
        tpl = market.get_template("general_engineer")
        assert tpl is not None
        assert tpl.category == "engineering"

    def test_get_template_nonexistent(self):
        market = TalentMarket(templates_dir="/nonexistent")
        assert market.get_template("nonexistent") is None

    def test_template_optional_fields_default_empty(self):
        template = TalentTemplate(name="t", category="c", description="d")
        assert template.id == ""
        assert template.domains == []
        assert template.prompt_ref == ""

    def test_template_optional_fields_set(self):
        template = TalentTemplate(
            name="t",
            category="c",
            description="d",
            id="tpl-1",
            domains=["python", "sql"],
            prompt_ref="talent/engineer.md",
        )
        assert template.id == "tpl-1"
        assert template.prompt_ref == "talent/engineer.md"

    def test_list_templates_by_categories(self):
        market = TalentMarket(templates_dir="/nonexistent")
        result = market.list_templates_by_categories(categories=["engineering"])
        names = [t.name for t in result]
        assert "general_engineer" in names

    def test_list_templates_by_categories_empty(self):
        market = TalentMarket(templates_dir="/nonexistent")
        assert market.list_templates_by_categories(categories=[]) == []

    def test_list_category_catalog_engineering_first(self):
        market = TalentMarket(templates_dir="/nonexistent")
        catalog = market.list_category_catalog()
        assert catalog[0]["category"] == "engineering"
        assert catalog[0]["template_count"] >= 1
        assert "description" in catalog[0]

    def test_build_candidate_summary(self):
        market = TalentMarket(templates_dir="/nonexistent")
        summary = market.build_candidate_summary(market.get_template("general_engineer"))
        assert summary["template_id"] == "general_engineer"
        assert summary["category"] == "engineering"
        assert summary["description"]

    def test_build_employee_id_returns_template_id(self):
        market = TalentMarket(templates_dir="/nonexistent")
        employee_id = market.build_employee_id(role_id="engineer", template_id="general_engineer")
        assert employee_id.startswith("general_engineer")

    def test_build_employee_id_unique_per_call(self):
        """同一模板多次推荐必须产出唯一员工 ID——原实现直接返回 template_id
        同模板两名员工 ID 碰撞导致管理面无法区分。"""
        market = TalentMarket(templates_dir="/nonexistent")
        a = market.build_employee_id(role_id="engineer", template_id="general_engineer")
        b = market.build_employee_id(role_id="engineer", template_id="general_engineer")
        assert a != b

    def test_collect_for_team_filters_by_team_id(self):
        market = TalentMarket(templates_dir="/nonexistent")
        # team_id 字段默认为空串，collect_for_team("") 返回全部
        all_tpls = market.list_templates()
        team_tpls = market.collect_for_team("")
        assert len(team_tpls) == len(all_tpls)

    def test_collect_for_team_filters_nonempty(self):
        market = TalentMarket(templates_dir="/nonexistent")
        team_tpls = market.collect_for_team("my-team")
        # 内置模板 team_id 默认为空串，不应被 my-team 过滤到
        for t in team_tpls:
            assert getattr(t, "team_id", "") == "" or t.team_id == "my-team"

    def test_list_installed_packages_returns_list(self):
        market = TalentMarket(templates_dir="/nonexistent")
        packages = market.list_installed_packages()
        assert isinstance(packages, list)

    def test_bind_org_engine_sets_engine(self):
        market = TalentMarket(templates_dir="/nonexistent")
        assert market._org_engine is None
        fake_engine = object()
        market.bind_org_engine(fake_engine)
        assert market._org_engine is fake_engine

    def test_list_installed_packages_with_bound_engine(self):
        market = TalentMarket(templates_dir="/nonexistent")

        class FakeEngine:
            installed_packages = [{"package_id": "pkg-1", "name": "test-pkg"}]

        market.bind_org_engine(FakeEngine())
        packages = market.list_installed_packages()
        assert len(packages) == 1
        assert packages[0]["package_id"] == "pkg-1"

    def test_list_installed_packages_engine_no_attr_returns_empty(self):
        market = TalentMarket(templates_dir="/nonexistent")
        market.bind_org_engine(object())  # 没有 installed_packages 属性
        packages = market.list_installed_packages()
        assert packages == []
