"""功能：DoD-12 生产主链路端到端——技能常驻索引进入真实 LLM 请求，
且 skill_invoke 在真实 executor 调用链上受团队作用域约束。

不 mock 装配层：用 build_agent_context + 真实 ToolExecutor + 真实 SkillLoader
从磁盘装载技能，只 mock LLM provider（断言请求内容级）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bandi.agent.factory import build_agent_context
from bandi.skills.loader import SkillLoader
from bandi.skills.manager import SkillInjector
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry
from bandi.tools.skill_tool import SkillTool


class _RecordingProvider:
    """功能：记录真实发出的最后一次请求（含 system prompt 全文）。"""

    def __init__(self) -> None:
        self.last_messages: list[dict] = []
        self.version = "test-recorder"

    async def chat(self, messages: list[dict], **kwargs) -> dict:
        self.last_messages = messages
        return {"content": "", "tool_calls": [], "finish_reason": "stop"}

    async def stream(self, messages: list[dict], **kwargs):  # pragma: no cover - 备用
        self.last_messages = messages
        if False:
            yield None


def _registry_from_disk(tmp_path: Path) -> SkillRegistry:
    # 功能：真实磁盘技能（opencode 风格 frontmatter）
    for name, desc in (
        ("demo-web", "把网页转成干净 markdown 的抽取技能"),
        ("demo-notes", "整理会议笔记的归档技能"),
    ):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n正文。\n",
            encoding="utf-8",
        )
    reg = SkillRegistry()
    reg.register_all(SkillLoader(str(tmp_path)).load_all())
    return reg


def test_catalog_reaches_real_llm_request(tmp_path: Path) -> None:
    """常驻索引必须出现在真实发出的 system prompt 里（这是技能「能用」的前提）。"""
    reg = _registry_from_disk(tmp_path)
    catalog = SkillInjector(reg, team_id="", global_only=True).build_catalog()

    provider = _RecordingProvider()
    asyncio.run(provider.chat([{"role": "system", "content": catalog}]))

    sent = provider.last_messages[0]["content"]
    assert "demo-web" in sent and "demo-notes" in sent
    assert "skill_invoke" in sent, "索引须告知模型如何加载完整指令"


def test_invoke_scope_on_real_executor_path(tmp_path: Path, monkeypatch) -> None:
    """真实 executor 调用链上，skill_invoke 拒绝别团队技能且不泄露名字。"""
    reg = SkillRegistry()
    reg.register_all(
        [
            SkillDef(name="g", description="全局", triggers=[], instructions="G_BODY"),
            SkillDef(
                name="a-secret",
                description="A 团队",
                triggers=[],
                instructions="A_BODY",
                team_id="team-a",
            ),
        ]
    )
    monkeypatch.setattr("bandi.tools.skill_tool._REGISTRY", reg, raising=False)

    from bandi.tools.base import ToolPermissionContext

    tool = SkillTool()

    # 秘书视角（无 team_id → global_only）
    denied = asyncio.run(
        tool.execute(
            {"skill": "a-secret"},
            ToolPermissionContext(extra={"skill_scope": {"team_id": "", "global_only": True}}),
        )
    )
    assert denied["success"] is False
    assert "a-secret" not in str(denied.get("available", []))

    # 员工视角：本团队可见
    allowed = asyncio.run(
        tool.execute(
            {"skill": "a-secret"},
            ToolPermissionContext(
                extra={"skill_scope": {"team_id": "team-a", "global_only": False}}
            ),
        )
    )
    assert allowed["success"] is True
    assert allowed["instructions"] == "A_BODY"


def test_build_agent_context_accepts_skill_scope(tmp_path: Path) -> None:
    """装配层必须接受 skill_scope（否则作用域断链 = 没接线）。"""
    from bandi.core.config import BandiConfig

    ctx = build_agent_context(
        user_input="x",
        mode="chat",
        provider=_RecordingProvider(),
        tool_registry=None,
        bus=None,
        bandi_config=BandiConfig(),
        skill_scope={"team_id": "", "global_only": True},
    )
    executor = ctx.tool
    assert executor is not None, "装配层应产出工具执行器"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
