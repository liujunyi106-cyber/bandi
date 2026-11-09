"""功能：契约 — 工具输出高风险截断口径（缺陷④/缺陷② 修复）

钉的行为：
- 口径为「行 + 字节」双上限，非字符
- `read` 不在截断名单（其产出是用户指定的文本，且自带 offset/limit 分段）
- 截断必须给出可行动的取回出路（统一「重新调用」指引——命令类无 offset 语义）
- 折叠占位符必须给出主动取回出路
- 不切半行
"""

from __future__ import annotations

from bandi.agent.states import _cap_high_risk_output, _exceeds_high_risk_cap


class TestHighRiskCap:
    def test_under_cap_not_truncated(self) -> None:
        assert _exceeds_high_risk_cap("a\nb\nc", max_lines=10, max_bytes=1024) is False

    def test_line_cap_triggers_on_lines_not_chars(self) -> None:
        """核心回归：2000 行（哪怕 20 万字符）不该被砍；2001 行才砍。"""
        many_lines = "\n".join(f"line {i}" for i in range(2001))
        assert _exceeds_high_risk_cap(many_lines, max_lines=2000, max_bytes=50 * 1024) is True
        under = "\n".join(f"line {i}" for i in range(1999))
        assert _exceeds_high_risk_cap(under, max_lines=2000, max_bytes=50 * 1024) is False

    def test_byte_cap_triggers_on_cjk_size(self) -> None:
        """中文按 UTF-8 字节计（1 汉字 3 字节）——字符数不是字节数。"""
        cjk = "汉" * 100  # 100 字符 = 300 字节
        assert len(cjk) == 100
        assert _exceeds_high_risk_cap(cjk, max_lines=2000, max_bytes=299) is True
        assert _exceeds_high_risk_cap(cjk, max_lines=2000, max_bytes=301) is False

    def test_truncation_states_shown_and_total(self) -> None:
        out = _cap_high_risk_output(
            "\n".join(f"L{i}" for i in range(1, 31)),
            tool_name="bash",
            max_lines=10,
            max_bytes=50 * 1024,
        )
        assert "显示第 1-10 行，共 30 行" in out, out[-200:]
        assert "重新调用 bash" in out, "非分段工具须给出「重新调用」出路"
        assert "L10" in out and "L11" not in out

    def test_cap_output_has_single_non_resumable_outlet(self) -> None:
        """高风险截断出路统一为「重新调用」——原 resumable 分支永不执行
        （高风险名单与读类名单交集恒为空），已删除，不存在 offset 文案。"""
        out = _cap_high_risk_output(
            "\n".join(f"L{i}" for i in range(1, 51)),
            tool_name="bash",
            max_lines=20,
            max_bytes=50 * 1024,
        )
        assert "重新调用 bash" in out, out[-200:]
        assert "Use offset" not in out, "命令类工具无 offset 语义"

    def test_no_partial_line_at_byte_cap(self) -> None:
        """字节帽命中时保留完整行（pi：never partial lines）。"""
        lines = ["x" * 100 for _ in range(20)]
        out = _cap_high_risk_output(
            "\n".join(lines), tool_name="bash", max_lines=2000, max_bytes=350
        )
        body = out.split("\n\n[输出已截断")[0]
        for line in body.splitlines():
            assert len(line) == 100, f"出现了被切半的行：{len(line)}"

    def test_read_not_in_high_risk_list(self) -> None:
        """`read` 必须已移出截断名单（回归钉）。"""
        import inspect

        import bandi.agent.states as states_mod

        src = inspect.getsource(states_mod._append_tool_results_to_messages)
        block = src.split("_high_risk_tools = {")[1].split("}")[0]
        assert "read" not in block, f"read 不应再被截断：{block}"
        for keep in ("bash", "web_fetch", "python_exec", "subagent_spawn"):
            assert keep in block, f"{keep} 仍应保留防护"


class TestFoldPlaceholderOutlet:
    def test_placeholder_tells_model_how_to_retrieve(self) -> None:
        from bandi.agent.request_view import build_request_view
        from bandi.core.message import Message, MessageRole, ToolCall

        tc = ToolCall(id="t1", name="read", arguments={"path": "a.md"})
        # fold_before = max_turn - keep_recent_turns = 4-2 = 2；TOOL 在 turn 1 ⇒ 1 < 2 ⇒ 折叠
        msgs = [
            Message(role=MessageRole.USER, content="q1", turn_index=1),
            Message(role=MessageRole.ASSISTANT, content="", tool_calls=[tc], turn_index=1),
            Message(
                role=MessageRole.TOOL,
                content="X" * 5000,
                tool_call_id="t1",
                name="read",
                turn_index=1,
            ),
            Message(role=MessageRole.USER, content="q2", turn_index=2),
            Message(role=MessageRole.ASSISTANT, content="a2", turn_index=2),
            Message(role=MessageRole.USER, content="q3", turn_index=3),
            Message(role=MessageRole.ASSISTANT, content="a3", turn_index=3),
            Message(role=MessageRole.USER, content="q4", turn_index=4),
            Message(role=MessageRole.ASSISTANT, content="a4", turn_index=4),
        ]
        view = build_request_view(msgs, fold_max_tool_chars=1200, keep_recent_turns=2)
        folded = [m for m in view if m.role == MessageRole.TOOL][0]
        assert folded.content and "重新调用" in folded.content, folded.content
        assert "read" in folded.content, "出路提示应点名工具名"

    def test_recent_turns_still_protected(self) -> None:
        """keep_recent_turns 保护语义不变（近 2 轮不折叠）。"""
        from bandi.agent.request_view import build_request_view
        from bandi.core.message import Message, MessageRole, ToolCall

        tc = ToolCall(id="t1", name="read", arguments={"path": "a.md"})
        msgs = [
            Message(role=MessageRole.USER, content="q1", turn_index=9),
            Message(role=MessageRole.ASSISTANT, content="", tool_calls=[tc], turn_index=9),
            Message(
                role=MessageRole.TOOL,
                content="X" * 5000,
                tool_call_id="t1",
                name="read",
                turn_index=9,
            ),
        ]
        view = build_request_view(msgs, fold_max_tool_chars=1200, keep_recent_turns=2)
        tool_msg = [m for m in view if m.role == MessageRole.TOOL][0]
        assert "X" * 100 in tool_msg.content, "最近轮次不应被折叠"


def _fold_scene_messages() -> list:
    from bandi.core.message import Message, MessageRole, ToolCall

    tc = ToolCall(id="t1", name="read", arguments={"path": "a.md"})
    return [
        Message(role=MessageRole.USER, content="q1", turn_index=1),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[tc], turn_index=1),
        Message(
            role=MessageRole.TOOL,
            content="X" * 5000,
            tool_call_id="t1",
            name="read",
            turn_index=1,
        ),
        Message(role=MessageRole.USER, content="q2", turn_index=2),
        Message(role=MessageRole.ASSISTANT, content="a2", turn_index=2),
        Message(role=MessageRole.USER, content="q3", turn_index=3),
        Message(role=MessageRole.ASSISTANT, content="a3", turn_index=3),
        Message(role=MessageRole.USER, content="q4", turn_index=4),
        Message(role=MessageRole.ASSISTANT, content="a4", turn_index=4),
    ]


class TestFoldPersistChannel:
    def test_fold_persists_and_returns_path(self) -> None:
        """提供 fold_persist 时占位符带落盘路径（自动重取通道）
        原文（非占位符）进入持久化。"""
        from bandi.agent.request_view import build_request_view
        from bandi.core.message import MessageRole

        calls: list[tuple[str, str]] = []

        def fake_persist(name: str, content: str) -> str:
            calls.append((name, content))
            return "C:/proj/.bandi/tool_outputs/tool_output_fold_read_abc123.txt"

        view = build_request_view(
            _fold_scene_messages(),
            fold_max_tool_chars=1200,
            keep_recent_turns=2,
            fold_persist=fake_persist,
        )
        folded = [m for m in view if m.role == MessageRole.TOOL][0]
        assert "tool_output_fold_read_abc123.txt" in folded.content
        assert calls and calls[0][0] == "read"
        assert "X" * 100 in calls[0][1], "落盘必须是原文而非占位符"

    def test_fold_persist_failure_falls_back(self) -> None:
        """落盘失败回退「重新调用」出路（不阻断视图构建）。"""
        from bandi.agent.request_view import build_request_view
        from bandi.core.message import MessageRole

        def bad_persist(_name: str, _content: str) -> None:
            return None

        view = build_request_view(
            _fold_scene_messages(),
            fold_max_tool_chars=1200,
            keep_recent_turns=2,
            fold_persist=bad_persist,
        )
        folded = [m for m in view if m.role == MessageRole.TOOL][0]
        assert "重新调用" in folded.content
        assert "已存至" not in folded.content


class TestFoldDefaultsAligned:
    def test_config_default_matches_request_view_default(self) -> None:
        """配置默认与折叠视图默认同值（1200 残留会让「抬到 4000」失效）。"""
        from bandi.agent.request_view import DEFAULT_FOLD_MAX_TOOL_CHARS
        from bandi.core.config import AgentConfig

        assert AgentConfig().fold_tool_view_chars == DEFAULT_FOLD_MAX_TOOL_CHARS
