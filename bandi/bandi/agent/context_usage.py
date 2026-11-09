# 功能：上下文用量快照——把单轮请求按五类统计 token 占用，供前端「上下文用量」面板
"""五类口径（与实际请求逐字对应，不做二次猜测）：

- system     : 非工件类 system 消息（静态人格段 + 动态项目/安全段 + 状态块）
- tools      : 内置工具 schema + 工具面工件（含子智能体工具）
- messages   : 对话消息（历史 + 用户输入 + 记忆召回等易变注入）
- connectors : MCP / 连接器工具 schema（按 `mcp_remote` 标记拆分）
- skills     : 技能工件（索引 + 命中正文 + 约束）

统计对象是**真正发出去的那份请求**（`provider_messages` + `active_tools`），
因此面板数字与模型实际收到的上下文一致；provider 侧真实计费值仍以 usage 为准。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from bandi.company.output_contract import (
    ARTIFACT_TITLE_SKILLS,
    ARTIFACT_TITLE_TOOL_SURFACE,
    RUNTIME_ARTIFACT_DELTA_HEADER,
    RUNTIME_ARTIFACT_HEADER,
)
from bandi.utils.token_counter import TokenCounter

BUCKET_SYSTEM = "system"
BUCKET_TOOLS = "tools"
BUCKET_MESSAGES = "messages"
BUCKET_CONNECTORS = "connectors"
BUCKET_SKILLS = "skills"

# 前端图例与进度条按此顺序渲染（顺序即展示顺序，不随请求变化）
BUCKETS: tuple[str, ...] = (
    BUCKET_SYSTEM,
    BUCKET_TOOLS,
    BUCKET_MESSAGES,
    BUCKET_CONNECTORS,
    BUCKET_SKILLS,
)

_SKILLS_HEADERS: tuple[str, ...] = (
    f"{RUNTIME_ARTIFACT_HEADER} {ARTIFACT_TITLE_SKILLS}",
    f"{RUNTIME_ARTIFACT_DELTA_HEADER} {ARTIFACT_TITLE_SKILLS}",
)
_TOOL_HEADERS: tuple[str, ...] = (
    f"{RUNTIME_ARTIFACT_HEADER} {ARTIFACT_TITLE_TOOL_SURFACE}",
    f"{RUNTIME_ARTIFACT_DELTA_HEADER} {ARTIFACT_TITLE_TOOL_SURFACE}",
)


def _count(text: str) -> int:
    # 功能：单段文本 token 估算（空串直接 0，避免计数器空转）
    return TokenCounter.count(text) if text else 0


def _schema_name(schema: dict[str, Any]) -> str:
    # 功能：兼容 OpenAI function 包装与扁平 schema 的工具名。
    function = schema.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(schema.get("name") or "")


def _artifact_bucket(content: str) -> str | None:
    # 功能：工件 system 消息归因——按标题前缀判定技能/工具面，其余返回 None
    for header in _SKILLS_HEADERS:
        if content.startswith(header):
            return BUCKET_SKILLS
    for header in _TOOL_HEADERS:
        if content.startswith(header):
            return BUCKET_TOOLS
    return None


def build_snapshot(
    *,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None = None,
    mcp_tool_names: Iterable[str] = (),
    model: str = "",
    context_window: int = 0,
    updated_at: str = "",
) -> dict[str, Any]:
    """功能：构建上下文用量快照——五类 token 数 + 总量 + 窗口。

    `messages` 为 provider 请求体（role/content 字典），`tools` 为同一次请求的
    schema 列表；两者相加即该轮上下文总占用。
    """
    counts: dict[str, int] = {bucket: 0 for bucket in BUCKETS}
    for message in messages:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        artifact_bucket = _artifact_bucket(content) if role == "system" else None
        if artifact_bucket is not None:
            counts[artifact_bucket] += TokenCounter.count_message(message)
        elif role == "system":
            counts[BUCKET_SYSTEM] += TokenCounter.count_message(message)
        else:
            counts[BUCKET_MESSAGES] += TokenCounter.count_message(message)

    remote_names = {name for name in mcp_tool_names if name}
    for schema in tools or ():
        payload = json.dumps(schema, ensure_ascii=False, default=str)
        bucket = BUCKET_CONNECTORS if _schema_name(schema) in remote_names else BUCKET_TOOLS
        counts[bucket] += _count(payload)

    return {
        "model": model,
        "context_window": max(0, int(context_window or 0)),
        "used_tokens": sum(counts.values()),
        "buckets": counts,
        "updated_at": updated_at or datetime.now().isoformat(timespec="seconds"),
    }
