from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from bandi.memory.routing import classify_memory_query

logger = logging.getLogger(__name__)

PrefetchProviderFn = Callable[[str, str, list[dict]], Awaitable[dict[str, str]]]

_QUERY_MAX_CHARS = 800
_QUERY_RECENT_COUNT = 2


def build_prefetch_query(messages: list[dict]) -> str:
    """从最近 2 条 user/assistant 消息构建检索 query。"""

    recent = [
        str(m.get("content", ""))[:_QUERY_MAX_CHARS]
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return "\n".join(recent[-_QUERY_RECENT_COUNT:])


class PrefetchHandle:
    """并行预取句柄：

    - start(): 幂等，首次调用创建后台 task
    - consume(timeout): wait_for(shield(task), timeout)
        - 按时完成 → 缓存结果并返回
        - 超时 → 返回空 payload，不 cancel 不 cache（后台 task 继续跑）
        - 其它异常 → 返回空 payload 并 cache（warning）
    - cancel(): 显式取消未完成任务
    """

    def __init__(
        self,
        provider: PrefetchProviderFn,
        query: str,
        session_id: str,
        messages: list[dict],
    ) -> None:
        self._provider = provider
        self._query = query
        self._session_id = session_id
        self._messages = messages
        self.task: asyncio.Task | None = None
        self._result: dict[str, str] | None = None

    def start(self) -> PrefetchHandle:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(
                self._provider(self._query, self._session_id, self._messages)
            )
        return self

    async def consume(self, timeout: float = 1.0) -> dict[str, str]:
        if self._result is not None:
            return self._result
        if self.task is None:
            return {}
        try:
            result = await asyncio.wait_for(asyncio.shield(self.task), timeout=timeout)
        except TimeoutError:
            return {}
        except Exception as e:  # noqa: BLE001
            logger.warning("prefetch provider failed: %s", e)
            self._result = {}
            return self._result
        self._result = result or {}
        return self._result

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def cancel_and_wait(self) -> None:
        """
        后台执行（memory.close 后 SQLite ProgrammingError 被吞、静默丢结果）。
        带超时防悬挂。"""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # 关键：cancel() 只取消协程，run_in_executor 的底层线程仍会跑完并访问
        # provider.close() 做 executor.shutdown(wait=True)，真等后台线程结束再放行。
        provider_close = getattr(self._provider, "close", None)
        if provider_close is not None:
            provider_close()


class MemoryPrefetchProvider:
    """默认预取实现：统一检索出口聚合跨分类记忆，focused 节 clip 到 max_chars。

    检索经 turn 级 ThreadPoolExecutor 执行（`run_in_executor`），轮末由
    PrefetchHandle.cancel_and_wait 触发 close() → shutdown(wait=True)
    保证后台线程在 memory.close() 前结束（无 closed database 竞态）。
    """

    def __init__(
        self,
        memory_manager: Any,
        max_chars: int = 4000,
        focused_memory: bool = True,
        project_id: str | None = None,
        employee_id: str | None = None,
    ) -> None:
        self._memory = memory_manager
        self._max_chars = max_chars
        self._focused_memory = focused_memory
        self._project_id = project_id
        self._employee_id = employee_id
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bandi-prefetch")

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def _run_executor(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Awaitable[Any]:
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(self._executor, functools.partial(fn, *args, **kwargs))

    async def __call__(self, query: str, session_id: str, messages: list[dict]) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self._memory is None:
            return payload
        unified_fn = getattr(self._memory, "search_unified", None)
        if callable(unified_fn):
            hits = await self._run_executor(
                unified_fn,
                query,
                session_id=session_id,
                project_id=self._project_id,
                employee_id=self._employee_id,
                question_hint=classify_memory_query(query),
            )
            pairs = [
                (str(getattr(h, "key", "") or ""), str(getattr(h, "value", "") or "")) for h in hits
            ]
        elif self._project_id is None and self._employee_id is None:
            hits = await self._run_executor(self._memory.search_all, query, 5)
            pairs = [(h.key, h.value) for h in hits.get("long_term", [])]
        else:
            hits = await self._run_executor(
                self._memory.search_all,
                query,
                5,
                project_id=self._project_id,
                employee_id=self._employee_id,
            )
            pairs = [(h.key, h.value) for h in hits.get("long_term", [])]
        if self._focused_memory:
            focused = [(k, v) for k, v in pairs if v.strip() and k]
            if focused:
                joined = "\n".join(v for _, v in focused)
                clipped = joined[: self._max_chars]
                payload["focused_memory"] = clipped
                # states._filter_prefetch_payload 据此做 key 级去重（防跨会话重复注入）
                items: list[dict[str, str]] = []
                cursor = 0
                for k, v in focused:
                    frag = v if cursor == 0 else "\n" + v
                    if cursor + len(frag) <= len(clipped):
                        items.append({"key": k, "value": v})
                    else:
                        remain = clipped[cursor:]
                        if remain:
                            items.append({"key": k, "value": remain})
                        break
                    cursor += len(frag)
                payload["_focused_memory_items"] = items
        return payload


def render_prefetch_block(payload: dict[str, str]) -> str:
    """把 payload 渲染为 `## Runtime Prefetch` 分节块（第二轮起注入 provider_messages）。
    `_` 开头键为隐藏元数据（如 _focused_memory_items），不参与渲染。"""
    if not payload:
        return ""
    sections = "\n\n".join(
        f"## {name}\n{content}" for name, content in payload.items() if not name.startswith("_")
    )
    if not sections:
        return ""
    return f"## Runtime Prefetch\n\n{sections}"
