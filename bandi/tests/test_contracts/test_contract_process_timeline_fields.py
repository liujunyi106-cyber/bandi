"""功能：契约——过程节点带 seq/step/summary，供前端按真实顺序渲染并在折叠态给结论。"""

from __future__ import annotations

import asyncio
from typing import Any

from bandi.core.process_recorder import ProcessRecorder, tool_result_summary


class _Inner:
    """功能：StreamHandler 空实现（只关心 recorder 自己的累积结果）。"""

    async def on_thinking(self, *a: Any, **k: Any) -> None: ...

    async def on_tool_call_start(self, *a: Any, **k: Any) -> None: ...

    async def on_tool_result(self, *a: Any, **k: Any) -> None: ...

    async def on_tool_progress(self, *a: Any, **k: Any) -> None: ...

    async def on_skill_injected(self, *a: Any, **k: Any) -> None: ...

    async def on_note(self, *a: Any, **k: Any) -> None: ...

    async def on_content_delta(self, *a: Any, **k: Any) -> None: ...


def _recorder(monkeypatch: Any) -> ProcessRecorder:
    rec = ProcessRecorder(_Inner(), store=None, session_id="s1")
    # 落库动作由其它契约覆盖，这里只看节点形态
    monkeypatch.setattr(rec, "_persist", lambda *a, **k: None, raising=False)
    return rec


def _nodes(rec: ProcessRecorder) -> list[dict[str, Any]]:
    return list(rec._nodes)


async def _drive(rec: ProcessRecorder) -> None:
    await rec.on_thinking("s1", "先看目录结构")
    await rec.on_tool_call_start("s1", "list_dir", {"path": "."}, "c1")
    await rec.on_tool_result("s1", "list_dir", True, "a.py\nb.py", 12, "c1")
    await rec.on_thinking("s1", "再读配置")
    await rec.on_tool_call_start("s1", "read", {"path": "a.py"}, "c2")
    await rec.on_tool_result("s1", "read", True, "x = 1", 8, "c2")


def test_nodes_carry_monotonic_seq(monkeypatch: Any) -> None:
    """契约：节点按追加顺序获得严格递增的 seq（前端据此平铺真实次序）。"""
    rec = _recorder(monkeypatch)
    asyncio.run(_drive(rec))
    nodes = _nodes(rec)
    seqs = [n.get("seq") for n in nodes]
    assert all(isinstance(s, int) for s in seqs), f"每个节点都要有整数 seq，实际 {seqs}"
    assert seqs == sorted(seqs), f"seq 必须随追加递增，实际 {seqs}"
    assert len(set(seqs)) == len(seqs), f"seq 不得重复，实际 {seqs}"


def test_tool_nodes_carry_summary(monkeypatch: Any) -> None:
    """契约：工具完成态带一行结论（折叠态可见），失败态前置失败标记。"""
    rec = _recorder(monkeypatch)
    asyncio.run(_drive(rec))
    tools = [n for n in _nodes(rec) if n["kind"] == "tool"]
    assert tools, "应有工具节点"
    assert tools[0]["summary"] == "已查看目录 · a.py", tools[0].get("summary")
    assert tools[1]["summary"] == "已读取文件 · x = 1", tools[1].get("summary")

    rec2 = _recorder(monkeypatch)
    asyncio.run(rec2.on_tool_call_start("s1", "bash", {"command": "nope"}, "c9"))
    asyncio.run(rec2.on_tool_result("s1", "bash", False, "command not found", 5, "c9"))
    failed = [n for n in _nodes(rec2) if n["kind"] == "tool"][0]
    assert failed["summary"].startswith("执行失败"), failed["summary"]


def test_nodes_carry_step(monkeypatch: Any) -> None:
    """契约：节点带推理步号，同一轮内多段思考执行可区分先后批次。"""
    rec = _recorder(monkeypatch)
    asyncio.run(_drive(rec))
    steps = [n.get("step") for n in _nodes(rec)]
    assert all(isinstance(s, int) and s >= 1 for s in steps), f"step 必须是正整数，实际 {steps}"


def test_summary_shape() -> None:
    """契约：结论摘要 = 动作短语 [+ 首个非空输出行]，无输出只留动作短语。"""
    assert tool_result_summary("bash", True, "ok\nmore", None) == "执行了命令 · ok"
    assert tool_result_summary("edit", True, "", None) == "修改了文件"
    assert tool_result_summary("bash", False, "boom", None) == "执行失败 · boom"
    assert tool_result_summary("mcp_custom", True, "", None) == "已调用 mcp_custom"
    long_out = "x" * 400
    assert len(tool_result_summary("bash", True, long_out, None)) <= 160
