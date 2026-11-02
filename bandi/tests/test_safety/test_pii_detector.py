# 功能：PIIDetector 单元测试 — PII 检测/脱敏/误报过滤

from bandi.safety.pii_detector import (
    _HEX_ONLY_RE,
    PII_PATTERNS,
    PIIDetector,
)


class TestPIIDetector:
    """测试 PIIDetector 的 PII 检测和误报控制。"""

    def setup_method(self) -> None:
        self.detector = PIIDetector()

    def test_default_patterns_loaded(self) -> None:
        """验证默认模式集非空且格式正确。"""
        assert len(PII_PATTERNS) > 0
        for pii_type, pattern in PII_PATTERNS.items():
            assert isinstance(pii_type, str) and len(pii_type) > 0
            assert isinstance(pattern, str) and len(pattern) > 0

    def test_detect_phone_number(self) -> None:
        """验证检测到手机号。"""
        result = self.detector.detect("我的电话是13812345678")
        assert result.has_pii is True
        assert "phone" in result.pii_types
        assert "13812345678" in result.findings.get("phone", [])

    def test_detect_email(self) -> None:
        """验证检测到邮箱。"""
        result = self.detector.detect("联系邮箱 test@example.com")
        assert result.has_pii is True
        assert "email" in result.pii_types

    def test_detect_id_card(self) -> None:
        """验证检测到身份证号。"""
        result = self.detector.detect("身份证 110101199001011234")
        assert result.has_pii is True
        assert "id_card" in result.pii_types

    def test_id_card_not_match_in_longer_digit_run(self) -> None:
        """#3：id_card 正则须加数字边界——19 位数字串中只取 18 位子串是误报。"""
        result = self.detector.detect("编号 1234567890123456789")
        assert "id_card" not in result.pii_types

    def test_id_card_not_match_after_digit(self) -> None:
        """#3：前邻数字不得匹配（如 19 位数字中间截断）。"""
        result = self.detector.detect("x1101011990010112345")
        assert "id_card" not in result.pii_types

    def test_detect_api_key(self) -> None:
        """验证检测到 API Key。"""
        result = self.detector.detect("sk-abcdefghijklmnopqrstuvwxyz")
        assert result.has_pii is True
        assert "api_key" in result.pii_types

    def test_skip_hex_false_positive(self) -> None:
        """验证跳过纯十六进制字符串（commit hash）。"""
        # commit hash 是 40 位十六进制，应被 _HEX_ONLY_RE 匹配
        hex_str = "abc123def456abc123def456abc123def456"
        assert _HEX_ONLY_RE.match(hex_str) is not None
        # bank_card 模式应跳过
        result = self.detector.detect(f"值: {hex_str}")
        assert "bank_card" not in result.pii_types

    def test_bank_card_requires_context(self) -> None:
        """验证银行卡号需要上下文词才检测。"""
        # 长数字串但无上下文 → 不应被误报为银行卡
        result = self.detector.detect("时间戳 1234567890123456789")
        assert "bank_card" not in result.pii_types

    def test_sanitize_replaces_pii(self) -> None:
        """验证脱敏函数替换 PII 为占位符。"""
        text = "电话 13812345678 邮箱 test@example.com"
        sanitized = self.detector.sanitize(text)
        assert "13812345678" not in sanitized
        assert "test@example.com" not in sanitized
        assert "<PII:PHONE>" in sanitized
        assert "<PII:EMAIL>" in sanitized

    def test_empty_text_no_pii(self) -> None:
        """验证空文本无 PII。"""
        result = self.detector.detect("")
        assert result.has_pii is False
        assert len(result.pii_types) == 0

    def test_sanitize_preserves_non_pii(self) -> None:
        """验证脱敏保留非 PII 内容。"""
        text = "这是一段正常文本 123"
        sanitized = self.detector.sanitize(text)
        assert sanitized == text

    def test_detect_and_sanitize_share_find_matches(self) -> None:
        """#12：detect 与 sanitize 共用 _find_matches——同一文本双方命中一致。"""
        text = "手机 13812345678 邮箱 test@example.com"
        result = self.detector.detect(text)
        sanitized = self.detector.sanitize(text)
        assert "phone" in result.pii_types
        assert "email" in result.pii_types
        assert "13812345678" not in sanitized
        assert "test@example.com" not in sanitized
        assert "<PII:PHONE>" in sanitized
        assert "<PII:EMAIL>" in sanitized
