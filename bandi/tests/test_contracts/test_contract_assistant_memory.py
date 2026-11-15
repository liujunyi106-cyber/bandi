# 功能：契约 — 助理项目会话无 employee_id 时仍可读取 global/project MEMORY.md
from __future__ import annotations

from pathlib import Path

from bandi.company.store import WorkItemStore
from bandi.memory.global_store import GlobalMemoryStore
from bandi.memory.markdown_memory import MarkdownMemoryStore
from bandi.memory.memory_manager import MemoryHierarchyManager


def test_project_memory_context_without_employee(tmp_path: Path) -> None:
    hierarchy = MemoryHierarchyManager(
        global_store=GlobalMemoryStore(tmp_path / "global.db"),
        markdown_memory=MarkdownMemoryStore(tmp_path / "md"),
        work_item_store=WorkItemStore(str(tmp_path / "snap.db")),
        store_dir=tmp_path / "md-store",
    )
    hierarchy.append_memory("project", "项目约定：先确认验收标准", project_id="p1")
    hierarchy.save_memory("global", "[教训] 先读现有代码", allow_shared=True)

    text = hierarchy.collect_memory_context(
        project_id="p1",
        session_id="s1",
        employee_id="",
    )

    assert "项目约定：先确认验收标准" in text
    assert "先读现有代码" in text
