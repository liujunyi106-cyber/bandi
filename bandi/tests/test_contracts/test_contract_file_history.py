# 功能：契约——文件级快照回滚。
# 钉行为：编辑前按需备份（同 (path, turn) 幂等保护 v1）/ 版本索引持久化与损坏隔离 /
# 回滚语义（恢复 turn>=T 最早版本；删除标记；内容一致 skip；dry-run；幂等；备份缺失
# unavailable 不 touch）/ 会话路径安全（摘要目录）/ 三写工具 executor 接线 / CLI·API 入口。
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bandi.agent.file_history import (
    FileHistoryStore,
    get_file_history_store,
    reset_file_history_store,
)


@pytest.fixture(autouse=True)
def _isolate_file_history(tmp_path: Path) -> None:
    reset_file_history_store(tmp_path / "file-history")
    yield
    reset_file_history_store()


@pytest.fixture
def store(tmp_path: Path) -> FileHistoryStore:
    return FileHistoryStore(base_dir=tmp_path / "file-history")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(ws))
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", ws)
    return ws


class _AutoApprove:
    # 功能：E2E 用同步交互桩（模拟 CLI 在场，审批门零介入 + ask 层放行）
    async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
        return True

    async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool:
        return True

    async def ask_input(self, prompt: str, *, secret: bool = False) -> str:
        return ""

    async def show_notification(self, message: str, level: str = "info") -> None:
        return None


# ──────────────────────────────────────────────────────────────────────
# A. 版本记录族
# ──────────────────────────────────────────────────────────────────────


class TestFileHistoryRecord:
    def test_first_record_backs_up_original_content(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.txt"
        f.write_text("v1 content", encoding="utf-8")
        entry = store.record_edit(f, "s1", 1)
        assert entry is not None
        assert entry.version == 1
        assert entry.turn_index == 1
        backup = store.base_dir / next(iter(store.base_dir.iterdir())).name / str(entry.backup_name)
        assert backup.read_text(encoding="utf-8") == "v1 content"

    def test_same_turn_record_protects_v1(self, store: FileHistoryStore, tmp_path: Path) -> None:
        # 破坏者：同 (path, turn) 重复记录（第二次时文件已改）——v1 备份不得被覆盖
        f = tmp_path / "a.txt"
        f.write_text("original", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        f.write_text("edited", encoding="utf-8")
        again = store.record_edit(f, "s1", 1)
        assert again is not None and again.version == 1
        versions = store.versions("s1")
        assert [v.version for v in versions] == [1]

        report = store.rewind("s1", 1)
        assert f.read_text(encoding="utf-8") == "original", "v1 必须仍是编辑前内容"
        assert report.changed

    def test_cross_turn_creates_next_version(self, store: FileHistoryStore, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("v0", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        f.write_text("v1", encoding="utf-8")
        second = store.record_edit(f, "s1", 2)
        assert second is not None and second.version == 2
        assert [v.version for v in store.versions("s1")] == [1, 2]

    def test_missing_file_records_null_backup(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        entry = store.record_edit(tmp_path / "ghost.txt", "s1", 1)
        assert entry is not None and entry.backup_name is None

    def test_multiple_files_same_turn(self, store: FileHistoryStore, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("A", encoding="utf-8")
        b.write_text("B", encoding="utf-8")
        store.record_edit(a, "s1", 3)
        store.record_edit(b, "s1", 3)
        assert {Path(v.path).name for v in store.versions("s1")} == {"a.txt", "b.txt"}

    def test_version_cap_prunes_oldest_and_files(
        self, store: FileHistoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bandi.agent.file_history.MAX_VERSIONS_PER_SESSION", 3)
        f = tmp_path / "a.txt"
        for turn in range(1, 5):
            f.write_text(f"content-{turn}", encoding="utf-8")
            store.record_edit(f, "s1", turn)
        versions = store.versions("s1")
        assert [v.turn_index for v in versions] == [2, 3, 4], "超限必须裁最旧条目"
        backups = list(store.base_dir.rglob("*@v*"))
        assert len(backups) == 3, f"被裁条目的备份文件必须清理，实际: {backups}"

    def test_sessions_are_isolated(self, store: FileHistoryStore, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        store.record_edit(f, "s2", 1)
        assert len(store.versions("s1")) == 1
        assert len(store.versions("s2")) == 1

    def test_unsafe_session_id_uses_hashed_dir(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        # 破坏者：company 会话 id 含 :: 等字符——目录必须摘要化，且可正常读写
        sid = "role-session::ceo"
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        assert store.record_edit(f, sid, 1) is not None
        subdirs = [p for p in store.base_dir.iterdir() if p.is_dir()]
        assert len(subdirs) == 1
        assert "::" not in subdirs[0].name and "role" not in subdirs[0].name
        assert len(store.versions(sid)) == 1

    def test_corrupt_index_quarantined_and_rebuilt(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        index_file = next(store.base_dir.rglob("history.json"))
        index_file.write_text("{ not json", encoding="utf-8")
        f.write_text("y", encoding="utf-8")
        entry = store.record_edit(f, "s1", 2)
        assert entry is not None
        assert list(store.base_dir.rglob("*.corrupt")), "损坏索引必须隔离而非静默丢失"
        assert [v.turn_index for v in store.versions("s1")] == [2]


# ──────────────────────────────────────────────────────────────────────
# B. 回滚语义族
# ──────────────────────────────────────────────────────────────────────


class TestFileHistoryRewind:
    def test_rewind_restores_earliest_version_since_turn(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.txt"
        f.write_text("v0", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        f.write_text("v1", encoding="utf-8")
        store.record_edit(f, "s1", 2)
        f.write_text("v2", encoding="utf-8")

        report = store.rewind("s1", 2)

        assert f.read_text(encoding="utf-8") == "v1", "撤销 turn>=2 的编辑（恢复到第 2 轮开始前）"
        assert report.changed and not report.dry_run

    def test_rewind_to_earliest_turn_restores_v1(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.txt"
        f.write_text("v0", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        f.write_text("v1", encoding="utf-8")
        store.record_edit(f, "s1", 2)
        f.write_text("v2", encoding="utf-8")

        store.rewind("s1", 1)

        assert f.read_text(encoding="utf-8") == "v0"

    def test_rewind_ignores_edits_before_target_turn(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.txt"
        f.write_text("orig", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        f.write_text("edited", encoding="utf-8")

        report = store.rewind("s1", 2)

        assert f.read_text(encoding="utf-8") == "edited", "目标轮之前发生的编辑不得被撤销"
        assert report.changed == []

    def test_rewind_middle_turn_picks_next_edit_version(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        # 核心语义：T 落在两次编辑之间——取 turn>=T 最早版本（=下一次编辑的编辑前内容）
        f = tmp_path / "a.txt"
        f.write_text("A", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        f.write_text("B", encoding="utf-8")
        store.record_edit(f, "s1", 3)
        f.write_text("C", encoding="utf-8")

        store.rewind("s1", 2)

        assert f.read_text(encoding="utf-8") == "B", "T=2 时文件应为 B（turn3 编辑前状态）"

    def test_rewind_deletes_file_created_after_target(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        created = tmp_path / "new.txt"
        store.record_edit(created, "s1", 2)
        created.write_text("brand new", encoding="utf-8")

        report = store.rewind("s1", 2)

        assert not created.exists(), "编辑前不存在的文件在回滚时必须删除"
        assert str(created) in report.changed

    def test_rewind_skips_identical_content(self, store: FileHistoryStore, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("same", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        before_mtime = f.stat().st_mtime

        report = store.rewind("s1", 1)

        assert [a.action for a in report.actions] == ["skip"]
        assert f.stat().st_mtime == before_mtime, "内容一致时不得重写文件"
        assert report.changed == []

    def test_rewind_dry_run_reports_without_touching(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.txt"
        f.write_text("orig", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        f.write_text("edited", encoding="utf-8")

        report = store.rewind("s1", 1, dry_run=True)

        assert f.read_text(encoding="utf-8") == "edited", "dry-run 不得改动文件"
        assert [a.action for a in report.actions] == ["restore"]
        assert report.dry_run is True

    def test_rewind_is_idempotent(self, store: FileHistoryStore, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("orig", encoding="utf-8")
        store.record_edit(f, "s1", 1)
        f.write_text("edited", encoding="utf-8")

        store.rewind("s1", 1)
        report = store.rewind("s1", 1)

        assert report.changed == [], "重复回滚必须全 skip（幂等）"

    def test_rewind_unknown_session_returns_empty(self, store: FileHistoryStore) -> None:
        report = store.rewind("nobody", 1)
        assert report.actions == [] and report.changed == []

    def test_rewind_missing_backup_marked_unavailable(
        self, store: FileHistoryStore, tmp_path: Path
    ) -> None:
        # 破坏者：备份文件被删除——必须报告 unavailable 且不 touch 当前文件
        f = tmp_path / "a.txt"
        f.write_text("orig", encoding="utf-8")
        entry = store.record_edit(f, "s1", 1)
        assert entry is not None and entry.backup_name
        backup_file = next(store.base_dir.rglob(str(entry.backup_name)))
        backup_file.unlink()
        f.write_text("edited", encoding="utf-8")

        report = store.rewind("s1", 1)

        assert [a.action for a in report.actions] == ["unavailable"]
        assert f.read_text(encoding="utf-8") == "edited", "备份缺失时不得改动文件"
        assert report.changed == []


# ──────────────────────────────────────────────────────────────────────
# C. 三写工具 executor 接线族
# ──────────────────────────────────────────────────────────────────────


def _executor(workspace: Path, *tools: object):
    from bandi.tools.approval import reset_approval_store
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.permissions_pipeline import PermissionsPipeline
    from bandi.tools.registry import ToolRegistry

    reset_approval_store()
    registry = ToolRegistry()
    for tool in tools:
        registry.register_instance(tool)
    pipeline = PermissionsPipeline()
    pipeline.set_user_interaction(_AutoApprove())
    return ToolExecutor(
        registry=registry, project_root=str(workspace), permissions_pipeline=pipeline
    )


class TestToolWiring:
    @pytest.mark.asyncio
    async def test_edit_tool_records_and_rewinds(self, workspace: Path) -> None:
        from bandi.core.message import ToolCall
        from bandi.tools.file_edit import FileEditTool

        executor = _executor(workspace, FileEditTool())
        executor.bind_session_meta({"session_id": "s-ct05", "turn_index": 4})
        fp = workspace / "code.py"
        fp.write_text("old\n", encoding="utf-8")

        (result,) = await executor.execute(
            [
                ToolCall(
                    id="c1",
                    name="edit",
                    arguments={"file_path": str(fp), "old_string": "old", "new_string": "new"},
                )
            ]
        )
        assert result.success, result.error
        versions = get_file_history_store().versions("s-ct05")
        assert len(versions) == 1 and versions[0].turn_index == 4
        assert fp.read_text(encoding="utf-8") == "new\n"

        report = get_file_history_store().rewind("s-ct05", 4)
        assert fp.read_text(encoding="utf-8") == "old\n"
        assert report.changed

    @pytest.mark.asyncio
    async def test_write_tool_records_and_rewinds(self, workspace: Path) -> None:
        from bandi.core.message import ToolCall
        from bandi.tools.file_ops import FileWriteTool

        executor = _executor(workspace, FileWriteTool())
        executor.bind_session_meta({"session_id": "s-w", "turn_index": 1})
        fp = workspace / "note.md"
        fp.write_text("orig", encoding="utf-8")

        (result,) = await executor.execute(
            [
                ToolCall(
                    id="c1",
                    name="write",
                    arguments={"path": str(fp), "content": "overwritten"},
                )
            ]
        )
        assert result.success, result.error

        get_file_history_store().rewind("s-w", 1)
        assert fp.read_text(encoding="utf-8") == "orig"

    @pytest.mark.asyncio
    async def test_apply_patch_records_and_rewinds(self, workspace: Path) -> None:
        from bandi.core.message import ToolCall
        from bandi.tools.apply_patch import ApplyPatchTool

        executor = _executor(workspace, ApplyPatchTool())
        executor.bind_session_meta({"session_id": "s-p", "turn_index": 2})
        fp = workspace / "abc.txt"
        fp.write_text("abc\n", encoding="utf-8")

        patch_text = "--- a/abc.txt\n+++ b/abc.txt\n@@ -1 +1 @@\n-abc\n+xyz\n"
        (result,) = await executor.execute(
            [ToolCall(id="c1", name="apply_patch", arguments={"patch": patch_text})]
        )
        assert result.success, result.error
        assert fp.read_text(encoding="utf-8") == "xyz\n"

        get_file_history_store().rewind("s-p", 2)
        assert fp.read_text(encoding="utf-8") == "abc\n"

    @pytest.mark.asyncio
    async def test_direct_call_without_context_does_not_crash(self, workspace: Path) -> None:
        from bandi.tools.file_edit import FileEditTool

        fp = workspace / "solo.txt"
        fp.write_text("a\n", encoding="utf-8")
        result = await FileEditTool().execute(
            {"file_path": str(fp), "old_string": "a", "new_string": "b"}
        )
        assert "replaced" in result
        assert get_file_history_store().versions("") == [], "无会话上下文不得记录"


# ──────────────────────────────────────────────────────────────────────
# D. CLI / API 入口族
# ──────────────────────────────────────────────────────────────────────


class TestRevertFilesEntrypoints:
    def _seed(self, tmp_path: Path, sid: str) -> Path:
        from bandi.memory.session.store import SessionStore

        home = tmp_path / "home"
        db_path = home / ".bandi" / "data" / "bandi.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = SessionStore(str(db_path))
        store.create_session(sid, title="t")
        store.close()
        f = tmp_path / "doc.txt"
        f.write_text("orig", encoding="utf-8")
        get_file_history_store().record_edit(f, sid, 1)
        f.write_text("edited", encoding="utf-8")
        return f

    def test_cli_revert_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        from bandi.cli.app import app as cli_app

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        f = self._seed(tmp_path, "s-cli")

        runner = CliRunner()
        result = runner.invoke(cli_app, ["session", "revert-files", "s-cli", "--turn", "1"])

        assert result.exit_code == 0, result.output
        assert f.read_text(encoding="utf-8") == "orig"

    def test_api_revert_files_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        from bandi.api.app import app
        from bandi.core.config import BandiConfig

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        f = self._seed(tmp_path, "s-api")
        db_path = tmp_path / "home" / ".bandi" / "data" / "bandi.db"
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        with (
            patch("bandi.core.config.BandiConfig.load", return_value=cfg),
            patch("bandi.api.app._API_KEY", "test"),
            patch("bandi.api.app._db_path", db_path),
            patch("bandi.api.app._store", None),
        ):
            client = TestClient(app)
            client.headers["Authorization"] = "Bearer test"

            missing = client.post("/api/sessions/nope/revert-files", json={"turn": 1})
            assert missing.status_code == 404

            dry = client.post("/api/sessions/s-api/revert-files", json={"turn": 1, "dry_run": True})
            assert dry.status_code == 200
            assert dry.json()["dry_run"] is True
            assert f.read_text(encoding="utf-8") == "edited", "dry-run 不得改动文件"

            resp = client.post("/api/sessions/s-api/revert-files", json={"turn": 1})
            assert resp.status_code == 200
            assert resp.json()["changed"], resp.text
            assert f.read_text(encoding="utf-8") == "orig"
