# 功能：工具风险分级——全仓唯一来源（审批收敛）
#
# * `company/approval.py` 的 `_LOW/_MEDIUM/_HIGH/_CRITICAL` 四张名单；
# * `tools/permissions_pipeline.py` 的 `_BYPASS_TOOLS` / `_ASK_TOOLS`。
# 二者对同一工具有过**互相矛盾**的定性（`todo_write` 在前者是 CRITICAL、在后者是
# 免检放行），而改动任一处都不会被另一处感知。本模块把「风险分级」这一知识收敛到
# 单点，并由契约钉死它与权限管道名单之间的关系（见
# `tests/test_contracts/test_contract_t3_tool_risk_single_source.py`）。
#
# 注意区分：本模块的 `RiskLevel` 是**工具**风险；组织重构用的是
# `company/reorg_types.py` 的 `ReorgRiskLevel`——两者语义不同，刻意不合并。

from __future__ import annotations

from enum import StrEnum
from typing import Any


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# 只读 / 无副作用工具。与 `permissions_pipeline._BYPASS_TOOLS`（免检放行）语义一致——
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read",
        "list_dir",
        "grep",
        "glob",
        "web_search",
        "web_fetch",
        "ontology_query",
        "todo_read",
        # 语义上 todo_write 只写任务清单（爆炸半径极小），以生产生效方为准归 LOW。
        "todo_write",
        "request_user_input",
    }
)

WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "python_exec",
        "file_edit",
        "file_write",
        "file_read",
        "git_diff",
        "git_status",
    }
)

# 外部副作用 / 不可逆提交
HIGH_RISK_TOOLS: frozenset[str] = frozenset(
    {
        "git_commit",
        "git_push",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_snapshot",
    }
)

# 破坏性 / 高危
CRITICAL_TOOLS: frozenset[str] = frozenset(
    {
        "shell_exec",
        "apply_patch",
        "ontology_retract",
    }
)

_RISK_RANK: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}

# 参数关键词 → 风险升级。命中即取「升级后」与「工具基准」的较高者。
_ARG_RISK_PATTERNS: list[tuple[list[str], RiskLevel]] = [
    (["recursive", "force", "dangerous"], RiskLevel.CRITICAL),
    (["delete", "remove", "destroy"], RiskLevel.HIGH),
    (["overwrite", "replace", "update"], RiskLevel.MEDIUM),
    (["read", "list", "search", "get", "find"], RiskLevel.LOW),
]


def classify_tool_risk(tool_name: str) -> RiskLevel:
    """按工具名给出基准风险级。未登记的工具落 ``（不假设安全）。"""
    if tool_name in READ_ONLY_TOOLS:
        return RiskLevel.LOW
    if tool_name in WRITE_TOOLS:
        return RiskLevel.MEDIUM
    if tool_name in HIGH_RISK_TOOLS:
        return RiskLevel.HIGH
    if tool_name in CRITICAL_TOOLS:
        return RiskLevel.CRITICAL
    return RiskLevel.MEDIUM


def predict_risk_from_arguments(tool_name: str, arguments: dict[str, Any] | None) -> RiskLevel:
    """在工具基准风险上，按键参数关键词**上调**风险（只会升，不会降）。"""
    base = classify_tool_risk(tool_name)
    if not arguments:
        return base
    args_str = str(arguments).lower()
    for keywords, level in _ARG_RISK_PATTERNS:
        if any(k in args_str for k in keywords) and _RISK_RANK[level] > _RISK_RANK[base]:
            return level
    return base
