# 功能：Provider 注册表（自动注册/按名获取/关闭清理）
from __future__ import annotations

import hashlib
import os

from bandi.core.config import BandiConfig, LLMConfig
from bandi.core.exceptions import LLMUnavailableError
from bandi.llm.base import BaseProvider

# 功能：OpenAI 兼容家族的默认端点——跨 provider 链候选不继承档案端点时的回退
_COMPAT_DEFAULT_BASE_URLS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "opencode": "https://opencode.ai/zen/v1",
    "opencode-go": "https://opencode.ai/inference/go/openai/v1",
}


class ProviderRegistry:  # 功能：Provider 注册与工厂，模型/供应商→Provider 映射 + 按需实例化
    def __init__(self, config: BandiConfig | None = None):
        self._providers: dict[str, type[BaseProvider]] = {}
        self._instances: dict[str, BaseProvider] = {}
        self._model_to_provider: dict[str, str] = {}
        if config is not None:
            self._auto_register(self._resolved(config))

    @staticmethod
    def _resolved(config: BandiConfig) -> BandiConfig:
        # 功能：套用 Provider 档案覆盖
        from bandi.llm.profiles import with_resolved_llm

        return with_resolved_llm(config)

    def _auto_register(self, config: BandiConfig) -> None:
        from bandi.llm.model_catalog import BUILTIN_MODELS
        from bandi.llm.providers.anthropic_provider import AnthropicProvider
        from bandi.llm.providers.deepseek import DeepSeekProvider
        from bandi.llm.providers.gemini import GeminiProvider
        from bandi.llm.providers.moonshot import MoonshotProvider
        from bandi.llm.providers.openai import OpenAICompatProvider, OpenAIProvider
        from bandi.llm.providers.opencode_go import OpenCodeGoProvider
        from bandi.llm.providers.zhipu import ZhipuProvider

        self.register("openai", OpenAIProvider)
        self.register("deepseek", DeepSeekProvider)
        self.register("openai_compat", OpenAICompatProvider)
        self.register("anthropic", AnthropicProvider)
        self.register("gemini", GeminiProvider)
        # 均为 OpenAI 兼容协议，base_url 由档案/配置提供（moonshot=api.moonshot.cn/v1
        # zhipu=Z.ai API——两者亦提供 Anthropic 协议端点与 Coding Plan 订阅通道）
        self.register("moonshot", MoonshotProvider)
        self.register("zhipu", ZhipuProvider)
        # Ollama（本地/云端）同为 OpenAI 兼容协议，base_url 由档案/配置给出；
        # 思考字段暂不发（兼容基类钩子返回 {}，专属适配接线后再列挡位）
        self.register("minimax", OpenAICompatProvider)
        self.register("mimo", OpenAICompatProvider)
        self.register("ollama", OpenAICompatProvider)
        # opencode zen（opencode.ai/zen/v1）同为 OpenAI 兼容协议；免费档零定价
        self.register("opencode", OpenAICompatProvider)
        # OpenCode Go（opencode.ai/inference/go/openai/v1）——网关强制会话头，
        # 思考挡位经顶层 reasoning_effort（专属适配：x-opencode-session 自动注入）
        self.register("opencode-go", OpenCodeGoProvider)

        # 单一来源——从 ModelCatalog 自动构建模型→provider 映射
        for model_name, spec in BUILTIN_MODELS.items():
            if spec.provider != "mock":
                self.register_model(model_name, spec.provider)

        if config.llm and config.llm.base_url:
            model = config.llm.default_model or "default"
            if model not in self._model_to_provider:
                self.register_model(model, "openai_compat")

    def register(self, name: str, provider_cls: type[BaseProvider]) -> None:
        self._providers[name] = provider_cls

    def register_model(self, model: str, provider_name: str) -> None:
        self._model_to_provider[model] = provider_name

    def _resolve_model_provider(self, model: str) -> str | None:
        # 功能：模型→provider 归属解析——精确优先；网关命名空间（vendor/name）
        # 取末段再查（`xiaomi/mimo-v2.6-flash` → 目录 `mimo-v2.6-flash`）
        hit = self._model_to_provider.get(model)
        if hit:
            return hit
        if "/" in model:
            return self._model_to_provider.get(model.rsplit("/", 1)[-1])
        return None

    def _candidate_endpoint_and_key(
        self, provider_name: str, config: BandiConfig
    ) -> tuple[str, str]:
        # 功能：链候选的端点/凭证解析——归属 provider == 激活档案 provider 时
        # 沿用档案端点/凭证；跨 provider 候选不继承（防「候选被劫持到错误协议
        # 端点」——端点锁定的真实机制），改经 per-provider 环境变量
        # （`{PROVIDER}_BASE_URL` / `{PROVIDER}_API_KEY`）与默认端点回退
        profile_provider = (config.llm.provider or "").strip()
        if not profile_provider or provider_name == profile_provider:
            return config.llm.base_url, config.llm.api_key
        env_base = os.environ.get(f"{provider_name.upper()}_BASE_URL", "").strip()
        env_key = os.environ.get(f"{provider_name.upper()}_API_KEY", "").strip()
        base = env_base or _COMPAT_DEFAULT_BASE_URLS.get(provider_name, "")
        return base, env_key

    def get_provider(
        self, model: str, config: BandiConfig, *, resolve_profile: bool = True
    ) -> BaseProvider:
        # 显式 llm.provider 优先（用户显式指定语义，热切换不返回旧实例）；
        # 未显式时按模型目录归属解析
        #
        # resolve_profile=False：**已由调用方解析完档案的直连路径**（judge/反馈档案）
        # 专用——再套一次激活档案会把反馈档案的端点与凭证反向覆盖，只剩模型名。
        if resolve_profile:
            config = self._resolved(config)
        provider_name = (config.llm.provider or "").strip() or self._resolve_model_provider(model)
        if not provider_name:
            if config.llm and config.llm.base_url:
                provider_name = "openai_compat"
            else:
                raise LLMUnavailableError(f"No provider for model '{model}' and no base_url set")
        return self._get_or_create(model, provider_name, config, resolve_profile=resolve_profile)

    def get_provider_for_model(self, model: str, config: BandiConfig) -> BaseProvider:
        """功能：按**模型目录归属**解析 provider（fallback 链消费专用）——显式
        llm.provider 不参与：链项是模型名，跨厂商链项不得被劫持到错误协议端点
        （deepseek 端点 + gpt 模型名必 400）。无目录归属时与 get_provider 同兜底。"""
        config = self._resolved(config)
        provider_name = self._resolve_model_provider(model)
        if not provider_name:
            provider_name = (config.llm.provider or "").strip()
            if not provider_name:
                if config.llm and config.llm.base_url:
                    provider_name = "openai_compat"
                else:
                    raise LLMUnavailableError(
                        f"No provider for model '{model}' and no base_url set"
                    )
        return self._get_or_create(model, provider_name, config)

    def _get_or_create(
        self,
        model: str,
        provider_name: str,
        config: BandiConfig,
        *,
        resolve_profile: bool = True,
    ) -> BaseProvider:
        # 功能：实例缓存/构造（缓存键含 provider + base_url + 凭证指纹）
        # 唯一解析入口：档案覆盖后的凭证/地址进缓存指纹——不同档案不得复用同一实例
        if resolve_profile:
            config = self._resolved(config)

        # 缓存键含 provider 名 + 凭证指纹（api_key 不进键明文）：同模型同网关
        # 不同凭证（多员工自带 key）不得复用同一实例（凭证串用/计费串号）；
        # 思考档位进键——档位不同的实例行为不同，复用会让覆盖静默失效
        base_url, api_key = self._candidate_endpoint_and_key(provider_name, config)
        api_fp = hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:12]
        thinking = str(getattr(config.llm, "thinking_mode", "") or "")
        cache_key = f"{model}:{provider_name}:{base_url}:{api_fp}:{thinking}"
        if cache_key in self._instances:
            return self._instances[cache_key]

        provider_cls = self._providers.get(provider_name)
        if not provider_cls:
            # 自定义网关（前端「自定义…」档案语义：任意标识 + base_url）⇒ 按 OpenAI
            if base_url:
                from bandi.llm.providers.openai import OpenAICompatProvider

                provider_cls = OpenAICompatProvider
            else:
                raise LLMUnavailableError(f"Provider '{provider_name}' not registered")

        llm_config = LLMConfig(
            default_model=model,
            provider=provider_name,
            api_key=api_key,
            base_url=base_url,
            timeout=config.llm.timeout,
            max_output_tokens=config.llm.max_output_tokens,
            thinking_mode=config.llm.thinking_mode,
            budget_limit=config.llm.budget_limit,
            fallback_chain=config.llm.fallback_chain,
            extra_headers=getattr(config.llm, "extra_headers", None) or {},
        )
        if provider_name in ("openai_compat",):
            instance = provider_cls(llm_config, base_url)
        else:
            instance = provider_cls(llm_config)
        self._instances[cache_key] = instance
        return instance

    def available_models(self) -> list[str]:
        return list(self._model_to_provider.keys())


def validate_model_provider(model: str, config: BandiConfig | None = None) -> str:
    """功能：模型→provider 预检（换大脑）——通过返回空串，否则返回错误说明。

    判定与 ProviderRegistry 解析同源：模型在目录（BUILTIN_MODELS 非 mock）→ 通过；
    未注册但配置了 base_url → openai_compat 回退通道，通过；否则 fail-closed。
    """
    if not model:
        return ""
    if config is None:
        config = BandiConfig.load()
    registry = ProviderRegistry(config)
    if model in registry._model_to_provider:
        return ""
    if config.llm and config.llm.base_url:
        return ""
    return (
        f"model '{model}' 未在模型目录注册且未配置 base_url，"
        "无法解析 provider（EX-3 预检 fail-closed）"
    )
