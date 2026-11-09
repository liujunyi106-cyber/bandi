# 功能：dispatch guard 判定——DELEGATE 轮未产生 board 变更时给出重试/未解决（纯函数）。

from __future__ import annotations

from enum import Enum

DISPATCH_GUARD_MAX_ATTEMPTS: int = 2


class DispatchGuardVerdict(Enum):
    OK = "ok"
    RETRY = "retry"
    UNRESOLVED = "unresolved"


def evaluate_dispatch_guard(
    *,
    is_delegate_turn: bool,
    board_changed: bool,
    has_justification: bool,
    attempts: int,
) -> DispatchGuardVerdict:
    """判定一次 DELEGATE 轮的处置：合规放行 / 限次退回重试 / 超限未解决。

    board_changed = 本轮产生了新子卡或既有子卡修订/取消；has_justification =
    输出含 NO_DELEGATION_JUSTIFICATION 行；attempts = 已重试次数。
    """
    if not is_delegate_turn or board_changed or has_justification:
        return DispatchGuardVerdict.OK
    if attempts < DISPATCH_GUARD_MAX_ATTEMPTS:
        return DispatchGuardVerdict.RETRY
    return DispatchGuardVerdict.UNRESOLVED
