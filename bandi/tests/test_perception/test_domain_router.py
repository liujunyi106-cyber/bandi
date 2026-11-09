# 功能：DomainRouter 测试

from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp

import yaml

from bandi.perception.domain_router import (
    CONFIDENCE_CONFIRM,
    DomainRouter,
    YamlDomainConfigProvider,
    _fuzzy_keyword_score,
    _levenshtein_distance,
    _tokenize,
)


def _make_yaml_provider(domains: dict) -> YamlDomainConfigProvider:
    tmpdir = Path(mkdtemp())
    path = tmpdir / "domains.yaml"
    path.write_text(yaml.dump({"domains": domains}, allow_unicode=True), encoding="utf-8")
    provider = YamlDomainConfigProvider(str(path))
    return provider


class TestYamlDomainConfigProvider:
    def test_loads_yaml(self) -> None:
        provider = _make_yaml_provider(
            {
                "test_domain": {
                    "label": "测试",
                    "entity_types": ["note"],
                    "allowed_tools": ["read", "write"],
                    "keywords": ["测试", "test"],
                    "synonyms": {"试一下": "测试"},
                },
            }
        )
        domains = provider.get_domains()
        assert "test_domain" in domains
        dd = domains["test_domain"]
        assert dd.label == "测试"
        assert dd.entity_types == ["note"]
        assert dd.allowed_tools == ["read", "write"]
        assert dd.keywords == ["测试", "test"]
        assert dd.synonyms == {"试一下": "测试"}

    def test_missing_file_returns_empty(self) -> None:
        provider = YamlDomainConfigProvider("/nonexistent/path.yaml")
        assert provider.get_domains() == {}

    def test_get_domain(self) -> None:
        provider = _make_yaml_provider(
            {
                "test_domain": {"label": "测试", "keywords": ["test"]},
            }
        )
        dd = provider.get_domain("test_domain")
        assert dd is not None
        assert dd.label == "测试"
        assert provider.get_domain("not_exists") is None

    def test_reload(self) -> None:
        tmpdir = Path(mkdtemp())
        path = tmpdir / "domains.yaml"
        path.write_text(
            yaml.dump({"domains": {"d1": {"label": "one"}}}, allow_unicode=True), encoding="utf-8"
        )
        provider = YamlDomainConfigProvider(str(path))
        assert "d1" in provider.get_domains()
        path.write_text(
            yaml.dump({"domains": {"d2": {"label": "two"}}}, allow_unicode=True), encoding="utf-8"
        )
        provider.reload()
        assert "d2" in provider.get_domains()
        assert "d1" not in provider.get_domains()


class TestDomainRouter:
    def test_exact_match_project(self) -> None:
        router = DomainRouter()
        route = router.route("查一下项目进度")
        assert route.domain_key == "project_management"
        assert route.action == "confirm"
        assert route.confidence >= CONFIDENCE_CONFIRM
        assert route.is_level1
        assert len(route.matched_keywords) >= 2

    def test_exact_match_content_creation(self) -> None:
        router = DomainRouter()
        route = router.route("写小红书文章")
        assert route.domain_key == "content_creation"
        assert route.action == "auto"
        assert route.is_level1

    def test_exact_match_knowledge(self) -> None:
        router = DomainRouter()
        route = router.route("搜索Python知识")
        assert route.domain_key == "knowledge_management"
        assert route.is_level1

    def test_exact_match_contact(self) -> None:
        router = DomainRouter()
        route = router.route("找一下张三的联系方式")
        assert route.domain_key == "contact_management"
        assert route.action == "confirm"

    def test_exact_match_tool(self) -> None:
        router = DomainRouter()
        route = router.route("运行这个脚本")
        assert route.domain_key == "tool_operation"
        assert route.confidence >= CONFIDENCE_CONFIRM

    def test_exact_match_general_chat(self) -> None:
        router = DomainRouter()
        route = router.route("你是谁？")
        assert route.domain_key == "general_chat"
        assert route.action == "confirm"

    def test_exact_match_data_analysis(self) -> None:
        router = DomainRouter()
        route = router.route("分析一下这些数据")
        assert route.domain_key == "data_analysis"
        assert route.action == "confirm"

    def test_fuzzy_match(self) -> None:
        provider = _make_yaml_provider(
            {
                "tool_operation": {
                    "label": "工具操作",
                    "entity_types": [],
                    "allowed_tools": ["bash"],
                    "keywords": ["运行", "执行", "命令"],
                },
            }
        )
        router = DomainRouter(provider)
        route = router.route("帮我执一下这个脚本")
        assert route.domain_key == "tool_operation"
        assert not route.is_level1
        assert route.confidence > 0.0

    def test_fallback_clarify(self) -> None:
        provider = _make_yaml_provider(
            {
                "project": {
                    "label": "项目",
                    "entity_types": [],
                    "allowed_tools": ["read"],
                    "keywords": ["unique_project_kw_xyz123"],
                },
            }
        )
        router = DomainRouter(provider)
        route = router.route("今天天气怎么样")
        assert route.action == "clarify"
        assert route.domain is None

    def test_empty_input(self) -> None:
        router = DomainRouter()
        route = router.route("")
        assert route.action == "clarify"
        assert route.domain is None

    def test_confirm_level(self) -> None:
        provider = _make_yaml_provider(
            {
                "test_domain": {
                    "label": "测试",
                    "entity_types": [],
                    "allowed_tools": ["read"],
                    "keywords": ["边界测试关键词"],
                },
            }
        )
        router = DomainRouter(provider)
        route = router.route("边界")
        assert route.action == "clarify"

    def test_exact_match_over_fuzzy(self) -> None:
        router = DomainRouter()
        route = router.route("写一篇小红书文章")
        assert route.is_level1

    def test_multi_domain_confirm(self) -> None:
        provider = _make_yaml_provider(
            {
                "domain_a": {
                    "label": "领域A",
                    "entity_types": [],
                    "allowed_tools": ["read"],
                    "keywords": ["ambig"],
                },
                "domain_b": {
                    "label": "领域B",
                    "entity_types": [],
                    "allowed_tools": ["read"],
                    "keywords": ["ambig", "test"],
                },
            }
        )
        router = DomainRouter(provider)
        route = router.route("ambig")
        assert route.domain is not None


class TestHelpers:
    def test_tokenize_chinese(self) -> None:
        tokens = _tokenize("写一篇小红书文章")
        assert len(tokens) >= 1
        assert tokens[0] == "写一篇小红书文章"

    def test_tokenize_mixed(self) -> None:
        tokens = _tokenize("查一下 project 进度")
        assert "project" in tokens
        assert any("进度" in t for t in tokens)

    def test_levenshtein_identical(self) -> None:
        assert _levenshtein_distance("hello", "hello") == 0

    def test_levenshtein_completely_different(self) -> None:
        assert _levenshtein_distance("abc", "xyz") == 3

    def test_levenshtein_one_insert(self) -> None:
        assert _levenshtein_distance("cat", "cats") == 1

    def test_levenshtein_one_substitute(self) -> None:
        assert _levenshtein_distance("cat", "car") == 1

    def test_fuzzy_keyword_score_exact(self) -> None:
        score = _fuzzy_keyword_score("执行", "执行")
        assert score == 1.0

    def test_fuzzy_keyword_score_one_typo(self) -> None:
        score = _fuzzy_keyword_score("执行", "执形")
        assert score >= 0.5
        assert score < 1.0

    def test_fuzzy_keyword_score_no_match(self) -> None:
        score = _fuzzy_keyword_score("xyz", "abc")
        assert score == 0.0
