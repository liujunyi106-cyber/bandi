from __future__ import annotations

import asyncio

import pytest

from bandi.datasource.core import (
    AdapterExecutionInput,
    AdapterPreviewInput,
    AdapterSqlInput,
    DataSourceAdapter,
    SchemaSummary,
    TableResult,
    _adapter_guard,
)


class _SlowAdapter(DataSourceAdapter):
    """模拟慢 IO 的适配器（3 个入口方法都挂 guard）。"""

    @_adapter_guard
    async def inspect_schema(
        self, input: AdapterExecutionInput = AdapterExecutionInput()
    ) -> SchemaSummary:
        await asyncio.sleep(0.2)
        return SchemaSummary()

    @_adapter_guard
    async def preview_table(self, input: AdapterPreviewInput) -> TableResult:
        await asyncio.sleep(0.2)
        return TableResult()

    @_adapter_guard
    async def run_sql_readonly(self, input: AdapterSqlInput) -> TableResult:
        await asyncio.sleep(0.2)
        return TableResult(columns=["a"], rows=[[1]], row_count=1)


class TestAdapterGuard:
    @pytest.mark.asyncio
    async def test_timeout_raises(self) -> None:
        """
        慢适配器无限阻塞。"""
        adapter = _SlowAdapter()
        with pytest.raises(asyncio.TimeoutError):
            await adapter.inspect_schema(AdapterExecutionInput(timeout_ms=20))

    @pytest.mark.asyncio
    async def test_signal_pre_set_cancels(self) -> None:
        adapter = _SlowAdapter()
        sig = asyncio.Event()
        sig.set()
        with pytest.raises(asyncio.CancelledError):
            await adapter.preview_table(
                AdapterPreviewInput(table="t", limit=10, signal=sig, timeout_ms=1000)
            )

    @pytest.mark.asyncio
    async def test_normal_path_unaffected(self) -> None:
        adapter = _SlowAdapter()
        result = await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT 1", timeout_ms=5000))
        assert result.row_count == 1

    @pytest.mark.asyncio
    async def test_signal_set_during_run_cancels(self) -> None:
        adapter = _SlowAdapter()

        async def _cancel_later() -> None:
            await asyncio.sleep(0.05)
            sig.set()

        sig = asyncio.Event()
        task = asyncio.create_task(
            adapter.inspect_schema(AdapterExecutionInput(signal=sig, timeout_ms=5000))
        )
        cancel_task = asyncio.create_task(_cancel_later())
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            await task
        await cancel_task
