"""契约（canonical）：长期记忆「要不要写 / 写什么 / 落哪」只有一个判定入口。

背景（2026-11 审计）：这段判定曾在助理 / 员工私聊 / 群聊三条分支各写一遍，
改一处漏一处；真机 bug「口头称呼出不了会话」「任务模板被当用户陈述写成记忆」
「每轮都写记忆」都出在这里。判定现已收口到 `memory/decide.decide_long_term`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bandi.core import agent_runner
from bandi.memory.decide import decide_long_term, scope_for
from bandi.memory.memory_types import MemoryType

ENTRY_SCOPES = [
    ("assistant", None, None),
    ("dm", "dm:abc123", None),
    ("group", "group:xyz789", None),
]


@pytest.mark.parametrize(("entry", "fallback", "employee"), ENTRY_SCOPES)
def test_preference_goes_global_from_every_entry(entry, fallback, employee) -> None:
    """称呼/偏好从任何入口进来都落全局层——换个会话、换个项目都该记得。"""
    decision = decide_long_term(
        raw_input="记住：以后叫我老板",
        fallback_project=fallback,
        fallback_employee=employee,
        project_id="proj-1",
    )
    assert decision.write is True
    assert decision.memory_type == MemoryType.PREFERENCE.value
    assert (decision.project_id, decision.employee_id) == (None, None), (
        f"{entry} 入口把称呼写进了 {decision.project_id}/{decision.employee_id}"
    )


@pytest.mark.parametrize(("entry", "fallback", "employee"), ENTRY_SCOPES)
def test_session_fact_stays_in_entry_scope(entry, fallback, employee) -> None:
    """会话内事实留在各自作用域——不跨会话污染，但也不越权写全局。"""
    decision = decide_long_term(
        raw_input="临时规范：导出表统一命名 staff_dedup_v2",
        fallback_project=fallback,
        fallback_employee=employee,
        project_id="proj-1",
    )
    assert decision.write is True
    assert decision.project_id == fallback, f"{entry} 入口的作用域丢了"


@pytest.mark.parametrize(("entry", "fallback", "employee"), ENTRY_SCOPES)
@pytest.mark.parametrize("noise", ["你好", "ping", "谢谢", "收到"])
def test_noise_writes_nothing_from_every_entry(entry, fallback, employee, noise) -> None:
    """寒暄/探针从任何入口进来都不写长期记忆。"""
    decision = decide_long_term(
        raw_input=noise, fallback_project=fallback, fallback_employee=employee
    )
    assert decision.write is False
    assert decision.content == ""


@pytest.mark.parametrize(("entry", "fallback", "employee"), ENTRY_SCOPES)
def test_task_contract_template_not_written(entry, fallback, employee) -> None:
    """公司派单拼的任务契约模板不是用户陈述，三入口都不得写。"""
    template = "## 任务契约\n请按以下要求完成：\n- 至少 3 个步骤\n- 输出到 out/ 目录\n## 输出要求\n"
    decision = decide_long_term(
        raw_input=template, fallback_project=fallback, fallback_employee=employee
    )
    assert decision.write is False, f"{entry} 入口把任务模板写成了记忆"


def test_decision_is_entry_independent() -> None:
    """同一段用户输入，三入口的判定结果必须一致（除作用域按类型分流外）。"""
    payload = "记住：我偏好报告统一导出 CSV 格式"
    results = [
        decide_long_term(
            raw_input=payload,
            fallback_project=fallback,
            fallback_employee=employee,
            project_id="p1",
        )
        for _entry, fallback, employee in ENTRY_SCOPES
    ]
    assert {r.write for r in results} == {True}
    assert {r.memory_type for r in results} == {MemoryType.PREFERENCE.value}
    assert {r.content for r in results} == {results[0].content}


def test_scope_for_matches_legacy_thunk() -> None:
    """`agent_runner._long_term_scope_for` 是薄壳，必须与 canonical 实现一致。"""
    cases = [
        (MemoryType.PREFERENCE, "p1"),
        (MemoryType.PROCESS_CONCLUSION, "p1"),
        (MemoryType.BUSINESS_FACT, "p1"),
        (MemoryType.PROCESS_CONCLUSION, None),
    ]
    for memory_type, project in cases:
        legacy = agent_runner._long_term_scope_for("dm:x", None, memory_type, project_id=project)
        canonical = scope_for(
            memory_type, fallback_project="dm:x", fallback_employee=None, project_id=project
        )
        assert legacy == canonical, f"{memory_type}/{project} 薄壳与 canonical 漂移"


def test_agent_runner_has_no_second_judgement_site() -> None:
    """机械保证：判定只许在 memory.decide 里，agent_runner 不得再直接调 review_ingest。"""
    source = Path(agent_runner.__file__).read_text(encoding="utf-8")
    assert "review_ingest(" not in source, "agent_runner 又出现了一处独立的价值判定"
    assert "is_rememberable_user_input(" not in source, "内容源选择必须走 decide_long_term"
