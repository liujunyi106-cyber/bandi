from __future__ import annotations

from bandi.core.error_codes import ApiResult, AppErrorCode


class TestAppErrorCode:
    def test_error_code_is_enum(self) -> None:
        assert AppErrorCode.TOOL_DENIED.value == "TOOL_DENIED"
        assert AppErrorCode.UNKNOWN_ERROR.value == "UNKNOWN_ERROR"

    def test_all_codes_unique(self) -> None:
        codes = list(AppErrorCode)
        values = [c.value for c in codes]
        assert len(values) == len(set(values))


class TestApiResult:
    def test_success_result(self) -> None:
        result = ApiResult.ok(data={"key": "value"})
        assert result.success is True
        assert result.data == {"key": "value"}
        assert result.error_code is None

    def test_error_result(self) -> None:
        result = ApiResult.err(AppErrorCode.TOOL_DENIED, "tool not allowed")
        assert result.success is False
        assert result.error_code == AppErrorCode.TOOL_DENIED
        assert result.message == "tool not allowed"

    def test_error_result_with_data(self) -> None:
        result = ApiResult.err(AppErrorCode.TOOL_TIMEOUT, "timeout", data={"tool": "bash"})
        assert result.success is False
        assert result.error_code == AppErrorCode.TOOL_TIMEOUT
        assert result.data == {"tool": "bash"}
