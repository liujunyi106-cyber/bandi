# 功能：契约——技能团队归属与过滤（SkillDef.team_id 全链，=A）。
# 钉行为不钉实现：
# - SkillInjector(team_id) 注入过滤：A 团队上下文不注入 B 团队技能（双向破坏者）；
# - SkillLoader 解析 frontmatter team_id（缺省 ""）；SkillImporter 带属导入
# （写 frontmatter + 覆盖归属 + 无 fm 创建块；非法 team_id 拒绝）；
# - agent_runner 接线：_resolve_skill_team_id（role→team_id 三态）+ run_agent_turn
# 端到端（LLM 请求内容级：含三技能触发词输入，A 团队只收到全局+A 指令）；
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider
from bandi.skills.importer import SkillImporter, SkillImportError
from bandi.skills.loader import SkillLoader
from bandi.skills.manager import SkillInjector, create_skill_registry
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


def _skill(name: str, team_id: str = "") -> SkillDef:
    return SkillDef(
        name=name,
        description=f"{name} 描述",
        triggers=[name],
        instructions=f"{name} 指令",
        team_id=team_id,
    )


def _registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register_all(
        [
            _skill("global-skill"),
            _skill("a-skill", "team-a"),
            _skill("b-skill", "team-b"),
        ]
    )
    return reg


def _write_skill(base: Path, name: str, team_id: str = "") -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = f"name: {name}\ndescription: {name} 描述\n"
    if team_id:
        fm += f"team_id: {team_id}\n"
    (skill_dir / "SKILL.md").write_text(
        f"---\n{fm}---\n\n## Behavior\n- {name}\n\n## Instructions\n{name} 指令\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


def test_f1_collect_for_team_three_states() -> None:
    reg = _registry()
    assert {s.name for s in reg.collect_for_team("")} == {
        "global-skill",
        "a-skill",
        "b-skill",
    }
    assert {s.name for s in reg.collect_for_team("team-a")} == {"global-skill", "a-skill"}
    assert {s.name for s in reg.collect_for_team("team-b")} == {"global-skill", "b-skill"}


def test_f2_injector_filters_by_team() -> None:
    catalog = SkillInjector(_registry(), team_id="team-a").build_catalog()
    assert "**a-skill**" in catalog
    assert "**global-skill**" in catalog
    assert "**b-skill**" not in catalog, "B 团队技能不得进 A 团队目录"


def test_f3_bidirectional_isolation_and_no_context() -> None:
    catalog_b = SkillInjector(_registry(), team_id="team-b").build_catalog()
    assert "**b-skill**" in catalog_b
    assert "**a-skill**" not in catalog_b

    catalog_all = SkillInjector(_registry()).build_catalog()
    assert "**a-skill**" in catalog_all
    assert "**b-skill**" in catalog_all
    assert "**global-skill**" in catalog_all


def test_f4_loader_parses_team_id(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    _write_skill(base, "a-skill", team_id="team-a")
    _write_skill(base, "plain-skill")
    skills = {s.name: s for s in SkillLoader(str(base)).load_all()}
    assert skills["a-skill"].team_id == "team-a"
    assert skills["plain-skill"].team_id == ""


def test_f5_importer_team_attribution(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "my-skill"
    src_dir.mkdir(parents=True)
    (src_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: 测试技能\n---\n\n## Instructions\n做事。\n",
        encoding="utf-8",
    )
    no_fm_dir = tmp_path / "src-no-fm" / "bare-skill"
    no_fm_dir.mkdir(parents=True)
    (no_fm_dir / "SKILL.md").write_text("## Instructions\n裸技能。\n", encoding="utf-8")

    lib = tmp_path / "lib"
    importer = SkillImporter(skills_dir=str(lib))

    importer.import_skill(str(src_dir), team_id="team-a")
    text = (lib / "my-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "team_id: team-a" in text
    assert SkillLoader(str(lib)).load_all()[0].team_id == "team-a"

    importer.import_skill(str(src_dir), team_id="team-b")
    text2 = (lib / "my-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "team_id: team-b" in text2
    assert "team-a" not in text2, "重复带属导入必须覆盖旧归属"

    importer.import_skill(str(no_fm_dir), team_id="team-c")
    text3 = (lib / "bare-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert text3.startswith("---\nteam_id: team-c\n---"), "无 frontmatter 时创建 fm 块"
    assert "裸技能。" in text3


def test_f6_importer_rejects_unsafe_team_id(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "safe-skill"
    src_dir.mkdir(parents=True)
    (src_dir / "SKILL.md").write_text(
        "---\nname: safe-skill\ndescription: 安全\n---\n\n## Instructions\n做事。\n",
        encoding="utf-8",
    )
    importer = SkillImporter(skills_dir=str(tmp_path / "lib"))
    for bad in ("../evil", "a\nb", "a b"):
        with pytest.raises(SkillImportError):
            importer.import_skill(str(src_dir), team_id=bad)
    assert not (tmp_path / "lib" / "safe-skill").exists() or (
        "team_id" not in (tmp_path / "lib" / "safe-skill" / "SKILL.md").read_text("utf-8")
    )


def test_f7_production_chain_catalog_scope(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    _write_skill(base, "global-skill")
    _write_skill(base, "a-skill", team_id="team-a")
    _write_skill(base, "b-skill", team_id="team-b")

    registry = create_skill_registry(str(base))
    catalog = SkillInjector(registry, team_id="team-a").build_catalog()
    assert "**a-skill**" in catalog
    assert "**global-skill**" in catalog
    assert "**b-skill**" not in catalog
    assert "a-skill 指令" not in catalog, "F 模式不注入正文"

    catalog_all = SkillInjector(registry).build_catalog()
    assert "**a-skill**" in catalog_all
    assert "**b-skill**" in catalog_all


def test_f8_resolve_skill_team_id() -> None:
    from bandi.company.org_engine import OrgEngine
    from bandi.core.agent_runner import _resolve_skill_team_id

    org = OrgEngine()
    org.register_role("team-a_lead", "A 队长", team_id="team-a")
    assert _resolve_skill_team_id(org, "team-a_lead") == "team-a"
    assert _resolve_skill_team_id(org, "ceo") == "", "内置角色无团队 → 不过滤"
    assert _resolve_skill_team_id(org, "ghost-role") == ""
    assert _resolve_skill_team_id(None, "team-a_lead") == ""


class _RecordingProvider(MockProvider):
    """功能：记录请求消息的 MockProvider（端到端请求内容级断言）。"""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[list[dict]] = []

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        self.requests.append(messages)
        return await super().complete(messages, tools, **kwargs)

    async def stream(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        self.requests.append(messages)
        async for event in super().stream(messages, tools, **kwargs):
            yield event


@pytest.mark.asyncio
async def test_f9_run_agent_turn_filters_catalog_by_role_team(
    home: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bandi.company.org_engine import OrgEngine
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    base = tmp_path / "skills"
    _write_skill(base, "global-skill")
    _write_skill(base, "a-skill", team_id="team-a")
    _write_skill(base, "b-skill", team_id="team-b")
    monkeypatch.setenv("BANDI_SKILLS_DIRS", str(base))
    monkeypatch.chdir(tmp_path)

    org = OrgEngine()
    org.register_role("team-a_lead", "A 队长", team_id="team-a")
    store = SessionStore(str(tmp_path / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        await run_agent_turn(
            user_input="执行 a-skill b-skill global-skill",
            session_id=sid,
            session_store=store,
            company_org_engine=org,
            agent_role="team-a_lead",
        )

    prompt_text = "\n".join(str(m) for request in provider.requests for m in request)
    assert "## 可用技能" in prompt_text, "F 模式仍常驻技能目录（模型据此调用 skill_invoke）"
    assert "**a-skill**" in prompt_text, "本团队技能必须出现在目录"
    assert "**global-skill**" in prompt_text, "全局技能对团队可见"
    assert "**b-skill**" not in prompt_text, "B 团队技能必须被过滤"
    assert "a-skill 指令" not in prompt_text, "F 模式不得自动注入技能正文"
    assert "[技能指令]" not in prompt_text, "F 模式不再插入技能指令消息"


# （否则 name 退化、team_id 静默变全局——importer 的 BOM 兼容正则会写出这种文件）
def test_f10_bom_frontmatter_team_id_parsed(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    skill_dir = base / "bom-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\ufeff---\nname: bom-skill\ndescription: BOM 技能\nteam_id: team-a\n---\n\n"
        "## Behavior\n- bom-skill\n\n## Instructions\nBOM 指令\n",
        encoding="utf-8",
    )
    skills = {s.name: s for s in SkillLoader(str(base)).load_all()}
    assert "bom-skill" in skills, f"BOM 文件必须正确解析，实际 {sorted(skills)}"
    assert skills["bom-skill"].team_id == "team-a", (
        f"BOM 文件的 team_id 不得静默失效（变全局），实际 {skills['bom-skill'].team_id!r}"
    )


def test_f11_duplicate_team_id_replaced(tmp_path: Path) -> None:
    src = tmp_path / "src" / "dup-skill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: dup-skill\ndescription: 重复键\nteam_id: stale1\nteam_id: stale2\n---\n\n"
        "## Behavior\n- dup-skill\n\n## Instructions\n做事。\n",
        encoding="utf-8",
    )
    lib = tmp_path / "lib"
    SkillImporter(skills_dir=str(lib)).import_skill(str(src), team_id="team-new")
    loaded = SkillLoader(str(lib)).load_all()[0]
    assert loaded.team_id == "team-new", f"重复键必须全部替换，实际 {loaded.team_id!r}"


def test_f12_multiline_team_id_rejected(tmp_path: Path) -> None:
    src = tmp_path / "src" / "multi-skill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: multi-skill\ndescription: 多行\nteam_id: |\n  team-a\n  team-b\n---\n\n"
        "## Behavior\n- multi-skill\n\n## Instructions\n做事。\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillImportError):
        SkillImporter(skills_dir=str(tmp_path / "lib")).import_skill(str(src), team_id="team-new")
