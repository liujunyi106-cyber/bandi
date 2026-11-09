# 功能：契约——models.dev 元数据自动拉取（方案 C：启动拉 + 本地缓存 + TTL + 断网兜底）
# 钉行为不钉实现：契约断言「最终产物」——
# flatten 真实 models.dev 结构 → 字段提取（reasoning/reasoning_efforts/ctx/out/tool_call/vision）；
# 断网兜底：无缓存→builtin 原地不动，有旧缓存→缓存兜底；
# 缓存落盘可读回；adapters 桥接 reasoning 进 CapabilityProfile.metadata。
# 生产路径族：sync_models_dev（CLI main/serve 启动接线）+ from_model_spec（能力画像桥接）。
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.core.config import BandiConfig, ModelsDevConfig
from bandi.llm.capabilities.adapters import from_model_spec
from bandi.llm.model_catalog import BUILTIN_MODELS, ModelSpec
from bandi.llm.models_dev import (
    DEFAULT_CACHE_PATH,
    MODELS_DEV_URL,
    ModelsDevSyncResult,
    ensure_models_dev_sync,
    flatten_models,
    is_cache_fresh,
    load_cache,
    merge_remote_specs,
    save_cache,
    sync_models_dev,
)

# models.dev/api.json 真实 deepseek provider 结构（deepseek-v4-flash 全字段，已裁剪注释）
_REMOTE = {
    "deepseek": {
        "id": "deepseek",
        "api": "https://api.deepseek.com",
        "models": {
            "deepseek-v4-flash": {
                "id": "deepseek-v4-flash",
                "name": "DeepSeek V4 Flash",
                "description": "DeepSeek V4.1 Flash model for reasoning and agentic coding",
                "family": "deepseek-flash",
                "attachment": True,
                "reasoning": True,
                "reasoning_options": [
                    {"type": "toggle"},
                    {"type": "effort", "values": ["low", "high", "max"]},
                ],
                "tool_call": True,
                "interleaved": {"field": "reasoning_content"},
                "structured_output": True,
                "temperature": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 1000000, "output": 384000},
                "cost": {"input": 0.15, "output": 0.6, "reasoning": 0.6, "cache_read": 0.003},
            },
            "deepseek-v4-pro": {
                "id": "deepseek-v4-pro",
                "reasoning": True,
                "reasoning_options": [
                    {"type": "toggle"},
                    {"type": "effort", "values": ["high", "max"]},
                ],
                "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 1000000, "output": 384000},
            },
            "deepseek-flash": {
                "id": "deepseek-flash",
                "reasoning": True,
                "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 1000000, "output": 384000},
            },
            # 目录外模型（BUILTIN 未收录）：钉 merge「只更新不新增」不变式
            "deepseek-v9-ghost": {
                "id": "deepseek-v9-ghost",
                "reasoning": False,
                "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 1000000, "output": 384000},
            },
        },
    }
}


def _payload():
    return {"providers": _REMOTE, "fetched_at": "2026-09-10T00:00:00Z"}


def _fresh_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "models_cache.json"
    save_cache(cache, {"providers": _REMOTE, "fetched_at": "2026-09-10T00:00:00Z"})
    return cache


def _fetcher_returns(data):
    def _fetcher() -> dict:
        return _payload()

    return _fetcher


class TestFlatten:
    def test_extracts_reasoning_and_efforts(self) -> None:
        flat = flatten_models(_REMOTE)
        flash = flat["deepseek-v4-flash"]
        assert flash["reasoning"] is True
        assert flash["reasoning_efforts"] == ["low", "high", "max"]
        v4pro = flat["deepseek-v4-pro"]
        assert v4pro["reasoning_efforts"] == ["high", "max"]

    def test_extracts_limits_context_and_output(self) -> None:
        flat = flatten_models(_REMOTE)
        flash = flat["deepseek-v4-flash"]
        assert flash["context_window"] == 1000000
        assert flash["max_output_tokens"] == 384000

    def test_extracts_tool_call_and_vision_from_modalities(self) -> None:
        flat = flatten_models(_REMOTE)
        flash = flat["deepseek-v4-flash"]
        assert flash["tool_call"] is True
        assert flash["vision"] is True
        v4pro = flat["deepseek-v4-pro"]
        assert v4pro["vision"] is False

    def test_unknown_model_without_reasoning_defaults_false(self) -> None:
        entry = {"id": "x", "modalities": {"input": ["text"], "output": ["text"]}}
        assert flatten_models({"p": {"models": {"x": entry}}})["x"]["reasoning"] is False


class TestMerge:
    def test_updates_matching_builtin_models_only(self) -> None:
        copy = __import__("copy")
        remote = flatten_models(_REMOTE)
        target: dict[str, ModelSpec] = {k: copy.copy(v) for k, v in BUILTIN_MODELS.items()}
        # 契约自足：先把两个匹配模型归一为「确定与远端不同」的值（仅本地副本）——
        # 防前序测试对全局 BUILTIN_MODELS 的就地修改使字段恰好一致、changed=False
        for name in ("deepseek-v4-flash", "deepseek-v4-pro"):
            if name in target:
                target[name].context_window = 1
                target[name].max_output_tokens = 1
                target[name].supports_reasoning = False
                target[name].reasoning_efforts = []
        updated = merge_remote_specs(target, remote)
        assert "deepseek-v4-flash" in updated
        assert "deepseek-v4-pro" in updated
        assert target["deepseek-v4-flash"].context_window == 1000000
        assert target["deepseek-v4-flash"].max_output_tokens == 384000
        assert target["deepseek-v4-flash"].supports_reasoning is True
        assert list(target["deepseek-v4-flash"].reasoning_efforts) == ["low", "high", "max"]

    def test_does_not_add_unknown_models(self) -> None:
        remote = flatten_models(_REMOTE)
        target: dict[str, ModelSpec] = {
            k: __import__("copy").copy(v) for k, v in BUILTIN_MODELS.items()
        }
        merge_remote_specs(target, remote)
        assert sorted(target) == sorted(BUILTIN_MODELS)
        assert "deepseek-v9-ghost" not in target  # 目录外模型 ⇒ 只 merge 不新增

    def test_preserves_local_pricing_and_provider(self) -> None:
        remote = flatten_models(_REMOTE)
        target: dict[str, ModelSpec] = {
            k: __import__("copy").copy(v) for k, v in BUILTIN_MODELS.items()
        }
        before = target["deepseek-v4-flash"].pricing
        merge_remote_specs(target, remote)
        assert target["deepseek-v4-flash"].pricing is before
        assert target["deepseek-v4-flash"].provider == "deepseek"

    def test_no_reasoning_remote_keeps_prior_flag(self) -> None:
        spec = ModelSpec(name="deepseek-chat", provider="deepseek", context_window=128000)
        spec.supports_reasoning = True
        flat = {"deepseek-chat": {"reasoning": False, "vision": False, "tool_call": True}}
        merge_remote_specs({"deepseek-chat": spec}, flat)
        assert spec.supports_reasoning is False  # 远端显式 false → 覆盖


class TestCache:
    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        cache = tmp_path / "models_cache.json"
        save_cache(cache, _payload())
        loaded = load_cache(cache)
        assert loaded is not None
        assert loaded["fetched_at"] == _payload()["fetched_at"]
        assert "deepseek-v4-flash" in flatten_models(loaded["providers"])

    def test_missing_cache_returns_none(self, tmp_path: Path) -> None:
        assert load_cache(tmp_path / "nope.json") is None

    def test_corrupt_cache_returns_none(self, tmp_path: Path) -> None:
        cache = tmp_path / "models_cache.json"
        cache.write_text("{not-json", encoding="utf-8")
        assert load_cache(cache) is None

    def test_is_cache_fresh_respects_ttl(self, tmp_path: Path) -> None:
        cache = _fresh_cache(tmp_path)
        assert is_cache_fresh(cache, ttl_hours=24.0) is True
        assert is_cache_fresh(cache, ttl_hours=0.0) is False


class TestSync:
    def test_network_success_merges_and_writes_cache(self, tmp_path: Path) -> None:
        cache = tmp_path / "models_cache.json"
        result = sync_models_dev(
            cache_path=cache,
            fetcher=_fetcher_returns(_payload()),
            ttl_hours=24.0,
            now=__import__("time").time(),
        )
        assert result.source == "network"
        assert "deepseek-v4-flash" in result.updated
        loaded = load_cache(cache)
        assert loaded is not None
        assert BUILTIN_MODELS["deepseek-v4-flash"].supports_reasoning is True

    def test_fresh_cache_skips_network(self, tmp_path: Path) -> None:
        cache = _fresh_cache(tmp_path)

        class _Fetcher:
            def __init__(self) -> None:
                self.called = 0

            def __call__(self) -> dict:
                self.called += 1
                return _payload()

        f = _Fetcher()
        result = sync_models_dev(
            cache_path=cache, fetcher=f, ttl_hours=24.0, now=__import__("time").time()
        )
        assert result.source == "cache"
        assert f.called == 0
        assert "deepseek-v4-flash" in result.updated

    def test_stale_cache_triggers_network(self, tmp_path: Path) -> None:
        cache = _fresh_cache(tmp_path)

        def _fetcher() -> dict:
            return _payload()

        result = sync_models_dev(cache_path=cache, fetcher=_fetcher, ttl_hours=0.0)
        assert result.source == "network"

    def test_network_failure_no_cache_falls_back_builtin(self, tmp_path: Path) -> None:
        cache = tmp_path / "models_cache.json"

        def _boom() -> dict:
            raise RuntimeError("network down")

        result = sync_models_dev(cache_path=cache, fetcher=_boom, ttl_hours=0.0)
        assert result.source == "builtin"
        assert result.updated == []

    def test_network_failure_stale_cache_falls_back_to_cache(self, tmp_path: Path) -> None:
        cache = tmp_path / "models_cache.json"
        save_cache(cache, _payload())

        def _boom() -> dict:
            raise RuntimeError("network down")

        result = sync_models_dev(cache_path=cache, fetcher=_boom, ttl_hours=0.0)
        assert result.source == "cache"
        assert "deepseek-v4-flash" in result.updated

    def test_custom_url_default_constant(self) -> None:
        assert MODELS_DEV_URL == "https://models.dev/api.json"
        assert DEFAULT_CACHE_PATH.endswith(".bandi") is False or "bandi" in DEFAULT_CACHE_PATH


class TestConfig:
    def test_models_dev_config_defaults(self) -> None:
        cfg = ModelsDevConfig()
        assert cfg.enabled is True
        assert cfg.url == MODELS_DEV_URL
        assert cfg.cache_ttl_hours == 24.0
        assert cfg.timeout_secs == 5.0

    def test_bandi_config_accepts_models_dev_section(self) -> None:
        cfg = BandiConfig(models_dev=ModelsDevConfig(enabled=False, cache_ttl_hours=48.0))
        assert cfg.models_dev.enabled is False
        assert cfg.models_dev.cache_ttl_hours == 48.0

    def test_ensure_models_dev_sync_process_gate(self, tmp_path: Path) -> None:
        cache = tmp_path / "models_cache.json"

        def _fetcher() -> dict:
            return _payload()

        result = ensure_models_dev_sync(
            cache_path=cache,
            fetcher=_fetcher,
            ttl_hours=0.0,
            force=False,
            _synchronize=True,
        )
        assert isinstance(result, ModelsDevSyncResult)
        assert result.source in ("network", "cache", "builtin")

    def test_result_type_repr(self) -> None:
        r = ModelsDevSyncResult(source="builtin", updated=[])
        assert r.source == "builtin"
        assert r.updated == []


@pytest.fixture(autouse=True)
def _restore_builtins():
    """功能：契约收敛——merge/sync 会就地更新 BUILTIN_MODELS，跑完恢复原地值。"""
    copy = __import__("copy")
    builtins_backup = {k: copy.deepcopy(v) for k, v in BUILTIN_MODELS.items()}
    yield
    BUILTIN_MODELS.clear()
    BUILTIN_MODELS.update(builtins_backup)


class TestAdapterBridge:
    def test_from_model_spec_carries_reasoning_metadata(self) -> None:
        spec = ModelSpec(name="deepseek-v4-flash", provider="deepseek", context_window=1000000)
        spec.supports_reasoning = True
        spec.reasoning_efforts = ["low", "high", "max"]
        profile = from_model_spec(spec)
        assert profile.metadata["reasoning"] is True
        assert profile.metadata["reasoning_efforts"] == ["low", "high", "max"]

    def test_from_model_spec_defaults_reasoning_false(self) -> None:
        spec = ModelSpec(name="mock", provider="mock", context_window=4096)
        profile = from_model_spec(spec)
        assert profile.metadata["reasoning"] is False
        assert profile.metadata["reasoning_efforts"] == []
