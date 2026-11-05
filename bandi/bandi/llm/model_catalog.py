# 功能：模型目录（定价/规格/上下文长度查询）
from dataclasses import dataclass, field

USD_TO_CNY: float = 7.2


@dataclass
class ModelPricing:  # 功能：模型定价（输入/输出/缓存读写单价 + 币种）
    input_per_1m: float
    output_per_1m: float
    currency: str = "CNY"
    # （等价「无折扣」，绝不臆造折扣）。Anthropic 官方比例 read 0.1x / write 1.；
    # OpenAI 系 read 约 0.1x；DeepSeek read 约 0.——**真实值由 models.dev 的
    # `cost.cache_read` / `cost.cache_write` 填充**（见 models_dev.merge_remote_specs）
    # 避免厂商调价时改代码。
    cache_read_per_1m: float = 0.0
    cache_write_per_1m: float = 0.0


@dataclass
class ModelSpec:  # 功能：模型规格元数据（上下文窗口/能力标记/定价）
    name: str
    provider: str
    context_window: int
    max_output_tokens: int = 4096
    supports_tool_calling: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True
    # models.dev 自动同步接入字段：推理能力开关 + 可调 effort 档位表（如 low/high/max）
    supports_reasoning: bool = False
    reasoning_efforts: list[str] = field(default_factory=list)
    # -C 组：该模型能否**关闭**思考（False = 强制思考，前端不渲染「关闭」档；
    # 空档时适配层下发该 provider 的最低档而非「什么都不发」——
    # 后者在 Kimi/GLM-5.3 语义上等于默认最强，与用户选的「关闭」相反）
    supports_thinking_off: bool = True
    pricing: ModelPricing = field(default_factory=lambda: ModelPricing(0, 0))


# **硬原则：目录列出的挡位必须是适配层真能发出的值**——否则滑杆会给出打挂请求或
# 静默无效的档位（契约 test_builtin_reasoning_efforts_offline_fallback 钉死）。
#
# 已接线（可列挡位）：
# DeepSeek /.1（官方 change log）：reasoning_effort = low/high/max（medium/xhigh 是兼容
# 映射值，直传与 high 同档 ⇒ 不列）；thinking 默认**关闭**（opt-in）。
# OpenAI GPT-5.x/GPT-6（官方 Help Center + models.dev）：reasoning.effort =
# none/low/medium/high/xhigh/max；GPT-5.6 三件套全六档、gpt-6-astra 五档（low..max）
# GPT-5.2 五档、gpt-5.2-pro 三档；**默认 medium（思考开）**，故「关闭」由适配层显式发 none 落实。
#
# 未接线（一律不列挡位——滑杆显示「该模型不支持」，由适配层改造后再开）：
# Anthropic：适配层走已废弃的 manual thinking（type=enabled + budget_tokens）
# Claude 4.7+ / 5 系会 400；现行机制为 adaptive thinking + output_config.effort。
# Gemini：适配层走已废弃的整数 thinkingBudget；Gemini 3.x 已改为 thinking_level。
# thinking 字段（官方为 always-on reasoning + low/high/max，待专属适配）。
_BUILTIN_REASONING_EFFORTS: dict[str, list[str]] = {
    # DeepSeek（low/high/max；默认关闭）
    "deepseek-flash": ["low", "high", "max"],
    "deepseek-v4.1-flash": ["low", "high", "max"],
    "deepseek-v4-flash": ["low", "high", "max"],
    "deepseek-v4-pro": ["low", "high", "max"],
    # OpenAI（GPT-6 五档 / 5.6 三件套六档 / 5.2 五档 / pro 三档 / 较早推理三档；none = 关闭）
    "gpt-6-astra": ["low", "medium", "high", "xhigh", "max"],
    "gpt-5.6-terra": ["none", "low", "medium", "high", "xhigh", "max"],
    "gpt-5.6-sol": ["none", "low", "medium", "high", "xhigh", "max"],
    "gpt-5.6-luna": ["none", "low", "medium", "high", "xhigh", "max"],
    "gpt-5.2": ["none", "low", "medium", "high", "xhigh"],
    "gpt-5.2-pro": ["medium", "high", "xhigh"],
    "gpt-5.1": ["low", "medium", "high"],
    "gpt-5-mini": ["low", "medium", "high"],
    "gpt-5-nano": ["low", "medium", "high"],
    "o3": ["low", "medium", "high"],
    # Anthropic：4.7+/Opus 5/Sonnet 5/Fable/Mythos 系仅 adaptive（enabled 会 400）
    # 挡位走 output_config.effort（low/medium/high/max）；4.6 系 enabled 已弃用但仍可用
    "claude-opus-5": ["low", "medium", "high", "max"],
    "claude-sonnet-5": ["low", "medium", "high", "max"],
    "claude-opus-4-8": ["low", "medium", "high", "max"],
    "claude-opus-4-7": ["low", "medium", "high", "max"],
    "claude-opus-4-6": ["low", "medium", "high"],
    "claude-sonnet-4-6": ["low", "medium", "high"],
    "claude-haiku-4-5": ["low", "medium", "high"],
    # Gemini 3.x：thinkingLevel 枚举（原整数 thinkingBudget 已弃用；官方「不再建议」）
    # 3.x 不能真正关闭（minimal 为最近似「不思考」，见适配层空档映射）
    "gemini-3.8-flash": ["low", "medium", "high"],
    "gemini-3.6-flash": ["low", "medium", "high"],
    "gemini-3.5-flash": ["low", "medium", "high"],
    "gemini-3.5-flash-lite": ["low", "medium", "high"],
    "kimi-k3": ["low", "high", "max"],
    # GLM-5.3 系：thinking.type 强制 enabled + reasoning_effort（low/high/max）
    "glm-5.3": ["low", "high", "max"],
    "glm-5.3-flash": ["low", "high", "max"],
}

# -C 组：**无法关闭思考**的模型（挡位只有「最低」没有「关闭」；适配层空档下发最低档）
_BUILTIN_THINKING_OFF_UNSUPPORTED: set[str] = {
    "kimi-k3",  # K3 始终推理（传 disabled 报错）
    "glm-5.3",  # 5.3 系强制思考（传 disabled 请求失败）
    "glm-5.3-flash",
}

BUILTIN_MODELS: dict[str, ModelSpec] = {
    # off-peak $0.15/$0.6）；deepseek-v4-pro off-peak $0.435/$0.87（open_weights）。
    # v4-flash 条目保留仅为兼容历史配置（其本地价为退役期旧值，勿用于新配置）。
    "deepseek-flash": ModelSpec(
        name="deepseek-flash",
        provider="deepseek",
        context_window=1000000,
        max_output_tokens=393216,
        supports_vision=True,
        pricing=ModelPricing(round(0.15 * USD_TO_CNY, 4), round(0.6 * USD_TO_CNY, 4), "CNY"),
    ),
    # 「Change the model name to deepseek-flash」）。
    # ollama-cloud、digitalocean、llmgateway、requesty、vancine、vercel 等 **20+ 家**
    # ⇒ 收录为**别名而非改名**：两种端点都能解析出定价与上级窗口；否则网关用户会落到
    "deepseek-v4.1-flash": ModelSpec(
        name="deepseek-v4.1-flash",
        provider="deepseek",
        context_window=1000000,
        max_output_tokens=393216,
        supports_vision=True,
        pricing=ModelPricing(round(0.15 * USD_TO_CNY, 4), round(0.6 * USD_TO_CNY, 4), "CNY"),
    ),
    "deepseek-v4-flash": ModelSpec(
        name="deepseek-v4-flash",
        provider="deepseek",
        context_window=1000000,
        max_output_tokens=384000,
        pricing=ModelPricing(1.584, 4.752),
    ),
    "deepseek-v4-pro": ModelSpec(
        name="deepseek-v4-pro",
        provider="deepseek",
        context_window=1000000,
        max_output_tokens=384000,
        pricing=ModelPricing(4.752, 14.256),
    ),
    # o4-mini、o1 等已弃用 ⇒ 删。
    "gpt-6-astra": ModelSpec(
        name="gpt-6-astra",
        provider="openai",
        context_window=1050000,
        max_output_tokens=128000,
        pricing=ModelPricing(round(10.0 * USD_TO_CNY, 4), round(50.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "gpt-5.6-terra": ModelSpec(
        name="gpt-5.6-terra",
        provider="openai",
        context_window=1050000,
        max_output_tokens=128000,
        pricing=ModelPricing(round(2.0 * USD_TO_CNY, 4), round(12.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "gpt-5.6-sol": ModelSpec(
        name="gpt-5.6-sol",
        provider="openai",
        context_window=1050000,
        max_output_tokens=128000,
        pricing=ModelPricing(round(4.0 * USD_TO_CNY, 4), round(20.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "gpt-5.6-luna": ModelSpec(
        name="gpt-5.6-luna",
        provider="openai",
        context_window=1050000,
        max_output_tokens=128000,
        pricing=ModelPricing(round(0.2 * USD_TO_CNY, 4), round(1.2 * USD_TO_CNY, 4), "CNY"),
    ),
    "gpt-5.2": ModelSpec(
        name="gpt-5.2",
        provider="openai",
        context_window=400000,
        max_output_tokens=128000,
        pricing=ModelPricing(round(1.75 * USD_TO_CNY, 4), round(14.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "gpt-5.2-pro": ModelSpec(
        name="gpt-5.2-pro",
        provider="openai",
        context_window=400000,
        max_output_tokens=128000,
        pricing=ModelPricing(round(1.75 * USD_TO_CNY, 4), round(21.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "gpt-5.1": ModelSpec(
        name="gpt-5.1",
        provider="openai",
        context_window=400000,
        max_output_tokens=128000,
        pricing=ModelPricing(round(1.25 * USD_TO_CNY, 4), round(10.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "gpt-5-mini": ModelSpec(
        name="gpt-5-mini",
        provider="openai",
        context_window=400000,
        max_output_tokens=128000,
        pricing=ModelPricing(round(0.25 * USD_TO_CNY, 4), round(2.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "gpt-5-nano": ModelSpec(
        name="gpt-5-nano",
        provider="openai",
        context_window=400000,
        max_output_tokens=128000,
        pricing=ModelPricing(round(0.05 * USD_TO_CNY, 4), round(0.4 * USD_TO_CNY, 4), "CNY"),
    ),
    "gpt-4.1": ModelSpec(
        name="gpt-4.1",
        provider="openai",
        context_window=1000000,
        max_output_tokens=32768,
        pricing=ModelPricing(round(2.0 * USD_TO_CNY, 4), round(8.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "o3": ModelSpec(
        name="o3",
        provider="openai",
        context_window=200000,
        max_output_tokens=100000,
        pricing=ModelPricing(round(2.0 * USD_TO_CNY, 4), round(8.0 * USD_TO_CNY, 4), "CNY"),
    ),
    # 4.5 及更早（budget_tokens 机制）已属旧线 ⇒ 不收录；haiku id 官方为连字符 4-5。
    "claude-opus-5": ModelSpec(
        name="claude-opus-5",
        provider="anthropic",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(5.0 * USD_TO_CNY, 4), round(25.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "claude-sonnet-5": ModelSpec(
        name="claude-sonnet-5",
        provider="anthropic",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(2.0 * USD_TO_CNY, 4), round(10.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "claude-opus-4-8": ModelSpec(
        name="claude-opus-4-8",
        provider="anthropic",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(5.0 * USD_TO_CNY, 4), round(25.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "claude-opus-4-7": ModelSpec(
        name="claude-opus-4-7",
        provider="anthropic",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(5.0 * USD_TO_CNY, 4), round(25.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "claude-opus-4-6": ModelSpec(
        name="claude-opus-4-6",
        provider="anthropic",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(5.0 * USD_TO_CNY, 4), round(25.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "claude-sonnet-4-6": ModelSpec(
        name="claude-sonnet-4-6",
        provider="anthropic",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(2.0 * USD_TO_CNY, 4), round(10.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "claude-haiku-4-5": ModelSpec(
        name="claude-haiku-4-5",
        provider="anthropic",
        context_window=200000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(1.0 * USD_TO_CNY, 4), round(5.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "gemini-3.8-flash": ModelSpec(
        name="gemini-3.8-flash",
        provider="gemini",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(1.5 * USD_TO_CNY, 4), round(7.5 * USD_TO_CNY, 4), "CNY"),
    ),
    "gemini-3.6-flash": ModelSpec(
        name="gemini-3.6-flash",
        provider="gemini",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(1.5 * USD_TO_CNY, 4), round(7.5 * USD_TO_CNY, 4), "CNY"),
    ),
    "gemini-3.5-flash": ModelSpec(
        name="gemini-3.5-flash",
        provider="gemini",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(1.5 * USD_TO_CNY, 4), round(9.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "gemini-3.5-flash-lite": ModelSpec(
        name="gemini-3.5-flash-lite",
        provider="gemini",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(0.3 * USD_TO_CNY, 4), round(2.5 * USD_TO_CNY, 4), "CNY"),
    ),
    "gemini-3.1-pro-preview": ModelSpec(
        name="gemini-3.1-pro-preview",
        provider="gemini",
        context_window=1000000,
        max_output_tokens=64000,
        pricing=ModelPricing(round(2.0 * USD_TO_CNY, 4), round(12.0 * USD_TO_CNY, 4), "CNY"),
    ),
    # 独立 provider（OpenAI 兼容 api.moonshot.cn/v1 + Anthropic 兼容端点）；
    "kimi-k3": ModelSpec(
        name="kimi-k3",
        provider="moonshot",
        context_window=1000000,
        max_output_tokens=131000,
        pricing=ModelPricing(round(3.0 * USD_TO_CNY, 4), round(15.0 * USD_TO_CNY, 4), "CNY"),
    ),
    "kimi-k2.7-code": ModelSpec(
        name="kimi-k2.7-code",
        provider="moonshot",
        context_window=262144,
        max_output_tokens=16384,
        pricing=ModelPricing(round(0.71 * USD_TO_CNY, 4), round(3.5 * USD_TO_CNY, 4), "CNY"),
    ),
    # 独立 provider（Z.ai API 同说 OpenAI 与 Anthropic 协议）；GLM-5.3 旗舰
    # 档案自定义 base_url。glm-5/glm-5.1 已被 5.2/5.3 取代 ⇒ 删。
    "glm-5.3": ModelSpec(
        name="glm-5.3",
        provider="zhipu",
        context_window=1048576,
        max_output_tokens=128000,
        pricing=ModelPricing(round(1.4 * USD_TO_CNY, 4), round(4.4 * USD_TO_CNY, 4), "CNY"),
    ),
    "glm-5.3-flash": ModelSpec(
        name="glm-5.3-flash",
        provider="zhipu",
        context_window=1048576,
        max_output_tokens=128000,
        pricing=ModelPricing(round(1.4 * USD_TO_CNY, 4), round(4.4 * USD_TO_CNY, 4), "CNY"),
    ),
    "glm-5.2": ModelSpec(
        name="glm-5.2",
        provider="zhipu",
        context_window=1048576,
        max_output_tokens=128000,
        pricing=ModelPricing(round(1.4 * USD_TO_CNY, 4), round(4.4 * USD_TO_CNY, 4), "CNY"),
    ),
    # 独立 provider（OpenAI 兼容端点，base_url 由档案/配置给出）；= 多模态长
    # 上下文旗舰（1M，文本/图像/视频输入）、.7 = 编码/办公旗舰（204K）。
    "MiniMax-M3": ModelSpec(
        name="MiniMax-M3",
        provider="minimax",
        context_window=1000000,
        max_output_tokens=512000,
        supports_vision=True,
        pricing=ModelPricing(round(0.3 * USD_TO_CNY, 4), round(1.2 * USD_TO_CNY, 4), "CNY"),
    ),
    "MiniMax-M2.7": ModelSpec(
        name="MiniMax-M2.7",
        provider="minimax",
        context_window=204800,
        max_output_tokens=131072,
        pricing=ModelPricing(round(0.3 * USD_TO_CNY, 4), round(1.2 * USD_TO_CNY, 4), "CNY"),
    ),
    # agnes hub 聚合模型（OpenAI 兼容端点 apihub.agnes-ai.com/v1；
    # 上下文窗口约 500K）。
    "agnes-2.5-flash": ModelSpec(
        name="agnes-2.5-flash",
        provider="agnes",
        context_window=500000,
        max_output_tokens=65536,
    ),
    # 多模态（文本/图像/音频/视频输入）旗舰与轻量档。
    "mimo-v2.6-pro": ModelSpec(
        name="mimo-v2.6-pro",
        provider="mimo",
        context_window=1048576,
        max_output_tokens=131072,
        supports_vision=True,
        pricing=ModelPricing(round(0.435 * USD_TO_CNY, 4), round(0.87 * USD_TO_CNY, 4), "CNY"),
    ),
    "mimo-v2.6-flash": ModelSpec(
        name="mimo-v2.6-flash",
        provider="mimo",
        context_window=1048576,
        max_output_tokens=131072,
        supports_vision=True,
        pricing=ModelPricing(round(0.14 * USD_TO_CNY, 4), round(0.28 * USD_TO_CNY, 4), "CNY"),
    ),
    "space-bunny-free": ModelSpec(
        name="space-bunny-free",
        provider="opencode",
        # 实测下限：374188 prompt_tokens 请求成功（2026-10-09）。800K 探针服务端
        # 直接断连且未给出上下文超限错误，原因不明 ⇒ 保守登记实测下限而非臆造上限。
        context_window=400000,
        max_output_tokens=32768,
        # 服务端返回 reasoning_content（实测），但思考开关不可控 ⇒ 不列 effort 档位，
        # 前端据 supports_reasoning=False 隐藏「思考」挡位。
        supports_reasoning=False,
        # 端点零费用（免费档实测可用），故定价为 0——不计入成本台账，避免污染评测成本口径。
        pricing=ModelPricing(0, 0),
    ),
    "mock": ModelSpec(
        name="mock",
        provider="mock",
        context_window=1000000,
        max_output_tokens=4096,
        pricing=ModelPricing(0, 0),
    ),
}

# 功能：把离线兜底挡位落到对应模型条目（models.dev 同步只在拿到真实字段时覆写）
for _model_name, _efforts in _BUILTIN_REASONING_EFFORTS.items():
    _spec = BUILTIN_MODELS.get(_model_name)
    if _spec is not None:
        _spec.supports_reasoning = True
        _spec.reasoning_efforts = list(_efforts)

for _model_name in _BUILTIN_THINKING_OFF_UNSUPPORTED:
    _spec = BUILTIN_MODELS.get(_model_name)
    if _spec is not None:
        _spec.supports_thinking_off = False


class ModelCatalog:  # 功能：模型目录（管理内置+自定义模型规格，按模型/提供方查询/降级链）
    def __init__(
        self, models: dict[str, ModelSpec] | None = None, fallback_chain: list[str] | None = None
    ):
        self._models = dict(BUILTIN_MODELS)
        if models:
            self._models.update(models)
        # 降级链首选现役主名 deepseek-flash（= .1-Flash；deepseek-chat 已于
        # 链首与默认模型同名；第二项保留官方现役名作**双命名端点兜底**（见 BUILTIN_MODELS 注）
        self._fallback_chain = fallback_chain or [
            "deepseek-v4.1-flash",
            "deepseek-flash",
            "gpt-5.2",
        ]

    def get_fallback_chain(self) -> list[str]:  # 功能：返回降级模型链
        return list(self._fallback_chain)

    def get(self, name: str) -> ModelSpec | None:
        # 功能：按模型名查询规格（**含网关命名空间归一**）
        # （如 openrouter 的 xiaomi/mimo-v2.6-flash，其厂商自有 id 就是 mimo-v2.6-flash
        # ——见 OpenRouter 文档「On OpenRouter they carry the xiaomi/ prefix」）。
        # 精确未命中时取最后一段路径再查一次；否则带前缀的名字永远查不到，后果是
        spec = self._models.get(name)
        if spec is not None:
            return spec
        if "/" in name:
            return self._models.get(name.rsplit("/", 1)[-1])
        return None

    def list_models(
        self, provider: str | None = None
    ) -> list[ModelSpec]:  # 功能：列出所有模型规格，可选按提供方过滤
        if provider:
            return [m for m in self._models.values() if m.provider == provider]
        return list(self._models.values())

    def register(self, spec: ModelSpec) -> None:  # 功能：注册自定义模型规格到目录
        self._models[spec.name] = spec

    def __contains__(self, name: str) -> bool:
        return name in self._models
