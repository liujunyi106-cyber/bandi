from __future__ import annotations

from bandi.llm.adapters.anthropic import anthropic_complete, anthropic_stream
from bandi.llm.providers._http_provider import HttpAdapterProvider


class AnthropicProvider(
    HttpAdapterProvider
):  # 功能：Anthropic Claude Provider（消息序列化协议 anthropic + Anthropic adapter）
    # 适配器为模块级裸函数，须 staticmethod 挂载——否则实例访问变绑定方法，
    # self 占据适配器首参 client（位置）再叠加 client= 关键字即 TypeError
    _adapter_complete = staticmethod(anthropic_complete)
    _adapter_stream = staticmethod(anthropic_stream)
    _provider_label = "Anthropic"
    _protocol = "anthropic"
    _api_protocol = "anthropic"
