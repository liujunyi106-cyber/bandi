from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _seed_events(db_dir: Path) -> None:
    from bandi.memory.global_store import GlobalMemoryStore

    db_dir.mkdir(parents=True, exist_ok=True)
    store = GlobalMemoryStore(str(db_dir / "hierarchy_global.db"))
    store.append_event(
        "memory_persist",
        {"summary": "用户: a → Agent: b", "session_id": "s-1", "turn_index": 1},
        event_id="memory_persist:s-1:1",
    )
    store.append_event(
        "memory_persist",
        {"summary": "用户: c → Agent: d", "session_id": "s-2", "turn_index": 2},
        event_id="memory_persist:s-2:2",
    )
    store.append_event("other_type", {"k": "v"})
    store.close()


def test_query_memory_events_reads_store(home: Path) -> None:
    """生产路径 1（CLI）：_query_memory_events 直读 hierarchy_global.db——
    写入→读回闭环 + type 过滤 + limit 截断。"""
    from bandi.cli.memory import query_memory_events

    _seed_events(home / ".bandi" / "data")
    db = home / ".bandi" / "data" / "hierarchy_global.db"

    all_events = query_memory_events(db, event_type="", limit=50)
    assert len(all_events) == 3, f"无过滤必须返回全量事件，实际 {len(all_events)}"

    filtered = query_memory_events(db, event_type="memory_persist", limit=50)
    assert len(filtered) == 2
    assert all(e["event_type"] == "memory_persist" for e in filtered)
    assert filtered[0]["payload"]["session_id"] == "s-1", "按 seq 升序（写入顺序）"
    assert filtered[-1]["payload"]["session_id"] == "s-2"

    capped = query_memory_events(db, event_type="", limit=1)
    assert len(capped) == 1 and capped[0]["seq"] == max(e["seq"] for e in all_events), (
        "limit 截断保留最新"
    )


def test_api_memory_events_endpoint_roundtrip(home: Path, monkeypatch: Any) -> None:
    """生产路径 2（API）：GET /api/memory/events——参数生效 + JSON 结构。"""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from bandi.api.app import app

    _seed_events(home / ".bandi" / "data")
    with patch("bandi.api.app._API_KEY", "test"):
        client = TestClient(app)
        client.headers["Authorization"] = "Bearer test"
        resp = client.get("/api/memory/events")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["events"]) == 3

        resp = client.get("/api/memory/events", params={"event_type": "memory_persist", "limit": 1})
        data = resp.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["event_type"] == "memory_persist"


def test_api_events_reflects_idempotent_append(home: Path) -> None:
    """幂等写经端点可见：同 event_id 重复 append 只产生一行（防重复注入审计噪声）。"""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from bandi.api.app import app
    from bandi.memory.global_store import GlobalMemoryStore

    db_dir = home / ".bandi" / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    store = GlobalMemoryStore(str(db_dir / "hierarchy_global.db"))
    payload = {"summary": "dup"}
    store.append_event("memory_persist", payload, event_id="memory_persist:s-9:1")
    store.append_event("memory_persist", payload, event_id="memory_persist:s-9:1")
    store.close()

    with patch("bandi.api.app._API_KEY", "test"):
        client = TestClient(app)
        client.headers["Authorization"] = "Bearer test"
        data = client.get("/api/memory/events", params={"event_type": "memory_persist"}).json()
    assert len(data["events"]) == 1, "同 event_id 幂等不得产生重复行"

    dumped = json.dumps(data["events"][0], ensure_ascii=False)
    assert "dup" in dumped


def test_cli_command_wired(home: Path) -> None:
    """typer 命令薄包装真实调用查询函数（wiring：命令 → query_memory_events）。"""
    from typer.testing import CliRunner

    from bandi.cli.memory import memory_app

    _seed_events(home / ".bandi" / "data")
    result = CliRunner().invoke(memory_app, ["events", "--type", "memory_persist"])
    assert result.exit_code == 0, result.output
    events = json.loads(result.output)
    assert len(events) == 2 and events[0]["event_type"] == "memory_persist"
