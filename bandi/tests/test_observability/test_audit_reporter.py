from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore
from bandi.observability.audit_reporter import AuditFilter, AuditReporter


@pytest.fixture
def store() -> SessionStore:
    tmp = tempfile.mktemp(suffix=".db")
    s = SessionStore(tmp)
    yield s
    s.close()
    if os.path.exists(tmp):
        os.unlink(tmp)


@pytest.fixture
def reporter(store: SessionStore) -> AuditReporter:
    return AuditReporter(store)


@pytest.fixture
def populated_store(store: SessionStore) -> SessionStore:
    for i in range(3):
        sid = store.create_session(title=f"会话{i}")
        store.append_message(sid, Message(role=MessageRole.USER, content=f"用户消息{i}"))
        store.append_message(sid, Message(role=MessageRole.ASSISTANT, content=f"回复{i}"))
    return store


@pytest.fixture
def populated_reporter(populated_store: SessionStore) -> AuditReporter:
    return AuditReporter(populated_store)


class TestAuditReporter:
    def test_empty_store(self, reporter: AuditReporter) -> None:
        report = reporter.generate(AuditFilter())
        assert report.summary["total_sessions"] == 0
        assert report.summary["total_turns"] == 0

    def test_generate_with_data(self, populated_reporter: AuditReporter) -> None:
        report = populated_reporter.generate(AuditFilter())
        assert report.summary["total_sessions"] == 3
        assert report.summary["total_turns"] == 6

    def test_json_output(self, populated_reporter: AuditReporter) -> None:
        json_str = populated_reporter.export_json(AuditFilter())
        data = json.loads(json_str)
        assert data["summary"]["total_sessions"] == 3
        assert data["summary"]["total_turns"] == 6
        assert "generated_at" in data
        assert "sessions" in data

    def test_export_to_file(self, populated_reporter: AuditReporter) -> None:
        path = populated_reporter.export_file(AuditFilter(), fmt="json")
        try:
            assert path.exists()
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["summary"]["total_sessions"] == 3
        finally:
            if path.exists():
                path.unlink()

    def test_filter_by_session_id(self, populated_store: SessionStore) -> None:
        reporter = AuditReporter(populated_store)
        sessions = populated_store.list_sessions()
        target_id = sessions[0]["id"]
        report = reporter.generate(AuditFilter(session_id=target_id))
        assert report.summary["total_sessions"] == 1
        assert len(report.sessions) == 1


class TestAuditHashChain:
    @pytest.fixture
    def chain_path(self) -> str:
        tmp = tempfile.mktemp(suffix="_audit_chain.jsonl")
        yield tmp
        if os.path.exists(tmp):
            os.unlink(tmp)

    @pytest.fixture
    def chain_reporter(self, store: SessionStore, chain_path: str) -> AuditReporter:
        return AuditReporter(store, chain_path=Path(chain_path))

    def test_record_event_writes_and_verifies(
        self, chain_reporter: AuditReporter, chain_path: str
    ) -> None:
        chain_reporter.record_event({"action": "audit_report", "total_sessions": 0})
        assert os.path.exists(chain_path)
        assert chain_reporter.verify() == []

    def test_verify_detects_chain_break(
        self, chain_reporter: AuditReporter, chain_path: str
    ) -> None:
        chain_reporter.record_event({"action": "audit_report", "seq": 1})
        chain_reporter.record_event({"action": "audit_report", "seq": 2})
        lines = Path(chain_path).read_text(encoding="utf-8").strip().splitlines()
        last = json.loads(lines[-1])
        last["prev_hash"] = "0" * 64
        Path(chain_path).write_text(
            "\n".join(lines[:-1] + [json.dumps(last, ensure_ascii=False)]) + "\n",
            encoding="utf-8",
        )
        issues = chain_reporter.verify()
        assert any(i["error"] == "chain break" for i in issues)

    def test_verify_detects_content_tamper(
        self, chain_reporter: AuditReporter, chain_path: str
    ) -> None:
        chain_reporter.record_event({"action": "audit_report", "seq": 1})
        lines = Path(chain_path).read_text(encoding="utf-8").strip().splitlines()
        tampered = json.loads(lines[-1])
        tampered["event"] = {"action": "audit_report", "seq": 999}
        Path(chain_path).write_text(json.dumps(tampered) + "\n", encoding="utf-8")
        issues = chain_reporter.verify()
        assert any(i["error"] == "hash mismatch" for i in issues)

    def test_verify_empty_or_missing_chain(self, chain_reporter: AuditReporter) -> None:
        assert chain_reporter.verify() == []

    def test_write_entry_reads_tail_only_once(
        self, chain_reporter: AuditReporter, chain_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads = {"n": 0}
        import builtins

        real_open = builtins.open

        def counting_open(file, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if str(file) == str(chain_path) and mode == "r":
                reads["n"] += 1
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", counting_open)
        h1 = chain_reporter.record_event({"action": "t", "seq": 1})
        after_first = reads["n"]
        h2 = chain_reporter.record_event({"action": "t", "seq": 2})
        assert h1 != h2, "两次事件哈希必须不同"
        assert reads["n"] == after_first, (
            f"第二次写链不得再读尾部（prev_hash 内存缓存），读取增量 {reads['n'] - after_first}"
        )
        lines = Path(chain_path).read_text(encoding="utf-8").strip().splitlines()
        second = json.loads(lines[-1])
        first = json.loads(lines[-2])
        assert second["prev_hash"] == first["entry_hash"], "链必须连续"
