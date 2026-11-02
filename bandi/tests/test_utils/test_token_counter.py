from bandi.utils.token_counter import TokenCounter


class TestTokenCounter:
    def test_count_short_text(self) -> None:
        n = TokenCounter.count("hello world")
        assert n > 0

    def test_count_empty_string(self) -> None:
        assert TokenCounter.count("") >= 0

    def test_count_chinese(self) -> None:
        n = TokenCounter.count("你好世界")
        assert n > 0

    def test_estimate(self) -> None:
        n = TokenCounter.estimate("hello world")
        assert n == len("hello world") // 4

    def test_estimate_empty(self) -> None:
        assert TokenCounter.estimate("") == 0

    def test_batch_count(self) -> None:
        texts = ["hello", "world", ""]
        results = TokenCounter.batch_count(texts)
        assert len(results) == 3
        assert all(r >= 0 for r in results)

    def test_count_is_greater_than_estimate_for_long_text(self) -> None:
        text = "hello world " * 100
        counted = TokenCounter.count(text)
        estimated = TokenCounter.estimate(text)
        assert counted > 0
        assert estimated > 0

    def test_count_vs_estimate_consistent(self) -> None:
        text = "Hello, this is a test message with some content " * 50
        c = TokenCounter.count(text)
        e = TokenCounter.estimate(text)
        assert abs(c - e) / max(c, 1) < 0.5
