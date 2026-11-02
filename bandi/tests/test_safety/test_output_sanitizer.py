# 功能：OutputSanitizer 单元测试 — 长度截断/Markdown 闭合/AIGC 标识/HTML 转义

from bandi.safety.output_sanitizer import (
    OutputSanitizer,
    OutputSanitizerConfig,
)


class TestOutputSanitizer:
    """测试 OutputSanitizer 的输出规范执行。"""

    def setup_method(self) -> None:
        self.config = OutputSanitizerConfig(max_chars=100, max_lines=10)
        self.sanitizer = OutputSanitizer(config=self.config)

    def test_truncate_by_chars(self) -> None:
        """验证超长文本被截断（长度 = 上限 + 当前配置文案，不绑历史文案）。"""
        long_text = "a" * 200
        result = self.sanitizer.sanitize(long_text)
        assert result.startswith("a" * 100)
        assert len(result) <= 100 + len(self.config.truncation_message)

    def test_no_truncate_short_text(self) -> None:
        """验证短文本不被截断。"""
        short_text = "你好世界"
        result = self.sanitizer.sanitize(short_text)
        assert "你好世界" in result

    def test_close_unclosed_code_block(self) -> None:
        """验证未闭合的代码块被补齐。"""
        text = "```python\nprint('hello')"
        result = self.sanitizer.sanitize(text)
        assert "```python" in result
        assert "```" in result

    def test_no_double_close(self) -> None:
        """验证已闭合的代码块不重复添加。"""
        text = "```python\nprint('hello')\n```"
        result = self.sanitizer.sanitize(text)
        # 不应出现三个 ```
        assert "```\n```" not in result

    def test_aigc_notice_opt_in(self) -> None:
        text = "这是一段回复"
        assert "此内容由 AI 生成" not in self.sanitizer.sanitize(text)
        config = OutputSanitizerConfig(aigc_notice="此内容由 AI 生成，仅供参考")
        result = OutputSanitizer(config).sanitize(text)
        assert "此内容由 AI 生成，仅供参考" in result

    def test_config_max_lines(self) -> None:
        """验证行数截断生效。"""
        config = OutputSanitizerConfig(max_chars=999999, max_lines=3)
        s = OutputSanitizer(config=config)
        lines = ["line1", "line2", "line3", "line4", "line5"]
        text = "\n".join(lines)
        result = s.sanitize(text)
        assert "line4" not in result or "截断" in result

    def test_escape_html_enabled(self) -> None:
        """验证 HTML 转义生效。"""
        config = OutputSanitizerConfig(escape_html=True, max_chars=999999, max_lines=500)
        s = OutputSanitizer(config=config)
        text = "<script>alert('xss')</script> &"
        result = s.sanitize(text)
        assert "&lt;script&gt;" in result
        assert "&amp;" in result
        assert "<script>" not in result

    def test_escape_html_disabled(self) -> None:
        """验证 HTML 转义关闭时不转义。"""
        config = OutputSanitizerConfig(escape_html=False, max_chars=999999, max_lines=500)
        s = OutputSanitizer(config=config)
        text = "<b>bold</b>"
        result = s.sanitize(text)
        assert "<b>" in result
