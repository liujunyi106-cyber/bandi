# 功能：置信度检测单元测试 — detect_confidence 信号识别/边界/异常
#
# 口径（2026-10-16 修复）：只计**自指式不确定**（模型对自身回答没把握）；
# 普通分析措辞（可能/大概/也许/似乎/maybe/perhaps）不计——研究/规划文本
# 的正常措辞不得触发澄清循环。

from bandi.llm.confidence import detect_confidence


class TestDetectConfidence:
    def test_no_signal_full_confidence(self) -> None:
        assert detect_confidence("答案是 42。") == 1.0

    def test_analysis_hedging_not_penalized(self) -> None:
        # 回归：正常中文分析文（含 可能/大概/也许/似乎）不得被误判低置信
        text = "该方向可能增长，大概 30%，也许受政策影响，似乎竞争较激烈。"
        assert detect_confidence(text) == 1.0

    def test_english_analysis_hedging_not_penalized(self) -> None:
        assert detect_confidence("Maybe the market grows; perhaps faster.") == 1.0

    def test_chinese_uncertainty(self) -> None:
        assert detect_confidence("我不确定这个答案是否正确。") == 0.9

    def test_english_uncertainty(self) -> None:
        assert detect_confidence("I'm not sure about this approach.") == 0.9

    def test_multiple_signals(self) -> None:
        assert detect_confidence("我不确定，不能确定，无法确定。") == 0.7

    def test_floor_not_below_0_1(self) -> None:
        text = " ".join(
            [
                "不确定",
                "不能确定",
                "无法确定",
                "不肯定",
                "不太好说",
                "无法确认",
                "无法判断",
                "not sure",
                "not certain",
                "cannot determine",
            ]
        )
        assert detect_confidence(text) == 0.1

    def test_empty_text(self) -> None:
        assert detect_confidence("") == 1.0

    def test_low_confidence_below_0_6(self) -> None:
        text = "我不确定，不能确定，无法确定，不肯定，not sure about it."
        assert detect_confidence(text) <= 0.6

    def test_very_low_below_0_3(self) -> None:
        text = "不确定 不能确定 无法确定 不肯定 不太好说 无法确认 not sure not certain"
        assert detect_confidence(text) <= 0.3
