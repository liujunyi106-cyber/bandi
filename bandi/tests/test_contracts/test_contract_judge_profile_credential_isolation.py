# 功能：Judge / 反馈档案装配契约——反馈档案的端点与密钥不得被激活档案覆盖
#
# 钉行为不钉实现：
# 1. judge provider 用**反馈档案**的 base_url + 凭证，不得串成激活档案的端点
# 2. judge provider 的模型名取反馈档案的模型
# 3. 实例缓存键含端点+凭证指纹——两个档案不得复用同一实例（凭证串用/计费串号）
# 4. 激活档案为 None 时行为不变（fail-open 跟主模型）
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from bandi.agent.factory import _resolve_judge_provider
from bandi.core.config import BandiConfig
from bandi.llm.mock import MockProvider
from bandi.llm.profiles import LlmProfile, ProfileStore, reset_store_singleton

_ACTIVE_KEY = "sk-active-profile-key-aaaa"
_FEEDBACK_KEY = "sk-feedback-profile-key-bbbb"
_ACTIVE_BASE = "https://active.example.invalid/v1"
_FEEDBACK_BASE = "https://feedback.example.invalid/v1"


@pytest.fixture()
def two_profiles(tmp_path: Path) -> Iterator[ProfileStore]:
    # 功能：隔离档案存储（tmp 路径，绝不触碰用户真实 llm_profiles.json）
    #
    # 必须在收尾把单例还原为默认路径——否则本文件的 tmp 档案（含激活档案的
    # base_url）会经进程级单例泄漏给后续测试，把它们的 provider 解析改写掉。
    path = tmp_path / "llm_profiles.json"
    store = ProfileStore(path)
    store.upsert(
        LlmProfile(
            profile_id="active-x",
            name="active",
            provider="openai_compat",
            model="active-model",
            api_key=_ACTIVE_KEY,
            base_url=_ACTIVE_BASE,
        )
    )
    store.upsert(
        LlmProfile(
            profile_id="feedback-y",
            name="feedback",
            provider="openai_compat",
            model="feedback-model",
            api_key=_FEEDBACK_KEY,
            base_url=_FEEDBACK_BASE,
        )
    )
    store.activate("active-x")
    store.set_feedback("feedback-y")
    reset_store_singleton(path)
    try:
        yield store
    finally:
        reset_store_singleton()


def _endpoint_and_key(provider: object) -> tuple[str, str]:
    # 功能：取 provider 实例实际使用的端点与凭证（构造入参层面，非源码断言）
    return str(getattr(provider, "_base_url", "")), str(getattr(provider, "auth_token", ""))


class TestJudgeProfileNotOverwritten:
    def test_judge_uses_feedback_endpoint(self, two_profiles: ProfileStore) -> None:
        # 功能：judge provider 的端点必须是**反馈档案**的端点
        judge = _resolve_judge_provider(BandiConfig(), _FakeMain())
        assert judge is not None, "已设反馈档案却未构建 judge"
        base_url, api_key = _endpoint_and_key(judge)
        assert base_url == _FEEDBACK_BASE, f"judge 端点被激活档案串改：{base_url}"
        assert api_key == _FEEDBACK_KEY, "judge 凭证被激活档案串改"

    def test_judge_model_is_feedback_model(self, two_profiles: ProfileStore) -> None:
        # 功能：judge 的模型名取反馈档案（不得回落激活档案模型）
        judge = _resolve_judge_provider(BandiConfig(), _FakeMain())
        assert judge is not None
        assert judge.model_name == "feedback-model"

    def test_mock_main_provider_builds_no_judge(self, two_profiles: ProfileStore) -> None:
        # 功能：主模型为 Mock 时不建 judge（测试链路零外部调用，既有语义）
        main = MockProvider()
        assert _resolve_judge_provider(BandiConfig(), main) is None


class TestInstanceCacheNotSharingCredentials:
    def test_two_profiles_do_not_share_instance(self, two_profiles: ProfileStore) -> None:
        # 功能：不同档案（不同端点+凭证）不得复用同一 provider 实例
        from bandi.llm.registry import ProviderRegistry

        reg = ProviderRegistry(BandiConfig())
        main = reg.get_provider("feedback-model", BandiConfig(llm=_llm(_ACTIVE_BASE, _ACTIVE_KEY)))
        judge = _resolve_judge_provider(BandiConfig(), _FakeMain())
        assert judge is not None
        assert main is not judge, "不同档案复用了同一 provider 实例（凭证串用）"
        assert _endpoint_and_key(main) == (_ACTIVE_BASE, _ACTIVE_KEY)


class TestNoFeedbackProfileFallsBack:
    def test_returns_none_without_feedback_profile(self, tmp_path: Path) -> None:
        # 功能：未设反馈档案且无 judge_model → None（跟随主模型，零回归）
        path = tmp_path / "llm_profiles.json"
        reset_store_singleton(path)
        try:
            ProfileStore(path).upsert(
                LlmProfile(
                    profile_id="only-active",
                    provider="openai_compat",
                    model="m",
                    api_key=_ACTIVE_KEY,
                    base_url=_ACTIVE_BASE,
                )
            )
            reset_store_singleton(path)
            judge = _resolve_judge_provider(BandiConfig(), _FakeMain())
            assert judge is None
        finally:
            reset_store_singleton()


def _llm(base_url: str, api_key: str):  # type: ignore[no-untyped-def]
    from bandi.core.config import LLMConfig

    return LLMConfig(
        provider="openai_compat", default_model="m", base_url=base_url, api_key=api_key
    )


class _FakeMain:
    """非 Mock 的主 provider 占位（judge 路径只看 isinstance MockProvider）"""

    model_name = "main-model"

    def complete(self, *a: object, **k: object) -> object:  # pragma: no cover - 占位
        raise NotImplementedError
