"""契约：技能级模型/思考档位覆盖真实生效（不再只回显）。

口径：`fork` 技能把 model/effort 传给子代理；子代理按覆盖后的配置解析 provider；
解析失败回退父 provider 且不阻断执行。`paths` 已从模型/载荷移除。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bandi.agent.subagent import (
    SubagentConfig,
    subagent_overridden_config,
    subagent_provider_for_overrides,
)
from bandi.core.config import BandiConfig


def test_skill_model_and_effort_override_config_copy() -> None:
    cfg = BandiConfig()
    cfg.llm.default_model = "global-model"
    cfg.llm.thinking_mode = "low"
    tuned = subagent_overridden_config(cfg, SubagentConfig(model="skill-model", effort="high"))

    assert tuned.llm.default_model == "skill-model"
    assert tuned.llm.thinking_mode == "high"
    assert cfg.llm.default_model == "global-model", "覆盖不得改写全局配置对象"
    assert cfg.llm.thinking_mode == "low"


def test_no_override_returns_same_config_object() -> None:
    cfg = BandiConfig()
    assert subagent_overridden_config(cfg, SubagentConfig()) is cfg


def test_provider_override_builds_model_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    sentinel = object()

    class _FakeRegistry:
        def get_provider(self, model: str, config: BandiConfig) -> object:
            seen["model"] = model
            seen["provider_config"] = config
            return sentinel

    monkeypatch.setattr("bandi.agent.factory.get_provider_registry", lambda cfg: _FakeRegistry())
    parent = object()
    cfg = BandiConfig()
    cfg.llm.default_model = "global-model"

    got = subagent_provider_for_overrides(parent, SubagentConfig(model="skill-model"), cfg)

    assert got is sentinel
    assert seen["model"] == "skill-model"
    assert seen["provider_config"] is cfg


def test_provider_override_falls_back_to_parent_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken_registry(config: BandiConfig) -> object:
        raise RuntimeError("no credentials")

    monkeypatch.setattr("bandi.agent.factory.get_provider_registry", _broken_registry)
    parent = object()
    cfg = BandiConfig()
    cfg.llm.default_model = "global-model"

    got = subagent_provider_for_overrides(parent, SubagentConfig(model="skill-model"), cfg)

    assert got is parent, "解析失败必须回退父 provider（子代理不能因此失败）"


def test_provider_registry_is_process_cached() -> None:
    from bandi.agent.factory import get_provider_registry

    cfg = BandiConfig()
    assert get_provider_registry(cfg) is get_provider_registry(cfg), (
        "同一 provider/网关必须复用注册表（否则每次技能 fork 新建客户端）"
    )


def test_skill_paths_no_longer_parsed_or_exposed(tmp_path: Path) -> None:
    from bandi.skills.loader import SkillLoader

    skill_file = tmp_path / "skills" / "p" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: p\ndescription: d\ntriggers: [x]\npaths: [src/**]\n---\n\n正文\n",
        encoding="utf-8",
    )
    skill = SkillLoader(str(tmp_path / "skills")).load(str(skill_file))
    assert skill is not None
    assert not hasattr(skill, "paths"), "无消费方的 paths 字段不得留在模型上"


def test_registry_cache_separates_thinking_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """破坏者：同模型同凭证、仅思考档位不同——不得复用同一 provider 实例。

    否则 `effort` 覆盖命中缓存实例，档位静默失效（B1 覆盖链的隐藏断点）。
    """
    from bandi.core.config import LLMConfig
    from bandi.llm.registry import ProviderRegistry

    cfg = BandiConfig()
    cfg.llm = LLMConfig(
        default_model="m", provider="openai_compat", api_key="k", base_url="http://gw.example/v1"
    )
    cfg.llm.provider = "openai_compat"
    registry = ProviderRegistry(cfg)

    cfg.llm.thinking_mode = "low"
    low = registry.get_provider("m", cfg)
    cfg.llm.thinking_mode = "high"
    high = registry.get_provider("m", cfg)
    cfg.llm.thinking_mode = "high"
    high_again = registry.get_provider("m", cfg)

    assert low is not high, "档位不同必须拿到不同实例"
    assert high is high_again, "档位相同应复用实例（缓存仍生效）"
