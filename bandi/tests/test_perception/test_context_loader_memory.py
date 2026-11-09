from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bandi.core.message import Message
from bandi.memory.session.store import SessionStore
from bandi.perception.context_loader import ContextLoader


class TestProjectMemoryInjection:
    def test_memory_md_injected_into_project_instructions(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        mem_dir = proj / ".bandi" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "MEMORY.md").write_text(
            "# 项目记忆\n\n## 关键事实\n用户偏好中文回复\n", encoding="utf-8"
        )
        store = SessionStore(":memory:")
        loader = ContextLoader(store, project_root=str(proj))
        bundle = asyncio.run(loader.load(Message(role="user", content="hi"), ""))
        assert "用户偏好中文回复" in bundle.project_instructions

    def test_memory_md_without_project_root_not_injected(self, tmp_path: Path) -> None:
        store = SessionStore(":memory:")
        loader = ContextLoader(store)
        bundle = asyncio.run(loader.load(Message(role="user", content="hi"), ""))
        assert "MEMORY.md" not in bundle.project_instructions

    def test_missing_memory_md_is_noop(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        store = SessionStore(":memory:")
        loader = ContextLoader(store, project_root=str(proj))
        bundle = asyncio.run(loader.load(Message(role="user", content="hi"), ""))
        assert bundle.project_instructions == ""


class TestProjectRootFromSessionMetadata:
    def test_session_metadata_project_root_wins_over_init(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        mem_dir = proj / ".bandi" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "MEMORY.md").write_text("## 事实\n会话级项目记忆\n", encoding="utf-8")
        store = SessionStore(":memory:")
        sid = store.create_session()
        store.update_session(
            sid,
            metadata=json.dumps({"project_root": str(proj)}, ensure_ascii=False),
        )
        loader = ContextLoader(store)  # 无 init project_root
        bundle = asyncio.run(loader.load(Message(role="user", content="hi"), sid))
        assert "会话级项目记忆" in bundle.project_instructions

    def test_no_session_project_root_uses_init_root(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        mem_dir = proj / ".bandi" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "MEMORY.md").write_text("## 事实\ninit 项目记忆\n", encoding="utf-8")
        store = SessionStore(":memory:")
        sid = store.create_session()
        loader = ContextLoader(store, project_root=str(proj))
        bundle = asyncio.run(loader.load(Message(role="user", content="hi"), sid))
        assert "init 项目记忆" in bundle.project_instructions
