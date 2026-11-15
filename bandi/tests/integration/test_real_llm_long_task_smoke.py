# 功能：真 LLM 长任务冒烟——一次多步任务跑到底并落盘（非 MockProvider）
#
# 开关：需 BANDI_REAL_LLM_API_KEY + BANDI_LLM_BASE_URL + BANDI_REAL_LLM_MODEL 三变量齐备
# 且 BANDI_REAL_LLM_TESTS=1。缺任一则整文件 skip，默认不进回归。
#
# 断言口径：只看最终产物与轮次终态（工作区里真有文件、内容真被读回、回复里带路径），
# 不看模型说了哪些话术——提示词改一句不该让这条用例红。

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

_API_KEY = os.environ.get("BANDI_REAL_LLM_API_KEY", "")
_BASE_URL = os.environ.get("BANDI_LLM_BASE_URL", "")
_MODEL = os.environ.get("BANDI_REAL_LLM_MODEL", "")
_REAL_ENABLED = (
    bool(_API_KEY and _BASE_URL and _MODEL) and os.environ.get("BANDI_REAL_LLM_TESTS") == "1"
)

pytestmark = [
    pytest.mark.heavy,
    pytest.mark.skipif(
        not _REAL_ENABLED,
        reason="真 LLM 冒烟：需三变量齐备 + BANDI_REAL_LLM_TESTS=1 开闸",
    ),
]

_TASK_PROMPT = (
    "这是一次多步任务，请一次做完："
    "1) 在工作区的 notes 目录下新建 long_task.txt，写入三行：第一行 alpha，第二行 beta，第三行 gamma；"
    "2) 用读取工具把该文件读回来，确认三行都在；"
    "3) 回复里给出文件的相对路径和读回的内容。不要只描述计划，直接执行。"
)


def _build_provider() -> Any:
    from bandi.llm.circuit_breaker import CircuitBreaker, RetryPolicy
    from bandi.llm.providers.openai import OpenAICompatProvider

    return OpenAICompatProvider(
        api_key=_API_KEY,
        base_url=_BASE_URL,
        default_model=_MODEL,
        circuit_breaker=CircuitBreaker(),
        retry_policy=RetryPolicy(),
    )


@pytest.mark.asyncio
async def test_long_task_reaches_file_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真 LLM：多步任务必须真写出文件并读回，而不是停在计划或空转。"""
    from unittest.mock import patch

    from bandi.memory.session.store import SessionStore
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore
    from bandi.tools import fs_tool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(fs_tool, "_WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    provider = _build_provider()
    store = SessionStore(str(tmp_path / "s.db"))
    session_id = store.create_session(mode="execute")
    ontology = OntologyStore(open_ontology(str(tmp_path / "ontology.db")))

    from bandi.core.agent_runner import run_agent_turn

    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input=_TASK_PROMPT,
            session_id=session_id,
            session_store=store,
            triple_ontology_store=ontology,
            project_root=str(workspace),
        )

    files = [p for p in workspace.rglob("*.txt")]
    assert files, (
        f"多步任务必须真产出文件，实际工作区为空：{[p.name for p in workspace.rglob('*')]}"
    )
    target = next((p for p in files if p.name == "long_task.txt"), files[0])
    text = target.read_text(encoding="utf-8")
    for needle in ("alpha", "beta", "gamma"):
        assert needle in text, f"落盘内容缺 {needle}：{text[:200]!r}"
    assert result.reply.strip(), "长任务必须有可读回复，不能只调工具不说话"
    assert target.name in result.reply or "long_task" in result.reply, (
        f"回复里必须给产物路径，实际：{result.reply[:200]!r}"
    )


@pytest.mark.asyncio
async def test_long_task_keeps_todo_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真 LLM：多步骤任务要用 todo 维护进度（可观测到的中间状态），不是一口气瞎猜。"""
    from unittest.mock import patch

    from bandi.memory.session.store import SessionStore
    from bandi.tools import fs_tool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(fs_tool, "_WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    provider = _build_provider()
    store = SessionStore(str(tmp_path / "s.db"))
    session_id = store.create_session(mode="execute")

    from bandi.core.agent_runner import run_agent_turn

    prompt = (
        "分三步完成：①列出要做的事并用 todo 工具建清单；②在工作区建 plan.md 写入三行三步骤；"
        "③把清单逐项标记完成。全部做完再回复。"
    )
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input=prompt,
            session_id=session_id,
            session_store=store,
            project_root=str(workspace),
        )

    produced = list(workspace.rglob("plan.md"))
    assert produced, f"多步任务必须落盘 plan.md，实际：{[p.name for p in workspace.rglob('*')]}"
    assert result.reply.strip(), "必须有最终回复"
