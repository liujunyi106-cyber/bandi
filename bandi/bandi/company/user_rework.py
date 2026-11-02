# 功能：用户在对话中修正「某次已派发任务」的执行细节
#
# 用户修正与评审驳回**共用** `rework_feedback` 通道（执行侧一处注入逻辑）。

from __future__ import annotations

from typing import Any

from bandi.company.phase import Phase

# 功能：用户修正指令键（区别于评审写回，供审计与前端区分来源）
USER_REWORK_DIRECTIVE_KEY = "user_rework_directive"
# 功能：用户修正轮次计数键（与评审 review_rework_count 并行，各自独立计数）
USER_REWORK_COUNT_KEY = "user_rework_count"
# 功能：回转时必须丢弃的上一版产出键（防校验/回写用旧产出冒充新结果）
STALE_RUNTIME_OUTPUT_KEYS = ("_last_output",)
# 功能：上一版产出的搬运键——清 runtime 前先搬进 metadata，供返工 brief 引用
PREVIOUS_OUTPUT_KEY = "rework_previous_output"


def prepare_user_rework_target(
    work_item: Any,
    directive: str,
    store: Any,
    *,
    target_phase: Phase = Phase.READY_FOR_REWORK,
) -> Any:
    """功能：把用户对话中的修正指令落到工作项上并受控回转，等待重跑。

    写入面（元数据）：
    - `user_rework_directive` / `latest_user_directive`：用户原文（审计 + 前端展示）；
    - `rework_feedback`：复用执行侧既有注入通道（seat_executor 读它拼进 brief）；
    - `user_rework_count`：用户修正轮次自增。

    回转面：交给 `store.reopen_approved_work_item_for_rework` 的受控入口（唯一合法
    路径——非 一律 fail-closed，不放开状态机全表）。

    返回回转后的工作项；卡不存在返回 None（调用方据此判空）。
    """
    text = str(directive or "").strip()
    if not text:
        raise ValueError("用户修正指令不得为空")
    work_item_id = str(getattr(work_item, "id", "") or "").strip()
    if not work_item_id:
        raise ValueError("工作项缺少 id，无法定位返工目标")

    current = store.get(work_item_id) or work_item
    metadata = dict(getattr(current, "metadata", None) or {})
    try:
        previous_count = int(metadata.get(USER_REWORK_COUNT_KEY, 0) or 0)
    except (TypeError, ValueError):
        previous_count = 0

    metadata_updates = {
        USER_REWORK_DIRECTIVE_KEY: text,
        "latest_user_directive": text,
        # 执行侧 brief 注入读这一个键（评审与用户修正共用通道）
        "rework_feedback": text,
        USER_REWORK_COUNT_KEY: previous_count + 1,
        "user_rework_source": "user_directive",
    }
    # 上一版产出清出 runtime（防校验/回写拿旧值）前先搬进 metadata——返工 brief 要
    # 「据此修改，不要从零重写」，产出本身不能丢
    previous_output = str(
        (getattr(current, "runtime_metadata", None) or {}).get("_last_output") or ""
    ).strip()
    if previous_output:
        metadata_updates[PREVIOUS_OUTPUT_KEY] = previous_output[:1500]

    reopened = store.reopen_approved_work_item_for_rework(
        work_item_id,
        target_phase=target_phase,
        metadata_updates=metadata_updates,
    )
    if reopened is None:
        return None
    # `_last_output` 在 runtime_metadata（重跑依据），不属于 metadata_updates 覆盖范围；
    runtime_metadata = dict(getattr(reopened, "runtime_metadata", None) or {})
    if any(key in runtime_metadata for key in STALE_RUNTIME_OUTPUT_KEYS):
        for key in STALE_RUNTIME_OUTPUT_KEYS:
            runtime_metadata.pop(key, None)
        reopened.runtime_metadata = runtime_metadata
        store.save(reopened)
    return reopened
