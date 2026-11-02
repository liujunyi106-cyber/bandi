# 功能：_adapter_guard 同步阻塞 IO 契约（防批3-1 超时/取消空转复发）
# 契约原则：guard 不得被同步阻塞 IO 卡死事件循环——超时/信号取消必须真实生效。
from __future__ import annotations

import asyncio
import time

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


class _SyncBlockingAdapter(DataSourceAdapter):
    """模拟真实同步阻塞 IO（time.sleep）的适配器——生产 csv/parquet/xlsx/sqlite
    适配器全部是这种形态（同步文件/DB 读取，方法体无 await）。"""

    @_adapter_guard
    async def inspect_schema(
        self, input: AdapterExecutionInput = AdapterExecutionInput()
    ) -> SchemaSummary:
        time.sleep(0.3)
        return SchemaSummary()

    @_adapter_guard
    async def preview_table(self, input: AdapterPreviewInput) -> TableResult:
        time.sleep(0.3)
        return TableResult()

    @_adapter_guard
    async def run_sql_readonly(self, input: AdapterSqlInput) -> TableResult:
        time.sleep(0.3)
        return TableResult(columns=["a"], rows=[[1]], row_count=1)


def test_contract_sync_io_timeout_still_raises() -> None:
    """契约 1：同步阻塞 IO 下 timeout_ms 必须仍抛 TimeoutError——
    超时后反而拿到完整结果（超时契约空转）。"""
    adapter = _SyncBlockingAdapter()

    async def _run() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await adapter.inspect_schema(AdapterExecutionInput(timeout_ms=20))

    asyncio.run(_run())


def test_contract_sync_io_keeps_loop_responsive() -> None:
    """契约 2：同步阻塞 IO 执行期间事件循环必须保持响应——"""
    adapter = _SyncBlockingAdapter()

    async def _run() -> None:
        task = asyncio.create_task(adapter.inspect_schema(AdapterExecutionInput(timeout_ms=5000)))
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.sleep(0.1)
        elapsed = loop.time() - start
        await task
        assert elapsed < 0.25, f"同步 IO 阻塞了事件循环 {elapsed:.2f}s"

    asyncio.run(_run())


def test_contract_sync_io_normal_path_unaffected() -> None:
    """契约 3：同步阻塞 IO 正常路径结果不受影响（防回归）。"""
    adapter = _SyncBlockingAdapter()

    async def _run() -> None:
        result = await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT 1", timeout_ms=5000))
        assert result.row_count == 1

    asyncio.run(_run())
