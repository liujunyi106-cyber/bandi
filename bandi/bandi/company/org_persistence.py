# 功能：组织本体 JSON 持久化（CLI 与 API 双入口共享装配——修复）
from __future__ import annotations

import json
import re
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from bandi.company.org_engine import OrgEngine
from bandi.company.recruiter import Recruiter

_BUILTIN_ROLE_IDS = {
    "ceo",
    "cto",
    "cfo",
    "em",
    "architect",
    "engineer",
    "analyst",
    "finance",
    "viewer",
}
_TEMP_ROLE_RE = re.compile(r"worker_\d+")


def is_persistable_role(role_id: str) -> bool:
    """角色是否应持久化：内置角色与 init 临时产物（`worker_<数字>`）不持久化。"""
    if role_id in _BUILTIN_ROLE_IDS:
        return False
    return _TEMP_ROLE_RE.fullmatch(role_id) is None


def org_json_path() -> Path:
    return Path.home() / ".bandi" / "company" / "org.json"


@contextmanager
def _suppress_persist(engine: Any) -> Iterator[None]:
    """功能：批量注册/写前合并期间临时禁用 engine 持久化回调，退出恢复原值。

    防写放大 O(N²)：加载/合并逐角色注册会触发 `_persist` → save_org → 写前合并
    再注册，形成嵌套全量写盘（N 个角色 N(N+1)/2 次写）。"""
    saved = getattr(engine, "_persist_callable", None)
    if saved is None:
        yield
        return
    engine._persist_callable = None
    try:
        yield
    finally:
        engine._persist_callable = saved


def _assert_no_report_cycle(roles: list) -> None:
    """
    写入前必须显式拒绝。"""
    by_id = {r.id: r for r in roles}
    for role in roles:
        seen: set[str] = set()
        current: str | None = role.id
        while current:
            if current in seen:
                raise ValueError(f"org reports_to cycle detected at role `{current}`")
            seen.add(current)
            parent = by_id.get(current)
            current = parent.reports_to if parent else None


def _employee_from_entry(emp: dict[str, Any]) -> Any:
    """org.json 员工条目 → OrgEmployee（字段防御；无效返回 None）——load/merge 共用。"""
    from bandi.company.org_engine import OrgEmployee

    eid = str(emp.get("employee_id") or "")
    if not eid:
        return None
    raw_metadata = emp.get("metadata", {}) or {}
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    raw_domains = emp.get("domains", []) or []
    domains = list(raw_domains) if isinstance(raw_domains, list) else []
    raw_llm_config = emp.get("llm_config")
    llm_config = dict(raw_llm_config) if isinstance(raw_llm_config, dict) else None
    return OrgEmployee(
        employee_id=eid,
        name=str(emp.get("name") or eid),
        role_id=str(emp.get("role_id") or ""),
        description=str(emp.get("description") or ""),
        template_id=str(emp.get("template_id") or ""),
        category=str(emp.get("category") or ""),
        domains=domains,
        metadata=metadata,
        status=str(emp.get("status") or "active"),
        llm_config=llm_config,
    )


def _read_disk_revision(path: Path) -> int:
    """功能：org.json 现有 revision（不存在/损坏返回 0）——写入侧递增数据源。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get("revision", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _id_set_attr(obj: Any, attr: str) -> set[str]:
    """功能：安全读取「id 集合」属性（Mock/旧对象返回空集合 → 合并退化为旧语义）。"""
    raw = getattr(obj, attr, None)
    if isinstance(raw, (set, frozenset, list, tuple)):
        return {str(item) for item in raw}
    return set()


def _merge_disk_into_engine(engine: OrgEngine, recruiter: Recruiter, path: Path) -> bool:
    """功能：写前重读合并——盘上已有但本进程内存缺失的角色/员工/雇佣
    注册回内存，防跨进程 last-write-wins 覆盖他人新增；同 id 条目以内存（本进程）
    为权威。盘上文件缺失/损坏时静默跳过（保持原全量写语义）。

    返回盘上内容是否可用（缺失/损坏/顶层非对象 → False）——读面刷新据此判定
    「本轮是否真的对齐了盘上」，损坏文件不得被记为已消费。
    合并边界（封堵「只增不减」）：本进程相对加载基线**主动删除**的角色、**主动改过
    状态**的席位不被盘上旧值写回——否则删除/释放会被静默复活。"""
    from bandi.company.seat_recruiter import RecruitStatus

    removed_roles = _id_set_attr(engine, "deleted_role_ids")
    changed_seats = _id_set_attr(recruiter, "changed_seat_ids")

    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    existing_roles = {r.id for r in engine.list_agents()}
    for role in payload.get("roles", []):
        if not isinstance(role, dict):
            continue
        rid = str(role.get("id") or "")
        if not rid or rid in existing_roles or rid in removed_roles or not is_persistable_role(rid):
            continue
        try:
            engine.register_role(
                rid,
                str(role.get("name") or rid),
                str(role.get("responsibility") or ""),
                None,
                list(role.get("can_spawn", []) or []),
                dict(role.get("permissions", {}) or {}),
                tool_tags=list(role.get("tool_tags", []) or []),
                is_final_decider=bool(role.get("is_final_decider", False)),
                team_id=str(role.get("team_id") or ""),
                fs_scopes=list(role.get("fs_scopes", []) or []),
            )
        except ValueError:
            continue
        reports_to = role.get("reports_to")
        if reports_to and str(reports_to) != rid and engine.get_role(str(reports_to)) is not None:
            try:
                engine.set_reports_to(rid, str(reports_to))
            except (ValueError, AttributeError):
                continue
    existing_employees = {e.employee_id for e in engine.list_employees(include_inactive=True)}
    deleted_employees = _id_set_attr(engine, "deleted_employee_ids")
    for emp in payload.get("employees", []):
        if not isinstance(emp, dict):
            continue
        parsed = _employee_from_entry(emp)
        if parsed is None or parsed.employee_id in existing_employees:
            continue
        if parsed.employee_id in deleted_employees:
            continue
        engine.register_employee(parsed)
    # 席位合并：盘上已雇佣而内存未雇佣 → 对齐（防"他进程已 recruit"丢失）
    for role_id, status in (payload.get("seats", {}) or {}).items():
        if not role_id or not is_persistable_role(str(role_id)):
            continue
        if str(role_id) in changed_seats:
            continue  # 本进程已改过该席位状态（如 release）——盘上旧状态不得翻回
        if status != RecruitStatus.RECRUITED.value:
            continue
        try:
            seat = recruiter.get_seat(str(role_id))
            if seat is None:
                recruiter.register_seat(str(role_id))
                seat = recruiter.get_seat(str(role_id))
            if seat is not None and seat.status != RecruitStatus.RECRUITED:
                recruiter.recruit(str(role_id))
        except (ValueError, RuntimeError):
            continue
    return True


_REFRESH_LOCK = threading.RLock()


def disk_signature(path: str | Path | None = None) -> tuple[int, int] | None:
    """功能：org.json 变更指纹 `(mtime_ns, size)`（不存在返回 None）——读面刷新的廉价判据。"""
    resolved = Path(path) if path is not None else org_json_path()
    try:
        stat = resolved.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def refresh_org_from_disk(
    engine: OrgEngine, recruiter: Recruiter, path: str | Path | None = None
) -> bool:
    """功能：读面刷新——org.json 有本进程未见的新增时并入内存（写前合并的读侧对偶）。

    只在**写**路径重读盘上合并，故读面长期陈旧：他进程（CLI / 另一后端 /
    桌面壳）实例化团队落盘后，本进程 `add_team` 的「已组建」校验仍按旧内存判
    未组建/不存在。读面因此必须能主动对齐。

    与写前合并同一套语义（`_merge_disk_into_engine`）：**只增不减**——并入盘上
    缺失的角色/员工/席位，跳过墓碑（不复活本进程已删角色），同 id 以内存为权威。
    指纹未变直接返回 False（每次读至多一次 stat）；盘上缺失/损坏 fail-safe 返回
    False 且不动内存；全程在持久化抑制上下文内合并，读面零写盘（不抬 revision）。
    """
    resolved = Path(path) if path is not None else org_json_path()
    signature = disk_signature(resolved)
    if signature is None:
        return False
    with _REFRESH_LOCK:
        if getattr(engine, "_disk_refresh_signature", None) == signature:
            return False
        with _suppress_persist(engine):
            applied = _merge_disk_into_engine(engine, recruiter, resolved)
        if not applied:
            return False
        engine._disk_refresh_signature = signature
        return True


def save_org(engine: OrgEngine, recruiter: Recruiter, path: str | Path | None = None) -> None:
    """将自定义角色/员工/席位状态写入 org.json（原子写）。

    检测到环抛 ValueError 拒绝写入（显式错误而非静默修复）。
    写前重读合并盘上他进程新增 + revision 单调递增。
    """
    resolved_path = Path(path) if path is not None else org_json_path()
    with _suppress_persist(engine):
        _merge_disk_into_engine(engine, recruiter, resolved_path)
    roles = [r for r in engine.list_agents() if is_persistable_role(r.id)]
    _assert_no_report_cycle(roles)

    payload: dict[str, Any] = {
        "roles": [
            {
                "id": r.id,
                "name": r.name,
                "reports_to": r.reports_to,
                "can_spawn": list(r.can_spawn),
                "permissions": dict(r.permissions),
                "responsibility": r.responsibility,
                "tool_tags": list(r.tool_tags),
                "is_final_decider": r.is_final_decider,
                "team_id": r.team_id,
                "fs_scopes": list(r.fs_scopes),
            }
            for r in engine.list_agents()
            if is_persistable_role(r.id)
        ],
        "employees": [
            # 复用 OrgEmployee.to_dict()（单一序列化来源）——手写 dict 曾漏 llm_config
            # 致 per-employee 模型配置重启即失（换大脑断链）
            e.to_dict()
            # 招募/选派面，retired 员工随 org.json 保留否则重启即丢失
            for e in engine.list_employees(include_inactive=True)
        ],
        "seats": {
            seat.role_id: seat.status.value
            for seat in recruiter.list_seats()
            if is_persistable_role(seat.role_id)
        },
    }
    payload["revision"] = _read_disk_revision(resolved_path) + 1
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(resolved_path.parent), delete=False
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp_name = f.name
    Path(tmp_name).replace(resolved_path)
    # 功能：写后对齐读面基线——内存即刚落盘内容，读面无需再做一次合并解析
    engine._disk_refresh_signature = disk_signature(resolved_path)


def load_org(engine: OrgEngine, recruiter: Recruiter, path: str | Path | None = None) -> None:
    """从 org.json 加载自定义角色/员工/席位（幂等，文件缺失静默返回）。

    深度复审修复：① 角色两遍加载——先全部注册（reports_to 暂存），再补
    时 A 被 ValueError 静默跳过 → 角色跨进程丢失）；② 未知上级降级为无上级
    （不崩溃不丢角色）；③ 损坏文件记录告警而非静默。
    """
    import logging

    logger = logging.getLogger(__name__)
    path = Path(path) if path is not None else org_json_path()
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("org.json 解析失败（角色/员工将不加载）: %s", e)
        return
    # 结构级校验——顶层非 dict 的损坏文件降级返回（不崩溃 CLI/API 启动路径）
    if not isinstance(payload, dict):
        logger.warning("org.json 顶层结构损坏（非对象），角色/员工将不加载")
        return
    # 批量注册全程抑制持久化回调（防 O(N²) 写放大：逐角色注册触发 save →
    # 写前合并再注册）；contextmanager 保证异常路径同样恢复回调
    with _suppress_persist(engine):
        _apply_org_payload(engine, recruiter, payload)
    # 功能：加载完成后记录基线——写前合并据此区分「本进程主动删除/改状态」与
    # 「他进程新增」，防删除复活；未经 load_org 的进程基线为空，合并不设限（旧语义）。
    # 取属性后调用：测试桩等 duck-typed 对象无该方法时跳过（基线为空＝旧语义）。
    for target in (engine, recruiter):
        snapshot = getattr(target, "snapshot_baseline", None)
        if callable(snapshot):
            snapshot()
    # 功能：记录读面基线指纹——刚加载的内容即「已同步」，读面刷新无须再解析一次
    engine._disk_refresh_signature = disk_signature(path)


def _apply_org_payload(engine: OrgEngine, recruiter: Recruiter, payload: dict[str, Any]) -> None:
    """功能：org.json payload 注册进引擎（load 事务体：角色两遍 + 员工 + 席位）——
    由 load_org 在持久化抑制上下文内调用；损坏条目逐项告警跳过、不中断其余加载。"""
    import logging

    from bandi.company.seat_recruiter import RecruitStatus

    logger = logging.getLogger(__name__)
    # ① 第一遍：注册全部角色（reports_to 先不强校验）
    pending_reports: dict[str, str | None] = {}
    for role in payload.get("roles", []):
        if not isinstance(role, dict):
            logger.warning("org.json 角色条目结构损坏（跳过）")
            continue
        rid = str(role.get("id") or "")
        if not rid or not is_persistable_role(rid):
            continue
        try:
            engine.register_role(
                rid,
                str(role.get("name") or rid),
                str(role.get("responsibility") or ""),
                None,  # reports_to 第二遍补
                list(role.get("can_spawn", []) or []),
                dict(role.get("permissions", {}) or {}),
                tool_tags=list(role.get("tool_tags", []) or []),
                is_final_decider=bool(role.get("is_final_decider", False)),
                team_id=str(role.get("team_id") or ""),
                fs_scopes=list(role.get("fs_scopes", []) or []),
            )
            pending_reports[rid] = role.get("reports_to")
        except ValueError:
            logger.warning("org.json 角色 %s 注册失败（跳过）", rid)
    # ② 第二遍：补 reports_to（未知上级降级为无上级，不丢角色）
    for rid, reports_to in pending_reports.items():
        if not reports_to or reports_to == rid:
            continue
        if engine.get_role(reports_to) is None:
            logger.warning("org.json 角色 %s 的上级 %s 不存在（降级为无上级）", rid, reports_to)
            continue
        try:
            engine.set_reports_to(rid, reports_to)
        except (ValueError, AttributeError):
            logger.warning("org.json 角色 %s 设置上级 %s 失败（降级）", rid, reports_to)
    for emp in payload.get("employees", []):
        if not isinstance(emp, dict):
            logger.warning("org.json 员工条目结构损坏（跳过）")
            continue
        parsed_employee = _employee_from_entry(emp)
        if parsed_employee is not None:
            engine.register_employee(parsed_employee)
    seats = payload.get("seats", {}) or {}
    if not isinstance(seats, dict):
        logger.warning("org.json seats 结构损坏（降级为空）")
        seats = {}
    for role_id, status in seats.items():
        if not role_id or not is_persistable_role(role_id):
            continue
        recruiter.register_seat(role_id)
        if status == RecruitStatus.RECRUITED.value:
            recruiter.recruit(role_id)
