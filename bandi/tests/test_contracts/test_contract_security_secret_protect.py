# 功能：契约——API key 静态加密（protect/unprotect）行为与 fail-safe
from __future__ import annotations

import sys

import pytest

from bandi.core.security import secret_protect
from bandi.core.security.secret_protect import is_protected, protect, unprotect

_WIN = sys.platform == "win32"


class TestProtectedDetection:
    def test_prefix_marks_protected(self) -> None:
        assert is_protected("dpapi:QUJD") is True
        assert is_protected("sk-plain-key") is False
        assert is_protected("") is False


class TestRoundTrip:
    @pytest.mark.skipif(not _WIN, reason="DPAPI 仅在 Windows 可用")
    def test_protect_unprotect_roundtrip_ascii(self) -> None:
        secret = "sk-test-1234567890abcdef"
        protected = protect(secret)
        assert protected.startswith("dpapi:")
        assert protected != secret
        assert unprotect(protected) == secret

    @pytest.mark.skipif(not _WIN, reason="DPAPI 仅在 Windows 可用")
    def test_protect_unprotect_roundtrip_cjk_emoji(self) -> None:
        secret = "密钥-测试-🔐-Ω"
        protected = protect(secret)
        assert protected.startswith("dpapi:"), "中文/emoji 明文必须产出密文前缀（防 no-op 假绿）"
        assert protected != secret
        assert unprotect(protected) == secret


class TestZeroBehavior:
    def test_empty_text_unchanged(self) -> None:
        assert protect("") == ""
        assert unprotect("") == ""

    def test_unprotect_plain_value_returns_as_is(self) -> None:
        assert unprotect("sk-plain-key") == "sk-plain-key"

    def test_protect_is_idempotent_on_protected_value(self) -> None:
        already = "dpapi:QUJD"
        assert protect(already) == already


class TestFailSafe:
    def test_unprotect_bad_base64_keeps_original(self) -> None:
        bad = "dpapi:!!!not-base64!!!"
        assert unprotect(bad) == bad

    def test_unprotect_random_base64_keeps_original(self) -> None:
        bad = "dpapi:QUJDREVGRw=="
        assert unprotect(bad) == bad

    def test_unavailable_dpapi_returns_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(secret_protect, "_dpapi_available", lambda: False)
        assert protect("sk-plain") == "sk-plain"
        assert unprotect("dpapi:QUJD") == "dpapi:QUJD"
