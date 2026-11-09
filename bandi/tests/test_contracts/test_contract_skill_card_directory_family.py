# 功能：契约——serializer(落盘 *.card.json) ↔ SkillLoader(读 SKILL.md 目录) 目录族互不吞并。
# 钉行为不钉实现：save_card 绝不吞并或改写同目录下的 SKILL.md 技能目录 / 拒绝路径零副作用——
# 两条持久化族（卡片资产 vs Markdown 技能）可并存于同一 skills_dir。
# （name=skill_id；同名时 Markdown 技能优先），原「卡片对 loader 完全不可见」承诺由
# test_contract_skill_card_tool_surface.py 的新承诺取代——两族互不污染的核心承诺不变。
# 生产路径族：skills_dir ~/.bandi/skills 同时承载 importer/loader 管理的 SKILL.md 目录族

from __future__ import annotations

from pathlib import Path

import pytest

from bandi.skills.card import SkillCard, SkillGraphNode
from bandi.skills.loader import SkillLoader
from bandi.skills.serializer import SkillCardSaveError, save_card


def _min_card(skill_id: str, name: str) -> SkillCard:
    return SkillCard(
        skill_id=skill_id,
        name=name,
        version="1.0.0",
        start_node_id="n1",
        terminal_node_ids=["n1"],
        nodes=[SkillGraphNode(node_id="n1", name="单节点")],
    )


class TestDirectoryFamilyIsolation:
    def test_card_files_and_skill_md_family_load_side_by_side(self, tmp_path: Path) -> None:
        """契约（端到端；行为演进版）：同一 skills_dir 同时落盘 *.card.json 与
        SKILL.md 目录技能——两族并列装载互不污染：Markdown 技能名称/描述逐字保持
        卡片以 skill_id 可见（蒸馏工具面接线），卡片资产不作为目录技能解析。"""
        save_card(_min_card("sk_alice", "Alice 技能"), tmp_path)
        save_card(_min_card("sk_bob", "Bob 技能"), tmp_path)
        md_dir = tmp_path / "md_skill"
        md_dir.mkdir()
        (md_dir / "SKILL.md").write_text(
            "---\nname: 真实技能\ndescription: 走 Markdown 族\n---\n\n# 真实技能\n",
            encoding="utf-8",
        )
        loader = SkillLoader(str(tmp_path))
        loaded = {skill.name: skill for skill in loader.load_all()}
        assert set(loaded) == {"真实技能", "sk_alice", "sk_bob"}
        assert loaded["真实技能"].description == "走 Markdown 族"

    def test_save_card_never_touches_existing_skill_directory(self, tmp_path: Path) -> None:
        """契约（破坏者）：skills_dir 已存在与 skill_id 同名的 SKILL.md 目录
        → save_card 落盘 <skill_id>.card.json 文件，原技能目录及 SKILL.md 内容逐字保持
        （文件覆盖绝不跨界吞并目录；两族同名共存不冲突）。"""
        original = "---\nname: 同名技能\ndescription: 原有内容不可变\n---\n\n# 同名技能\n"
        skill_dir = tmp_path / "same_name"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(original, encoding="utf-8")
        save_card(_min_card("same_name", "卡片同名"), tmp_path)
        after = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert after == original
        assert (tmp_path / "same_name.card.json").is_file()
        loader = SkillLoader(str(tmp_path))
        assert loader.load_all()[0].name == "同名技能"

    def test_save_card_refusal_never_destroys_directory(self, tmp_path: Path) -> None:
        """契约（破坏者）：save_card 因非法 skill_id 被拒时不产生副作用
        ——既有 SKILL.md 目录族保持原样（拒绝路径零删改）。"""
        original = "---\nname: 存续技能\n---\n\n# 存续技能\n"
        skill_dir = tmp_path / "survivor"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(original, encoding="utf-8")
        with pytest.raises(SkillCardSaveError):
            save_card(_min_card("../evil", "恶意"), tmp_path)
        assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == original
        assert sorted(p.name for p in tmp_path.iterdir()) == ["survivor"]
