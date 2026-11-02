"""连接静态权限检查与 UI 审批卡片：需要审批的工具暂停 turn
（AWAITING_APPROVAL），呈现 Allow/Deny 卡片，用户决策后恢复。

组成：
- ApprovalStore：per-session 内存态——审批策略（untrusted/on-request/never）
  会话级/一次性授权（绑定 user/role/project/policy_version 维度）
  暂停中的 pending 请求队列（并行多槽，容量上限）（线程安全）
- parse_approval_decision：Allow/Deny 回复归一化（中英文 token + JSON 形态）
- approval_reply_content / build_approval_question：决策回填体与问题卡片

进程级单例经 get_approval_store() 获取；策略/授权/pending 均持久化恢复
重启不丢（fail-safe 语义由决策链承担而非丢弃 pending）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from bandi.core.events import Event, EventType

logger = logging.getLogger(__name__)

ApprovalDecision = Literal["allow_once", "allow_session", "deny"]

_VALID_POLICIES: frozenset[str] = frozenset({"untrusted", "on-request", "never"})

APPROVAL_REQUIRED_PREFIX = "APPROVAL_REQUIRED:"

APPROVAL_QUESTION_PREFIX = "approval::"

_PENDING_MAX_PER_SESSION = 5

# pending 存活上限：超过即视为作废（与「等待授权的审批窗口 30 分钟」同口径）。
# 过期只在读取/入队时清理，不会自动重放——用户错过窗口后由重试入口重新发起。
_PENDING_TTL_SECONDS = 1800.0

_MAX_SESSION_BUS_BINDINGS = 512

# 过期后需重新审批）
_GRANT_TTL_SECONDS = 86400.0
# once 授权窗口：仅本次确认=单次同工具全部调用——
# 「单次」以执行轮近似：窗口内同工具任意参数放行，过期后重新弹卡。
_ONCE_GRANT_TTL_SECONDS = 900.0


def default_approval_policy() -> str:
    raw = os.environ.get("BANDI_APPROVAL_POLICY", "").strip().lower()
    if raw in _VALID_POLICIES:
        return raw
    return "on-request"


def params_digest(params: dict[str, Any] | None) -> str:
    # 功能：工具参数稳定短摘要（授权匹配与审计用）
    try:
        blob = json.dumps(params or {}, sort_keys=True, default=str)[:20_000]
    except Exception:  # noqa: BLE001
        blob = repr(params)[:20_000]
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class PendingApproval:
    # 功能：暂停 turn 的审批请求
    tool_call_id: str
    tool_name: str
    params_digest: str
    reason: str
    detail: str = ""
    rule_pattern: str = ""
    rule_source: str = ""
    created_at: float = field(default_factory=time.time)
    # 服务端不可变请求快照 + 决策状态机。全部带默认值——旧盘 JSON
    # 经 _load 的 PendingApproval(**item) 反序列化时不会因缺字段抛 TypeError。
    params: dict[str, Any] | None = None
    result_receipt: str = ""
    consumed: bool = False
    decision: str = ""


def _pending_created_at(item: PendingApproval) -> float:
    # 功能：pending 创建时间（脏值按 0 处理＝过期，防旧盘字段形态让清理抛错）
    try:
        return float(getattr(item, "created_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _pending_items(items: object) -> list[object]:
    # 升级后包装为单元素 list（多槽格式）
    if isinstance(items, list):
        return items
    if isinstance(items, dict):
        return [items]
    return []


class ApprovalStore:
    # 功能：per-session 审批状态（策略/授权/pending 请求，授权键绑定身份维度）
    def __init__(self, storage_path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._storage_path = Path(storage_path).expanduser() if storage_path else None
        self._policies: dict[str, str] = {}
        self._reviewers: dict[str, str] = {}
        self._session_grants: dict[str, set[tuple[str, str]]] = {}
        self._once_grants: dict[str, set[tuple[str, str]]] = {}
        self._grant_ts: dict[tuple[str, str, str], float] = {}
        self._grant_wall: dict[tuple[str, str, str], float] = {}
        self._pending: dict[str, list[PendingApproval]] = {}
        # 人工决策事件路由：进程级单例 vs 每 run 新建的 EventBus——按会话绑定防串扰
        self._event_bus: Any = None
        # 会话 → EventBus 路由表：OrderedDict 保插入序供 LRU 淘汰。用独立
        # `_routing_lock`（不复用 `_lock`）——本表是路由设施，不应与策略/pending
        # 的持久化事务互相阻塞。
        self._session_buses: OrderedDict[str, Any] = OrderedDict()
        self._routing_lock = threading.Lock()
        self._load()

    def bind_event_bus(self, bus: Any, session_id: str = "") -> None:
        """功能：绑定事件总线（run 装配点调用）——人工决策事件按会话路由。

        ApprovalStore 为进程级单例而 EventBus 每 run 新建：按 session_id 记录路由
        防多会话并发时决策事件串到别的会话审计链；未提供会话 id 时仅记最近总线。
        """
        sid = (session_id or "").strip()
        with self._routing_lock:
            self._event_bus = bus
            if not sid:
                return
            # 逐条 LRU 淘汰最旧（不是整张 clear）——重绑同一会话时先摘旧位再入队尾
            # 保持"最近活跃者不被淘汰"。
            self._session_buses.pop(sid, None)
            while len(self._session_buses) >= _MAX_SESSION_BUS_BINDINGS:
                self._session_buses.popitem(last=False)
            self._session_buses[sid] = bus

    async def emit_decision_event(
        self,
        decision: str,
        session_id: str,
        tool_name: str,
        *,
        params_digest: str = "",
        scope: str = "",
    ) -> None:
        """功能：人工决策事件发射（审计用途；失败不阻断决策主链路）。"""
        with self._routing_lock:
            bus = self._session_buses.get(str(session_id)) or self._event_bus
        if bus is None:
            return
        event_type = EventType.APPROVAL_DENIED if decision == "deny" else EventType.APPROVAL_GRANTED
        try:
            await bus.emit(
                Event(
                    type=event_type,
                    data={
                        "tool_name": tool_name,
                        "params_digest": params_digest,
                        "scope": scope,
                        "decision": decision,
                        "decided_by": "user",
                        "session_id": str(session_id),
                    },
                )
            )
        except Exception:  # noqa: BLE001 — 审计事件失败不得阻断决策
            pass

    @staticmethod
    def _sid_key(
        session_id: str,
        user_id: str = "",
        role: str = "",
        project_id: str = "",
        policy_version: str = "",
    ) -> str:
        # 功能：授权存储键——身份维度复合键（\x1f 单元分隔防 sid 含 | 干扰）；
        # 全空维度 = 旧签名兼容键（无分隔符）
        dims = [session_id, user_id, role, project_id, policy_version]
        if not any(dims[1:]):
            return session_id
        return "\x1f".join(dims)

    def _load(self) -> None:
        if self._storage_path is None or not self._storage_path.exists():
            return
        try:
            data = json.loads(self._storage_path.read_text(encoding="utf-8"))
            self._policies = {str(k): str(v) for k, v in data.get("policies", {}).items()}
            self._reviewers = {str(k): str(v) for k, v in data.get("reviewers", {}).items()}
            self._session_grants = {
                str(sid): {tuple(item) for item in grants if len(item) == 2}
                for sid, grants in data.get("session_grants", {}).items()
            }
            self._once_grants = {
                str(sid): {tuple(item) for item in grants if len(item) == 2}
                for sid, grants in data.get("once_grants", {}).items()
            }
            self._pending = {
                str(sid): [
                    PendingApproval(**item)
                    for item in _pending_items(items)
                    if isinstance(item, dict)
                ]
                for sid, items in data.get("pending", {}).items()
            }
            now = time.monotonic()
            now_wall = time.time()
            stored_wall = {
                (str(item[0]), str(item[1]), str(item[2])): float(item[3])
                for item in data.get("grant_wall", [])
                if isinstance(item, list) and len(item) == 4
            }
            for grants in (self._session_grants, self._once_grants):
                for sid, items in grants.items():
                    for tool_name, digest in items:
                        key = (sid, tool_name, digest)
                        self._grant_ts[key] = now
                        self._grant_wall[key] = stored_wall.get(key, now_wall)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._policies = {}
            self._reviewers = {}
            self._session_grants = {}
            self._once_grants = {}
            self._pending = {}

    def _persist_locked(self) -> None:
        if self._storage_path is None:
            return
        payload = {
            "policies": self._policies,
            "reviewers": self._reviewers,
            "session_grants": {
                sid: [list(item) for item in grants] for sid, grants in self._session_grants.items()
            },
            "once_grants": {
                sid: [list(item) for item in grants] for sid, grants in self._once_grants.items()
            },
            "grant_wall": [
                [sid, tool_name, digest, granted_at]
                for (sid, tool_name, digest), granted_at in self._grant_wall.items()
            ],
            "pending": {
                sid: [
                    {
                        "tool_call_id": item.tool_call_id,
                        "tool_name": item.tool_name,
                        "params_digest": item.params_digest,
                        "reason": item.reason,
                        "detail": item.detail,
                        "rule_pattern": item.rule_pattern,
                        "rule_source": item.rule_source,
                        "created_at": item.created_at,
                        "params": item.params,
                        "result_receipt": item.result_receipt,
                        "consumed": item.consumed,
                        "decision": item.decision,
                    }
                    for item in items
                ]
                for sid, items in self._pending.items()
            },
        }
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._storage_path.with_suffix(self._storage_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(self._storage_path)

    def set_policy(self, session_id: str, policy: str) -> None:
        policy = (policy or "").strip().lower()
        if policy not in _VALID_POLICIES:
            raise ValueError(f"Unknown approval policy {policy!r}")
        with self._lock:
            self._policies[str(session_id)] = policy
            self._persist_locked()

    def get_policy(self, session_id: str | None) -> str:
        if session_id is None:
            return default_approval_policy()
        with self._lock:
            return self._policies.get(str(session_id), default_approval_policy())

    def set_reviewer(self, session_id: str, reviewer: str) -> None:
        from bandi.tools.auto_review import normalize_reviewer

        normalized = normalize_reviewer(reviewer)
        with self._lock:
            self._reviewers[str(session_id)] = normalized
            self._persist_locked()

    def get_reviewer(self, session_id: str | None) -> str:
        from bandi.tools.auto_review import default_reviewer

        if session_id is None:
            return default_reviewer()
        with self._lock:
            return self._reviewers.get(str(session_id), default_reviewer())

    def grant(
        self,
        session_id: str,
        tool_name: str,
        *,
        scope: Literal["once", "session"],
        params_digest: str = "",
        user_id: str = "",
        role: str = "",
        project_id: str = "",
        policy_version: str = "",
    ) -> None:
        sid = self._sid_key(session_id, user_id, role, project_id, policy_version)
        grant = (tool_name, params_digest)
        with self._lock:
            self._purge_expired_locked()
            if scope == "session":
                self._session_grants.setdefault(sid, set()).add(grant)
            else:
                self._once_grants.setdefault(sid, set()).add(grant)
            self._grant_ts[(sid, tool_name, params_digest)] = time.monotonic()
            self._grant_wall[(sid, tool_name, params_digest)] = time.time()
            self._persist_locked()

    def _purge_expired_locked(self) -> None:
        """功能：6.13 过期授权清理——TTL 外的 grants 移除并同步时间戳账；
        once 用短窗口（单次执行轮近似）、session 用长窗口。"""
        now = time.monotonic()
        removed = False
        for grants, ttl in (
            (self._session_grants, _GRANT_TTL_SECONDS),
            (self._once_grants, _ONCE_GRANT_TTL_SECONDS),
        ):
            for sid in list(grants):
                kept: set[tuple[str, str]] = set()
                for g in grants[sid]:
                    ts = self._grant_ts.get((sid, g[0], g[1]))
                    # >= 语义：TTL=0 即过期（Windows monotonic 15.6ms 同 tick 时
                    # now-ts==0，严格 > 判不出）；正常 TTL 等价
                    if ts is not None and now - ts >= ttl:
                        self._grant_ts.pop((sid, g[0], g[1]), None)
                        self._grant_wall.pop((sid, g[0], g[1]), None)
                        removed = True
                        continue
                    kept.add(g)
                if kept:
                    grants[sid] = kept
                else:
                    grants.pop(sid, None)
        if removed:
            self._persist_locked()

    def is_granted(
        self,
        session_id: str | None,
        tool_name: str,
        params_digest: str = "",
        user_id: str = "",
        role: str = "",
        project_id: str = "",
        policy_version: str = "",
    ) -> bool:
        # 功能：检查既有授权（one-shot 消费即焚；身份维度精确匹配；6.13 TTL）
        if session_id is None:
            return False
        sid = self._sid_key(session_id, user_id, role, project_id, policy_version)
        with self._lock:
            self._purge_expired_locked()
            grants = self._session_grants.get(sid, ())
            if (
                (tool_name, params_digest) in grants
                or (tool_name, "") in grants
                or (not params_digest and any(name == tool_name for name, _ in grants))
            ):
                return True
            once = self._once_grants.get(sid)
            exact = (tool_name, params_digest)
            wildcard = (tool_name, "")
            compatible = exact if exact in (once or ()) else wildcard
            if not params_digest and once:
                compatible = next((item for item in once if item[0] == tool_name), compatible)
            if once and compatible in once:
                # once 语义：仅本次确认=单次（本轮）同工具全部调用放行——
                # 不再消费即焚，由 _ONCE_GRANT_TTL_SECONDS 窗口到期回收。
                return True
        return False

    def is_once_granted(
        self,
        session_id: str | None,
        tool_name: str,
        params_digest: str = "",
        user_id: str = "",
        role: str = "",
        project_id: str = "",
        policy_version: str = "",
    ) -> bool:
        """仅查 once 授权——审批回执只对「仅本次」生效。

        session 授权是「本会话信任该工具」，重复调用是预期行为，不得被回执
        吞掉；once 授权才代表「这一次调用已批准」，二次重放应返回同一回执。
        """
        if session_id is None:
            return False
        sid = self._sid_key(session_id, user_id, role, project_id, policy_version)
        with self._lock:
            self._purge_expired_locked()
            once = self._once_grants.get(sid)
            if not once:
                return False
            if params_digest:
                return (tool_name, params_digest) in once
            return any(name == tool_name for name, _ in once)

    def list_grants(self) -> list[dict[str, Any]]:
        """功能：列出全部 always-allow（session/once），供设置页查看与撤销"""
        now_wall = time.time()
        with self._lock:
            self._purge_expired_locked()
            items: list[dict[str, Any]] = []
            for scope, grants, ttl in (
                ("session", self._session_grants, _GRANT_TTL_SECONDS),
                ("once", self._once_grants, _ONCE_GRANT_TTL_SECONDS),
            ):
                for sid, pairs in grants.items():
                    for tool_name, digest in pairs:
                        granted_at = self._grant_wall.get((sid, tool_name, digest), now_wall)
                        expires_at = granted_at + ttl
                        items.append(
                            {
                                "session_key": sid,
                                "tool_name": tool_name,
                                "params_digest": digest,
                                "scope": scope,
                                "granted_at": granted_at,
                                "expires_at": expires_at,
                                "remaining_s": max(0, int(expires_at - now_wall)),
                            }
                        )
        return sorted(
            items, key=lambda item: (item["scope"], item["tool_name"], item["session_key"])
        )

    def revoke_grant(
        self,
        session_key: str,
        tool_name: str,
        params_digest: str,
        scope: str,
    ) -> bool:
        """功能：撤销一条 always-allow；不存在返回 False（幂等）"""
        sid = str(session_key or "")
        target = self._session_grants if scope == "session" else self._once_grants
        grant = (str(tool_name or ""), str(params_digest or ""))
        with self._lock:
            items = target.get(sid)
            if not items or grant not in items:
                return False
            items.discard(grant)
            if not items:
                target.pop(sid, None)
            self._grant_ts.pop((sid, grant[0], grant[1]), None)
            self._grant_wall.pop((sid, grant[0], grant[1]), None)
            self._persist_locked()
        return True

    def set_pending(self, session_id: str, request: PendingApproval) -> None:
        with self._lock:
            self._purge_expired_pending_locked()
            queue = self._pending.setdefault(str(session_id), [])
            if len(queue) >= _PENDING_MAX_PER_SESSION:
                evicted = queue.pop(0)
                logger.warning(
                    "approval pending queue full (%d): evicted oldest tool_call_id=%s session=%s",
                    _PENDING_MAX_PER_SESSION,
                    evicted.tool_call_id,
                    session_id,
                )
            queue.append(request)
            self._persist_locked()

    def list_pending(self, session_id: str | None) -> list[PendingApproval]:
        if session_id is None:
            return []
        with self._lock:
            self._purge_expired_pending_locked()
            return list(self._pending.get(str(session_id), []))

    def list_all_pending(self) -> list[tuple[str, PendingApproval]]:
        # 功能：列全部会话 pending（员工卡审批入口数据源——员工执行
        # headless 落 pending 后前端无从感知，本接口给列表/决策入口供数）
        with self._lock:
            self._purge_expired_locked()
            self._purge_expired_pending_locked()
            out: list[tuple[str, PendingApproval]] = []
            for sid, queue in self._pending.items():
                out.extend((sid, item) for item in queue)
            return out

    def get_pending(
        self, session_id: str | None, tool_call_id: str | None = None
    ) -> PendingApproval | None:
        """功能：取 pending——给了 tool_call_id 精确命中该条，否则取队首。"""
        if session_id is None:
            return None
        with self._lock:
            self._purge_expired_pending_locked()
            queue = self._pending.get(str(session_id))
            if not queue:
                return None
            if tool_call_id is not None:
                return next((p for p in queue if p.tool_call_id == tool_call_id), None)
            return queue[0]

    def pop_pending(
        self,
        session_id: str | None,
        *,
        tool_call_id: str | None = None,
    ) -> PendingApproval | None:
        if session_id is None:
            return None
        sid = str(session_id)
        with self._lock:
            self._purge_expired_pending_locked()
            queue = self._pending.get(sid)
            if not queue:
                return None
            if tool_call_id is not None:
                idx = next((i for i, p in enumerate(queue) if p.tool_call_id == tool_call_id), -1)
                if idx < 0:
                    return None
                result = queue.pop(idx)
            else:
                result = queue.pop(0)
            if not queue:
                self._pending.pop(sid, None)
            self._persist_locked()
            return result

    def _purge_expired_pending_locked(self) -> None:
        """功能：清理超 TTL 的 pending（作废不再可决策；丢弃数量入日志）"""
        if not self._pending:
            return
        cutoff = time.time() - _PENDING_TTL_SECONDS
        dropped = 0
        for sid in list(self._pending):
            queue = self._pending[sid]
            alive = [p for p in queue if _pending_created_at(p) > cutoff]
            if len(alive) == len(queue):
                continue
            dropped += len(queue) - len(alive)
            if alive:
                self._pending[sid] = alive
            else:
                self._pending.pop(sid, None)
        if dropped:
            logger.warning(
                "approval pending expired: dropped %d item(s) older than %.0fs",
                dropped,
                _PENDING_TTL_SECONDS,
            )
            self._persist_locked()

    def cascade_pending_grants(
        self,
        session_id: str,
        tool_name: str,
        *,
        scope: Literal["once", "session"] = "session",
    ) -> list[PendingApproval]:
        """always 级联——allow_session 后同 session 同工具 pending 逐条继承
        授权（各自 params_digest 精确授予）并移出队列（opencode always 级联适配；
        不同工具/不同参数互不越权）。返回被级联条目。"""
        sid = self._sid_key(session_id)
        with self._lock:
            queue = self._pending.get(str(session_id))
            if not queue:
                return []
            self._purge_expired_locked()
            target = self._session_grants if scope == "session" else self._once_grants
            cascaded: list[PendingApproval] = []
            kept: list[PendingApproval] = []
            for item in queue:
                if item.tool_name != tool_name:
                    kept.append(item)
                    continue
                target.setdefault(sid, set()).add((item.tool_name, item.params_digest))
                self._grant_ts[(sid, item.tool_name, item.params_digest)] = time.monotonic()
                cascaded.append(item)
            if kept:
                self._pending[str(session_id)] = kept
            else:
                self._pending.pop(str(session_id), None)
            if cascaded:
                self._persist_locked()
            return cascaded

    def clear_session(self, session_id: str) -> None:
        sid = str(session_id)
        with self._lock:
            for table in (
                self._policies,
                self._reviewers,
                self._session_grants,
                self._once_grants,
                self._pending,
            ):
                table.pop(sid, None)
            for key in [key for key in self._grant_wall if key[0] == sid]:
                self._grant_wall.pop(key, None)
                self._grant_ts.pop(key, None)
            self._persist_locked()
        with self._routing_lock:
            self._session_buses.pop(sid, None)

    def clear_all(self) -> None:
        with self._lock:
            self._policies.clear()
            self._reviewers.clear()
            self._session_grants.clear()
            self._once_grants.clear()
            self._grant_ts.clear()
            self._grant_wall.clear()
            self._pending.clear()
            self._persist_locked()


_STORE: ApprovalStore | None = None


def get_approval_store() -> ApprovalStore:
    global _STORE
    if _STORE is None:
        path = os.environ.get("BANDI_APPROVAL_STORE_PATH", "~/.bandi/approvals.json")
        _STORE = ApprovalStore(path)
    return _STORE


def reset_approval_store() -> None:
    # 功能：测试钩子——重置进程级单例
    global _STORE
    if _STORE is not None:
        _STORE.clear_all()
    else:
        path = Path(
            os.environ.get("BANDI_APPROVAL_STORE_PATH", "~/.bandi/approvals.json")
        ).expanduser()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    _STORE = None


# ---------------------------------------------------------------------------
# 决策解析 + 回填补全
# ---------------------------------------------------------------------------

_ALLOW_ONCE_TOKENS = frozenset(
    {
        "allow",
        "allow once",
        "allow_once",
        "approve",
        "approved",
        "yes",
        "允许",
        "允许一次",
        "批准",
    }
)
_ALLOW_SESSION_TOKENS = frozenset(
    {
        "allow for session",
        "allow_session",
        "always allow",
        "allow always",
        "本会话允许",
        "始终允许",
    }
)
_DENY_TOKENS = frozenset(
    {
        "deny",
        "denied",
        "reject",
        "rejected",
        "no",
        "拒绝",
        "不允许",
    }
)


def parse_approval_decision(content: str | None) -> ApprovalDecision | None:
    # 功能：把用户审批卡回复映射为决策（中英文 token + JSON 形态）
    raw = (content or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            cand = data.get("decision") or data.get("answer") or data.get("value")
            if isinstance(cand, str):
                raw = cand
            elif isinstance(data.get("answers"), dict) and data["answers"]:
                first = next(iter(data["answers"].values()))
                if isinstance(first, str):
                    raw = first
    token = raw.strip().lower()
    if token in _ALLOW_SESSION_TOKENS:
        return "allow_session"
    if token in _ALLOW_ONCE_TOKENS:
        return "allow_once"
    if token in _DENY_TOKENS:
        return "deny"
    return None


async def apply_decision(
    store: ApprovalStore,
    session_id: str,
    decision: ApprovalDecision,
    tool_name: str,
    tool_call_id: str | None = None,
) -> str:
    # 功能：决策落库并产出回填消息体（API 决策端点生产路径）——
    pending = store.pop_pending(session_id, tool_call_id=tool_call_id)
    # .2：连续拒绝追踪——人工决策的唯一落点在此，记账也收在这一处。
    from bandi.safety.denial_tracker import get_denial_tracker

    tracker = get_denial_tracker()
    if decision == "deny":
        tracker.record_denial(session_id, tool_name)
        await store.emit_decision_event(
            "deny",
            session_id,
            tool_name,
            params_digest=pending.params_digest if pending is not None else "",
        )
        return approval_reply_content(decision, tool_name)
    tracker.record_approval(session_id, tool_name)
    if pending is None:
        # 无待决请求 = 已消费/重复决策——fail-closed，不写任何授权。
        # 此前会以空 digest 落 once wildcard，双击批准即退化为工具级放权。
        logger.warning(
            "approval decision without pending: session=%s tool=%s decision=%s",
            session_id,
            tool_name,
            decision,
        )
        return approval_reply_content(decision, tool_name)
    pending_digest = pending.params_digest
    pending.consumed = True
    pending.decision = decision
    # 混合粒度：allow_session=本会话信任该工具（工具级 wildcard，换参仍命中）；
    # allow_once=仅本次这一条具体调用（写真实 params_digest，改参数即重新审批）。
    # once 仍由短窗口（_ONCE_GRANT_TTL_SECONDS）到期回收。
    store.grant(
        session_id,
        tool_name,
        scope="session" if decision == "allow_session" else "once",
        params_digest="" if decision == "allow_session" else pending_digest,
    )
    await store.emit_decision_event(
        decision,
        session_id,
        tool_name,
        params_digest=pending_digest,
        scope="session" if decision == "allow_session" else "once",
    )
    return approval_reply_content(decision, tool_name)


def approval_reply_content(decision: ApprovalDecision, tool_name: str, feedback: str = "") -> str:
    # 功能：决策后模型可见的工具行内容——re-issue 或放弃；拒绝理由回灌
    if decision == "deny":
        note = (
            f"The user DENIED running `{tool_name}`. Do not retry this "
            "call. Explain the situation or propose an alternative."
        )
        if feedback:
            note += f" User feedback: {feedback}"
        return json.dumps({"approval_decision": "deny", "note": note})
    scope = "for this session" if decision == "allow_session" else "once"
    return json.dumps(
        {
            "approval_decision": decision,
            "note": (
                f"The user APPROVED running `{tool_name}` ({scope}). "
                "Re-issue the exact same tool call now; it will execute."
            ),
        }
    )


def build_approval_question(
    *,
    tool_call_id: str,
    tool_name: str,
    reason: str,
    detail: str = "",
    rule_pattern: str = "",
    rule_source: str = "",
) -> dict[str, Any]:
    # 功能：合成 ask_user 形态的权限问题（UI 卡片数据源）
    return {
        "id": f"{APPROVAL_QUESTION_PREFIX}{tool_call_id}",
        "prompt": build_permission_prompt(tool_name),
        "choices": ["Allow once", "Allow for session", "Deny"],
        "allow_custom": False,
        "multi_select": False,
        "ui_variant": "permission",
        "permission_kind": "tool_run",
        "detail": (detail or reason)[:500],
        "rule_pattern": rule_pattern,
        "rule_source": rule_source,
        "primary_choice": "Allow once",
        "secondary_choice": "Deny",
    }


# 结构化写入工具的中文意图说明（权限卡 prompt 与 detail 用）：
# 卡片必须说清「调用什么工具、提取什么内容、写入哪里」——泛化英文 prompt
# 让用户在不知情下批准结构化写入。
_TOOL_INTENT_PROMPTS: dict[str, str] = {
    "ontology_define": "允许 Agent 调用 `ontology_define` 向本体库声明词汇（类与关系）吗？",
    "ontology_assert": "允许 Agent 调用 `ontology_assert` 向本体库写入实体与事实吗？",
    "ontology_retract": "允许 Agent 调用 `ontology_retract` 从本体库删除实体或事实吗？",
    "memory_assert": "允许 Agent 调用 `memory_assert` 向长期记忆库写入结构化三元组吗？",
}

_TOOL_INTENT_LABELS: dict[str, str] = {
    "ontology_define": "声明本体词汇（类与关系定义）",
    "ontology_assert": "向本体库写入实体与事实（结构化提取）",
    "ontology_retract": "从本体库撤回实体或事实（删除操作）",
    "memory_assert": "向长期记忆写入结构化三元组",
}


def build_permission_prompt(tool_name: str) -> str:
    # 功能：权限卡 prompt——结构化写入工具用中文写明目标，其余保持原形态
    return _TOOL_INTENT_PROMPTS.get(tool_name, f"Allow the agent to run `{tool_name}`?")


def _clip(text: str, limit: int = 60) -> str:
    # 功能：卡片说明单项截断（防超长内容撑爆卡片）
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def approval_params_preview(params: dict[str, Any] | None, *, limit: int = 500) -> dict[str, Any]:
    # 功能：审批卡参数预览——敏感键打码、长值截断，供前端命令/路径。
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        name = str(key)
        if any(
            token in name.lower() for token in ("key", "token", "password", "secret", "credential")
        ):
            out[name] = "***"
            continue
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        out[name] = _clip(str(rendered), limit)
    return out


def describe_tool_intent(tool_name: str, params: dict[str, Any]) -> str:
    # 功能：把结构化写入工具的参数渲染成人读说明——调用什么工具、提取什么内容。
    # 非结构化写入工具返回空串（不给卡片塞无意义文案）。
    if tool_name in ("bash", "python_exec"):
        raw = params.get("command") or params.get("code") or ""
        return f"运行命令：{_clip(str(raw), 200)}"
    if tool_name in ("write", "edit", "apply_patch"):
        path = params.get("path") or params.get("file_path") or ""
        content = params.get("content") or params.get("patch") or ""
        return f"修改文件：{_clip(str(path), 120)}（内容 {len(str(content))} 字符）"
    label = _TOOL_INTENT_LABELS.get(tool_name)
    if label is None:
        return ""
    params = params or {}
    lines: list[str] = [f"调用工具 `{tool_name}`：{label}。"]
    if tool_name == "ontology_define":
        classes = [str(c.get("id", "")) for c in params.get("classes") or [] if isinstance(c, dict)]
        relations = [
            str(r.get("id", "")) for r in params.get("relations") or [] if isinstance(r, dict)
        ]
        if classes:
            lines.append("声明类：" + "、".join(_clip(c) for c in classes))
        if relations:
            lines.append("声明关系：" + "、".join(_clip(r) for r in relations))
    elif tool_name == "ontology_assert":
        entities = [
            str(e.get("id", "")) for e in params.get("entities") or [] if isinstance(e, dict)
        ]
        if entities:
            lines.append("写入实体：" + "、".join(_clip(e) for e in entities))
        facts = [
            f"{f.get('subject', '')}·{f.get('predicate', '')}·{f.get('object', '')}"
            for f in params.get("facts") or []
            if isinstance(f, dict)
        ]
        if facts:
            lines.append("写入事实：" + "；".join(_clip(f) for f in facts))
    elif tool_name == "ontology_retract":
        for key, header in (
            ("terms", "撤回词表项"),
            ("entities", "撤回实体"),
        ):
            values = [str(v) for v in params.get(key) or []]
            if values:
                lines.append(f"{header}：" + "、".join(_clip(v) for v in values))
        facts = [
            f"{f.get('subject', '')}·{f.get('predicate', '')}·{f.get('object', '')}"
            for f in params.get("facts") or []
            if isinstance(f, dict)
        ]
        if facts:
            lines.append("撤回事实：" + "；".join(_clip(f) for f in facts))
    elif tool_name == "memory_assert":
        subject = _clip(str(params.get("subject", "")))
        predicate = _clip(str(params.get("predicate", "")))
        obj = _clip(str(params.get("object", "")))
        lines.append(f"写入三元组：{subject}·{predicate}·{obj}")
    return "\n".join(lines)
