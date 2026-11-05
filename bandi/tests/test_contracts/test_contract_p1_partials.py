# 功能：P1 半成品收口契约——记忆卫生 / 压缩落盘 / 会话截断归档撤销
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.agent.prompt_harness.builder import PromptHarness
from bandi.core.message import Message, MessageRole
from bandi.memory.compression_pipeline import MicroCompressor
from bandi.memory.session.store import SessionStore
from bandi.perception.context_loader import ContextBundle


def _bundle() -> ContextBundle:
    return ContextBundle(
        project_instructions="# 项目规则\n测试项目",
        memory_context="",
        memory_rules_active=True,
        ontology_schema="",
        referenced_entities=[],
        project_scoped=True,
    )


def test_memory_hygiene_section_in_prompt() -> None:
    sections = PromptHarness().build_sections(mode="execute", context=_bundle())
    dynamic = f"{sections.dynamic}\n\n{sections.tail}"
    assert "记忆卫生" in dynamic
    assert "忽略记忆" in dynamic
    assert "绝对日期" in dynamic
    assert "记忆卫生" not in sections.static


def test_micro_trim_persists_full_tool_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BANDI_TOOL_OUTPUT_DIR", str(tmp_path / "tool_outputs"))
    content = "头" * 500 + "中段" * 1000 + "尾" * 500
    msgs = [
        Message(role=MessageRole.USER, content="读文件"),
        Message(role=MessageRole.TOOL, content=content, name="read", tool_call_id="c1"),
    ]
    out, stats = MicroCompressor().compress(msgs)
    assert stats.micro_trimmed == 1
    trimmed = out[1].content or ""
    assert "[full output persisted:" in trimmed
    path = trimmed.split("[full output persisted: ", 1)[1].split("]", 1)[0]
    assert Path(path).read_text(encoding="utf-8") == content
    assert "中段" * 1000 in Path(path).read_text(encoding="utf-8")


def test_skill_frontmatter_fields(tmp_path: Path) -> None:
    from bandi.skills.loader import SkillLoader
    from bandi.skills.manager import SkillInjector
    from bandi.skills.registry import SkillRegistry

    skill_file = tmp_path / "skills" / "pytest-helper" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\n"
        "name: pytest-helper\n"
        "description: 帮助跑 pytest\n"
        "triggers: [pytest]\n"
        "paths: [src/**]\n"
        "fork: true\n"
        "model: deepseek-flash\n"
        "effort: high\n"
        "---\n\n"
        "## Instructions\n\n按项目约定补充测试。\n",
        encoding="utf-8",
    )
    skill = SkillLoader(str(tmp_path / "skills")).load(str(skill_file))
    assert skill is not None
    assert skill.fork is True
    assert skill.model == "deepseek-flash"
    assert skill.effort == "high"
    # paths 无消费方：不再解析进模型（未知 frontmatter 键按忽略处理）
    assert not hasattr(skill, "paths")

    registry = SkillRegistry()
    registry.register(skill)
    catalog = SkillInjector(registry).build_catalog()
    assert skill.name in catalog, "技能必须进目录"


def test_skill_fork_dispatches_subagent(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from types import SimpleNamespace

    from bandi.skills.models import SkillDef
    from bandi.tools.skill_tool import SkillTool

    calls: list[tuple[str, object, object]] = []

    class _FakeManager:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        async def spawn(self, task: str, config: object, tool_registry: object = None) -> object:
            calls.append((task, config, tool_registry))
            return SimpleNamespace(
                success=True, summary="子代理完成", message="", session_id="sub-1"
            )

    monkeypatch.setattr("bandi.agent.subagent.SubagentManager", _FakeManager)
    skill = SkillDef(
        name="fork-skill",
        description="fork",
        triggers=[],
        instructions="按流程执行",
        fork=True,
        model="deepseek-flash",
        effort="high",
    )
    tool = SkillTool(provider=object())
    result = asyncio.run(tool._execute_fork(skill, skill.instructions, {"args": "参数"}, None))
    assert result["forked"] is True
    assert result["summary"] == "子代理完成"
    assert calls and "按流程执行" in calls[0][0]
    fork_config = calls[0][1]
    assert fork_config.model == "deepseek-flash", "技能模型覆盖必须传入子代理"
    assert fork_config.effort == "high", "技能思考档位必须传入子代理"


def test_dependency_gate_and_journal(tmp_path: Path) -> None:
    from bandi.company.company_runtime import CompanyRuntime
    from bandi.company.phase import Phase
    from bandi.company.store import WorkItemStore
    from bandi.company.work_item import WorkItem, WorkItemTurnType

    store = WorkItemStore(str(tmp_path / "wi.db"))
    dep = WorkItem(
        id="dep",
        title="前置",
        description="",
        phase=Phase.RUNNING,
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="alice",
        parent_id=None,
        children_ids=[],
        links={},
        metadata={},
        runtime_metadata={},
    )
    item = WorkItem(
        id="item",
        title="后继",
        description="",
        phase=Phase.READY,
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="bob",
        parent_id=None,
        children_ids=[],
        links={},
        metadata={"dependency_work_item_ids": ["dep"]},
        runtime_metadata={},
    )
    store.save(dep)
    store.save(item)
    runtime = CompanyRuntime(
        project_id="p1",
        session_scope_id="s1",
        role_id="bob",
        employee_id="bob",
        dependency_ready=lambda dep_id: (
            (row := store.get(dep_id)) is None
            or row.phase in (Phase.APPROVED, Phase.FAILED, Phase.CANCELLED)
        ),
    )
    assert runtime.is_dispatchable(item) is False
    dep.phase = Phase.APPROVED
    store.save(dep)
    assert runtime.is_dispatchable(item) is True

    store.record_journal("item", "phase_changed", detail={"from": "ready", "to": "running"})
    entries = store.list_journal("item")
    assert entries and entries[0]["event"] == "phase_changed"
    assert entries[0]["detail"]["to"] == "running"


def test_truncate_branch_can_be_restored(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "sess.db"))
    sid = store.create_session(mode="execute")
    first_id = store.append_message(sid, Message(role=MessageRole.USER, content="第一条"))
    store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="第二条"))
    store.append_message(sid, Message(role=MessageRole.USER, content="第三条"))

    removed, operation_id = store.truncate_branch_archived(sid, first_id)
    assert removed == 2
    assert len(store.get_branch(sid)) == 1

    restored = store.restore_archived(sid, operation_id)
    assert restored == 2
    contents = [entry.get("content") for entry in store.get_branch(sid)]
    assert contents == ["第一条", "第二条", "第三条"]
