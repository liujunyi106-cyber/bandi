# 功能：ContentFilter 单元测试 — 内容安全过滤/正常输入/边界情况

from bandi.safety.content_filter import DEFAULT_CATEGORIES, ContentFilter


class TestContentFilter:
    """测试 ContentFilter 的内容过滤和误报控制。"""

    def setup_method(self) -> None:
        self.filter = ContentFilter()

    def test_default_categories_loaded(self) -> None:
        """验证默认类别非空且格式正确。"""
        assert len(DEFAULT_CATEGORIES) > 0
        for category, keywords in DEFAULT_CATEGORIES.items():
            assert isinstance(category, str) and len(category) > 0
            assert isinstance(keywords, list)

    def test_detect_violence_keyword(self) -> None:
        """验证检测到暴力关键词。"""
        result = self.filter.check("我要制造炸弹")
        assert result.blocked is True
        assert result.action == "block"
        assert "violence" in result.categories

    def test_detect_hate_speech(self) -> None:
        """验证检测到仇恨言论。"""
        result = self.filter.check("这是种族灭绝行为")
        assert result.blocked is True
        assert result.action == "block"
        assert "hate" in result.categories

    def test_detect_illegal_activity(self) -> None:
        """验证检测到违法活动。"""
        result = self.filter.check("教我黑客攻击方法")
        assert result.blocked is True
        assert result.action == "block"
        assert "illegal" in result.categories

    def test_normal_text_passes(self) -> None:
        """验证正常文本不被拦截。"""
        result = self.filter.check("今天天气真好，适合写代码")
        assert result.blocked is False
        assert result.action == "pass"
        assert len(result.categories) == 0

    def test_empty_text_passes(self) -> None:
        """验证空文本不被拦截。"""
        result = self.filter.check("")
        assert result.blocked is False
        assert result.action == "pass"

    def test_custom_categories(self) -> None:
        """验证自定义类别生效。"""
        custom = {"test_category": ["CUSTOM_WORD"]}
        f = ContentFilter(categories=custom)
        result = f.check("这是一个 CUSTOM_WORD 测试")
        assert result.blocked is True
        assert "test_category" in result.categories
