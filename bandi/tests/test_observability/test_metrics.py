from __future__ import annotations

from unittest.mock import MagicMock

from bandi.observability.metrics import PROMETHEUS_AVAILABLE, MetricsCollector


class TestMetricsCollector:
    def test_record_agent_turn(self) -> None:
        mc = MetricsCollector()
        mc.record_agent_turn("completed")
        mc.record_agent_turn("failed")

    def test_record_llm_call(self) -> None:
        mc = MetricsCollector()
        mc.record_llm_call("deepseek", "deepseek-chat", "success", 100, 50, 0.5)

    def test_record_llm_call_with_prometheus_counters(self) -> None:
        mc = MetricsCollector()
        mc._llm_calls = MagicMock()
        mc._token_usage = MagicMock()
        mc._llm_latency = MagicMock()
        mc.record_llm_call("deepseek", "deepseek-chat", "success", 100, 50, 0.5)
        mc._llm_calls.labels.return_value.inc.assert_called_once()
        mc._token_usage.labels.assert_any_call(type="prompt")
        mc._token_usage.labels.assert_any_call(type="completion")
        mc._llm_latency.labels.return_value.observe.assert_called_once_with(0.5)

    def test_record_llm_call_zero_tokens_skips_usage(self) -> None:
        mc = MetricsCollector()
        mc._token_usage = MagicMock()
        mc.record_llm_call("deepseek", "deepseek-chat", "error", 0, 0, 1.0)
        mc._token_usage.labels.assert_not_called()

    def test_record_tool_call(self) -> None:
        mc = MetricsCollector()
        mc.record_tool_call("bash", 1.2, success=True)
        mc.record_tool_call("bash", 0.5, success=False)

    def test_record_tool_call_status_labels(self) -> None:
        mc = MetricsCollector()
        mc._tool_calls = MagicMock()
        mc.record_tool_call("bash", 1.2, success=True)
        mc.record_tool_call("bash", 0.5, success=False)
        mc._tool_calls.labels.assert_any_call(tool="bash", status="success")
        mc._tool_calls.labels.assert_any_call(tool="bash", status="failure")

    def test_prometheus_available_flag(self) -> None:
        assert isinstance(PROMETHEUS_AVAILABLE, bool)

    def test_expose_metrics_accepts_app(self) -> None:
        mc = MetricsCollector()
        result = mc.expose_metrics(None)
        assert result is None
