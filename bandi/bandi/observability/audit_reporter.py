# 功能：审计报告（过滤/聚合/导出审计记录）
from __future__ import annotations

import hashlib
import json
import os
import time as time_module
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.core.events import EventType
from bandi.memory.session.store import SessionStore
from bandi.observability.audit_chain import (
    compute_entry_hmac,
    resolve_audit_hmac_key,
    verify_entry_hmac,
)
from bandi.observability.context import current_log_context


def _truncate_to(path: Path, size: int) -> None:
    """把文件截断回指定字节数。失败就静默忽略（回滚区报告里不抛出在恢复原始异常）。"""
    try:
        with open(path, "r+", encoding="utf-8") as f:
            f.truncate(size)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def _rollback_append(path: Path, original_size: int, line: str) -> None:
    """条件回滚：仅当文件尾部恰为本行内容（或其前缀，即半行）时才截断。

    无条件截断会误伤并发写者——多进程（API+CLI 共用审计链）场景下，回滚窗口内
    他方已 fsync 的记录会被一并截掉。检测到尾部含他方内容时
    保守不动：宁可留半行由 `verify()` 报出，也不静默删除他人记录。
    """
    try:
        if path.stat().st_size <= original_size:
            return
        with open(path, "rb") as f:
            f.seek(original_size)
            tail = f.read()
    except OSError:
        return
    if not line.encode("utf-8").startswith(tail):
        return
    _truncate_to(path, original_size)


@dataclass
class AuditFilter:
    time_from: str | None = None
    time_to: str | None = None
    event_types: list[EventType] | None = None
    session_id: str | None = None


@dataclass
class AuditReport:
    generated_at: str = ""
    filter: AuditFilter = field(default_factory=AuditFilter)
    summary: dict[str, Any] = field(default_factory=dict)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)


VALID_ACTOR_KINDS = frozenset({"user", "system", "agent", "auto_review"})


@dataclass(frozen=True)
class AuditActor:
    """审计链里的「谁」。修复前审计只记「何时做了什么」，没有身份维度。"""

    kind: str = "system"
    id: str = "local"


def resolve_audit_actor(explicit: AuditActor | None = None) -> AuditActor:
    """确定本条审计记录的主体。

    显式传入优先（调用方最清楚）；否则从日志上下文派生——多 Agent 场景下
    `loop._run_loop` 已绑定 `agent_id`，据此可自动落到 `agent` 主体。
    无任何身份线索时回落 `system`/`local`。
    """
    if explicit is not None:
        if explicit.kind not in VALID_ACTOR_KINDS:
            raise ValueError(
                f"非法 audit actor kind：{explicit.kind!r}，可选 {sorted(VALID_ACTOR_KINDS)}"
            )
        return explicit

    from bandi.observability.context import current_log_context

    ctx = current_log_context()
    if getattr(ctx, "user_id", ""):
        return AuditActor(kind="user", id=ctx.user_id)
    if ctx.agent_id:
        return AuditActor(kind="agent", id=ctx.agent_id)
    return AuditActor(kind="system", id="local")


class AuditReporter:
    def __init__(
        self,
        session_store: SessionStore,
        tool_failure_alert_threshold: float = 0.2,
        safety_block_alert_threshold: int = 5,
        chain_path: Path | None = None,
    ) -> None:
        self._store = session_store
        self._tool_failure_alert_threshold = tool_failure_alert_threshold
        self._safety_block_alert_threshold = safety_block_alert_threshold
        self._chain_path_override = chain_path
        self._last_entry_hash: str | None = None

    def generate(self, filter: AuditFilter) -> AuditReport:
        sessions = self._store.list_sessions(limit=1000)

        if filter.session_id:
            s = self._store.get_session(filter.session_id)
            sessions = [s] if s else []
        if filter.time_from:
            sessions = [s for s in sessions if s.get("created_at", "") >= filter.time_from]
        if filter.time_to:
            sessions = [s for s in sessions if s.get("created_at", "") <= filter.time_to]

        total_turns = 0
        total_tool_calls = 0
        session_list: list[dict[str, Any]] = []

        for s in sessions:
            entries = self._store.get_branch(s["id"], full=True)
            messages = [e for e in entries if e.get("entry_type") == "message"]
            turns = len(messages)
            total_turns += turns
            # assistant metadata tool_calls，生产 assistant 消息不带该字段致恒 0
            total_tool_calls += sum(1 for e in messages if e.get("role") == "tool")

            session_list.append(
                {
                    "id": s["id"],
                    "title": s.get("title", ""),
                    "mode": s.get("mode", ""),
                    "created_at": s.get("created_at", ""),
                    "turns": turns,
                    "status": s.get("status", ""),
                }
            )

        # 吞掉告警与链计数；失败率分母已改 TOOL 消息源）
        alerts: list[str] = []
        tool_failure_rate = 0.0
        safety_blocks = 0
        session_tool_errors = 0
        session_safety_blocks = 0
        for s in sessions:
            entries = self._store.get_branch(s["id"], full=True)
            for e in entries:
                meta = e.get("metadata", "{}")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                if meta.get("tool_error"):
                    session_tool_errors += 1
                if meta.get("safety_blocked"):
                    session_safety_blocks += 1
        if total_tool_calls > 0:
            tool_failure_rate = round(session_tool_errors / total_tool_calls, 4)
        safety_blocks = max(
            session_safety_blocks, self._count_chain_events("SAFETY_GUARD_TRIGGERED")
        )
        if total_tool_calls > 0 and tool_failure_rate > self._tool_failure_alert_threshold:
            alerts.append(
                f"工具调用失败率 {tool_failure_rate:.1%} 超过阈值 {self._tool_failure_alert_threshold:.0%}"
            )
        if safety_blocks > self._safety_block_alert_threshold:
            alerts.append(
                f"安全拦截次数 {safety_blocks} 超过阈值 {self._safety_block_alert_threshold}"
            )

        return AuditReport(
            generated_at=datetime.now(UTC).isoformat(),
            filter=filter,
            summary={
                "total_sessions": len(sessions),
                "total_turns": total_turns,
                "total_tool_calls": total_tool_calls,
                "tool_failure_rate": tool_failure_rate,
                "safety_blocks": safety_blocks,
                "alerts": alerts,
            },
            sessions=session_list,
        )

    def export_json(self, filter: AuditFilter) -> str:
        report = self.generate(filter)
        data = {
            "generated_at": report.generated_at,
            "filter": {
                "time_from": filter.time_from,
                "time_to": filter.time_to,
                "session_id": filter.session_id,
            },
            "summary": report.summary,
            "sessions": report.sessions,
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    def export_file(self, filter: AuditFilter, fmt: str = "json") -> Path:
        # 调用方传 "csv"/"yaml" 静默拿到 json（契约违反）
        if fmt != "json":
            raise ValueError(f"unsupported export format: {fmt!r} (json only)")
        content = self.export_json(filter)
        ext = ".json"
        data_dir = Path.home() / ".bandi" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / f"audit_{datetime.now(UTC):%Y%m%d_%H%M%S}{ext}"
        path.write_text(content, encoding="utf-8")
        return path

    def record_event(self, event_data: dict[str, Any], *, actor: AuditActor | None = None) -> str:
        """# 功能：将事件写入防篡改哈希链，返回本条 entry_hash

        `actor` 省略时从日志上下文派生（多 Agent 场景自动落 `agent` 主体）。
        """
        return self._write_entry(event_data, actor=resolve_audit_actor(actor))

    def _chain_path(self) -> Path:
        if self._chain_path_override is not None:
            return self._chain_path_override
        return Path.home() / ".bandi" / "data" / "audit_chain.jsonl"

    def _count_chain_events(self, event_type: str) -> int:
        # 功能：B5b 8.6a——审计链中指定事件计数（safety 拦截等无消息级写方的指标源）
        chain_path = self._chain_path()
        if not chain_path.exists():
            return 0
        count = 0
        try:
            with open(chain_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    event = entry.get("event") or {}
                    if isinstance(event, dict) and event.get("event_type") == event_type:
                        count += 1
        except OSError:
            return 0
        return count

    def _write_entry(self, event_data: dict[str, Any], *, actor: AuditActor | None = None) -> str:
        chain_path = self._chain_path()
        chain_path.parent.mkdir(parents=True, exist_ok=True)

        prev_hash = self._last_entry_hash or ""
        if not prev_hash and chain_path.exists():
            last_line = ""
            with open(chain_path, encoding="utf-8") as f:
                for last_line in f:
                    pass
            if last_line:
                try:
                    last_entry = json.loads(last_line)
                    prev_hash = last_entry.get("entry_hash", "")
                except (json.JSONDecodeError, TypeError):
                    pass

        resolved = actor or resolve_audit_actor()
        ctx = current_log_context()
        entry = {
            "timestamp": time_module.time(),
            "event": event_data,
            "actor": {"kind": resolved.kind, "id": resolved.id},
            "session_id": ctx.session_id,
            "request_id": ctx.request_id,
            "prev_hash": prev_hash,
        }
        try:
            raw = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        except TypeError:
            entry["event"] = str(event_data)
            raw = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        entry_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        # ⑧ 可选 密钥存在时对同一规范 JSON 附加防伪标记（不参与 entry_hash 计算）
        hmac_key = resolve_audit_hmac_key()
        if hmac_key:
            entry["hmac"] = compute_entry_hmac(raw, hmac_key)
        entry["entry_hash"] = entry_hash

        self._append_line(chain_path, json.dumps(entry, ensure_ascii=False) + "\n")
        self._last_entry_hash = entry_hash
        return entry_hash

    @staticmethod
    def _append_line(chain_path: Path, line: str) -> None:
        """追加一行并 fsync；失败则**截断回原长**后重抛。

        为什么要 fsync：审计链的完整性靠逐条 sha256 串联，崩溃时若尾部只落半行
        链校验会直接判「invalid JSON」而无法区分是崩溃还是篡改。
        为什么要回滚：写失败后留下半行同样会污染链——照 deepseek-harness
        `session-persistence-jsonl` 的 rollbackAppend（truncate + sync）做法。
        """
        try:
            original_size = chain_path.stat().st_size
        except OSError:
            original_size = 0

        # 回滚必须在文件**关闭之后**执行：若在 with 块内 truncate，退出时关闭文件
        # 可能再次 flush 缓冲，把已截断的半行重新写回去。
        appended = False
        try:
            # 写入字节（\r\n）与回滚比较用的字节（\n）不一致，回滚判定永远失败
            with open(chain_path, "a", encoding="utf-8", newline="") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            appended = True
        finally:
            if not appended:
                _rollback_append(chain_path, original_size, line)

    def verify(self) -> list[dict[str, Any]]:
        chain_path = self._chain_path()
        if not chain_path.exists():
            return []

        issues: list[dict[str, Any]] = []
        hmac_key = resolve_audit_hmac_key()
        prev_hash = ""
        with open(chain_path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    issues.append({"line": line_no, "error": "invalid JSON"})
                    continue

                stored_hash = entry.get("entry_hash", "")
                stored_prev = entry.get("prev_hash", "")

                # ⑧ hmac 字段不参与 entry_hash（防伪标记独立校验）；历史无 hmac 条目零影响
                recompute = {k: v for k, v in entry.items() if k not in ("entry_hash", "hmac")}
                recompute_raw = json.dumps(recompute, sort_keys=True, ensure_ascii=False)
                expected_hash = hashlib.sha256(recompute_raw.encode("utf-8")).hexdigest()

                if stored_hash != expected_hash:
                    issues.append(
                        {
                            "line": line_no,
                            "error": "hash mismatch",
                            "expected": expected_hash,
                            "found": stored_hash,
                        }
                    )

                if hmac_key:
                    stored_hmac = entry.get("hmac", "")
                    if not stored_hmac:
                        issues.append({"line": line_no, "error": "hmac missing"})
                    elif not isinstance(stored_hmac, str):
                        # 坏数据（int/null 等）记为 issue——不得让 compare_digest 抛错中断全链校验
                        issues.append(
                            {"line": line_no, "error": "hmac invalid type", "found": stored_hmac}
                        )
                    elif not verify_entry_hmac(recompute_raw, stored_hmac, hmac_key):
                        issues.append(
                            {"line": line_no, "error": "hmac mismatch", "found": stored_hmac}
                        )

                if stored_prev != prev_hash:
                    issues.append(
                        {
                            "line": line_no,
                            "error": "chain break",
                            "expected_prev": prev_hash,
                            "found_prev": stored_prev,
                        }
                    )

                prev_hash = stored_hash

        return issues
