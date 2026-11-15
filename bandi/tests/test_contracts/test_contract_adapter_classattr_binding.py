# 功能：契约 — HttpAdapterProvider 子类的适配器挂载必须是静态函数
#
# 不变式：`_adapter_complete` / `_adapter_stream` 经实例访问不得是绑定方法——
# 类体裸函数赋值会把 self 绑进适配器首参 `client`（位置）再叠加 `client=` 关键字，
# complete/stream 双路径同时炸 `TypeError: got multiple values for argument 'client'`。
from __future__ import annotations

import inspect
from typing import Any

import pytest

from bandi.core.config import LLMConfig
from bandi.llm.providers.anthropic_provider import AnthropicProvider
from bandi.llm.providers.gemini import GeminiProvider

_SUBCLASSES = (AnthropicProvider, GeminiProvider)


def _provider(cls) -> Any:
    return cls(LLMConfig(api_key="sk-test", default_model="claude-sonnet-5"))


class TestAdapterMountIsStatic:
    @pytest.mark.parametrize("cls", _SUBCLASSES)
    def test_instance_access_is_not_bound_method(self, cls) -> None:
        provider = _provider(cls)
        for name in ("_adapter_complete", "_adapter_stream"):
            attr = getattr(provider, name)
            assert not inspect.ismethod(attr), (
                f"{cls.__name__}.{name} 是绑定方法：self 会占据适配器首参 client"
            )

    @pytest.mark.parametrize("cls", _SUBCLASSES)
    def test_first_param_is_client(self, cls) -> None:
        provider = _provider(cls)
        for name in ("_adapter_complete", "_adapter_stream"):
            params = list(inspect.signature(getattr(provider, name)).parameters)
            assert params and params[0] == "client", (cls.__name__, name, params[:1])

    @pytest.mark.asyncio
    async def test_registry_direct_resolution_passes_crash_point(self) -> None:
        """破坏者视角：注册表直解析 claude → complete 必须越过绑定方法崩溃点，
        结局是 HTTP 层行为（鉴权/网络等），而非 TypeError。"""
        from bandi.core.config import BandiConfig
        from bandi.llm.registry import ProviderRegistry

        cfg = BandiConfig.load()
        provider = ProviderRegistry(cfg).get_provider_for_model("claude-sonnet-5", cfg)
        try:
            await provider.complete([{"role": "user", "content": "ok"}])
        except TypeError as exc:
            pytest.fail(f"适配器绑定方法回归（self 占据 client 形参）：{exc}")
        except Exception as exc:  # noqa: BLE001 — 鉴权/网络等 HTTP 层结局 = 已越过崩溃点
            assert not isinstance(exc, (AttributeError, RecursionError)), (
                f"越过崩溃点后出现非 HTTP 层异常：{type(exc).__name__}: {exc}"
            )
