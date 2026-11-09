# 功能：契约——company 域复查加固：CLI reorg 同库 / 会议共识不虚标 /
from __future__ import annotations

from pathlib import Path


def test_contract_cli_reorg_shares_company_db(tmp_path: Path, monkeypatch) -> None:
    """契约（同库·破坏者）：CLI reorg 必须与 dispatcher 共享 company.db——
    修复前读 company_reorg.db 空库，任务迁移恒 "0 tasks migrated" 静默失效。"""
    import pathlib

    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    import bandi.cli.company as cli

    cli._reorg_manager = None
    cli._reorg_store = None
    mgr = cli._reorg()
    expected = tmp_path / ".bandi" / "company" / "company.db"
    assert Path(mgr.store.db_path) == expected, (
        f"reorg 必须与 dispatcher 同库: {mgr.store.db_path} != {expected}"
    )
    cli._reorg_manager = None
    cli._reorg_store = None


def test_contract_custom_runtime_invalidate_unhooks_dispatcher() -> None:
    """契约（回调不泄漏·破坏者）：invalidate 必须注销 dispatcher wake 回调——
    修复前只 release_all，_DISPATCHER_WAKE_HOOKS 单调增长（长驻进程泄漏 +
    唤醒已失效实例）。"""
    from types import SimpleNamespace

    from bandi.company.custom_runtime import CustomRuntimeRunner

    class _StubDispatcher:
        def __init__(self) -> None:
            self.shutdown_called = 0
            self.released = 0

        def shutdown(self) -> None:
            self.shutdown_called += 1

        def release_all(self) -> None:
            self.released += 1

    stub = _StubDispatcher()
    mgr = CustomRuntimeRunner.__new__(CustomRuntimeRunner)
    mgr._bundle_cache = {("org", "p", "s", "", ()): SimpleNamespace(dispatcher=stub)}

    mgr.invalidate()

    assert stub.shutdown_called == 1, "invalidate 必须调用 dispatcher.shutdown()（注销 wake 回调）"
    assert stub.released == 1, "release_all 语义保持"
