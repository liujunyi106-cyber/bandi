# 功能：座位调度——AgentSeat + RecruitStatus + Recruiter（独立于 LLM 招募 CompanyRecruiter）

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class RecruitStatus(StrEnum):
    AVAILABLE = "available"
    RECRUITED = "recruited"
    BUSY = "busy"


@dataclass
class AgentSeat:
    role_id: str
    status: RecruitStatus
    recruited_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Recruiter:
    def __init__(self) -> None:
        self._seats: dict[str, AgentSeat] = {}
        # 功能：加载基线快照（role_id → status）——写前合并据此判定本进程主动改过的
        # 席位，防「释放被盘上 recruited 复活」（基线为空时合并不设限，保持旧语义）。
        self._baseline_seat_status: dict[str, str] = {}

    def snapshot_baseline(self) -> None:
        """功能：记录当前席位状态为本进程基线（load_org 加载完成后调用）。"""
        self._baseline_seat_status = {s.role_id: s.status.value for s in self._seats.values()}

    @property
    def changed_seat_ids(self) -> frozenset[str]:
        """功能：相对基线被本进程改动过状态的席位 role_id——写前合并须跳过其状态对齐。"""
        return frozenset(
            seat.role_id
            for seat in self._seats.values()
            if self._baseline_seat_status.get(seat.role_id, seat.status.value) != seat.status.value
        )

    def register_seat(self, role_id: str) -> AgentSeat:
        seat = AgentSeat(role_id=role_id, status=RecruitStatus.AVAILABLE)
        self._seats[role_id] = seat
        return seat

    def recruit(self, role_id: str) -> AgentSeat | None:
        seat = self._seats.get(role_id)
        if seat is None:
            return None
        if seat.status != RecruitStatus.AVAILABLE:
            return None
        seat.status = RecruitStatus.RECRUITED
        seat.recruited_at = datetime.now(UTC)
        return seat

    def release(self, role_id: str) -> bool:
        seat = self._seats.get(role_id)
        if seat is None:
            return False
        if seat.status not in (RecruitStatus.RECRUITED, RecruitStatus.BUSY):
            return False
        seat.status = RecruitStatus.AVAILABLE
        return True

    def get_seat(self, role_id: str) -> AgentSeat | None:
        return self._seats.get(role_id)

    def list_available(self) -> list[AgentSeat]:
        return [s for s in self._seats.values() if s.status == RecruitStatus.AVAILABLE]

    def list_recruited(self) -> list[AgentSeat]:
        return [s for s in self._seats.values() if s.status == RecruitStatus.RECRUITED]

    def list_seats(self) -> list[AgentSeat]:
        return list(self._seats.values())

    def dispatch(self, role_id: str) -> bool:
        # 功能：认领席位——AVAILABLE 先招募；已 RECRUITED（模板实例化/重启恢复）
        # 直接置 BUSY；BUSY 拒绝（互斥保持，防双执行）
        seat = self._seats.get(role_id)
        if seat is None:
            return False
        if seat.status == RecruitStatus.AVAILABLE:
            seat.status = RecruitStatus.RECRUITED
            seat.recruited_at = datetime.now(UTC)
        if seat.status != RecruitStatus.RECRUITED:
            return False
        seat.status = RecruitStatus.BUSY
        return True
