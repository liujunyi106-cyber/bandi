# 功能：契约——审计哈希链 （可选密钥 BANDI_AUDIT_HMAC_KEY，向后兼容）
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bandi.memory.session.store import SessionStore
from bandi.observability.audit_chain import verify_entry_hmac
from bandi.observability.audit_reporter import AuditReporter


@pytest.fixture
def reporter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("BANDI_AUDIT_HMAC_KEY", raising=False)
    store = SessionStore(str(tmp_path / "sessions.db"))
    yield AuditReporter(store, chain_path=tmp_path / "audit_chain.jsonl")
    store.close()


def _chain_entries(reporter: AuditReporter) -> list[dict[str, Any]]:
    path = reporter._chain_path()
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").strip().splitlines()
        if line.strip()
    ]


def _rewrite_chain(reporter: AuditReporter, entries: list[dict[str, Any]]) -> None:
    path = reporter._chain_path()
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8"
    )


class TestHmacDisabled:
    def test_no_key_omits_hmac_field(self, reporter: AuditReporter) -> None:
        reporter.record_event({"action": "x", "seq": 1})
        entry = _chain_entries(reporter)[0]
        assert "hmac" not in entry
        assert reporter.verify() == []


class TestHmacEnabled:
    def test_key_writes_hmac_and_verifies(self, reporter: AuditReporter, monkeypatch) -> None:
        monkeypatch.setenv("BANDI_AUDIT_HMAC_KEY", "audit-key")
        reporter.record_event({"action": "x", "seq": 1})
        entry = _chain_entries(reporter)[0]
        assert len(entry["hmac"]) == 64
        assert reporter.verify() == []

    def test_tampered_event_reports_mismatch(self, reporter: AuditReporter, monkeypatch) -> None:
        monkeypatch.setenv("BANDI_AUDIT_HMAC_KEY", "audit-key")
        reporter.record_event({"action": "x", "seq": 1})
        entries = _chain_entries(reporter)
        entries[0]["event"] = {"action": "x", "seq": 999}
        _rewrite_chain(reporter, entries)
        issues = reporter.verify()
        assert any(i["error"] == "hash mismatch" for i in issues)
        assert any(i["error"] == "hmac mismatch" for i in issues)

    def test_forged_hmac_reports_mismatch(self, reporter: AuditReporter, monkeypatch) -> None:
        monkeypatch.setenv("BANDI_AUDIT_HMAC_KEY", "audit-key")
        reporter.record_event({"action": "x", "seq": 1})
        entries = _chain_entries(reporter)
        entries[0]["hmac"] = "0" * 64
        _rewrite_chain(reporter, entries)
        assert any(i["error"] == "hmac mismatch" for i in reporter.verify())

    def test_stripped_hmac_reports_missing(self, reporter: AuditReporter, monkeypatch) -> None:
        monkeypatch.setenv("BANDI_AUDIT_HMAC_KEY", "audit-key")
        reporter.record_event({"action": "x", "seq": 1})
        entries = _chain_entries(reporter)
        entries[0].pop("hmac")
        _rewrite_chain(reporter, entries)
        issues = reporter.verify()
        assert any(i["error"] == "hmac missing" for i in issues)
        assert not any(i["error"] == "hash mismatch" for i in issues)

    def test_historical_chain_without_hmac_verifies_hash_only(
        self, reporter: AuditReporter, monkeypatch
    ) -> None:
        reporter.record_event({"action": "old", "seq": 1})
        reporter.record_event({"action": "old", "seq": 2})
        monkeypatch.setenv("BANDI_AUDIT_HMAC_KEY", "audit-key")
        reporter.record_event({"action": "new", "seq": 3})
        issues = reporter.verify()
        assert sorted(i["line"] for i in issues) == [1, 2]
        assert all(i["error"] == "hmac missing" for i in issues)
        assert len(_chain_entries(reporter)) == 3
        assert "hmac" in _chain_entries(reporter)[2]

    def test_chain_break_still_detected_with_key(
        self, reporter: AuditReporter, monkeypatch
    ) -> None:
        monkeypatch.setenv("BANDI_AUDIT_HMAC_KEY", "audit-key")
        reporter.record_event({"action": "x", "seq": 1})
        reporter.record_event({"action": "x", "seq": 2})
        entries = _chain_entries(reporter)
        entries[1]["prev_hash"] = "0" * 64
        _rewrite_chain(reporter, entries)
        assert any(i["error"] == "chain break" for i in reporter.verify())

    def test_key_does_not_change_entry_hash_algorithm(
        self, reporter: AuditReporter, monkeypatch
    ) -> None:
        monkeypatch.setenv("BANDI_AUDIT_HMAC_KEY", "audit-key")
        reporter.record_event({"action": "x", "seq": 1})
        entry = _chain_entries(reporter)[0]
        assert len(entry["entry_hash"]) == 64
        assert entry["entry_hash"] != entry["hmac"]


class TestHmacTypeGuard:
    def test_non_string_stored_returns_false(self) -> None:
        # 破坏者：链中 hmac 为数字时 compare_digest(str, int) 抛 TypeError → verify 炸
        assert verify_entry_hmac("raw", 123, "key") is False
        assert verify_entry_hmac("raw", None, "key") is False

    def test_non_string_hmac_reported_without_crash(
        self, reporter: AuditReporter, monkeypatch
    ) -> None:
        monkeypatch.setenv("BANDI_AUDIT_HMAC_KEY", "audit-key")
        reporter.record_event({"action": "x", "seq": 1})
        entries = _chain_entries(reporter)
        entries[0]["hmac"] = 123
        _rewrite_chain(reporter, entries)
        issues = reporter.verify()
        assert any(i["error"] == "hmac invalid type" for i in issues)


class TestAuditVerifyExitCode:
    def _seed_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[AuditReporter, SessionStore]:
        # 功能：在隔离 home 下建库并返回 reporter（链文件走默认路径，供 CLI 直读）
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        data_dir = tmp_path / ".bandi" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        store = SessionStore(str(data_dir / "bandi.db"))
        store.close()
        reopened = SessionStore(str(data_dir / "bandi.db"))
        return AuditReporter(reopened), reopened

    @staticmethod
    def _invoke_audit_verify() -> Any:
        from typer.testing import CliRunner

        from bandi.cli.app import app as cli_app

        return CliRunner().invoke(cli_app, ["audit-verify"])

    def test_clean_chain_exit_zero(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("BANDI_AUDIT_HMAC_KEY", raising=False)
        reporter, store = self._seed_chain(tmp_path, monkeypatch)
        reporter.record_event({"action": "clean"})
        store.close()
        result = self._invoke_audit_verify()
        assert result.exit_code == 0, result.output

    def test_stripped_hmac_exit_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("BANDI_AUDIT_HMAC_KEY", "cli-key")
        reporter, store = self._seed_chain(tmp_path, monkeypatch)
        reporter.record_event({"action": "x", "seq": 1})
        entries = _chain_entries(reporter)
        entries[0].pop("hmac")
        _rewrite_chain(reporter, entries)
        store.close()
        result = self._invoke_audit_verify()
        assert result.exit_code != 0, result.output

    def test_tampered_hmac_exit_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("BANDI_AUDIT_HMAC_KEY", "cli-key")
        reporter, store = self._seed_chain(tmp_path, monkeypatch)
        reporter.record_event({"action": "x", "seq": 1})
        entries = _chain_entries(reporter)
        entries[0]["hmac"] = "0" * 64
        _rewrite_chain(reporter, entries)
        store.close()
        result = self._invoke_audit_verify()
        assert result.exit_code != 0, result.output

    def test_broken_chain_exit_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("BANDI_AUDIT_HMAC_KEY", raising=False)
        reporter, store = self._seed_chain(tmp_path, monkeypatch)
        reporter.record_event({"action": "x", "seq": 1})
        entries = _chain_entries(reporter)
        entries[0]["prev_hash"] = "0" * 64
        _rewrite_chain(reporter, entries)
        store.close()
        result = self._invoke_audit_verify()
        assert result.exit_code != 0, result.output
