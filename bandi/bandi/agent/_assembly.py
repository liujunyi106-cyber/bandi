# 功能：子 Agent 装配辅助——幂等存储默认取全局路径（与主 Agent factory 同路径
# 文件实现多实例安全共享；显式注入的 store 优先）
from __future__ import annotations

from typing import Any


def ensure_subagent_idempotency_store(store: Any | None) -> Any:
    if store is not None:
        return store
    from pathlib import Path

    from bandi.tools.idempotency import IdempotencyStore

    return IdempotencyStore(str(Path.home() / ".bandi" / "idempotency"))
