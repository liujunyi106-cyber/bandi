# 功能：项目上下文 TTL 缓存 + 热重载契约
# 契约：①同一文件 stat(mtime,size) 未变 → 只读一次，重复 load 零磁盘 IO；
# ②文件修改（mtime/size 变化）→ 自动重新读取（热重载，无需显式失效）；
# ③不同文件路径缓存键隔离；④模块级单例跨 ContextLoader 实例共享（每轮
# run_agent_turn 新建 pipeline 仍命中）；⑤ContextLoader 读 AGENTS.md/CLAUDE.md/

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.perception.project_file_cache import get_project_file_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    get_project_file_cache().clear()
    yield
    get_project_file_cache().clear()


def _reads(monkeypatch: pytest.MonkeyPatch, path: Path) -> list[int]:
    reads: list[int] = []
    original = Path.read_text

    def counting(self, *a, **kw):
        if self == path:
            reads.append(1)
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", counting)
    return reads


def test_cache_reads_disk_once(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """契约：内容未变时重复读取只落盘一次。"""
    f = tmp_path / "AGENTS.md"
    f.write_text("规则", encoding="utf-8")
    reads = _reads(monkeypatch, f)
    cache = get_project_file_cache()
    assert cache.read_text(f) == "规则"
    assert cache.read_text(f) == "规则"
    assert cache.read_text(f) == "规则"
    assert reads == [1], "stat 未变时不得重复 read_text"


def test_hot_reload_on_mtime_change(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """契约：文件被修改后自动读取新内容（mtime/size 判定，热重载）。"""
    f = tmp_path / "CLAUDE.md"
    f.write_text("v1", encoding="utf-8")
    reads = _reads(monkeypatch, f)
    cache = get_project_file_cache()
    assert cache.read_text(f) == "v1"
    f.write_text("v2 内容更长更长了", encoding="utf-8")
    assert cache.read_text(f) == "v2 内容更长更长了"
    assert len(reads) == 2, "内容变化必须重新读取"


def test_hot_reload_same_size_different_content(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约：同 size 但 mtime 变化（编辑器保存必然更新 mtime）也必须热重载。"""
    import os

    f = tmp_path / "MEMORY.md"
    f.write_text("AAAA", encoding="utf-8")
    cache = get_project_file_cache()
    assert cache.read_text(f) == "AAAA"
    f.write_text("BBBB", encoding="utf-8")  # 同长度 4
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert cache.read_text(f) == "BBBB", "同 size 异内容需靠 mtime 判定重载"


def test_cache_key_isolated_by_path(tmp_path: Any) -> None:
    """契约：不同文件路径互不污染。"""
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")
    cache = get_project_file_cache()
    assert cache.read_text(a) == "A"
    assert cache.read_text(b) == "B"
    b.write_text("B2", encoding="utf-8")
    assert cache.read_text(a) == "A"
    assert cache.read_text(b) == "B2"


def test_missing_file_not_cached_as_error(tmp_path: Any) -> None:
    """契约：文件不存在返回 None 且不污染后续（文件出现后可正常读取）。"""
    f = tmp_path / "ghost.md"
    cache = get_project_file_cache()
    assert cache.read_text(f) is None
    f.write_text("now exists", encoding="utf-8")
    assert cache.read_text(f) == "now exists"


def test_singleton_shared_across_instances(tmp_path: Any) -> None:
    """契约：模块级单例——两个 get 调用拿到同一缓存实例。"""
    assert get_project_file_cache() is get_project_file_cache()


def test_context_loader_uses_cache_for_project_files(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约：ContextLoader 加载项目指令走缓存——两次构造实例不重复读盘。"""
    from bandi.perception.context_loader import ContextLoader

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("AGENTS 规则", encoding="utf-8")
    (proj / "CLAUDE.md").write_text("CLAUDE 规则", encoding="utf-8")
    memory_dir = proj / ".bandi" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("记忆内容", encoding="utf-8")

    reads: list[str] = []
    original = Path.read_text

    def counting(self, *a, **kw):
        reads.append(str(self))
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", counting)
    # 需真实 SessionStore？ContextLoader 直接以 project_root 构造即可触发加载
    loader_a = ContextLoader(None, project_root=str(proj))
    loader_b = ContextLoader(None, project_root=str(proj))
    assert "AGENTS 规则" in loader_a.get_project_instructions()
    assert loader_b.get_project_instructions() == loader_a.get_project_instructions()
    disk_reads = [p for p in reads if p.endswith(("AGENTS.md", "CLAUDE.md", "MEMORY.md"))]
    assert len(disk_reads) <= 3, (
        f"两次构造最多落盘 3 次（每个文件一次），实际 {len(disk_reads)} 次: {disk_reads}"
    )


def test_hot_reload_visible_via_context_loader(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约：AGENTS.md 修改后重建 ContextLoader 读到新内容（热重载闭环）。"""
    from bandi.perception.context_loader import ContextLoader

    proj = tmp_path / "proj2"
    proj.mkdir()
    f = proj / "AGENTS.md"
    f.write_text("旧规则", encoding="utf-8")
    assert "旧规则" in ContextLoader(None, project_root=str(proj)).get_project_instructions()
    f.write_text("新规则内容", encoding="utf-8")
    assert "新规则内容" in ContextLoader(None, project_root=str(proj)).get_project_instructions()
