# 功能：数据源适配器核心类型（输入/输出模型 + DataSourceAdapter 基类）
from __future__ import annotations

import abc
import asyncio
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdapterExecutionInput:
    signal: asyncio.Event | None = None
    timeout_ms: int = 10000


@dataclass
class AdapterPreviewInput(AdapterExecutionInput):
    table: str = ""
    limit: int = 100


@dataclass
class AdapterSqlInput(AdapterExecutionInput):
    sql: str = ""
    limit: int = 1000


@dataclass
class ColumnSchema:
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False


@dataclass
class TableSchema:
    name: str
    columns: list[ColumnSchema] = field(default_factory=list)
    row_count: int | None = None


@dataclass
class SchemaSummary:
    tables: list[TableSchema] = field(default_factory=list)


@dataclass
class TableResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0


class DataSourceAdapter(abc.ABC):
    @abc.abstractmethod
    async def inspect_schema(self, input: AdapterExecutionInput) -> SchemaSummary: ...

    @abc.abstractmethod
    async def preview_table(self, input: AdapterPreviewInput) -> TableResult: ...

    @abc.abstractmethod
    async def run_sql_readonly(self, input: AdapterSqlInput) -> TableResult: ...


def _adapter_guard(
    fn: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """适配器入口统一守卫——signal 取消 + timeout_ms 超时。

    守卫语义：
    - signal 已置位 → 立即抛 CancelledError（不执行 IO）
    - 运行中 signal 置位或超时 → 取消执行并抛 CancelledError/TimeoutError
    """

    @functools.wraps(fn)
    async def wrapper(
        self: DataSourceAdapter,
        input: AdapterExecutionInput = AdapterExecutionInput(),
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if input.signal is not None and input.signal.is_set():
            raise asyncio.CancelledError("cancelled before adapter start")
        timeout_secs = (
            input.timeout_ms / 1000 if input.timeout_ms and input.timeout_ms > 0 else None
        )
        # 同步阻塞 IO（csv/parquet/xlsx/sqlite 文件与 DB 读取）必须在线程池执行——
        # 直接 create_task 会卡死事件循环，超时/信号取消全部空转。
        coro = fn(self, input, *args, **kwargs)
        loop = asyncio.get_running_loop()
        coro_task = loop.run_in_executor(None, asyncio.run, coro)
        coro_task.add_done_callback(lambda fut: fut.exception() if not fut.cancelled() else None)
        waiters: list[asyncio.Future[Any]] = [coro_task]
        signal_waiter: asyncio.Task[Any] | None = None
        if input.signal is not None:
            signal_waiter = asyncio.create_task(input.signal.wait())
            waiters.append(signal_waiter)
        done, pending = await asyncio.wait(
            waiters, timeout=timeout_secs, return_when=asyncio.FIRST_COMPLETED
        )
        if signal_waiter is not None and signal_waiter in pending:
            signal_waiter.cancel()
            await asyncio.gather(signal_waiter, return_exceptions=True)
        if coro_task in done:
            return await coro_task
        if input.signal is not None and input.signal.is_set():
            raise asyncio.CancelledError("cancelled by signal")
        raise TimeoutError(f"adapter timed out after {input.timeout_ms}ms")

    return wrapper


AdapterFactory = Callable[[dict], DataSourceAdapter]


class AdapterRegistry:
    _registry: dict[str, AdapterFactory] = {}

    @classmethod
    def register(cls, type: str, factory: AdapterFactory) -> None:
        cls._registry[type] = factory

    @classmethod
    def create(cls, type: str, config: dict) -> DataSourceAdapter:
        factory = cls._registry.get(type)
        if not factory:
            raise ValueError(f"Unsupported data source type: {type}")
        return factory(config)

    @classmethod
    def list_types(cls) -> list[str]:
        return list(cls._registry.keys())

    @classmethod
    def clear(cls) -> None:
        cls._registry.clear()
