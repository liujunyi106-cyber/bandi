from __future__ import annotations


class NonRetryableToolError(Exception):
    """确定性工具失败（参数/业务校验/不支持的操作）——重试无意义且放大副作用。"""


class ToolAbortedError(Exception):
    """用户中止/审批拒绝类终止——轮次应停止而非继续尝试。"""
