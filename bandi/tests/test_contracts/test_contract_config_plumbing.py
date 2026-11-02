"""
契约族：配置项必须真正到达消费者。

背景：全量发现 5 处「能跑通但配置无效」的静默错配——字段有定义、文档有承诺
用户改了却什么也不发生（属 同族缺陷）。本契约逐项钉「装配产物」，而非钉源码
文本：断言必须观察真实消费者（executor / 管道 / fetcher / PromptHarness）收到的值。

覆盖项：
1. `tools.temp_dir` → `ToolExecutor(temp_dir=...)`（此前只有形参默认，配置零消费者）
2. `models_dev.url` → `sync_models_dev` 的默认 fetcher（此前恒用模块常量）
3. `permissions.ask_risk_levels` → `PermissionsPipeline`（此前判定处硬编码 high/critical）
4. `preferred_language` → `PromptHarness`（此前生产两处裸构造，配置零消费者）
5. `agent.context_window` 哨兵 → 装配产物（端到端）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.agent.factory import build_agent_context
from bandi.core.config import BandiConfig
from bandi.llm.mock import MockProvider
from bandi.tools.registry import ToolRegistry


def _ctx_with(cfg: BandiConfig) -> Any:
    return build_agent_context(
        "hi",
        provider=MockProvider(),
        tool_registry=ToolRegistry(),
        bandi_config=cfg,
    )


def test_tools_temp_dir_reaches_executor(tmp_path: Path) -> None:
    """契约（1）：配置的 temp_dir 必须到达 ToolExecutor（长工具输出落盘位置）。"""
    cfg = BandiConfig()
    target = tmp_path / "bandi-tmp-dir"
    cfg.tools.temp_dir = str(target)

    ctx = _ctx_with(cfg)
    assert Path(ctx.tool_executor._temp_dir) == target, (
        "tools.temp_dir 未透传到 executor —— 用户改配置不改变落盘位置（死配置）"
    )


def test_context_window_sentinel_reaches_agent_config() -> None:
    """契约（5/端到端）：默认配置下 1M 模型的压缩窗口必须是 1M 口径，而非 128K。"""
    cfg = BandiConfig()  # 默认模型 deepseek-v4.1-flash（目录窗口 1,000,000）
    assert cfg.agent.context_window == 0, "字段默认必须是「未显式配置」哨兵"

    ctx = _ctx_with(cfg)
    assert ctx.config.context_window == 1_000_000, (
        f"默认 1M 模型实际装配窗口 {ctx.config.context_window} —— 仍被静态默认锁死"
    )
    # 压缩触发水位 ≈ (1M - 20K) × 0.85 ≈ 833K，而非修复前的 ~91.8K
    assert ctx.effective_context_window * 0.85 > 800_000


def test_catalog_window_wins_over_explicit_config_end_to_end() -> None:
    """放开端到端：已收录模型目录优先（1M）；显式配置不再锁死窗口。"""
    cfg = BandiConfig()
    cfg.agent.context_window = 64_000
    ctx = _ctx_with(cfg)
    assert ctx.config.context_window == 1_000_000, (
        "目录 1M 模型装配窗口应为 1M（85% 水位按真实窗口算，不被静态配置锁死）"
    )


def test_explicit_config_backs_unknown_model_end_to_end() -> None:
    """兜底端到端：目录未收录 + 显式配置 → 配置生效（兜底语义保留）。"""
    cfg = BandiConfig()
    cfg.llm.default_model = "unknown-plumbing-model"
    cfg.agent.context_window = 64_000
    ctx = _ctx_with(cfg)
    assert ctx.config.context_window == 64_000


def test_models_dev_url_reaches_default_fetcher(tmp_path: Path, monkeypatch: Any) -> None:
    """契约（2）：config.models_dev.url 必须传到默认 fetcher（自定义镜像/代理生效）。"""
    from bandi.llm import models_dev

    seen: dict[str, Any] = {}

    def _fake_fetcher(timeout_secs: float, url: str = "") -> dict[str, Any]:
        seen["timeout"] = timeout_secs
        seen["url"] = url
        return {"providers": {}}

    monkeypatch.setattr(models_dev, "_default_fetcher", _fake_fetcher)
    cache = tmp_path / "models_cache.json"

    models_dev.sync_models_dev(
        cache_path=cache,
        ttl_hours=0.0,
        timeout_secs=1.5,
        url="https://mirror.example/api.json",
    )
    assert seen.get("url") == "https://mirror.example/api.json", (
        "models_dev.url 未透传 —— 自定义镜像/代理静默失效（死配置）"
    )
    assert seen.get("timeout") == 1.5


def test_models_dev_url_defaults_to_module_constant(tmp_path: Path, monkeypatch: Any) -> None:
    """破坏者：未配置 url 时必须回落模块常量，不得变成空串（会静默打不出去）。"""
    from bandi.llm import models_dev

    seen: dict[str, Any] = {}

    def _fake_fetcher(timeout_secs: float, url: str = "") -> dict[str, Any]:
        seen["url"] = url
        return {"providers": {}}

    monkeypatch.setattr(models_dev, "_default_fetcher", _fake_fetcher)
    models_dev.sync_models_dev(cache_path=tmp_path / "c.json", ttl_hours=0.0, timeout_secs=1.0)
    assert seen.get("url") == models_dev.MODELS_DEV_URL


def test_ask_risk_levels_config_is_consumed() -> None:
    """契约（3）：ask_risk_levels 必须真正驱动风险级询问判定。"""
    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    class _MediumTool:
        name = "some_medium_tool"
        risk_level = "medium"
        permissions_override = None

    tc = ToolCall(id="t", name="some_medium_tool", arguments={})
    tool = _MediumTool()

    # 默认（high/critical）→ medium 不询问
    assert PermissionsPipeline().needs_approval(tc, tool, "execute") is False

    assert (
        PermissionsPipeline(ask_risk_levels=["medium", "high", "critical"]).needs_approval(
            tc, tool, "execute"
        )
        is True
    ), "ask_risk_levels 未被消费 —— 风险级判定仍是硬编码（死配置）"

    # 显式空列表 = 关闭风险级询问（与 deny/ask/bypass 三名单同语义）
    assert PermissionsPipeline(ask_risk_levels=[]).needs_approval(tc, tool, "execute") is False


def test_ask_risk_levels_default_matches_config() -> None:
    """双源防漂移：管道默认必须等于 config 默认（与 ask_tools 同纪律）。"""
    from bandi.core.config import PermissionsConfig
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    assert set(PermissionsPipeline()._ask_risk_levels) == set(PermissionsConfig().ask_risk_levels)


def test_preferred_language_reaches_prompt_harness() -> None:
    """契约（4）：preferred_language 必须到达 PromptHarness（此前生产裸构造）。"""
    from bandi.agent.prompt_harness.builder import prompt_harness_from_config

    zh = prompt_harness_from_config(BandiConfig(preferred_language="zh-CN"))
    en = prompt_harness_from_config(BandiConfig(preferred_language="en"))

    assert zh._language == "zh-CN"
    assert en._language == "en", "preferred_language 未透传 —— 配置 en 仍按中文作答"

    # 消费者侧真实生效：英文配置产出的 system prompt 与中文不同
    from bandi.perception.context_loader import ContextBundle

    zh_prompt = zh.build(mode="execute", context=ContextBundle())
    en_prompt = en.build(mode="execute", context=ContextBundle())
    assert zh_prompt != en_prompt


@pytest.mark.parametrize("value", ["", None])
def test_preferred_language_blank_falls_back_to_zh(value: Any) -> None:
    """破坏者：空值不得产出空语言（会让提示词语言段缺失）。"""
    from bandi.agent.prompt_harness.builder import prompt_harness_from_config

    class _Cfg:
        preferred_language = value

    assert prompt_harness_from_config(_Cfg())._language == "zh-CN"
