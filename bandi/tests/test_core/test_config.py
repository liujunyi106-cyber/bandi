from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from bandi.core.config import (
    AgentConfig,
    BandiConfig,
    LLMConfig,
    PermissionsConfig,
    SafetyConfig,
    SystemConfig,
    ToolsConfig,
)
from bandi.core.exceptions import ConfigError


class TestSystemConfig:
    def test_expand_tilde(self) -> None:
        resolved = SystemConfig.resolve_path("~/.bandi/data")
        assert os.path.isabs(resolved)  # absolute path

    def test_resolve_no_tilde(self) -> None:
        assert SystemConfig.resolve_path("/tmp/memo") == "/tmp/memo"

    def test_default_values(self) -> None:
        cfg = SystemConfig()
        assert "~" in cfg.temp_dir


class TestLLMConfig:
    def test_defaults(self) -> None:
        cfg = LLMConfig()
        # 默认模型 = deepseek-v4.1-flash（网关命名；官方现役名为 deepseek-flash
        # 目录两条都收录，见 model_catalog.BUILTIN_MODELS 注）
        assert cfg.default_model == "deepseek-v4.1-flash"
        assert cfg.timeout == 60
        assert cfg.budget_limit == -1.0

    def test_custom_values(self) -> None:
        cfg = LLMConfig(default_model="gpt-4", api_key="test-key", timeout=120)
        assert cfg.default_model == "gpt-4"
        assert cfg.api_key == "test-key"
        assert cfg.timeout == 120

    def test_fallback_chain_defaults_empty(self) -> None:
        # 功能：本地产品不预设降级链——模板不含该字段时不得自动带上跨厂商候选
        cfg = LLMConfig()
        assert cfg.fallback_chain == [], "默认降级链必须为空"

    def test_fallback_chain_configurable(self) -> None:
        # 功能：机制的逃生门仍在——用户显式配置即生效
        cfg = LLMConfig(fallback_chain=["model-a", "model-b"])
        assert cfg.fallback_chain == ["model-a", "model-b"]

    def test_api_key_empty_by_default(self) -> None:
        cfg = LLMConfig()
        assert cfg.api_key == ""

    def test_max_output_tokens_must_be_positive(self) -> None:
        """
        每轮显式失败）。"""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LLMConfig(max_output_tokens=0)
        with pytest.raises(ValidationError):
            LLMConfig(max_output_tokens=-1)
        assert LLMConfig(max_output_tokens=8192).max_output_tokens == 8192


class TestAgentConfig:
    def test_defaults(self) -> None:
        cfg = AgentConfig()
        assert cfg.mode == "execute"
        assert cfg.max_iterations == 15
        assert cfg.token_budget_limit == -1

    def test_custom_mode(self) -> None:
        cfg = AgentConfig(mode="plan")
        assert cfg.mode == "plan"


class TestSafetyConfig:
    def test_default_enabled(self) -> None:
        cfg = SafetyConfig()
        assert cfg.enabled is True
        assert cfg.max_output_chars == 8000

    def test_custom_max_lines(self) -> None:
        cfg = SafetyConfig(max_output_lines=100)
        assert cfg.max_output_lines == 100


class TestPermissionsConfig:
    def test_defaults(self) -> None:
        cfg = PermissionsConfig()
        assert "bash" in cfg.ask_tools
        assert "read" in cfg.bypass_tools
        assert "rm -rf /" in cfg.deny_commands

    def test_custom_deny(self) -> None:
        cfg = PermissionsConfig(deny_tools=["format"])
        assert "format" in cfg.deny_tools


class TestBandiConfig:
    def test_default_values(self) -> None:
        cfg = BandiConfig()
        assert cfg.preferred_language == "zh-CN"
        assert isinstance(cfg.system, SystemConfig)
        assert isinstance(cfg.llm, LLMConfig)
        assert isinstance(cfg.agent, AgentConfig)
        assert isinstance(cfg.tools, ToolsConfig)
        assert isinstance(cfg.safety, SafetyConfig)
        assert isinstance(cfg.permissions, PermissionsConfig)

    def test_deep_merge_simple(self) -> None:
        base = {"key1": "a", "key2": "b"}
        override = {"key2": "c"}
        result = BandiConfig.deep_merge(base, override)
        assert result == {"key1": "a", "key2": "c"}

    def test_deep_merge_nested(self) -> None:
        base = {"llm": {"model": "gpt-4", "timeout": 60}}
        override = {"llm": {"timeout": 120}}
        result = BandiConfig.deep_merge(base, override)
        assert result == {"llm": {"model": "gpt-4", "timeout": 120}}

    def test_deep_merge_ignores_none(self) -> None:
        base = {"key": "value"}
        override = {"key": None}
        result = BandiConfig.deep_merge(base, override)
        assert result == {"key": "value"}

    def test_deep_merge_nested_preserves_unrelated(self) -> None:
        base = {"llm": {"model": "gpt-4"}, "agent": {"mode": "execute"}}
        override = {"llm": {"timeout": 120}}
        result = BandiConfig.deep_merge(base, override)
        assert result["agent"]["mode"] == "execute"
        assert result["llm"]["model"] == "gpt-4"
        assert result["llm"]["timeout"] == 120

    def test_deep_merge_adds_new_keys(self) -> None:
        base = {"existing": "value"}
        override = {"new_key": "new_value"}
        result = BandiConfig.deep_merge(base, override)
        assert result == {"existing": "value", "new_key": "new_value"}

    def test_env_overrides_api_key(self) -> None:
        try:
            os.environ["BANDI_API_KEY"] = "sk-test123"
            overrides = BandiConfig._env_overrides("BANDI_")
            assert overrides["llm"]["api_key"] == "sk-test123"
        finally:
            del os.environ["BANDI_API_KEY"]

    def test_env_overrides_agent_config(self) -> None:
        try:
            os.environ["BANDI_AGENT_MAX_ITERATIONS"] = "42"
            overrides = BandiConfig._env_overrides("BANDI_")
            assert overrides["agent"]["max_iterations"] == "42"
        finally:
            del os.environ["BANDI_AGENT_MAX_ITERATIONS"]

    def test_env_overrides_llm_base_url(self) -> None:
        try:
            os.environ["BANDI_LLM_BASE_URL"] = "https://custom.api.com"
            overrides = BandiConfig._env_overrides("BANDI_")
            assert overrides["llm"]["base_url"] == "https://custom.api.com"
        finally:
            del os.environ["BANDI_LLM_BASE_URL"]

    def test_env_overrides_prefix_only(self) -> None:
        try:
            os.environ["BANDI_ONLY"] = "value"
            overrides = BandiConfig._env_overrides("BANDI_")
            assert overrides["only"] == "value"
        finally:
            del os.environ["BANDI_ONLY"]

    def test_env_overrides_unrelated_var(self) -> None:
        old_path = os.environ.get("PATH")
        try:
            os.environ["PATH"] = "/usr/bin"
            overrides = BandiConfig._env_overrides("BANDI_")
            assert "path" not in overrides
        finally:
            if old_path is None:
                del os.environ["PATH"]
            else:
                os.environ["PATH"] = old_path

    def test_load_from_yaml(self, tmp_path: Path, monkeypatch) -> None:
        data = {
            "preferred_language": "en",
            "agent": {"mode": "plan", "max_iterations": 20},
            "llm": {"default_model": "gpt-4"},
        }
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

        monkeypatch.setattr("bandi.core.config.BandiConfig._bandi_home", lambda: tmp_path)
        monkeypatch.setattr("bandi.core.config.BandiConfig._find_project_config", lambda: None)
        cfg = BandiConfig.load()
        assert cfg.preferred_language == "en"
        assert cfg.agent.mode == "plan"
        assert cfg.agent.max_iterations == 20
        assert cfg.llm.default_model == "gpt-4"

    def test_web_fetch_allow_private_networks_default_false(self) -> None:
        cfg = BandiConfig()
        assert cfg.tools.web_fetch_allow_private_networks is False

    def test_web_fetch_allow_private_networks_custom(self) -> None:
        cfg = BandiConfig.model_validate({"tools": {"web_fetch_allow_private_networks": True}})
        assert cfg.tools.web_fetch_allow_private_networks is True

    def test_load_yaml_with_chinese_content(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            f.write('preferred_language: "zh-CN"\nagent:\n  mode: "plan"\n')

        monkeypatch.setattr("bandi.core.config.BandiConfig._bandi_home", lambda: tmp_path)
        monkeypatch.setattr("bandi.core.config.BandiConfig._find_project_config", lambda: None)
        cfg = BandiConfig.load()
        assert cfg.preferred_language == "zh-CN"
        assert cfg.agent.mode == "plan"

    def test_model_validate_with_overrides(self) -> None:
        cfg = BandiConfig.model_validate(
            {
                "preferred_language": "en",
                "agent": {"mode": "plan", "max_iterations": 20},
                "llm": {"default_model": "gpt-4"},
            }
        )
        assert cfg.preferred_language == "en"
        assert cfg.agent.mode == "plan"
        assert cfg.agent.max_iterations == 20
        assert cfg.llm.default_model == "gpt-4"

    def test_save_and_reload(self, tmp_path: Path) -> None:
        cfg = BandiConfig(preferred_language="en", agent=AgentConfig(mode="plan"))
        save_path = tmp_path / "test_config.yaml"
        cfg.save(save_path)
        assert save_path.exists()

        with open(save_path) as f:
            loaded = yaml.safe_load(f)
        assert loaded["preferred_language"] == "en"
        assert loaded["agent"]["mode"] == "plan"

    def test_save_accepts_str_path(self, tmp_path: Path) -> None:
        cfg = BandiConfig(preferred_language="en")
        save_path = tmp_path / "test_config_str.yaml"
        cfg.save(str(save_path))
        assert save_path.exists()

        with open(save_path) as f:
            loaded = yaml.safe_load(f)
        assert loaded["preferred_language"] == "en"

    def test_config_error_on_invalid_yaml(self, tmp_path: Path, monkeypatch) -> None:
        bad_file = tmp_path / "config.yaml"
        bad_file.write_text("{invalid: yaml: broken", encoding="utf-8")

        monkeypatch.setattr("bandi.core.config.BandiConfig._bandi_home", lambda: tmp_path)
        monkeypatch.setattr("bandi.core.config.BandiConfig._find_project_config", lambda: None)
        with pytest.raises(ConfigError, match="failed to load"):
            BandiConfig.load()

    def test_config_error_on_validation_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "bandi.core.config.BandiConfig._bandi_home", lambda: Path(tempfile.mkdtemp())
        )
        monkeypatch.setattr("bandi.core.config.BandiConfig._find_project_config", lambda: None)
        monkeypatch.setattr(
            "bandi.core.config.BandiConfig._env_overrides",
            lambda prefix: {
                "agent": {"max_iterations": "not-a-number"},
            },
        )
        with pytest.raises(ConfigError, match="config validation failed"):
            BandiConfig.load()

    def test_home_path(self) -> None:
        home = BandiConfig._bandi_home()
        assert home.name == ".bandi"
        assert home.parent == Path.home()


class TestDeepMergeEdgeCases:
    def test_merge_non_dict_types(self) -> None:
        base = {"key": [1, 2, 3]}
        override = {"key": [4, 5]}
        result = BandiConfig.deep_merge(base, override)
        assert result["key"] == [4, 5]

    def test_merge_into_empty_base(self) -> None:
        result = BandiConfig.deep_merge({}, {"llm": {"model": "gpt"}})
        assert result == {"llm": {"model": "gpt"}}

    def test_merge_with_empty_override(self) -> None:
        result = BandiConfig.deep_merge({"key": "value"}, {})
        assert result == {"key": "value"}

    def test_deep_merge_nested_three_levels(self) -> None:
        base = {"a": {"b": {"c": 1, "d": 2}}}
        override = {"a": {"b": {"c": 99}}}
        result = BandiConfig.deep_merge(base, override)
        assert result == {"a": {"b": {"c": 99, "d": 2}}}
