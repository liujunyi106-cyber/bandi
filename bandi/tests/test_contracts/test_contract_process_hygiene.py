# 功能：契约——进程卫生两件（幂等存储原子写 / TokenCounter 模型名任务上下文隔离）。
# TokenCounter.set_active_model 在 asyncio 任务间隔离（并发子代理互不覆盖模型校准键）
# 非设置上下文回落最近一次类级设置（旧行为兼容）。
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bandi.core.message import ToolResult
from bandi.tools.idempotency import IdempotencyStore
from bandi.utils.token_counter import TokenCounter


def _result(output: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        tool_name="write",
        success=True,
        output=output,
        error=None,
        duration_ms=0.0,
    )


# ── 幂等存储原子写（replace 失败不损坏既有条目 + tmp 清理） ─────────────


def test_idempotency_set_is_atomic_on_replace_failure(tmp_path: Path, monkeypatch) -> None:
    import bandi.tools.idempotency as idem_mod

    store = IdempotencyStore(str(tmp_path))
    store.set("key-1", _result("旧值"))

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(idem_mod.os, "replace", _boom)
    store.set("key-1", _result("新值"))  # 失败路径：静默 warning，不得损坏旧条目

    loaded = store.get("key-1")
    assert loaded is not None and loaded.output == "旧值", "replace 失败不得损坏既有条目"
    assert list(tmp_path.glob("*.tmp")) == [], "失败路径必须清理临时文件"


# ── TokenCounter 模型名任务上下文隔离 ────────────────────────────────


@pytest.mark.asyncio
async def test_active_model_isolated_per_task() -> None:
    async def _worker(model: str) -> str:
        TokenCounter.set_active_model(model)
        await asyncio.sleep(0.02)
        return TokenCounter.active_model()

    results = await asyncio.gather(_worker("model-a"), _worker("model-b"))

    assert results == ["model-a", "model-b"], (
        "并发任务各自设置的 active model 不得互相覆盖（旧实现为类级全局）"
    )


def test_active_model_falls_back_to_class_level() -> None:
    TokenCounter.set_active_model("model-main")

    assert TokenCounter.active_model() == "model-main"
    assert TokenCounter._ratio() == TokenCounter._calibrations.get("model-main", 1.0)
