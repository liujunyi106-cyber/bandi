# 功能：团队记忆写入门（Phase2）——暂存组合层
#
# - 上报上级角色 = 可选，意见仅存档为建议（escalation），**无落库效力**；
# - 确认权 = **仅用户本人**（DEFAULT_LOCAL_USER_ID）——批准返回 payload 由调用方
# 带 team_id 落 episodic 团队分区；非本人确认 raise PermissionError；
# - 超时 = 丢弃（discard_expired 复用 purge_expired），不降级不改道。

from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.core.account import DEFAULT_LOCAL_USER_ID
from bandi.memory.staging import MemoryPendingStore

TEAM_MEMORY_SUBSYSTEM = "team_memory"

# 超时丢弃周期（拍板：丢弃，不降级；周期可调）——单一来源（API 端点引用此常量）
DEFAULT_TTL_DAYS = 7.0

_SUMMARY_MAX_CHARS = 500


class TeamMemoryStaging:
    """团队记忆暂存门——提名/上报/确认/超时，落库由确认方执行。"""

    def __init__(self, store_dir: str | Path) -> None:
        self._store = MemoryPendingStore(store_dir, subsystem=TEAM_MEMORY_SUBSYSTEM)

    def propose(
        self,
        *,
        summary: str,
        project_id: str,
        team_id: str,
        employee_id: str,
        session_id: str,
        turn_index: int,
        signature: str = "",
        extra: dict[str, Any] | None = None,
    ) -> str:
        """功能：提名一条反思结论进暂存；无 team_id/空摘要不提名（返回空串）；
        同 (session_id, turn_index) 幂等去重；`signature` 非空时跨轮内容级去重
        （同团队同任务同结论只留一条在途）；`extra` 为结构化补充字段
        （涉及文件/目标员工/建议技能等），不得覆盖核心键。

        写侧先做过期清理——原仅列表端点（读侧）
        触发，不访问 UI 时暂存无界堆积。
        """
        text = (summary or "").strip()
        team = (team_id or "").strip()
        if not text or not team:
            return ""
        self.discard_expired(DEFAULT_TTL_DAYS)
        dedup_key = f"{session_id}:{turn_index}"
        sig = str(signature or "").strip()
        for record in self.list_pending():
            if record.payload.get("dedup_key") == dedup_key:
                return record.id8
            if sig and record.payload.get("signature") == sig:
                return record.id8
        payload: dict[str, Any] = {
            "summary": text[:_SUMMARY_MAX_CHARS],
            "project_id": str(project_id or "").strip(),
            "team_id": team,
            "employee_id": str(employee_id or "").strip(),
            "session_id": str(session_id or ""),
            "turn_index": int(turn_index),
            "dedup_key": dedup_key,
            "signature": sig,
            "escalation": {},
        }
        for key, value in (extra or {}).items():
            if key not in payload:
                payload[key] = value
        record = self._store.stage(
            {
                "subsystem": TEAM_MEMORY_SUBSYSTEM,
                "action": "propose_team_episode",
                "summary": text[:_SUMMARY_MAX_CHARS],
                "payload": payload,
            }
        )
        return record.id8

    def report_to_superior(self, id8: str, *, opinion: str, reporter_role: str) -> bool:
        """功能：上报上级角色——意见仅存档为建议（escalation），不产生落库效力。"""
        record = self._store.get_pending(id8)
        if record is None or record.subsystem != TEAM_MEMORY_SUBSYSTEM:
            return False
        payload = dict(record.payload)
        payload["escalation"] = {
            "opinion": str(opinion or "").strip()[:300],
            "reporter_role": str(reporter_role or "").strip()[:100],
        }
        return self._store.update_payload(id8, payload)

    def confirm(self, id8: str, *, confirmer: str) -> dict[str, Any]:
        """功能：用户本人确认——返回 payload 供调用方带 team_id 落 episodic
        团队分区；**非本人确认 raise PermissionError**（确认权仅用户）。"""
        if str(confirmer or "").strip() != DEFAULT_LOCAL_USER_ID:
            raise PermissionError("团队记忆确认权仅限用户本人")
        record = self._store.get_pending(id8)
        if record is None or record.subsystem != TEAM_MEMORY_SUBSYSTEM:
            raise KeyError(f"pending team memory not found: {id8}")
        payload = dict(record.payload)
        self._store.discard_pending(id8)
        return payload

    def reject(self, id8: str, *, confirmer: str) -> bool:
        """功能：用户本人驳回——丢弃暂存项（与 confirm 同权限校验）。"""
        if str(confirmer or "").strip() != DEFAULT_LOCAL_USER_ID:
            raise PermissionError("团队记忆确认权仅限用户本人")
        record = self._store.get_pending(id8)
        if record is None or record.subsystem != TEAM_MEMORY_SUBSYSTEM:
            return False
        return self._store.discard_pending(id8)

    def discard_expired(self, max_age_days: float) -> int:
        """功能：超时丢弃——过期清理（拍板：不降级不改道）。"""
        return self._store.purge_expired(max_age_days)

    def get_pending(self, id8: str) -> Any | None:
        record = self._store.get_pending(id8)
        if record is None or record.subsystem != TEAM_MEMORY_SUBSYSTEM:
            return None
        return record

    def list_pending(self) -> list[Any]:
        return [r for r in self._store.list_pending() if r.subsystem == TEAM_MEMORY_SUBSYSTEM]

    def list_pending_count(self) -> int:
        return len(self.list_pending())
