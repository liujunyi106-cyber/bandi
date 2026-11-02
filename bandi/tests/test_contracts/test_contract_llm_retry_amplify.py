# 功能：契约 — LLM 层瞬时故障退避放大（网关抖动 30s 窗口内自愈，用户无感、不弹重试卡片）
from bandi.agent.factory import build_provider
from bandi.core.config import BandiConfig


def _real_provider():
    cfg = BandiConfig()
    cfg.llm.api_key = "sk-test"
    cfg.llm.default_model = "deepseek-v4-flash"
    return build_provider(cfg)


class TestLLMRetryAmplification:
    def test_retry_policy_amplified(self) -> None:
        provider = _real_provider()
        rp = provider._retry_policy
        assert rp is not None, "生产 provider 必须绑定重试策略（网络抖动不得直接抛错）"
        # 放大：指数退避 2/4/8/16 ≈ 30s 窗口——瞬时网关波动自愈，用户无感
        assert rp._max_retries >= 4
        assert rp._base_delay >= 2.0

    def test_mock_provider_path_unaffected(self) -> None:
        from bandi.llm.mock import MockProvider

        cfg = BandiConfig()
        cfg.llm.api_key = ""
        assert isinstance(build_provider(cfg), MockProvider)
