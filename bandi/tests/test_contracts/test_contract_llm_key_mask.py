"""契约：LLM 档案 API Key 出参掩码不得泄露明文任何部分。"""

from __future__ import annotations

from bandi.llm.profiles import mask_api_key


def test_mask_never_leaks_plaintext_tail() -> None:
    """破坏者视角：尾 4 位明文也算泄露，遮蔽必须不含任何可逆推片段"""
    plain = "sk-abcdefgh12345678"
    masked = mask_api_key(plain)
    assert masked != plain
    assert plain[-4:] not in masked, "掩码不得保留明文末 4 位"
    assert plain[-6:] not in masked
    assert "***" in masked, "掩码应保留易识别的星号"
    assert plain[:3] in masked, "掩码应保留前缀以便识别服务商"


def test_mask_edge_lengths() -> None:
    """短串与空值不得崩（短于 4 位的 key 也不能显示本身）"""
    assert mask_api_key("") == ""
    assert mask_api_key("   ") == ""
    short = mask_api_key("abc")
    assert "abc" not in short or short == "***", "短 key 必须全遮蔽"
    assert mask_api_key("abcd") != "abcd"
    assert mask_api_key("abcde") != "abcde"
