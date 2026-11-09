# 功能：契约——蒸馏技能卡（*.card.json）进入技能工具面（SkillLoader → SkillRegistry →
# skill_discover/skill_invoke）。钉行为不钉实现：断言最终产物（装载结果 / SkillDef 字段 /
# 工具 execute 返回的指令正文），SKILL.md 路径行为零变化（同名时 Markdown 技能优先）。
# 生产路径族：技能库同时承载 SKILL.md 目录族与 <skill_id>.card.json 卡片族；蒸馏器
# 工具面显式发现与调用」的闭环；卡片 triggers 留空（不参与自动触发注入）。
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from bandi.skills.card import SkillCard, SkillGraphNode
from bandi.skills.loader import SkillLoader
from bandi.skills.manager import create_skill_registry
from bandi.skills.serializer import SkillCardLoadError, load_card, save_card
from bandi.tools import skill_tool as skill_tool_mod
from bandi.tools.skill_tool import SkillDiscoverTool, SkillTool


def _refund_card(skill_id: str = "sk_refund_flow") -> SkillCard:
    """功能：契约基准——蒸馏形状的中文技能卡（双节点线性闭环）。"""
    return SkillCard(
        skill_id=skill_id,
        name="退款处理",
        version="1.0.0",
        description="处理用户退款申请的标准流程",
        nodes=[
            SkillGraphNode(
                node_id="check_order",
                name="核对订单",
                type="collect_info",
                instruction="核对订单号与支付流水，确认退款金额。",
            ),
            SkillGraphNode(
                node_id="submit_refund",
                name="发起退款",
                type="action",
                instruction="调用退款接口提交申请，并把结果反馈用户。",
            ),
        ],
        edges=[{"source_node_id": "check_order", "next_node_id": "submit_refund", "priority": 0}],
        start_node_id="check_order",
        terminal_node_ids=["submit_refund"],
    )


def _write_md_skill(root: Path, dir_name: str, name: str, description: str) -> None:
    """功能：写入一个 SKILL.md 目录技能（既有路径的行为基准）。"""
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n"
        "## Instructions\n按 Markdown 技能定义执行。\n",
        encoding="utf-8",
    )


@pytest.fixture()
def isolated_skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """功能：工具面技能库隔离——级联首目录指向 tmp + 重置注册表单例。"""
    monkeypatch.setenv("BANDI_SKILLS_DIRS", str(tmp_path))
    monkeypatch.setattr(skill_tool_mod, "_REGISTRY", None)
    yield tmp_path
    skill_tool_mod._REGISTRY = None


class TestCardVisibleToLoader:
    def test_saved_card_is_loaded_with_chinese_node_instructions(self, tmp_path: Path) -> None:
        """契约（CARD-1，端到端）：save_card 落盘 draft 卡后 loader.load_all 可见——
        name=skill_id / description 保真 / 正文含全部节点指令中文逐字且两次装载确定。"""
        save_card(_refund_card(), tmp_path)
        skills = SkillLoader(str(tmp_path)).load_all()
        assert [skill.name for skill in skills] == ["sk_refund_flow"]
        skill = skills[0]
        assert skill.description == "处理用户退款申请的标准流程"
        assert "核对订单" in skill.instructions
        assert "核对订单号与支付流水，确认退款金额。" in skill.instructions
        assert "调用退款接口提交申请，并把结果反馈用户。" in skill.instructions
        assert skill.instructions == SkillLoader(str(tmp_path)).load_all()[0].instructions
        assert skill.triggers == [], "卡片技能不得参与自动触发注入（仅显式工具可达）"

    def test_skill_md_and_card_coexist_with_md_priority(self, tmp_path: Path) -> None:
        """契约（CARD-2，破坏者）：同名 SKILL.md 与卡片并存时 Markdown 技能优先
        不同名卡片照常装载——两族共存不吞并、不覆盖。"""
        _write_md_skill(tmp_path, "refund_md", "sk_refund_flow", "来自 Markdown 的定义")
        save_card(_refund_card(), tmp_path)
        save_card(_refund_card("sk_purchase_flow"), tmp_path)
        loaded = {skill.name: skill for skill in SkillLoader(str(tmp_path)).load_all()}
        assert set(loaded) == {"sk_refund_flow", "sk_purchase_flow"}
        assert loaded["sk_refund_flow"].description == "来自 Markdown 的定义"
        assert "核对订单号与支付流水，确认退款金额。" in loaded["sk_purchase_flow"].instructions

    def test_malformed_cards_skipped_without_breaking_others(self, tmp_path: Path) -> None:
        """契约（CARD-3，破坏者）：非法 JSON / schema 非法的坏卡跳过不炸
        合法卡片与 SKILL.md 技能照常装载。"""
        (tmp_path / "broken_json.card.json").write_text("{not json", encoding="utf-8")
        bad_schema = _refund_card("sk_bad_schema").model_dump()
        bad_schema.pop("start_node_id")
        (tmp_path / "sk_bad_schema.card.json").write_text(
            json.dumps(bad_schema, ensure_ascii=False), encoding="utf-8"
        )
        save_card(_refund_card("sk_ok"), tmp_path)
        _write_md_skill(tmp_path, "md_ok", "md_skill", "Markdown 技能")
        names = sorted(skill.name for skill in SkillLoader(str(tmp_path)).load_all())
        assert names == ["md_skill", "sk_ok"]


class TestCardRobustness:
    def test_non_utf8_card_skipped_without_breaking_loading(self, tmp_path: Path) -> None:
        """契约（CARD-6，破坏者）：GBK 等非 UTF-8 编码坏卡只跳过自身，合法卡与
        SKILL.md 技能照常装载，装配点 create_skill_registry 不炸（原红：穿透炸全链）。"""
        (tmp_path / "gbk_bad.card.json").write_bytes("坏卡内容".encode("gbk"))
        save_card(_refund_card("sk_ok"), tmp_path)
        _write_md_skill(tmp_path, "md_ok", "md_skill", "Markdown 技能")
        names = sorted(skill.name for skill in SkillLoader(str(tmp_path)).load_all())
        assert names == ["md_skill", "sk_ok"]
        registry = create_skill_registry(str(tmp_path))
        assert registry.get("sk_ok") is not None

    def test_bom_card_loads_after_utf8_sig_compat(self, tmp_path: Path) -> None:
        """契约（CARD-7）：带 UTF-8 BOM 的合法卡可读回（utf-8-sig 兼容，不因 BOM 丢弃）。"""
        payload = json.dumps(_refund_card("sk_bom").model_dump(), ensure_ascii=False)
        (tmp_path / "sk_bom.card.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
        loaded = {skill.name: skill for skill in SkillLoader(str(tmp_path)).load_all()}
        assert "sk_bom" in loaded
        assert "核对订单号与支付流水，确认退款金额。" in loaded["sk_bom"].instructions

    @pytest.mark.parametrize(
        "bad_skill_id",
        ["", "../evil", "bad/name", "退款卡"],
        ids=["empty", "traversal", "slash", "chinese"],
    )
    def test_invalid_skill_id_cards_skipped(self, tmp_path: Path, bad_skill_id: str) -> None:
        """契约（CARD-8，破坏者，参数化）：空/路径穿越/含斜杠/非 ASCII（中文）skill_id
        卡一律跳过（与 save_card ascii_only 同口径），合法卡不受影响；单卡 load 同拒。"""
        data = _refund_card("sk_ok").model_dump()
        data["skill_id"] = bad_skill_id
        bad_path = tmp_path / "bad.card.json"
        bad_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        save_card(_refund_card("sk_ok"), tmp_path)
        loader = SkillLoader(str(tmp_path))
        assert [skill.name for skill in loader.load_all()] == ["sk_ok"]
        assert loader.load(str(bad_path)) is None

    def test_oversized_card_skipped_without_parsing(self, tmp_path: Path) -> None:
        """契约（CARD-9，破坏者）：超过 256KB 上限的合法 JSON 卡在解析前被跳过
        （load_card 显式报错），其余技能照常装载（原红：现装载）。"""
        oversized = _refund_card("sk_huge").model_dump()
        oversized["description"] = "膨胀描述" * 300_000
        huge_path = tmp_path / "sk_huge.card.json"
        huge_path.write_text(json.dumps(oversized, ensure_ascii=False), encoding="utf-8")
        save_card(_refund_card("sk_ok"), tmp_path)
        with pytest.raises(SkillCardLoadError):
            load_card(huge_path)
        assert [skill.name for skill in SkillLoader(str(tmp_path)).load_all()] == ["sk_ok"]

    def test_cascade_first_dir_wins_for_same_card_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """契约（CARD-10）：跨目录同名卡先到先得——级联前序目录覆盖后续目录。"""
        first = tmp_path / "first"
        second = tmp_path / "second"
        card_a = _refund_card("sk_shared")
        card_a.description = "第一目录版本"
        card_b = _refund_card("sk_shared")
        card_b.description = "第二目录版本"
        save_card(card_a, first)
        save_card(card_b, second)
        monkeypatch.setenv("BANDI_SKILLS_DIRS", os.pathsep.join([str(first), str(second)]))
        loaded = {skill.name: skill for skill in SkillLoader().load_all()}
        assert loaded["sk_shared"].description == "第一目录版本"


class TestCardVisibleToToolSurface:
    def test_tool_surface_can_discover_and_invoke_card_skill(
        self, isolated_skills_dir: Path
    ) -> None:
        """契约（CARD-4，端到端）：级联技能库中的卡片可被 skill_discover 找到
        （含中文关键词过滤）且 skill_invoke 返回节点指令正文（内容注入可用）。"""
        save_card(_refund_card(), isolated_skills_dir)
        discovered = asyncio.run(SkillDiscoverTool().execute({"keyword": "退款"}))
        assert discovered["success"] is True
        assert [item["name"] for item in discovered["skills"]] == ["sk_refund_flow"]
        invoked = asyncio.run(SkillTool().execute({"skill": "sk_refund_flow"}))
        assert invoked["success"] is True
        assert "核对订单号与支付流水，确认退款金额。" in invoked["instructions"]
        assert invoked["constraints"] == []

    def test_distiller_draft_card_reaches_tool_surface(self, isolated_skills_dir: Path) -> None:
        """契约（CARD-5，端到端）：真实 SkillDistiller（MockProvider 触发 最低草稿）
        落盘后，其 skill_id 可被 skill_invoke 直接调用——蒸馏产物对 Agent 工具面可见。"""
        from bandi.llm.mock import MockProvider
        from bandi.skills.distiller import SkillDistiller, SkillDistillRequest

        distiller = SkillDistiller(MockProvider(), skills_dir=str(isolated_skills_dir))
        response = asyncio.run(
            distiller.distill(
                SkillDistillRequest(
                    title="采购申请",
                    raw_content="员工提交采购申请，主管审批后下单采购。",
                )
            )
        )
        skill_id = response.draft_skill.skill_id
        assert skill_id
        assert (isolated_skills_dir / f"{skill_id}.card.json").is_file()
        invoked = asyncio.run(SkillTool().execute({"skill": skill_id}))
        assert invoked["success"] is True
        assert invoked["instructions"].strip() != ""


class TestInstructionSurfaceBounds:
    def test_oversized_instructions_truncated_with_note(self, isolated_skills_dir: Path) -> None:
        """契约（CARD-11，破坏者）：超长技能指令经 skill_invoke 返回时截断到 32K
        并带截断注记与标志（防 1MB 指令直灌模型上下文；原红：整段返回）。"""
        huge_dir = isolated_skills_dir / "huge_skill"
        huge_dir.mkdir()
        huge_instructions = "步骤说明" * 250_000
        (huge_dir / "SKILL.md").write_text(
            "---\nname: huge_skill\ndescription: 超长指令\n---\n\n# huge_skill\n\n"
            f"## Instructions\n{huge_instructions}\n",
            encoding="utf-8",
        )
        invoked = asyncio.run(SkillTool().execute({"skill": "huge_skill"}))
        note = "\n\n[truncated: instructions exceeded 32768 chars]"
        assert invoked["success"] is True
        assert invoked["instructions_truncated"] is True
        assert invoked["instructions"] == huge_instructions[:32768] + note
        assert len(invoked["instructions"]) < len(huge_instructions)
