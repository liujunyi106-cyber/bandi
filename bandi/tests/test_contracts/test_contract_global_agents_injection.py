# 功能：契约——全局 AGENTS.md（user 级）注入（A方案
# 普通会话归档落 ~/.bandi/AGENTS.md；所有会话注入 user 层规则，项目层在后）。
# 追加：用户偏好专区（~/.bandi/preferences.md —— user 私有，不混项目/团队；
# 注入含路径提示，供用户让 LLM 追加写入）。
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bandi.core.message import Message


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))


def _loader(project_root: str | None = None):
    from bandi.memory.session.store import SessionStore
    from bandi.perception.context_loader import ContextLoader

    return ContextLoader(SessionStore(":memory:"), project_root=project_root)


def _load(loader) -> str:
    msg = Message(role="user", content="hello")
    bundle = asyncio.run(loader.load(msg, ""))
    return bundle.project_instructions


def test_global_agents_md_injected(tmp_path: Path) -> None:
    """契约（A 核心）：~/.bandi/AGENTS.md 存在时注入到所有会话的
    project_instructions（user 层规则对无项目会话同样生效）。"""
    (tmp_path / ".bandi").mkdir(exist_ok=True)
    (tmp_path / ".bandi" / "AGENTS.md").write_text(
        "# 用户全局规则\n\n- 全局偏好示例内容XYZ", encoding="utf-8"
    )
    text = _load(_loader())
    assert "全局偏好示例内容XYZ" in text, f"全局 AGENTS.md 未注入：{text!r}"


def test_global_before_project(tmp_path: Path) -> None:
    """契约（顺序）：全局在后注入无法被项目覆盖——全局内容必须排在项目内容之前。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("项目规则内容ABC", encoding="utf-8")
    (tmp_path / ".bandi").mkdir(exist_ok=True)
    (tmp_path / ".bandi" / "AGENTS.md").write_text("全局规则内容XYZ", encoding="utf-8")
    text = _load(_loader(str(proj)))
    assert "全局规则内容XYZ" in text and "项目规则内容ABC" in text
    assert text.index("全局规则内容XYZ") < text.index("项目规则内容ABC"), (
        f"全局必须先于项目注入：{text!r}"
    )


def test_no_global_file_no_change(tmp_path: Path) -> None:
    """契约（对照）：全局文件不存在时不产生注入内容、不炸。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("项目规则内容ABC", encoding="utf-8")
    text = _load(_loader(str(proj)))
    assert "项目规则内容ABC" in text
    assert "全局 AGENTS.md" not in text


def test_preferences_injected_with_path_hint(tmp_path: Path) -> None:
    """契约（偏好专区 B）：~/.bandi/preferences.md 存在时注入「## 用户偏好」段
    且含文件路径提示（用户可让 LLM 追加更新）；不混项目/团队=只在 user 层。"""
    (tmp_path / ".bandi").mkdir(exist_ok=True)
    (tmp_path / ".bandi" / "preferences.md").write_text(
        "我喜欢简短回复，别用敬语XYZ", encoding="utf-8"
    )
    text = _load(_loader())
    assert "## 用户偏好" in text, f"偏好段缺失：{text!r}"
    assert "我喜欢简短回复，别用敬语XYZ" in text
    assert "preferences.md" in text, "必须含路径提示（可追加更新指引）"
    assert "直接将这些偏好应用于" in text, "必须含行为应用指令（防 LLM 复述偏好条目）"


def test_preferences_before_project(tmp_path: Path) -> None:
    """契约（顺序）：用户偏好属 user 层——必须排在项目内容之前。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("项目规则内容ABC", encoding="utf-8")
    (tmp_path / ".bandi").mkdir(exist_ok=True)
    (tmp_path / ".bandi" / "preferences.md").write_text("偏好内容XYZ", encoding="utf-8")
    text = _load(_loader(str(proj)))
    assert text.index("偏好内容XYZ") < text.index("项目规则内容ABC"), text


def test_no_preferences_no_section(tmp_path: Path) -> None:
    """契约（对照）：偏好文件不存在时不出现「## 用户偏好」段。"""
    text = _load(_loader())
    assert "## 用户偏好" not in text
