# 功能：System Prompt 静态前置 / cache_control 契约
# 契约：①静态段(identity/framework/本体/mode)与动态段(领域/项目惯例/记忆/实体)拆离
# 首条 system 跨轮字节稳定(缓存前缀命中的前提)；
# ②anthropic 协议下 system 为 content-block 数组，静态首块挂 cache_control(ephemeral)；
# ③动态/运行时 system 消息(技能指令/压缩边界/摘要)不得污染静态首块；
# ④领域指令逐轮由关键词路由决定 → 必须落在动态段，静态段跨领域字节不变
# （否则跨 Agent/跨领域的前缀缓存共享被打碎）。

from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.prompt_harness.builder import PromptHarness
from bandi.perception.context_loader import ContextBundle


def _bundle(
    *,
    project: str = "",
    memory: str = "",
    entities: bool = False,
    project_scoped: bool = True,
) -> ContextBundle:
    from bandi.ontology.lookup import EntityLookupRow

    return ContextBundle(
        project_instructions=project,
        memory_context=memory,
        memory_rules_active=bool(project),
        ontology_schema="## Ontology Schema\n(演示本体 schema)",
        referenced_entities=[EntityLookupRow(id="e1", name="演示实体", entity_type="project")]
        if entities
        else [],
        # 本契约文件测成员场景的提示词顺序/缓存（默认按项目会话身份）
        project_scoped=project_scoped,
    )


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """功能：隔离用户主目录（session store / .bandi 落点）。"""
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _static_and_dynamic(project: str, memory: str, mode: str = "execute") -> tuple[str, str]:
    harness = PromptHarness()
    sections = harness.build_sections(mode=mode, context=_bundle(project=project, memory=memory))
    return sections.static, f"{sections.dynamic}\n\n{sections.tail}"


def test_static_section_byte_stable_across_turns() -> None:
    """契约：动态内容变化时静态首段必须逐字节不变（缓存前缀稳定性）。"""
    static_a, _ = _static_and_dynamic(project="项目规则A", memory="记忆A")
    static_b, _ = _static_and_dynamic(project="项目规则B（已更新）", memory="记忆B 很长很长的内容")
    assert static_a == static_b, "动态上下文变化不得改写静态前缀"


def test_static_section_excludes_dynamic_content() -> None:
    """契约：项目惯例/记忆/实体不得混入静态段（它们会破坏跨轮前缀）。"""
    static, _ = _static_and_dynamic(project="独家项目规则XYZ", memory="独家记忆XYZ")
    assert "独家项目规则XYZ" not in static
    assert "独家记忆XYZ" not in static
    assert "Ontology Schema" in static, "本体 schema 属静态段（会话内不变）"


def test_dynamic_section_carries_project_and_memory() -> None:
    """契约：项目惯例与记忆归动态段，且 safety 段仍沉底于动态内容之后。"""
    harness = PromptHarness()
    sections = harness.build_sections(
        mode="execute",
        context=_bundle(project="规则P", memory="记忆M"),
    )
    assert "规则P" in sections.dynamic
    assert "记忆M" in sections.dynamic
    # safety 沉底：safety 关键词出现在 tail，不得早于动态内容被注入
    from bandi.agent.prompt_harness.artifacts import SAFETY_PROMPT

    marker = SAFETY_PROMPT.strip().splitlines()[0].strip()
    full = f"{sections.static}\n\n{sections.dynamic}\n\n{sections.tail}"
    assert full.find("规则P") < full.find(marker), "safety 必须位于所有动态内容之后"


def test_build_keeps_original_full_order() -> None:
    """契约：build()（subagent/旧路径）拼接顺序与原实现一致（identity→…→fallback）。"""
    harness = PromptHarness()
    full = harness.build(mode="execute", context=_bundle(project="规则P", memory="记忆M"))
    from bandi.agent.prompt_harness.artifacts import FRAMEWORK_PROMPT, IDENTITY_PROMPT

    assert full.startswith(IDENTITY_PROMPT.splitlines()[0])
    assert FRAMEWORK_PROMPT.splitlines()[0] in full
    assert "规则P" in full and "记忆M" in full


def test_anthropic_system_blocks_with_cache_control_on_first() -> None:
    """契约：anthropic 转换后 system 为 text-block 数组，仅首个静态块挂 cache_control。"""
    from bandi.llm.adapters.anthropic import _convert_to_anthropic

    messages = [
        {"role": "system", "content": "静态身份/框架段"},
        {"role": "system", "content": "动态项目段"},
        {"role": "user", "content": "你好"},
    ]
    system, anthropic_msgs = _convert_to_anthropic(messages)
    assert isinstance(system, list), "system 必须转为 content-block 数组以支持 cache_control"
    assert len(system) == 2
    assert system[0]["type"] == "text"
    assert system[0]["text"] == "静态身份/框架段"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[1], "动态 system 块不得挂缓存断点（内容会变）"
    assert anthropic_msgs[0]["role"] == "user"


def test_anthropic_single_system_still_blocked_and_cached() -> None:
    """契约：单条 system（subagent 等无动态上下文路径）同样输出为块数组并挂断点。"""
    from bandi.llm.adapters.anthropic import _convert_to_anthropic

    system, _ = _convert_to_anthropic([{"role": "system", "content": "仅静态"}])
    assert isinstance(system, list) and system[0]["cache_control"] == {"type": "ephemeral"}


def test_openai_messages_keep_static_first_position() -> None:
    """契约：openai 协议下消息数组头部为 静态system, 动态system 两条（动态靠后）。"""
    static, dynamic = _static_and_dynamic(project="规则P", memory="记忆M")
    messages = [
        {"role": "system", "content": static},
        {"role": "system", "content": dynamic},
        {"role": "user", "content": "hi"},
    ]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == static
    assert "规则P" not in messages[0]["content"]
    assert "规则P" in messages[1]["content"]


@pytest.mark.asyncio
async def test_assembler_two_system_messages(home: Any, tmp_path: Any) -> None:
    """契约（生产主链路）：run_agent_turn 首轮请求的消息数组 = [静态system, 动态system, …]。"""
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.message import LLMResponse
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    class _Rec(MockProvider):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[list[dict[str, Any]]] = []

        @property
        def supports_streaming(self) -> bool:
            return False

        async def complete(self, messages, tools=None, **kw):
            self.seen.append(messages)
            return await super().complete(messages, tools=tools, **kw)

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("项目规则：契约专用", encoding="utf-8")
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore

    ontology = OntologyStore(open_ontology(str(home / "ontology.db")))
    ontology.define_class({"id": "Project", "label": "项目"}, "T00:00:00")
    store = SessionStore(str(home / "s.db"))
    sid = store.create_session(mode="execute")
    provider = _Rec()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        with patch("bandi.perception.pipeline._detect_project_root", return_value=proj):
            result = await run_agent_turn(
                user_input="hello",
                session_id=sid,
                session_store=store,
                triple_ontology_store=ontology,
            )
    assert result.reply
    msgs = provider.seen[0]
    roles = [m.get("role") for m in msgs]
    assert roles[0] == "system" and roles[1] == "system"
    assert "项目规则：契约专用" not in msgs[0]["content"], "AGENTS 内容必须落在第二条动态 system"
    assert "项目规则：契约专用" in msgs[1]["content"]
    assert "# 本体" in msgs[0]["content"], "静态段含本体词表"


def test_static_section_invariant_across_domains() -> None:
    """契约④：领域标签逐轮关键词路由 → 不得进入静态段，否则不同领域员工的静态
    前缀不一致，跨 Agent 前缀缓存共享被打碎。"""
    harness = PromptHarness()
    a = harness.build_sections(mode="execute", context=_bundle(), domain_label="金融领域X")
    b = harness.build_sections(mode="execute", context=_bundle(), domain_label="医疗领域Y")
    assert a.static == b.static, "领域变化不得改写静态前缀（跨 Agent 共享的前提）"
    assert "金融领域X" not in a.static and "医疗领域Y" not in b.static


def test_domain_instruction_lands_in_dynamic_section() -> None:
    """契约④：领域指令改由动态段承载——约束不丢，只是不再占用可缓存前缀。"""
    harness = PromptHarness()
    sections = harness.build_sections(mode="execute", context=_bundle(), domain_label="金融领域X")
    assert "金融领域X" in sections.dynamic, "领域指令必须在动态段（否则工具面约束丢失）"
    assert "只使用下方列出的工具" in sections.dynamic
    mode_prompt = harness._loader.get_mode_prompts().get("execute") or ""
    expected_prefix = (mode_prompt + "\n\n") if mode_prompt else ""
    assert sections.dynamic.startswith(f"{expected_prefix}## 当前领域"), (
        "领域指令应位于动态段 mode 指令之后"
    )


def test_domain_instruction_precedes_project_rules_in_dynamic() -> None:
    """契约④：动态段顺序 = 领域指令 → 项目惯例/记忆（领域是硬边界，先于易变内容读到）。"""
    harness = PromptHarness()
    sections = harness.build_sections(
        mode="execute",
        context=_bundle(project="规则P", memory="记忆M"),
        domain_label="金融领域X",
    )
    assert sections.dynamic.index("当前领域") < sections.dynamic.index("规则P")
    assert "金融领域X" not in sections.static and "规则P" not in sections.static


def test_static_cache_key_excludes_domain() -> None:
    """契约④（破坏者视角）：连发 70 个不同领域也不得让静态缓存按领域膨胀——
    领域已移出键空间，恒为 1 条；若有人把领域加回键，此用例必红。"""
    harness = PromptHarness()
    for index in range(70):
        harness.build_sections(mode="execute", context=_bundle(), domain_label=f"领域{index}")
    assert harness.static_cache_size == 1, "领域不得成为静态缓存键维度"


def test_assemble_first_system_identical_across_domains() -> None:
    """契约④（生产装配路径）：ContextAssembler.assemble 在不同领域下产出的首条
    system 消息逐字节相同——跨 Agent/跨领域共享 provider 前缀缓存的直接依据。"""
    from bandi.core.message import Message, MessageRole
    from bandi.perception.context_assembler import ContextAssembler

    assembler = ContextAssembler(prompt_harness=PromptHarness())
    user_msg = Message(role=MessageRole.USER, content="你好")
    heads: set[str] = set()
    for label in ("金融领域X", "医疗领域Y", ""):
        msgs = assembler.assemble(user_msg, _bundle(), mode="execute", domain_label=label)
        assert msgs[0]["role"] == "system" and msgs[1]["role"] == "system"
        heads.add(msgs[0]["content"])
        if label:
            assert label in msgs[1]["content"], f"领域 {label} 指令应落在第二条 system"
    assert len(heads) == 1, "领域不得影响首条 system 内容（否则共享前缀被打碎）"


def test_static_prompt_identical_across_modes() -> None:
    """契约：mode 提示词不得占用可缓存前缀——不同"""
    harness = PromptHarness()
    execute_static = harness._static_prompt("execute", "")
    plan_static = harness._static_prompt("plan", "")
    assert execute_static == plan_static, "mode 不得影响 static 段（否则切模式即废缓存）"


def test_mode_instruction_in_dynamic_first_and_not_static() -> None:
    """契约：mode 指令改注入 dynamic 段**首位**（先于领域/项目惯例被
    读到），且不得出现在 static。"""
    harness = PromptHarness()
    mode_prompt = harness._loader.get_mode_prompts().get("plan") or ""
    assert mode_prompt, "plan 模式提示词必须存在"
    sections = harness.build_sections(
        mode="plan", context=_bundle(project="项目P"), domain_label="领域X"
    )
    assert mode_prompt not in sections.static, "mode 指令不得在 static 段"
    assert sections.dynamic.startswith(mode_prompt), "mode 指令必须位于 dynamic 段首位"
    assert sections.dynamic.index(mode_prompt) < sections.dynamic.index("当前领域")
