# 功能：用户交互接口（UserInteraction Protocol，权限/确认/输入/通知）+ InteractionStore（pending 交互登记/真挂起恢复）
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 交互种类：permission=工具授权 / confirm=确认 / input=文本输入（clarify 答复）
# notify=非交互提示（-只推帧告知，不等待决策；前端分流进通知中心
# **不得**进挂起交互槽——否则会永久占住决策 dock 并遮蔽后续真实审批卡）
INTERACTION_KIND_PERMISSION = "permission"
INTERACTION_KIND_CONFIRM = "confirm"
INTERACTION_KIND_INPUT = "input"
INTERACTION_KIND_NOTIFY = "notify"
INTERACTION_KINDS = (
    INTERACTION_KIND_PERMISSION,
    INTERACTION_KIND_CONFIRM,
    INTERACTION_KIND_INPUT,
    INTERACTION_KIND_NOTIFY,
)
_INTERACTION_STATUS_PENDING = "pending"
_INTERACTION_STATUS_RESOLVED = "resolved"
_INTERACTION_STATUS_REJECTED = "rejected"
_INTERACTION_STATUS_EXPIRED = "expired"
# 对齐 approval.py _DENIAL_TTL_SECONDS：交互默认窗口 1800s——提问/确认类卡片
# 曾按 300s 设计，员工链路实证「用户在办别的事、回来卡已死」后答复被弃
# （员工侧降级默认口径继续）；与 permission 挂起窗口同口径给足人工决策时间。
_DEFAULT_INTERACTION_TTL = 1800.0

# 交互帧推送超时：客户端不可达/半开连接上 `ws.send_json` 可能永久阻塞——
# 实证（2026-10-15）：推送阻塞会让整轮在 executing 态永久僵尸。
# 推送超时 = 该挂起不成立（用户永远看不到卡），立即拒绝并 fail-closed。
_FRAME_SEND_TIMEOUT = 5.0


class InteractionDeliveryError(RuntimeError):
    """功能：交互帧推送失败——挂起不成立，调用方按拒绝/空答复处理（fail-closed）。"""


# permission 挂起专用窗口：授权卡出现在长执行轮中途，用户可能离开，
# 短窗口会「人回来卡已死、轮次被静默拒绝烧完」——确认/提问类不受影响。
PERMISSION_INTERACTION_TTL = 1800.0


class UserInteraction(Protocol):
    """前端无关的用户交互接口。CLI/桌面端都实现此 Protocol。

    可选扩展（CorrectedError 等价物）：实现
    `ask_permission_with_feedback(tool_name, args, risk) -> (bool, str)` 时
    拒绝理由随返回值回灌模型（消费方 PermissionsPipeline 鸭子类型优先调用）。
    """

    async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool: ...

    async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool: ...

    async def ask_input(self, prompt: str, secret: bool = False) -> str: ...

    async def show_notification(self, message: str, level: str = "info") -> None: ...


@dataclass
class PendingInteraction:
    """一次待用户决策的交互（confirm/clarify/权限授权共用）。"""

    interaction_id: str
    session_id: str
    kind: str  # permission | confirm | input
    payload: dict[str, Any]
    status: str = _INTERACTION_STATUS_PENDING
    created_at: float = field(default_factory=time.time)
    # 本次挂起的实际超时（default ；ask_confirmation 等短超时场景按实际传入）——
    ttl_seconds: float = _DEFAULT_INTERACTION_TTL
    resolved_at: float | None = None
    # 决策先于 get_decision 到达时的暂存（future 惰性创建，见 resolve/reject）
    _early_value: Any = field(default=None, repr=False, compare=False)
    # 从持久化水合的条目（无进程内 future）——跨重启孤儿判定依据
    _hydrated: bool = field(default=False, repr=False, compare=False)

    def remaining_seconds(self) -> int:
        """功能：剩余挂起秒数（前端倒计时；已过期归零不为负）。"""
        return max(0, int(self.created_at + self.ttl_seconds - time.time()))


class InteractionStore:
    """pending 交互登记处 + asyncio 唤醒通道（单进程会话内有效）。

    AgentLoop 工具执行路径遇交互需求 → register 登记并 await get_decision（真挂起
    同一 turn 内等待，事件循环释放）；用户经 /confirm|/clarify 答复 → resolve/reject
    唤醒 future，挂起处恢复同一 turn 继续。TTL 过期惰性回收标 expired。
    """

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_INTERACTION_TTL,
        persistence: Any | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._persistence = persistence
        self._items: dict[str, PendingInteraction] = {}
        self._futures: dict[str, asyncio.Future[Any]] = {}
        self._hydrate()

    def _hydrate(self) -> None:
        """功能：启动水合——清扫过期并从持久化加载 pending（跨重启存活）。"""
        if self._persistence is None:
            return
        try:
            self._persistence.sweep_expired()
            rows = self._persistence.load_pending()
        except Exception:
            logger.warning("interaction store hydrate failed; memory-only", exc_info=True)
            return
        for row in rows:
            item = PendingInteraction(
                interaction_id=row["interaction_id"],
                session_id=row["session_id"],
                kind=row["kind"],
                payload=dict(row.get("payload") or {}),
                created_at=float(row["created_at"]),
                ttl_seconds=float(row["ttl_seconds"]),
                _hydrated=True,
            )
            self._items.setdefault(item.interaction_id, item)

    def _persist_upsert(self, item: PendingInteraction) -> None:
        if self._persistence is None:
            return
        try:
            self._persistence.upsert(item)
        except Exception:
            logger.warning("interaction persist upsert failed", exc_info=True)

    def _persist_status(self, item: PendingInteraction) -> None:
        if self._persistence is None:
            return
        try:
            self._persistence.update_status(item)
        except Exception:
            logger.warning("interaction persist status failed", exc_info=True)

    def _persist_delete(self, interaction_id: str) -> None:
        if self._persistence is None:
            return
        try:
            self._persistence.delete(interaction_id)
        except Exception:
            logger.warning("interaction persist delete failed", exc_info=True)

    def is_orphaned(self, item: PendingInteraction) -> bool:
        """功能：跨重启孤儿判定——水合条目且进程内无挂起 future（答复无处唤醒）。"""
        return bool(item._hydrated) and item.interaction_id not in self._futures

    def register(
        self,
        *,
        session_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        ttl_seconds: float | None = None,
    ) -> PendingInteraction:
        if kind not in INTERACTION_KINDS:
            raise ValueError(f"unknown interaction kind: {kind}")
        payload_dict = dict(payload or {})
        if kind == INTERACTION_KIND_PERMISSION:
            self._settle_duplicate_permissions(session_id, payload_dict)
        item = PendingInteraction(
            interaction_id=uuid.uuid4().hex,
            session_id=session_id,
            kind=kind,
            payload=payload_dict,
            ttl_seconds=self._ttl_seconds if ttl_seconds is None else float(ttl_seconds),
        )
        self._items[item.interaction_id] = item
        self._persist_upsert(item)
        return item

    def _settle_duplicate_permissions(self, session_id: str, payload: dict[str, Any]) -> None:
        """功能：作废同会话同工具同参的旧 pending 权限卡（防重试链堆叠）。

        工具失败引导重试三次会致同一命令连弹 3 张卡，
        批掉一张后其余仍挂着占满决策区。新卡登记即置换旧卡：旧卡置 expired、
        挂起方唤醒为拒绝（其执行侧本身已在重试循环，不会静默放行）。"""
        tool_name = str(payload.get("tool_name") or "")
        args_key = json.dumps(payload.get("args") or {}, sort_keys=True, ensure_ascii=False)
        for existing in list(self._items.values()):
            if (
                existing.status != _INTERACTION_STATUS_PENDING
                or existing.session_id != session_id
                or existing.kind != INTERACTION_KIND_PERMISSION
            ):
                continue
            existing_payload = existing.payload or {}
            if str(existing_payload.get("tool_name") or "") != tool_name:
                continue
            existing_key = json.dumps(
                existing_payload.get("args") or {}, sort_keys=True, ensure_ascii=False
            )
            if existing_key != args_key:
                continue
            existing.status = _INTERACTION_STATUS_EXPIRED
            existing.resolved_at = time.time()
            self._persist_status(existing)
            fut = self._futures.pop(existing.interaction_id, None)
            if fut is not None and not fut.done():
                fut.set_result(None)

    async def get_decision(self, interaction_id: str, timeout: float | None = None) -> Any:
        """等待用户决策（真挂起核心）。超时返回 None（调用方按 kind 解释为拒绝/无答复）。

        future 惰性创建：仅在挂起方（必有 running loop）进入时建，register 可在同步
        上下文登记；决策先于本调用到达（resolve/reject 早写）时直接返回 early 值。
        """
        self._expire_stale()
        item = self._items.get(interaction_id)
        if item is None:
            return None
        if item.status != _INTERACTION_STATUS_PENDING:
            # 决策已先期到达（early value）或已超时/拒绝——直接解释返回
            if item.status == _INTERACTION_STATUS_RESOLVED:
                return item._early_value
            return None
        fut = self._futures.get(interaction_id)
        if fut is None:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._futures[interaction_id] = fut
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            item.status = _INTERACTION_STATUS_EXPIRED
            item.resolved_at = time.time()
            self._futures.pop(interaction_id, None)
            self._persist_status(item)
            return None
        return result

    def resolve(self, interaction_id: str, value: Any) -> bool:
        """用户批准/输入答复 → 唤醒挂起。返回 False=未知/已结束。

        future 未建（决策先于挂起方到达）时暂存 early value，get_decision 首查即返。
        """
        item = self._items.get(interaction_id)
        if item is None or item.status != _INTERACTION_STATUS_PENDING:
            return False
        fut = self._futures.get(interaction_id)
        if fut is None:
            item._early_value = value
        else:
            if fut.done():
                return False
            fut.set_result(value)
        item.status = _INTERACTION_STATUS_RESOLVED
        item.resolved_at = time.time()
        self._futures.pop(interaction_id, None)
        self._persist_status(item)
        return True

    def reject(self, interaction_id: str) -> bool:
        """用户拒绝 → 唤醒挂起（决策=None）。返回 False=未知/已结束。"""
        item = self._items.get(interaction_id)
        if item is None or item.status != _INTERACTION_STATUS_PENDING:
            return False
        fut = self._futures.get(interaction_id)
        if fut is not None and not fut.done():
            fut.set_result(None)
            self._futures.pop(interaction_id, None)
        item.status = _INTERACTION_STATUS_REJECTED
        item.resolved_at = time.time()
        self._persist_status(item)
        return True

    def get(self, interaction_id: str) -> PendingInteraction | None:
        self._expire_stale()
        return self._items.get(interaction_id)

    def list_pending(
        self,
        session_id: str | None = None,
        kind: str | None = None,
        *,
        include_orphans: bool = True,
    ) -> list[PendingInteraction]:
        """活跃 pending（未过期、未结束）。TTL 惰性回收。

        include_orphans=False 时隐藏重启后无等待方的卡片（展示层用，避免幽灵卡）；
        持久化/恢复语义默认仍保留（include_orphans=True）。
        """
        self._expire_stale()
        out = []
        for item in self._items.values():
            if item.status != _INTERACTION_STATUS_PENDING:
                continue
            if not include_orphans and self.is_orphaned(item):
                continue
            if session_id is not None and item.session_id != session_id:
                continue
            if kind is not None and item.kind != kind:
                continue
            out.append(item)
        return out

    def _expire_stale(self) -> None:
        now = time.time()
        stale: list[str] = []
        for item in self._items.values():
            # 过期判定用 item 级 ttl（register 按实际挂起超时写入）——与 remaining_seconds()
            # 同源；用 store 级 ttl 会让短超时项（ask_confirmation ）的倒计时与真实
            if (
                item.status == _INTERACTION_STATUS_PENDING
                and now - item.created_at > item.ttl_seconds
            ):
                item.status = _INTERACTION_STATUS_EXPIRED
                item.resolved_at = now
                fut = self._futures.pop(item.interaction_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(None)  # 唤醒挂起方（决策=None）
                self._persist_status(item)
            if (
                item.status != _INTERACTION_STATUS_PENDING
                and now - (item.resolved_at or item.created_at) > self._ttl_seconds * 2
            ):
                stale.append(item.interaction_id)
        for key in stale:
            self._items.pop(key, None)
            self._persist_delete(key)


InteractionFrameCallback = Callable[[PendingInteraction], Awaitable[None]]


class HTTPUserInteraction:
    """API/桌面场景的 async 用户交互实现（真挂起通道）。

    实现 UserInteraction Protocol：ask_* 时经 InteractionStore 登记 pending →
    on_interaction 回调推送交互帧（WS/SSE）→ await get_decision（同一 turn 内真挂起
    事件循环释放可处理其他请求）→ 用户 POST /confirm|/clarify → resolve/reject 唤醒
    恢复，同一 turn 继续执行。headless 无答复超时 fail-closed（拒绝/空）。
    """

    def __init__(
        self,
        store: InteractionStore,
        session_id: str,
        on_interaction: InteractionFrameCallback | None = None,
        wait_timeout: float = _DEFAULT_INTERACTION_TTL,
        permission_ttl: float = PERMISSION_INTERACTION_TTL,
        frame_send_timeout: float = _FRAME_SEND_TIMEOUT,
        executor_session_id: str = "",
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._on_interaction = on_interaction
        self._wait_timeout = wait_timeout
        # permission 挂起窗口独立于 wait_timeout：授权卡出现在长执行轮中途，
        # 短窗口会「人回来卡已死、轮次被静默拒绝烧完」；测试可注入短值。
        self._permission_ttl = permission_ttl
        # 交互帧推送超时：实证推送阻塞会让整轮永久僵尸（executing 态零事件）。
        self._frame_send_timeout = frame_send_timeout
        # 员工执行会话 id：权限卡展示在 origin 聊天，但 session 授权必须写给执行会话。
        self._executor_session_id = executor_session_id

    async def _suspend(
        self, kind: str, payload: dict[str, Any], timeout: float | None = None
    ) -> Any | None:
        """登记 pending → 推帧 → await 决策（None=拒绝/超时/无答复）。

        推帧带超时：推送失败（客户端已离开/半开连接阻塞）= 用户永远看不到卡，
        继续等待毫无意义——立即拒绝该交互并抛 `InteractionDeliveryError`，
        调用方按拒绝/空答复 fail-closed 收口（轮次必须能推进）。
        """
        effective_timeout = self._wait_timeout if timeout is None else timeout
        item = self._store.register(
            session_id=self._session_id,
            kind=kind,
            payload=payload,
            ttl_seconds=effective_timeout,
        )
        if self._on_interaction is not None:
            # 回调可能是同步的（api/app.py 传 lambda item: handler.put(...)，返回 None）——
            # 无条件 await 会抛 TypeError，交互帧推不出去，提问链路在首次挂起就崩。
            outcome = self._on_interaction(item)
            if inspect.isawaitable(outcome):
                try:
                    await asyncio.wait_for(outcome, timeout=self._frame_send_timeout)
                except Exception as exc:
                    self._store.reject(item.interaction_id)
                    raise InteractionDeliveryError(
                        f"interaction frame delivery failed: {exc}"
                    ) from exc
        return await self._store.get_decision(item.interaction_id, timeout=effective_timeout)

    async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
        try:
            return await self._ask_permission_inner(tool_name, args, risk)
        except InteractionDeliveryError:
            return False

    async def _ask_permission_inner(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
        decision = await self._suspend(
            INTERACTION_KIND_PERMISSION,
            {
                "tool_name": tool_name,
                "args": dict(args),
                "risk": risk,
                # 分支永不命中（恒落两态 fallback）。此标记让前端渲染三态按钮
                # 决策经 confirm 端点的 scope 参数（once/session）落到真实机制。
                "ui_variant": "permission",
                "executor_session_id": self._executor_session_id,
            },
            timeout=self._permission_ttl,
        )
        # 会把「拒绝+理由」误当批准
        if isinstance(decision, dict):
            return bool(decision.get("granted"))
        return bool(decision)

    async def ask_permission_with_feedback(
        self, tool_name: str, args: dict[str, Any], risk: str
    ) -> tuple[bool, str]:
        """拒绝反馈通道——resolve 传 dict `{granted, feedback}` 时解析理由
        （桌面端可据此回填用户拒绝原因；bool/None 等旧形态按无理由处理）。"""
        try:
            return await self._ask_permission_with_feedback_inner(tool_name, args, risk)
        except InteractionDeliveryError:
            return False, ""

    async def _ask_permission_with_feedback_inner(
        self, tool_name: str, args: dict[str, Any], risk: str
    ) -> tuple[bool, str]:
        decision = await self._suspend(
            INTERACTION_KIND_PERMISSION,
            {
                "tool_name": tool_name,
                "args": dict(args),
                "risk": risk,
                "ui_variant": "permission",
                "executor_session_id": self._executor_session_id,
            },
            timeout=self._permission_ttl,
        )
        if isinstance(decision, dict):
            return bool(decision.get("granted")), str(decision.get("feedback") or "")
        return bool(decision), ""

    async def ask_permission_batch(
        self, items: list[dict[str, Any]], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """功能：单卡聚合多次高危调用——一次决策应用到全部（items 全量随帧下发）。"""
        if not items:
            return {"granted": False, "scope": "once", "feedback": ""}
        first = items[0] if isinstance(items[0], dict) else {}
        decision = await self._suspend(
            INTERACTION_KIND_PERMISSION,
            {
                "ui_variant": "permission",
                "executor_session_id": self._executor_session_id,
                "batch": True,
                "items": [dict(item) for item in items if isinstance(item, dict)],
                "tool_name": str(first.get("tool_name") or ""),
                "args": dict(first.get("args") or {}),
                "risk": str(first.get("risk") or "medium"),
            },
            timeout=self._permission_ttl if timeout is None else timeout,
        )
        if isinstance(decision, dict):
            return {
                "granted": bool(decision.get("granted")),
                "scope": str(decision.get("scope") or "once"),
                "feedback": str(decision.get("feedback") or ""),
            }
        return {"granted": bool(decision), "scope": "once", "feedback": ""}

    async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool:
        try:
            return await self._ask_confirmation_inner(prompt, timeout)
        except InteractionDeliveryError:
            return False

    async def _ask_confirmation_inner(self, prompt: str, timeout: float) -> bool:
        decision = await self._suspend(
            INTERACTION_KIND_CONFIRM, {"prompt": prompt}, timeout=min(timeout, self._wait_timeout)
        )
        if isinstance(decision, dict):
            return bool(decision.get("granted"))
        return bool(decision)

    async def ask_input(
        self,
        prompt: str,
        *,
        secret: bool = False,
        choices: list[str] | None = None,
        multi_select: bool = False,
        allow_custom: bool = False,
    ) -> str:
        """文本输入挂起；`choices` ≥2 时前端渲染**选项卡**（点选代替打字）。

        此前 payload 只有 prompt/secret，前端 `payload.choices` 恒空
        → 单问场景永远只出纯文本框，多选项卡无法触发。
        """
        payload: dict[str, Any] = {"prompt": prompt, "secret": secret}
        cleaned = [str(item).strip() for item in (choices or []) if str(item).strip()]
        if len(cleaned) >= 2:
            payload["choices"] = cleaned
            if multi_select:
                payload["multi_select"] = True
            if allow_custom:
                payload["allow_custom"] = True
        try:
            decision = await self._suspend(INTERACTION_KIND_INPUT, payload)
        except InteractionDeliveryError:
            return ""
        return decision if isinstance(decision, str) else ""

    async def ask_questions(self, questions: list[dict[str, Any]]) -> dict[str, str]:
        """多决策点一次挂起（-3）：payload.questions 数组 → 前端 <1/N> 分页逐点作答
        汇总 answers JSON 经 /clarify 回传；解析失败/拒绝/超时按空答复（answers={}）。"""
        try:
            decision = await self._suspend(
                INTERACTION_KIND_INPUT,
                {"questions": [dict(item) for item in questions]},
            )
        except InteractionDeliveryError:
            return {}
        if isinstance(decision, str) and decision.strip():
            try:
                parsed = json.loads(decision)
            except ValueError:
                return {}
            if isinstance(parsed, dict):
                return {str(key): str(value) for key, value in parsed.items()}
        return {}

    async def show_notification(self, message: str, level: str = "info") -> None:
        item = self._store.register(
            session_id=self._session_id,
            # -显式 notify 类型（不再冒用 permission——前端按 kind 分流
            # payload.notify 保留作旧前端兼容标记）
            kind=INTERACTION_KIND_NOTIFY,
            payload={"message": message, "level": level, "notify": True},
        )
        # 通知不挂起——立即标记结束，仅推帧
        self._store.reject(item.interaction_id)
        if self._on_interaction is not None:
            # 回调可能是同步的（api/app.py 传 lambda item: handler.put(...)，返回 None）——
            # 无条件 await 会抛 TypeError，交互帧推不出去，提问链路在首次挂起就崩。
            outcome = self._on_interaction(item)
            if inspect.isawaitable(outcome):
                await outcome


class AutoApproveInteraction:
    """功能：无人值守执行——权限与确认一律放行，文本输入返回空串，通知丢弃。"""

    async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
        return True

    async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool:
        return True

    async def ask_input(self, prompt: str, secret: bool = False) -> str:
        return ""

    async def show_notification(self, message: str, level: str = "info") -> None:
        return None
