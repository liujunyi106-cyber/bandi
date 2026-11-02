"""
契约族：事件持久化可靠性（七-）。

背景：EventBus 文件持久化仅 append + sequence——无篡改/损坏检测链、无旋转
无重复事件检测、损坏行拖垮恢复。本契约钉 append-only 可靠实现的 6 个加固面
（concurrent writer lock 与单调 sequence 已有，不在本契约重钉）。

本契约钉的行为族（生产调用路径）：
- `core/event_bus.py::EventBus.enable_persistence`（恢复：sequence + hash 链 + 去重集）
- `core/event_bus.py::EventBus.emit`（持久化写入：prev_hash 链 / duplicate 跳过 / rotation）
- 事件日志文件（~/.bandi/logs/events-*.jsonl）

断言「最终产物」：行间 hash 链连续可校验；同 event_id 重复 emit 只落一行；
超阈值自动旋转（.1 滚动）；文件尾损坏行不阻断恢复且 sequence 从最近完好行续。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType


def _read_lines(path: Path) -> list[dict]:
    out: list[dict] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue  # 损坏行跳过（append-only 保留，读取容错）
    return out


def _sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


async def _emit(bus: EventBus, ev: Event) -> None:
    await bus.emit(ev)


def test_contract_event_hash_chain_continuous(tmp_path) -> None:
    """连续事件行必须构成 hash 链：每行 prev_hash == 上一行完整文本的 sha256。"""
    path = tmp_path / "events-h.jsonl"
    bus = EventBus(persist_path=path)
    bus.enable_persistence()
    asyncio.run(_emit(bus, Event(type=EventType.LLM_REQUEST, data={"a": 1})))
    asyncio.run(_emit(bus, Event(type=EventType.TURN_STARTED, data={"b": 2})))
    asyncio.run(_emit(bus, Event(type=EventType.ERROR_OCCURRED, data={"c": 3})))
    bus.disable_persistence()

    raw = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(raw) == 3
    assert json.loads(raw[0])["prev_hash"] == "", "首行 prev_hash 必须为空"
    for i in range(1, len(raw)):
        assert json.loads(raw[i])["prev_hash"] == _sha(raw[i - 1]), (
            f"第 {i} 行 prev_hash 必须链接上一行"
        )


def test_contract_event_duplicate_event_id_single_line(tmp_path) -> None:
    """同 event_id 重复 emit 必须只落一行（duplicate event detection）。"""
    path = tmp_path / "events-d.jsonl"
    bus = EventBus(persist_path=path)
    bus.enable_persistence()
    ev = Event(type=EventType.LLM_REQUEST, data={"x": 1})
    asyncio.run(_emit(bus, ev))
    asyncio.run(_emit(bus, ev))  # 同对象重复 emit（同 event_id）
    bus.disable_persistence()

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, f"重复 event_id 必须只落一行，实际 {len(lines)}"


def test_contract_event_rotation_rolls_file(tmp_path, monkeypatch) -> None:
    """超阈值必须旋转：原文件滚动为 .1，新写入进入新文件。"""
    from bandi.core import event_bus as ev_mod

    monkeypatch.setattr(ev_mod, "_PERSIST_ROTATE_BYTES", 200)
    path = tmp_path / "events-r.jsonl"
    bus = EventBus(persist_path=path)
    bus.enable_persistence()
    for i in range(30):
        asyncio.run(
            _emit(bus, Event(type=EventType.TOOL_COMPLETED, data={"pad": "x" * 40, "i": i}))
        )
    bus.disable_persistence()

    rotated = tmp_path / "events-r.jsonl.1"
    assert rotated.exists(), "超阈值后必须产生 .1 滚动文件"
    assert path.exists(), "新文件必须继续写入"


def test_contract_event_corrupt_tail_does_not_block_recovery(tmp_path) -> None:
    """文件尾损坏行（partial line）不阻断恢复：sequence 从最近完好行续，不崩。"""
    path = tmp_path / "events-c.jsonl"
    bus = EventBus(persist_path=path)
    bus.enable_persistence()
    asyncio.run(_emit(bus, Event(type=EventType.LLM_REQUEST, data={"n": 1})))
    asyncio.run(_emit(bus, Event(type=EventType.TURN_STARTED, data={"n": 2})))
    bus.disable_persistence()
    # 追加两条损坏行（模拟写中断）
    with path.open("a", encoding="utf-8") as f:
        f.write('{"event_id": "broken-1", "sequence": 99\n')
        f.write("{garbage}\n")

    bus2 = EventBus(persist_path=path)
    bus2.enable_persistence()  # 不得抛异常
    asyncio.run(_emit(bus2, Event(type=EventType.TOOL_COMPLETED, data={"n": 3})))
    bus2.disable_persistence()

    lines = _read_lines(path)
    assert lines[-1]["sequence"] == 3, (
        f"sequence 必须从最近完好行续（2 之后是 3），实际 {lines[-1]['sequence']}"
    )
    # 破坏者视角：损坏行被跳过但未被删除（append-only 不修改历史）
    raw = path.read_text(encoding="utf-8").splitlines()
    assert any("broken-1" in ln for ln in raw), "损坏行应保留（append-only 语义）"
