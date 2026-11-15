"""契约：模型可用普通 write 工具自建技能，且写入后本进程立即可见。

参考 Claude Code 的 `.claude/skills/<name>/SKILL.md` 做法（`init-verifiers.ts:165`）：
不需要专用工具，靠「路径约定 + SKILL.md 模板 + 写完生效」三件事成立。

缺陷实证（2026-11-06）：技能注册表是进程级单例，只在 API 资源增删改后重置；
模型用 write 写完 SKILL.md 不刷新缓存 ⇒ 本次会话内 `skill_discover` 看不到新技能。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bandi.agent.prompt_harness.builder import SKILL_AUTHORING_RULES
from bandi.tools import fs_tool
from bandi.tools.file_ops import FileWriteTool
from bandi.tools.skill_tool import _load_registry, reset_skill_registry

SKILL_MD = """---
name: demo-skill
description: 演示技能——验证模型自建技能写完即可被发现
---

# Demo Skill

1. 读取用户给的表格。
2. 按部门汇总人数。
3. 输出一行结论。
"""


@pytest.fixture()
def skills_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把工作区根与技能目录都指到 tmp，隔离真实 ~/.bandi。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setenv("BANDI_SKILLS_DIRS", str(skills_dir))
    monkeypatch.delenv("BANDI_ALLOWED_SKILLS", raising=False)
    monkeypatch.setattr(fs_tool, "_WORKSPACE_ROOT", tmp_path)
    reset_skill_registry()
    yield skills_dir
    reset_skill_registry()


def test_written_skill_is_visible_without_restart(skills_env: Path) -> None:
    """行为：write 写 SKILL.md 后，注册表里立刻能查到这个技能。"""
    assert _load_registry().get("demo-skill") is None, "前置：技能尚不存在"

    target = skills_env / "demo-skill" / "SKILL.md"
    asyncio.run(FileWriteTool().execute({"path": str(target), "content": SKILL_MD}))

    found = _load_registry().get("demo-skill")
    assert found is not None, "写入 SKILL.md 后必须刷新注册表（否则要重启）"
    assert "演示技能" in found.description


def test_edit_existing_skill_also_refreshes(skills_env: Path) -> None:
    """破坏者视角：改已有技能的 description 也要立即可见（走 edit 而非 write）。"""
    from bandi.tools.file_edit import FileEditTool

    target = skills_env / "demo-skill" / "SKILL.md"
    asyncio.run(FileWriteTool().execute({"path": str(target), "content": SKILL_MD}))
    assert "演示技能" in _load_registry().get("demo-skill").description

    asyncio.run(
        FileEditTool().execute(
            {
                "file_path": str(target),
                "old_string": "演示技能——验证模型自建技能写完即可被发现",
                "new_string": "改过的描述——用于验证 edit 路径同样刷新",
            }
        )
    )
    assert "改过的描述" in _load_registry().get("demo-skill").description


def test_authoring_rules_cover_path_and_frontmatter() -> None:
    """约定必须写清「写哪」与「frontmatter 必填什么」——缺一条模型就会写废。"""
    for keyword in ("`.bandi/skills/<name>/SKILL.md`", "`~/.bandi/skills/<name>/SKILL.md`"):
        assert keyword in SKILL_AUTHORING_RULES, f"约定缺路径：{keyword}"
    for keyword in ("`name`", "`description`"):
        assert keyword in SKILL_AUTHORING_RULES, f"约定缺 frontmatter 字段：{keyword}"
