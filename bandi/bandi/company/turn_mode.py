# 功能：轮次模式——六模式推断 + manager dispatch 元数据

"""Stateless turn-mode classifier for role-instance dispatch.

执行多种不同轮次，``infer_turn_mode`` 根据工作项状态（phase + metadata +
turn_type）返回六种规范模式之一。纯函数、无 IO，同输入恒同输出。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from bandi.company._utils import as_int as _as_int
from bandi.company._utils import as_mapping as _as_mapping
from bandi.company.phase import Phase, TurnMode

MANAGER_DISPATCH_TURN_METADATA_KEYS: tuple[str, ...] = (
    "manager_board_mutation_performed",
    "manager_board_modified_work_item_ids",
    "manager_board_deleted_work_item_ids",
    "manager_no_delegation_justification",
    "no_delegation_justification",
    "manager_dispatch_guard_unresolved",
)


class WorkItemLike(Protocol):
    """infer_turn_mode 所需的最小工作项形状（phase / turn_type / metadata）。"""

    phase: Any
    turn_type: Any
    metadata: Mapping[str, Any]


def reset_manager_dispatch_turn_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """返回清除了上一轮管理者轮次产物的 metadata（不修改原 dict）。

    这些键描述一次 agent 轮次中发生了什么，不得带入重试/rework/用户跟进；
    持久 board 状态（依赖与子节点修订）有意保留。
    """
    result = dict(metadata or {})
    for key in MANAGER_DISPATCH_TURN_METADATA_KEYS:
        result.pop(key, None)
    return result


def _as_phase(value: Any) -> Phase | None:
    if isinstance(value, Phase):
        return value
    if isinstance(value, str):
        try:
            return Phase(value.strip().lower())
        except ValueError:
            return None
    return None


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(x).strip() for x in list(value or []) if str(x).strip()]


def infer_turn_mode(
    work_item: WorkItemLike,
    *,
    is_review_entry: bool = False,
) -> TurnMode:
    """分类即将执行的轮次模式。

    ``work_item`` 为 WorkItem（或暴露 phase/turn_type/metadata 属性的对象）。
    Bandi WorkItem 无 ``kind`` 字段，以 ``turn_type.value`` 回退
    ``is_review_entry`` 由调度器在弹出 review 队列入口时传入（Bandi 暂无线索
    """
    metadata = _as_mapping(getattr(work_item, "metadata", None))
    raw_kind = getattr(work_item, "turn_type", None)
    kind = str(getattr(raw_kind, "value", raw_kind) or "").strip().lower()
    phase = _as_phase(getattr(work_item, "phase", None))

    # Priority 0: report turn —— worker DONE 后产出结构化交接的隐藏卡。
    if bool(metadata.get("report_execution_work_item", False)) or kind == "report":
        return TurnMode.REPORT

    # Priority 1: review turn —— 队列入口标签 / 隐藏评审卡 / kind == "review" /
    # 评审等待 phase。
    if (
        is_review_entry
        or bool(metadata.get("review_execution_work_item", False))
        or kind == "review"
        or phase in {Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN}
    ):
        return TurnMode.REVIEW

    # Priority 2: rework —— READY_FOR_REWORK 为规范信号，但调度器会把卡翻到
    # RUNNING 后再组 prompt，故回退到评审者留下的 metadata 痕迹。
    if phase == Phase.READY_FOR_REWORK:
        return TurnMode.REWORK
    rework_feedback_raw = metadata.get("rework_feedback", "")
    rework_feedback = rework_feedback_raw.strip() if isinstance(rework_feedback_raw, str) else ""
    rework_count = _as_int(metadata.get("review_rework_count"))
    if rework_feedback or rework_count > 0:
        return TurnMode.REWORK

    # Priority 3: integrate —— 父卡有依赖（曾派活）且当前可运行
    # 说明子任务已全部完成、父卡被派发聚合轮次。
    # chain_step 卡例外：顺序链上的执行卡以「前一张完成」为放行条件，
    # 满足后应执行自己的交付物，而不是进入父卡聚合轮。
    dependency_ids = _as_string_list(metadata.get("dependency_work_item_ids", []))
    frontier = str(metadata.get("frontier", "") or "").strip().lower()
    if (
        dependency_ids
        and not bool(metadata.get("chain_step", False))
        and (
            phase in {Phase.RUNNING, Phase.READY, Phase.WAITING_FOR_CHILDREN}
            or frontier == "resumed"
        )
    ):
        return TurnMode.INTEGRATE

    # Priority 4: delegate —— 管理者角色、尚未派生子任务。
    allowed_delegate_role_ids = _as_string_list(metadata.get("allowed_delegate_role_ids", []))
    if allowed_delegate_role_ids and not dependency_ids:
        return TurnMode.DELEGATE

    # Default: worker 执行自己的工作项。
    return TurnMode.EXECUTE
