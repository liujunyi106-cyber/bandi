"""
契约：事件日志只读重放校验（七-replay dry-run）

行为族契约：EventBus.replay(dry_run=True) 必须以只读方式审计已落盘 JSONL——
校验 hash 链完整、sequence 单调、event_id 唯一、损坏行定位，且不得写入/修改任何内容。

契约钉行为（最终产物）：
- 完好文件 → report.ok True、valid_lines == 行数、hash_chain_broken False、无重复 event_id
- 篡改中间行（破坏 hash 链）→ report.hash_chain_broken True、ok False
- 篡改 sequence（非单调）→ 报 monotonic 错误
- 重复 event_id → 报 duplicate 错误
- 损坏行（非法 JSON）→ 报 parse 错误且不阻断后续行
- 只读性：replay 前后文件字节不变、目录无新增、EventBus 状态不被重置
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType


def _sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


async def _emit(bus: EventBus, ev: Event) -> None:
    await bus.emit(ev)


def _write_event(path: Path, seq: int, event_id: str, prev_hash: str, **extra: object) -> str:
    """构造一行与 emit() 落盘格式一致的事件行（用于构造篡改/异常场景）。"""
    line = (
        json.dumps(
            {
                "event_id": event_id,
                "sequence": seq,
                "type": "LLM_REQUEST",
                "data": {},
                "timestamp": 0.0,
                "prev_hash": prev_hash,
                "trace_id": "",
                "session_id": "",
                "turn_id": "",
                "attempt_id": "",
                "parent_event_id": "",
                "command_id": "",
                "schema_version": 1,
                "source": "",
                **extra,
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    return line.rstrip("\n")


def test_contract_replay_clean_file_reports_ok(tmp_path) -> None:
    """完好文件：replay 报告 ok、valid_lines 全覆盖、hash 链未断、无重复。"""
    path = tmp_path / "events-clean.jsonl"
    bus = EventBus(persist_path=path)
    bus.enable_persistence()
    asyncio.run(_emit(bus, Event(type=EventType.LLM_REQUEST, data={"a": 1})))
    asyncio.run(_emit(bus, Event(type=EventType.TURN_STARTED, data={"b": 2})))
    asyncio.run(_emit(bus, Event(type=EventType.TOOL_COMPLETED, data={"c": 3})))
    bus.disable_persistence()

    report = bus.replay(dry_run=True)
    assert report.ok, f"完好文件应 ok，错误: {report.errors}"
    assert report.valid_lines == 3
    assert report.lines_read == 3
    assert report.hash_chain_broken is False
    assert report.duplicate_event_ids == []
    assert report.errors == []


def test_contract_replay_never_writes(tmp_path) -> None:
    """破坏者视角：dry_run=True 审计不得写入/修改任何内容（字节不变、无 .1 滚动、不重置状态）。"""
    path = tmp_path / "events-ro.jsonl"
    bus = EventBus(persist_path=path)
    bus.enable_persistence()
    asyncio.run(_emit(bus, Event(type=EventType.LLM_REQUEST, data={"x": 5})))
    bus.disable_persistence()

    before_bytes = path.read_bytes()
    before_names = sorted(p.name for p in tmp_path.iterdir())

    report = bus.replay(dry_run=True)

    after_bytes = path.read_bytes()
    after_names = sorted(p.name for p in tmp_path.iterdir())
    assert before_bytes == after_bytes, "replay 不得改写日志文件"
    assert before_names == after_names, "replay 不得产生新文件（无 .1 滚动等）"
    assert report.valid_lines == 1


def test_contract_replay_detects_hash_chain_break(tmp_path) -> None:
    """篡改中间行：replay 必须报告 hash_chain_broken（链在篡改点断开）。"""
    path = tmp_path / "events-hash.jsonl"
    l1 = _write_event(path, 1, "e1", "")
    l2 = _write_event(path, 2, "e2", _sha(l1))
    l3 = _write_event(path, 3, "e3", _sha(l2))
    # 破坏者视角：篡改 l2 的 event_id，使 l3.prev_hash 不再等于 sha(l2')，同时 sequence 仍单调
    path.write_text(
        l1 + "\n" + l2.replace("e2", "e2-tampered") + "\n" + l3 + "\n", encoding="utf-8"
    )

    report = EventBus(persist_path=path).replay(dry_run=True)
    assert report.hash_chain_broken is True, "篡改中间行必须断开 hash 链"
    assert report.ok is False


def test_contract_replay_detects_non_monotonic_sequence(tmp_path) -> None:
    """破坏者视角：sequence 非单调（回退/重复）必须报错。"""
    path = tmp_path / "events-seq.jsonl"
    _write_event(path, 1, "e1", "")
    _write_event(path, 1, "e2", "")  # 与上一条 sequence 冲突（重复）
    _write_event(path, 3, "e3", "")

    report = EventBus(persist_path=path).replay(dry_run=True)
    assert report.ok is False
    assert any("sequence" in e.kind for e in report.errors), (
        f"必须报告非单调 sequence，实际错误: {[e.kind for e in report.errors]}"
    )


def test_contract_replay_detects_duplicate_event_id(tmp_path) -> None:
    """重复 event_id（两条不同行相同 id）必须报告 duplicate。"""
    path = tmp_path / "events-dup.jsonl"
    _write_event(path, 1, "dup-id", "")
    _write_event(path, 2, "dup-id", "")

    report = EventBus(persist_path=path).replay(dry_run=True)
    assert report.duplicate_event_ids == ["dup-id"]
    assert report.ok is False


def test_contract_replay_skips_corrupt_line_and_continues(tmp_path) -> None:
    """损坏行（非法 JSON）报 parse 错误，但不阻断后续行审计。"""
    path = tmp_path / "events-corrupt.jsonl"
    _write_event(path, 1, "e1", "")
    with path.open("a", encoding="utf-8") as f:
        f.write('{"event_id": "broken", "sequence": 2\n')  # 非法 JSON
    _write_event(path, 3, "e2", "")

    report = EventBus(persist_path=path).replay(dry_run=True)
    assert any("parse" in e.kind for e in report.errors), "损坏行必须报 parse 错误"
    assert report.lines_read == 3
    assert report.ok is False


def test_contract_replay_malformed_field_types_do_not_crash(tmp_path) -> None:
    """破坏者视角：合法 JSON 但字段值类型畸形（event_id 为对象 / sequence 非 int）不许让审计崩溃。

    审计工具承诺「损坏行定位但不阻断」——畸形但可解析的字段必须被归为错误
    而不是抛未捕获 TypeError / ValueError 使整个 `eventlog audit` 崩溃（fail-closed 而非 crash）。
    """
    path = tmp_path / "events-malformed.jsonl"
    _write_event(path, 1, "e1", "")
    with path.open("a", encoding="utf-8") as f:
        f.write('{"event_id": {"a": 1}, "sequence": 2, "type": "LLM_REQUEST"}\n')
        f.write('{"event_id": "e3", "sequence": "not-an-int", "type": "LLM_REQUEST"}\n')
    _write_event(path, 4, "e4", "")

    report = None
    try:
        report = EventBus(persist_path=path).replay(dry_run=True)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 这是 bug，绝不允许逸出
        raise AssertionError(f"replay 对畸形字段类型必须容错而非崩溃: {exc!r}") from exc

    assert report is not None
    assert report.ok is False
    assert any("field" in e.kind for e in report.errors), (
        f"畸形字段必须报 field 错误, 实际: {[e.kind for e in report.errors]}"
    )
    assert report.lines_read == 4
    assert report.valid_lines >= 2  # e1 + e4 至少两条有效


def test_contract_replay_empty_and_all_corrupt_edges(tmp_path) -> None:
    """边界：空文件与全损坏文件不许崩溃，报告各字段保持一致。"""
    empty_path = tmp_path / "events-empty.jsonl"
    empty_path.write_text("", encoding="utf-8")
    report = EventBus(persist_path=empty_path).replay(dry_run=True)
    assert report.ok, "空文件视为完好（0 行无错误）"
    assert report.valid_lines == 0
    assert report.lines_read == 0
    assert report.errors == []

    all_corrupt = tmp_path / "events-allcorrupt.jsonl"
    all_corrupt.write_text("not-json\n{{{\n", encoding="utf-8")
    report2 = EventBus(persist_path=all_corrupt).replay(dry_run=True)
    assert report2.ok is False
    assert any("parse" in e.kind for e in report2.errors)
    assert report2.valid_lines == 0
    assert report2.first_sequence == 0
    assert report2.last_sequence == 0


def test_contract_replay_single_line_first_sequence(tmp_path) -> None:
    """仅首行：first_sequence 与 last_sequence 都等于该行 sequence。"""
    path = tmp_path / "events-single.jsonl"
    _write_event(path, 7, "e1", "")
    report = EventBus(persist_path=path).replay(dry_run=True)
    assert report.ok
    assert report.first_sequence == 7
    assert report.last_sequence == 7
    assert report.valid_lines == 1


def test_contract_replay_restore_mode_matches_enable_persistence(tmp_path) -> None:
    """dry_run=False（恢复模式）后，新 emit 的 sequence 必须从最后一行续（与应用行为一致）。"""
    path = tmp_path / "events-restore.jsonl"
    l1 = _write_event(path, 1, "e1", "")
    _write_event(path, 2, "e2", _sha(l1))

    bus = EventBus(persist_path=path)
    bus.enable_persistence()  # 正常恢复路径
    report = bus.replay(dry_run=False)
    assert report.ok, f"恢复模式应 ok, {report.errors}"
    asyncio.run(_emit(bus, Event(type=EventType.TOOL_COMPLETED, data={"n": 3})))
    bus.disable_persistence()

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert json.loads(lines[-1])["sequence"] == 3, "恢复后 sequence 必须从最后一行续"
