# 功能：节点级工具重试服务（-3b）——按 tool_call_id 定位历史调用并 force 真跑。
# - 每次重试构造全新 ToolExecutor → 幂等键全新 + 无结果缓存 = 真跑，不复用旧结果；
# - 权限链与正常一致：headless（无交互通道）下 ask 类工具 fail-closed 返回
# APPROVAL_REQUIRED 提示，不开提权通道；
# - 重试结果不接回 Agent 循环、不写入 LLM 上下文（前端内存节点更新，3c 随过程持久化）。
from __future__ import annotations

import json
import logging
from typing import Any

from bandi.core.message import ToolCall
from bandi.memory.session.store import SessionStore
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def locate_tool_call(
    store: SessionStore, session_id: str, tool_call_id: str
) -> dict[str, Any] | None:
    """功能：在会话分支 assistant 消息 metadata.tool_calls 中按 id 定位调用
    （Day122z 读回还原先例）；未知 id 返回 None。"""
    # 定位调用需覆盖被压缩前的原始历史（重试可能指向边界之前的调用）
    for entry in store.get_branch(session_id, full=True):
        if entry.get("entry_type") != "message" or entry.get("role") != "assistant":
            continue
        raw = entry.get("metadata")
        try:
            meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (TypeError, ValueError):
            continue
        for call in meta.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id") == tool_call_id:
                return {
                    "id": str(call.get("id") or ""),
                    "name": str(call.get("name") or ""),
                    "arguments": call.get("arguments") or {},
                }
    return None


async def retry_tool_call(
    session_id: str,
    tool_call_id: str,
    *,
    store: SessionStore,
    project_root: str | None = None,
    registry: ToolRegistry | None = None,
    executor: ToolExecutor | None = None,
) -> dict[str, Any] | None:
    """功能：定位历史 tool_call 并 force 真跑一次，返回结果摘要。

    返回 None = 定位失败（端点层转 404）；否则
    ``{tool_name, success, output_preview, error, duration_ms}``。
    registry/executor 可注入（契约测试桩）；生产缺省 build_tool_registry 全装配。
    """
    call = locate_tool_call(store, session_id, tool_call_id)
    if call is None or not call["name"]:
        return None
    if executor is None:
        if registry is None:
            from bandi.agent.factory import build_tool_registry

            registry = build_tool_registry()
        # 全新 executor：幂等键全新（_idem_keys 空）+ 无结果缓存/幂等存储 = force 真跑
        executor = ToolExecutor(registry, project_root=project_root)
    results = await executor.execute(
        [ToolCall(id=call["id"], name=call["name"], arguments=dict(call["arguments"]))]
    )
    if not results:
        logger.warning("retry-tool executor returned no result: %s", tool_call_id)
        return {
            "tool_name": call["name"],
            "success": False,
            "output_preview": "",
            "error": "executor returned no result",
            "duration_ms": 0,
        }
    result = results[0]
    return {
        "tool_name": result.tool_name,
        "success": bool(result.success),
        "output_preview": str(result.output or "")[:200].replace("\n", " "),
        "error": result.error or "",
        "duration_ms": int(result.duration_ms),
    }
