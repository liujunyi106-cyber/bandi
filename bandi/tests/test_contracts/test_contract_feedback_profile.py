# 功能：反馈模型档案契约——反思验收 LLM-as-Judge 指定档案的存储/解析/装配/API
#
# 钉行为不钉实现：
# 1. 存储：set_feedback 持久化与重载；删反馈档案清指针；不存在拒绝；空串清除
# 2. 解析：resolve_feedback_llm_config 完整覆盖 model/provider/key/base_url/extra；
# 无档案 / 无 key / 坏文件 → None（fail-open 不阻断主链路）
# 3. 装配：_resolve_judge_provider 反馈档案优先（完整 provider 覆盖）；judge_model 兜底且
# 4. API：POST .../feedback 设置 + profiles 出参带 feedback_profile_id；ghost → 404
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.core.config import BandiConfig, LLMConfig, VerifierPolicyConfig
from bandi.llm.mock import MockProvider
from bandi.llm.profiles import (
    LlmProfile,
    ProfileStore,
    reset_store_singleton,
    resolve_feedback_llm_config,
)

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture()
def _auth() -> Iterator[None]:
    # 功能：测试凭证（常量时间 Bearer——对齐 test_api.py 先例）
    from unittest.mock import patch

    with patch("bandi.api.app._API_KEY", "test"):
        yield


@pytest.fixture()
def profile_store(tmp_path: Path) -> ProfileStore:
    # 功能：隔离档案存储（tmp 文件 + 单例重置双保险）；teardown 指向不存在的
    # 空路径——既不回真实路径、也不把「含激活档案的 tmp 文件」留给后续测试
    store = reset_store_singleton(tmp_path / "llm_profiles.json")
    yield store
    reset_store_singleton(tmp_path / "empty_after_profiles.json")


def _profile(
    profile_id: str, model: str, provider: str = "deepseek", key: str = "sk-x"
) -> LlmProfile:
    return LlmProfile(profile_id=profile_id, model=model, provider=provider, api_key=key)


def _config() -> BandiConfig:
    return BandiConfig(
        llm=LLMConfig(api_key="sk-main", default_model="main-model"),
        verification=VerifierPolicyConfig(),
    )


class TestStore:
    def test_set_feedback_persists_and_reloads(
        self, profile_store: ProfileStore, tmp_path: Path
    ) -> None:
        profile_store.upsert(_profile("fb", "judge-model"))
        assert profile_store.set_feedback("fb") is True
        assert profile_store.feedback_profile_id() == "fb"
        # 落盘持久化：新实例（同文件）读回
        reloaded = ProfileStore(tmp_path / "llm_profiles.json")
        assert reloaded.feedback_profile_id() == "fb"
        profile = reloaded.feedback_profile()
        assert profile is not None and profile.model == "judge-model"

    def test_delete_feedback_profile_clears_pointer(self, profile_store: ProfileStore) -> None:
        profile_store.upsert(_profile("fb", "judge-model"))
        profile_store.set_feedback("fb")
        assert profile_store.delete("fb") is True
        assert profile_store.feedback_profile_id() == ""

    def test_unknown_profile_rejected(self, profile_store: ProfileStore) -> None:
        assert profile_store.set_feedback("ghost") is False
        assert profile_store.feedback_profile_id() == ""

    def test_empty_string_clears(self, profile_store: ProfileStore) -> None:
        profile_store.upsert(_profile("fb", "judge-model"))
        profile_store.set_feedback("fb")
        assert profile_store.set_feedback("") is True
        assert profile_store.feedback_profile_id() == ""


class TestResolve:
    def test_feedback_overrides_full_llm(self, profile_store: ProfileStore) -> None:
        profile_store.upsert(_profile("fb", "judge-model", provider="openrouter", key="sk-fb"))
        profile_store.set_feedback("fb")
        base = LLMConfig(api_key="sk-main", default_model="main-model", provider="deepseek")
        resolved = resolve_feedback_llm_config(base)
        assert resolved is not None
        assert resolved.default_model == "judge-model"
        assert resolved.provider == "openrouter"
        assert resolved.api_key == "sk-fb"

    def test_base_url_and_extra_applied(self, profile_store: ProfileStore) -> None:
        profile_store.upsert(
            LlmProfile(
                profile_id="fb",
                model="judge-model",
                provider="openrouter",
                api_key="sk-fb",
                base_url="https://gw.example/v1",
                extra={"timeout": 42},
            )
        )
        profile_store.set_feedback("fb")
        resolved = resolve_feedback_llm_config(LLMConfig(api_key="sk-main", default_model="m"))
        assert resolved is not None
        assert resolved.base_url == "https://gw.example/v1"
        assert resolved.timeout == 42

    def test_no_feedback_returns_none(self, profile_store: ProfileStore) -> None:
        assert resolve_feedback_llm_config(LLMConfig(api_key="sk", default_model="m")) is None

    def test_keyless_profile_returns_none(self, profile_store: ProfileStore) -> None:
        profile_store.upsert(_profile("fb", "judge-model", key=""))
        profile_store.set_feedback("fb")
        assert resolve_feedback_llm_config(LLMConfig(api_key="sk", default_model="m")) is None

    def test_broken_file_fails_open(self, profile_store: ProfileStore, tmp_path: Path) -> None:
        (tmp_path / "llm_profiles.json").write_text("{oops", encoding="utf-8")
        assert resolve_feedback_llm_config(LLMConfig(api_key="sk", default_model="m")) is None


class TestAssembly:
    def test_feedback_profile_wins_over_active_and_judge(
        self, profile_store: ProfileStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 反馈档案优先：装配给 provider 构造的 llm = 反馈档案完整覆盖（跨 provider）
        profile_store.upsert(_profile("active", "active-model", key="sk-active"))
        profile_store.upsert(_profile("fb", "judge-x", provider="openrouter", key="sk-fb"))
        profile_store.activate("active")
        profile_store.set_feedback("fb")
        captured: dict[str, object] = {}

        def fake_build(cfg: BandiConfig) -> object:
            captured["llm"] = cfg.llm
            return object()

        monkeypatch.setattr("bandi.agent.factory._build_provider_direct", fake_build)
        from bandi.agent.factory import _resolve_judge_provider

        judge = _resolve_judge_provider(_config(), main_provider=object())
        assert judge is not None
        llm = captured["llm"]
        assert llm.default_model == "judge-x"
        assert llm.provider == "openrouter"
        assert llm.api_key == "sk-fb"

    def test_judge_model_not_swallowed_by_active_profile(self, profile_store: ProfileStore) -> None:
        profile_store.upsert(_profile("active", "active-model", key="sk-active"))
        profile_store.activate("active")
        cfg = _config()
        cfg.verification.judge_model = "deepseek-v4-pro"
        from bandi.agent.factory import _resolve_judge_provider

        judge = _resolve_judge_provider(cfg, main_provider=object())
        assert judge is not None
        assert judge.model_name == "deepseek-v4-pro"

    def test_nothing_configured_returns_none(self, profile_store: ProfileStore) -> None:
        from bandi.agent.factory import _resolve_judge_provider

        assert _resolve_judge_provider(_config(), main_provider=object()) is None

    def test_mock_main_provider_skips_judge(self, profile_store: ProfileStore) -> None:
        profile_store.upsert(_profile("fb", "judge-x"))
        profile_store.set_feedback("fb")
        from bandi.agent.factory import _resolve_judge_provider

        assert _resolve_judge_provider(_config(), main_provider=MockProvider()) is None


class TestApi:
    def test_post_feedback_sets_and_payload_reports(
        self, profile_store: ProfileStore, _auth: None
    ) -> None:
        profile_store.upsert(_profile("fb", "judge-x"))
        resp = client.post("/api/settings/llm/profiles/fb/feedback")
        assert resp.status_code == 200
        assert resp.json()["feedback_profile_id"] == "fb"
        payload = client.get("/api/settings/llm/profiles").json()
        assert payload["feedback_profile_id"] == "fb"

    def test_post_feedback_unknown_404(self, profile_store: ProfileStore, _auth: None) -> None:
        resp = client.post("/api/settings/llm/profiles/ghost/feedback")
        assert resp.status_code == 404
