# 功能：CLI 端到端契约——`bandi agents` 命令组（AgentProfileManager 生产入口）。
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bandi.cli.agents import agents_app
from bandi.knowledge.store import KnowledgeStore
from bandi.skills.card import SkillCard
from bandi.skills.serializer import save_card

runner = CliRunner()


@pytest.fixture(scope="module", autouse=True)
def _isolate_home(tmp_path_factory: pytest.TempPathFactory):
    """功能：隔离 Path.home 与 USERPROFILE/HOME，避免工具注册表写入真实 home。"""
    home = tmp_path_factory.mktemp("home")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    yield
    monkeypatch.undo()


def _card(skill_id: str = "sk_ship_order") -> SkillCard:
    """功能：两节点线性 SOP。"""
    return SkillCard(
        **{
            "skill_id": skill_id,
            "name": "发货 SOP",
            "version": "1.0.0",
            "description": "订单发货流程",
            "required_info": ["订单号"],
            "nodes": [
                {
                    "node_id": "collect_order",
                    "name": "收集订单",
                    "type": "collect_info",
                    "instruction": "收集订单号。",
                    "expected_user_info": ["订单号"],
                },
                {
                    "node_id": "reply_final_result",
                    "name": "反馈结果",
                    "type": "response",
                    "instruction": "给出最终回复。",
                },
            ],
            "edges": [
                {
                    "source_node_id": "collect_order",
                    "next_node_id": "reply_final_result",
                    "priority": 0,
                }
            ],
            "start_node_id": "collect_order",
            "terminal_node_ids": ["reply_final_result"],
        }
    )


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path]:
    profiles = tmp_path / "profiles"
    skills = tmp_path / "skills"
    skills.mkdir()
    save_card(_card(), str(skills))
    return profiles, skills


@pytest.fixture(scope="module")
def builtin_tools(tmp_path_factory: pytest.TempPathFactory):
    """功能：真实工具注册表（module 级缓存替身）——构建一次供全部用例注入。"""
    import bandi.agent.factory as factory_module
    import bandi.knowledge.store as knowledge_store_module

    tmp_store = KnowledgeStore(db_path=str(tmp_path_factory.mktemp("registry-kb") / "knowledge.db"))
    original = knowledge_store_module.get_shared_store
    knowledge_store_module.get_shared_store = lambda: tmp_store
    try:
        return factory_module.build_tool_registry()
    finally:
        knowledge_store_module.get_shared_store = original


def _base(profiles: Path, skills: Path) -> list[str]:
    return ["--profiles-dir", str(profiles), "--skills-dir", str(skills)]


def test_c1_cli_create_bind_show_roundtrip(env: tuple[Path, Path]) -> None:
    profiles, skills = env

    made = runner.invoke(
        agents_app, ["create", "emp-001", "--name", "客服小张", *_base(profiles, skills)]
    )
    assert made.exit_code == 0, made.output
    assert json.loads(made.output)["profile_id"] == "emp-001"

    bound = runner.invoke(
        agents_app,
        [
            "bind",
            "emp-001",
            "--type",
            "skill",
            "--resource",
            "sk_ship_order",
            *_base(profiles, skills),
        ],
    )
    assert bound.exit_code == 0, bound.output
    payload = json.loads(bound.output)
    assert payload["resources"] == [
        {"resource_type": "skill", "resource_id": "sk_ship_order", "status": "active"}
    ]

    shown = runner.invoke(agents_app, ["show", "emp-001", *_base(profiles, skills)])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["name"] == "客服小张"

    listed = runner.invoke(agents_app, ["list", *_base(profiles, skills)])
    assert listed.exit_code == 0, listed.output
    assert len(json.loads(listed.output)["profiles"]) == 1


def test_c2_cli_tool_binding_uses_production_registry(
    env: tuple[Path, Path], builtin_tools, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles, skills = env
    monkeypatch.setattr("bandi.agent.factory.build_tool_registry", lambda: builtin_tools)

    runner.invoke(agents_app, ["create", "emp-001", *_base(profiles, skills)])

    ok = runner.invoke(
        agents_app,
        ["bind", "emp-001", "--type", "tool", "--resource", "bash", *_base(profiles, skills)],
    )
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output)["resources"][0]["resource_id"] == "bash"

    bad = runner.invoke(
        agents_app,
        [
            "bind",
            "emp-001",
            "--type",
            "tool",
            "--resource",
            "ghost_tool",
            *_base(profiles, skills),
        ],
    )
    assert bad.exit_code == 1
    assert "工具未注册" in json.loads(bad.output)["error"]


def test_c3_cli_duplicate_create_and_missing_show_fail(env: tuple[Path, Path]) -> None:
    profiles, skills = env
    assert runner.invoke(agents_app, ["create", "emp-001", *_base(profiles, skills)]).exit_code == 0

    dup = runner.invoke(
        agents_app, ["create", "emp-001", "--name", "重复", *_base(profiles, skills)]
    )
    assert dup.exit_code == 1
    assert "已存在" in json.loads(dup.output)["error"]

    missing = runner.invoke(agents_app, ["show", "ghost", *_base(profiles, skills)])
    assert missing.exit_code == 1


def test_c4_cli_knowledge_base_binding(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles, skills = env
    store = KnowledgeStore(db_path=profiles.parent / "kb.db")
    store.ensure_base("kb-product-faq")
    monkeypatch.setattr("bandi.knowledge.store.get_shared_store", lambda: store)

    runner.invoke(agents_app, ["create", "emp-001", *_base(profiles, skills)])

    ok = runner.invoke(
        agents_app,
        [
            "bind",
            "emp-001",
            "--type",
            "knowledge_base",
            "--resource",
            "kb-product-faq",
            *_base(profiles, skills),
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output)["resources"][0]["resource_type"] == "knowledge_base"

    bad = runner.invoke(
        agents_app,
        [
            "bind",
            "emp-001",
            "--type",
            "knowledge_base",
            "--resource",
            "kb-ghost",
            *_base(profiles, skills),
        ],
    )
    assert bad.exit_code == 1
    assert "知识库不存在" in json.loads(bad.output)["error"]


def test_c5_cli_context_command(env: tuple[Path, Path]) -> None:
    profiles, skills = env
    runner.invoke(agents_app, ["create", "emp-001", *_base(profiles, skills)])

    ok = runner.invoke(
        agents_app,
        [
            "context",
            "emp-001",
            "--session",
            "sess-1",
            "--request",
            "req-1",
            *_base(profiles, skills),
        ],
    )
    assert ok.exit_code == 0, ok.output
    payload = json.loads(ok.output)
    assert payload["agent_id"] == "emp-001"
    assert payload["session_id"] == "sess-1"
    assert payload["attempt"] == 1

    bad_attempt = runner.invoke(
        agents_app,
        [
            "context",
            "emp-001",
            "--session",
            "sess-1",
            "--request",
            "req-1",
            "--attempt",
            "0",
            *_base(profiles, skills),
        ],
    )
    assert bad_attempt.exit_code == 1

    ghost = runner.invoke(
        agents_app,
        [
            "context",
            "ghost",
            "--session",
            "sess-1",
            "--request",
            "req-1",
            *_base(profiles, skills),
        ],
    )
    assert ghost.exit_code == 1
