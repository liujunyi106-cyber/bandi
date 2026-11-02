# 功能：目录外模型能力查询契约（第九轮）——思考挡位的 models.dev 实时解析
#
# 钉行为不钉实现：
# 1. lookup_model_capabilities：精确命中 / 末段归一命中 / 末段多义放弃 / 无数据 None /
# 缓存缺失 None / toggle 型（无 effort 值）返回空档位
# 2. /settings/model 动态补条目：目录外档案模型 → models.dev 有数据则补（none 档过滤）；
# 查不到不补（不虚构能力）；目录已含（归一）不重复补
from __future__ import annotations

import json
from pathlib import Path

import pytest

import bandi.llm.models_dev as models_dev
from bandi.core.config import BandiConfig


def _entry(reasoning: bool, opts: list | None = None) -> dict:
    entry: dict = {"reasoning": reasoning, "limit": {"context": 1000, "output": 100}}
    if opts is not None:
        entry["reasoning_options"] = opts
    return entry


@pytest.fixture()
def cache_path(tmp_path: Path) -> Path:
    # 功能：隔离能力数据集（不依赖用户真实 models.dev 缓存）
    path = tmp_path / "models_dev.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "somegw": {
                        "models": {
                            "mimo-v2.6-flash": _entry(
                                True,
                                [{"type": "effort", "values": ["none", "low", "high"]}],
                            ),
                            "x/shared": _entry(True),
                            "minimal-only": _entry(True, [{"type": "toggle"}]),
                        }
                    },
                    "anothergw": {"models": {"y/shared": _entry(False)}},
                }
            }
        ),
        encoding="utf-8",
    )
    models_dev._FLAT_CACHE = None
    yield path
    models_dev._FLAT_CACHE = None


class TestLookup:
    def test_exact_hit(self, cache_path: Path) -> None:
        cap = models_dev.lookup_model_capabilities("mimo-v2.6-flash", cache_path)
        assert cap is not None
        assert cap["reasoning"] is True
        assert cap["reasoning_efforts"] == ["none", "low", "high"]

    def test_tail_normalized_hit(self, cache_path: Path) -> None:
        # 网关前缀归一：xiaomi/mimo-v2.6-flash → mimo-v2.6-flash
        cap = models_dev.lookup_model_capabilities("xiaomi/mimo-v2.6-flash", cache_path)
        assert cap is not None and cap["reasoning"] is True

    def test_ambiguous_tail_returns_none(self, cache_path: Path) -> None:
        # x/shared 与 y/shared 末段同为 shared → 多义 → 放弃（宁可无挡位，不可错挡位）
        assert models_dev.lookup_model_capabilities("vendor/shared", cache_path) is None

    def test_unknown_returns_none(self, cache_path: Path) -> None:
        assert models_dev.lookup_model_capabilities("ghost-model", cache_path) is None

    def test_missing_cache_returns_none(self, tmp_path: Path) -> None:
        models_dev._FLAT_CACHE = None
        assert (
            models_dev.lookup_model_capabilities("mimo-v2.6-flash", tmp_path / "nope.json") is None
        )

    def test_toggle_only_has_no_efforts(self, cache_path: Path) -> None:
        cap = models_dev.lookup_model_capabilities("minimal-only", cache_path)
        assert cap is not None and cap["reasoning"] is True and cap["reasoning_efforts"] == []


class _FakeStore:
    # 功能：档案存储替身（隔离用户真实 llm_profiles.json）
    def __init__(self, profiles: list) -> None:
        self._profiles = profiles

    def list_profiles(self) -> list:
        return list(self._profiles)

    def active_profile(self) -> None:
        return None


def _patch_env(monkeypatch: pytest.MonkeyPatch, profiles: list, capabilities: dict | None) -> None:
    from bandi.llm.profiles import LlmProfile  # noqa: F401 —— 类型提醒

    monkeypatch.setattr(BandiConfig, "load", classmethod(lambda cls: BandiConfig()))
    monkeypatch.setattr("bandi.llm.profiles.store_singleton", lambda: _FakeStore(profiles))
    monkeypatch.setattr(
        "bandi.llm.models_dev.lookup_model_capabilities",
        lambda model, cache_path=None: capabilities,
    )


class TestSettingsModelEntry:
    @pytest.mark.asyncio
    async def test_extra_model_appended_with_capabilities(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.llm.profiles import LlmProfile

        _patch_env(
            monkeypatch,
            [
                LlmProfile(
                    profile_id="p1", model="customgw/deepseek-x", provider="customgw", api_key="sk"
                )
            ],
            {"reasoning": True, "reasoning_efforts": ["none", "low", "high"]},
        )
        from bandi.api.app import get_model

        payload = await get_model()
        entries = {item["name"]: item for item in payload["available_models"]}
        assert "customgw/deepseek-x" in entries
        item = entries["customgw/deepseek-x"]
        assert item["supports_reasoning"] is True
        assert item["reasoning_efforts"] == ["low", "high"]
        assert item["provider"] == "customgw"

    @pytest.mark.asyncio
    async def test_no_entry_when_capability_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.llm.profiles import LlmProfile

        _patch_env(
            monkeypatch,
            [
                LlmProfile(
                    profile_id="p1", model="customgw/unknown", provider="customgw", api_key="sk"
                )
            ],
            None,
        )
        from bandi.api.app import get_model

        payload = await get_model()
        names = {item["name"] for item in payload["available_models"]}
        assert "customgw/unknown" not in names

    @pytest.mark.asyncio
    async def test_catalog_model_not_duplicated_by_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 目录已有 mimo-v2.6-flash；档案写 xiaomi/mimo-v2.6-flash → 归一判存不重复补
        from bandi.llm.profiles import LlmProfile

        _patch_env(
            monkeypatch,
            [
                LlmProfile(
                    profile_id="p1",
                    model="xiaomi/mimo-v2.6-flash",
                    provider="openrouter",
                    api_key="sk",
                )
            ],
            {"reasoning": True, "reasoning_efforts": ["low"]},
        )
        from bandi.api.app import get_model

        payload = await get_model()
        names = [item["name"] for item in payload["available_models"]]
        assert "xiaomi/mimo-v2.6-flash" not in names
        assert "mimo-v2.6-flash" in names
