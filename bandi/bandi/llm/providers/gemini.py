from __future__ import annotations

from bandi.llm.adapters.google import google_complete, google_stream
from bandi.llm.providers._http_provider import HttpAdapterProvider


class GeminiProvider(
    HttpAdapterProvider
):  # 功能：Google Gemini Provider（消息序列化协议 openai + Gemini adapter）
    # 适配器为模块级裸函数，须 staticmethod 挂载——否则实例访问变绑定方法，
    # self 占据适配器首参 client（位置）再叠加 client= 关键字即 TypeError
    _adapter_complete = staticmethod(google_complete)
    _adapter_stream = staticmethod(google_stream)
    _provider_label = "Gemini"
    _api_protocol = "google"
