# 功能：契约——会话归档沉淀（POST /api/sessions/{id}/archive：LLM 摘要写项目 AGENTS.md 受管区块）。
# 钉行为：有项目会话 → 区块写入可读回 / 无项目·无目录 → summarized False 零副作用 /
# 归档后 status 置 archived 且默认列表不再出现（刷新不复活）/ archived 参数可反查 /
# 破坏者：二次归档幂等不重复写 / 摘要抛错不阻断且 status 不变（summarized False + reason 不 500）/
# 会话不存在 404 / 受管区块上限 20 条且区块外内容严格保留。
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


class _FakeProvider:
    """功能：归档摘要测试 provider（accounted_complete 需要的 complete 形态）。"""

    model_name = "fake-archive"

    async def complete(self, messages, **kwargs):
        return SimpleNamespace(
            content=("## 决策\n- 采用方案 A\n## 结论\n- 手冲定价 28 元\n## 待办\n- 印制新菜单")
        )


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """company 单例 + 归档 provider 隔离（每测试重建装配）。"""
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._custom_runtime_runner", None),
        patch("bandi.api.app._team_store", None),
        patch("bandi.api.app._project_store", None),
        patch("bandi.api.app._archive_provider", _FakeProvider()),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _make_project(tmp_path: Path, project_id: str = "p-arch", root_dir: str | None = None) -> Path:
    workdir = tmp_path / "proj-work"
    if root_dir is None:
        workdir.mkdir(exist_ok=True)
        root_dir = str(workdir)
    resp = client.post(
        "/api/company/projects",
        json={"project_id": project_id, "name": "归档项目", "root_dir": root_dir},
    )
    assert resp.status_code == 200, resp.text
    return workdir


def _make_session(tmp_path: Path, project_id: str = "p-arch", title: str = "测试会话") -> str:
    from bandi.memory.session.store import SessionStore

    db_dir = tmp_path / ".bandi" / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(str(db_dir / "bandi.db"))
    return store.create_session(title=title, project_id=project_id or None)


def _status_of(tmp_path: Path, session_id: str) -> str:
    """功能：直查库读会话 status（不依赖 API 投影）。"""
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(tmp_path / ".bandi" / "data" / "bandi.db"))
    row = store.get_session(session_id)
    assert row is not None, "归档不得物理删除会话行（软删除）"
    return str(row.get("status") or "active")


class TestArchiveToProject:
    def test_writes_block_and_readback(self, tmp_path: Path) -> None:
        workdir = _make_project(tmp_path)
        session_id = _make_session(tmp_path)
        resp = client.post(f"/api/sessions/{session_id}/archive")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["summarized"] is True, payload
        assert payload["distilled"] is True, "项目会话必须沉淀摘要到项目 AGENTS.md"
        text = (workdir / "AGENTS.md").read_text(encoding="utf-8")
        assert "<!-- bandi:archives -->" in text, "必须写入受管区块"
        assert "## 决策" in text and "## 结论" in text and "## 待办" in text, (
            "摘要必须含固定三段小标题（结构契约，供程序解析与去重）"
        )
        assert "测试会话" in text, "条目必须含会话标题"

    def test_plain_session_archives_without_writing_file(self, tmp_path: Path) -> None:
        """普通会话（无项目）归档 = 纯状态切换：不调 LLM、不写任何文件（零成本瞬时可逆）。"""
        session_id = _make_session(tmp_path, project_id="")
        with patch(
            "bandi.company.project_archive.summarize_session",
            side_effect=AssertionError("普通会话不得触发摘要"),
        ):
            resp = client.post(f"/api/sessions/{session_id}/archive")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["archived"] is True, payload
        assert payload["distilled"] is False, "普通会话不沉淀摘要"
        assert not (tmp_path / ".bandi" / "AGENTS.md").exists(), "不得写全局 AGENTS.md"
        assert not list((tmp_path / "proj-work").rglob("AGENTS.md")), "不得写项目目录"
        assert _status_of(tmp_path, session_id) == "archived"

    def test_project_without_root_dir(self, tmp_path: Path) -> None:
        _make_project(tmp_path, project_id="p-noroot", root_dir="")
        session_id = _make_session(tmp_path, project_id="p-noroot")
        resp = client.post(f"/api/sessions/{session_id}/archive")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["summarized"] is False
        assert payload["reason"] == "项目未设置工作目录"

    def test_second_archive_conflicts(self, tmp_path: Path) -> None:
        """破坏者视角：绕过 UI 二次归档必须 409 且不产生第二条条目。"""
        workdir = _make_project(tmp_path)
        session_id = _make_session(tmp_path)
        assert client.post(f"/api/sessions/{session_id}/archive").status_code == 200
        second = client.post(f"/api/sessions/{session_id}/archive")
        assert second.status_code == 409, "已归档会话二次归档必须 409"
        assert second.json()["message"] == "SESSION_ALREADY_ARCHIVED"
        text = (workdir / "AGENTS.md").read_text(encoding="utf-8")
        assert text.count("<!-- bandi:archives -->") == 1, "受管区块必须唯一（重写而非追加标记）"
        assert text.count("### [") == 1, "重复归档不得产生第二条条目"

    def test_breaker_summary_error_not_blocking(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        session_id = _make_session(tmp_path)
        with patch(
            "bandi.company.project_archive.summarize_session",
            side_effect=RuntimeError("llm down"),
        ):
            resp = client.post(f"/api/sessions/{session_id}/archive")
        assert resp.status_code == 200, "摘要失败必须 200 兜底不裸崩"
        payload = resp.json()
        assert payload["summarized"] is False
        assert "摘要失败" in payload["reason"]

    def test_unknown_session_404(self) -> None:
        resp = client.post("/api/sessions/ghost-session/archive")
        assert resp.status_code == 404


class TestArchiveHidesSession:
    def test_status_flipped_to_archived(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        session_id = _make_session(tmp_path)
        assert client.post(f"/api/sessions/{session_id}/archive").status_code == 200
        assert _status_of(tmp_path, session_id) == "archived", "归档必须落库为 archived"

    def test_absent_from_default_list(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        session_id = _make_session(tmp_path)
        listed = client.get("/api/sessions").json()
        assert any(item["id"] == session_id for item in listed), "归档前必须在列表中"
        client.post(f"/api/sessions/{session_id}/archive")
        listed_after = client.get("/api/sessions").json()
        assert not any(item["id"] == session_id for item in listed_after), (
            "归档后不得再出现在默认列表"
        )

    def test_archived_flag_lists_it_back(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        session_id = _make_session(tmp_path)
        client.post(f"/api/sessions/{session_id}/archive")
        archived = client.get("/api/sessions", params={"archived": True}).json()
        assert any(item["id"] == session_id for item in archived), "必须提供反查已归档会话的入口"
        assert any(item["id"] == session_id for item in client.get("/api/sessions").json()) is False

    def test_single_get_still_readable(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        session_id = _make_session(tmp_path)
        client.post(f"/api/sessions/{session_id}/archive")
        resp = client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 200, "软删除后单查仍可读（可追溯）"
        assert resp.json()["session"]["status"] == "archived"

    def test_failure_paths_keep_status_active(self, tmp_path: Path) -> None:
        _make_project(tmp_path, project_id="p-noroot2", root_dir="")
        session_no_root = _make_session(tmp_path, project_id="p-noroot2")
        resp = client.post(f"/api/sessions/{session_no_root}/archive")
        assert resp.json()["summarized"] is False
        assert _status_of(tmp_path, session_no_root) == "active", "归档失败不得改变会话状态"

        session_dead_project = _make_session(tmp_path, project_id="p-gone")
        client.delete("/api/company/projects/p-gone")
        resp2 = client.post(f"/api/sessions/{session_dead_project}/archive")
        assert resp2.status_code == 200
        assert resp2.json()["summarized"] is False
        assert _status_of(tmp_path, session_dead_project) == "active", "项目缺失不得改变会话状态"

        _make_project(tmp_path, project_id="p-ok3")
        session_llm_down = _make_session(tmp_path, project_id="p-ok3")
        with patch(
            "bandi.company.project_archive.summarize_session",
            side_effect=RuntimeError("llm down"),
        ):
            client.post(f"/api/sessions/{session_llm_down}/archive")
        assert _status_of(tmp_path, session_llm_down) == "active", "摘要失败不得把会话标记为已归档"


class TestArchiveBlockUpsert:
    def test_parsed_sections_expose_points(self) -> None:
        """摘要按固定小标题可被程序解析（去重与后续汇流的输入）。"""
        from bandi.company.project_archive import ARCHIVE_SECTIONS, parse_archive_points

        body = "## 决策\n- 采用方案 A\n- 采用方案 A\n- 采用方案 B\n## 结论\n- 定价 28 元\n"
        buckets = parse_archive_points(body)
        assert set(buckets) == set(ARCHIVE_SECTIONS), "三段小标题必须齐全"
        assert buckets["决策"] == ["采用方案 A", "采用方案 B"], "同段内重复要点必须去重"
        assert buckets["结论"] == ["定价 28 元"]
        assert buckets["待办"] == [], "缺失小标题返回空列表而不是报错"

    def test_block_cap_keeps_latest_20(self) -> None:
        from bandi.company.project_archive import (
            MAX_ARCHIVE_ENTRIES,
            render_archive_entry,
            upsert_archive_block,
        )

        text = ""
        for index in range(MAX_ARCHIVE_ENTRIES + 3):
            text = upsert_archive_block(
                text, render_archive_entry(f"会话{index}", f"- 要点{index}")
            )
        assert text.count("### [") == MAX_ARCHIVE_ENTRIES, "超出上限必须移除最旧"
        assert "会话22" in text, "最新条目必须保留"
        assert "会话0" not in text, "最旧条目必须被淘汰"
        assert text.count("<!-- bandi:archives -->") == 1

    def test_block_preserves_human_content(self) -> None:
        from bandi.company.project_archive import render_archive_entry, upsert_archive_block

        human = "# 项目规则\n\n人类手写的内容，不能被覆盖。"
        once = upsert_archive_block(human, render_archive_entry("会话A", "- 要点A"))
        assert human in once, "区块外人类内容必须原样保留"
        twice = upsert_archive_block(once, render_archive_entry("会话B", "- 要点B"))
        assert human in twice
        assert "会话A" in twice and "会话B" in twice
