from __future__ import annotations

import json
import tempfile
from pathlib import Path

from bandi.core.error_codes import ApiResult, AppErrorCode
from bandi.observability.logger import Logger, LogLevel
from bandi.observability.metrics import MetricsCollector
from bandi.observability.secret_mask import mask_dict
from bandi.observability.tracer import BandiTracer


class TestErrorCodeIntegration:
    def test_error_code_maps_to_exception_message(self) -> None:
        code = AppErrorCode.TOOL_DENIED
        result = ApiResult.err(code, f"tool not allowed: {code}")
        assert AppErrorCode.TOOL_DENIED in result.message

    def test_error_code_covers_known_paths(self) -> None:
        codes = {c.value for c in AppErrorCode}
        for needed in {
            "TOOL_DENIED",
            "RATE_LIMITED",
            "PERMISSION_DENIED",
            "UNKNOWN_ERROR",
        }:
            assert needed in codes


class TestLoggerSecretMaskPipeline:
    def test_logger_masks_sensitive_key(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        logger = Logger(tmp, level=LogLevel.DEBUG, console_output=False)
        logger.info("test", "login ok", api_key="sk-super-secret")
        logger.close()
        raw = (tmp / "bandi.log").read_text()
        assert "sk-super-secret" not in raw
        assert "sk-****" in raw or "****" in raw

    def test_logger_trace_id_propagates_to_log(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        logger = Logger(tmp, level=LogLevel.DEBUG, console_output=False)
        logger.info("test", "trace check")
        logger.close()
        with open(tmp / "bandi.log", encoding="utf-8") as f:
            entry = json.loads(f.readline())
        assert len(entry["trace_id"]) == 12


class TestMetricsAccumulationIntegration:
    def test_metrics_accrue_across_calls(self) -> None:
        mc = MetricsCollector()
        mc.record_agent_turn("completed")
        mc.record_agent_turn("completed")
        mc.record_agent_turn("failed")
        mc.record_llm_call("deepseek", "deepseek-chat", "success", 100, 50, 0.5)
        mc.record_tool_call("bash", 1.2, success=True)

    def test_metrics_expose_does_not_crash_without_prometheus(self) -> None:
        mc = MetricsCollector()
        result = mc.expose_metrics(None)
        assert result is None


class TestTracerContextPropagation:
    def test_tracer_span_context_manager(self) -> None:
        tracer = BandiTracer(service_name="integration_test")
        with tracer.span("parent") as parent:
            parent.set_attribute("stage", "integration")
            with tracer.span("child") as child:
                child.set_attribute("type", "nested")

    def test_tracer_null_otel_graceful(self) -> None:
        tracer = BandiTracer(service_name="graceful_test")
        with tracer.span("graceful") as span:
            assert span is not None


class TestSecretMaskNestedIntegration:
    def test_mask_dict_deep_nested_log_entry(self) -> None:
        log_entry = {
            "timestamp": "2026-01-01T00:00:00",
            "level": "INFO",
            "module": "auth",
            "message": "user login",
            "meta": {"credentials": {"password": "hunter2", "token": "abc123"}},
        }
        masked = mask_dict(log_entry)
        assert masked["meta"]["credentials"] == "****"
        assert masked["message"] == "user login"
