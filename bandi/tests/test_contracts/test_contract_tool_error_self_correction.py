"""功能：工具参数错误必须能自纠——回传 `next_step` 指明正确工具/调用方式。

真机缺陷（一次 3600 秒会话，309 次工具失败、无一构成连续失败）：

    tool list_dir failure count: 1      ← 出现 66 次
    tool glob failure count: 1          ← 出现 56 次
    tool memory_read failure count: 1   ← 出现 13 次

失败计数永远停在 1：模型拿到 `{"error": "entry_id is required"}` 后**不知道
该怎么改**，就用同一参数原样重试；期间只要有一次成功，`_reset_failure`
又把计数清零，`consecutive_failure_threshold=3` 永远够不到——
熔断不触发、本轮跑满 3600 秒、session_total 涨到 169 万 tokens、零正文。

契约钉住：缺参错误必须带 `next_step`，且明确指出该改用哪个工具。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from bandi.tools.memory_tools import MemoryReadTool


def _ctx(tmp_path: Path) -> MemoryReadTool:
    return MemoryReadTool(db_dir=tmp_path)


def test_missing_entry_id_carries_next_step(tmp_path: Path) -> None:
    """功能：缺 entry_id 的回传必须自带纠正指引（否则模型原样重试到预算耗尽）。"""
    result = asyncio.run(_ctx(tmp_path).execute({"query": "AI 新闻"}))
    assert result.get("error"), "缺少必填参数必须显式报错"
    next_step = str(result.get("next_step") or "")
    assert next_step.strip(), (
        "缺参错误没有 next_step：模型无法自纠，会拿同一参数反复重试，"
        "失败计数被成功清零后永远够不到熔断阈值"
    )


def test_next_step_names_the_right_tool(tmp_path: Path) -> None:
    """功能：纠正指引必须点名 memory_search（按关键词搜索的正确入口）。"""
    result = asyncio.run(_ctx(tmp_path).execute({}))
    next_step = str(result.get("next_step") or "")
    assert "memory_search" in next_step, "指引必须告诉模型正确的工具名"
    assert "query" in next_step, "指引必须给出正确参数形态"
    assert "不要" in next_step, "必须显式劝阻重复调用同一工具"


def test_query_is_still_rejected_not_silently_accepted(tmp_path: Path) -> None:
    """功能：不把 query 当 entry_id 用（防「静默接受错误参数」掩盖调用方错误）。"""
    result = asyncio.run(_ctx(tmp_path).execute({"query": "项目代号"}))
    assert result.get("error") == "entry_id is required"


def test_valid_entry_id_does_not_return_error(tmp_path: Path) -> None:
    """功能：给了 entry_id 就不再走缺参分支（防过度收紧）。"""
    result = asyncio.run(_ctx(tmp_path).execute({"entry_id": "no-such-entry"}))
    assert "is required" not in str(result.get("error") or "")
