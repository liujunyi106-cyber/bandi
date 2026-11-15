"""契约：请求前 prune 的工具结果改为「真实头尾 + 全文路径」，不再用规则摘要吞正文。

旧行为把超预算的旧工具输出替换为规则摘要（只留 error/tail 或前 N 行），信息直接丢失；
现行为先落盘全文，再用真实头/尾摘录 + 路径引回，中段可按路径回读。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bandi.core.message import Message, MessageRole
from bandi.memory.compression_pipeline import prune_before_request
from bandi.tools.result_summary import (
    PERSISTED_EXCERPT_PREFIX,
    PERSISTED_PATH_MARKER,
    is_already_shaped,
)

_HEAD_MARK = "HEAD-SENTINEL-第一段"
_TAIL_MARK = "TAIL-SENTINEL-最后一段"


def _big_tool_content() -> str:
    # 40 万字符 ≈ 10 万 token，确保触发保护预算 + 最小收益门槛
    middle = "x" * 400_000
    return f"{_HEAD_MARK}\n{middle}\n{_TAIL_MARK}"


def _conversation() -> list[Message]:
    # 工具消息之后还有 2 个 user turn（最近 2 轮受保护，旧轮才可剪）
    return [
        Message(role=MessageRole.USER, content="第一轮需求", turn_index=0),
        Message(role=MessageRole.ASSISTANT, content="收到", turn_index=0),
        Message(
            role=MessageRole.TOOL,
            content=_big_tool_content(),
            tool_call_id="call-old",
            name="bash",
            turn_index=1,
        ),
        Message(role=MessageRole.USER, content="第二轮", turn_index=2),
        Message(role=MessageRole.ASSISTANT, content="好的", turn_index=2),
        Message(role=MessageRole.USER, content="第三轮", turn_index=3),
    ]


def test_prune_keeps_real_head_tail_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BANDI_TOOL_OUTPUT_DIR", str(tmp_path / "outputs"))
    stripped, trimmed = prune_before_request(_conversation())

    assert trimmed == 1, "旧轮超预算工具输出应被剪一次"
    pruned = stripped[2]
    assert pruned.content.startswith(PERSISTED_EXCERPT_PREFIX), "摘录应带明确前缀（便于幂等识别）"
    assert _HEAD_MARK in pruned.content, "头段真实正文必须保留"
    assert _TAIL_MARK in pruned.content, "尾段真实正文必须保留"
    assert PERSISTED_PATH_MARKER in pruned.content
    assert len(pruned.content) < 5_000, f"摘录必须有界，实际 {len(pruned.content)}"
    assert is_already_shaped(pruned.content or ""), "剪过的内容必须幂等（再次 prune 不重复落盘）"

    path = pruned.content.split(PERSISTED_PATH_MARKER, 1)[1].strip().rstrip("]")
    saved = Path(path).read_text(encoding="utf-8")
    assert _HEAD_MARK in saved and _TAIL_MARK in saved and len(saved) > 400_000, (
        "全文必须完整落盘（不得只存摘要）"
    )

    # 破坏者视角：再剪一次不得重复处理/二次落盘
    again, trimmed_again = prune_before_request(stripped)
    assert trimmed_again == 0
    assert again[2].content == pruned.content


def test_prune_falls_back_to_rule_summary_when_persist_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 落盘不可用（返回 None）时才退规则摘要，避免上下文里出现指向不存在文件的路径
    monkeypatch.setattr("bandi.tools.output_store.persist_tool_content", lambda *_a, **_k: None)
    stripped, trimmed = prune_before_request(_conversation())

    assert trimmed == 1
    content = stripped[2].content or ""
    assert content.startswith("[tool:summary]"), f"回退应为规则摘要，实际 {content[:40]}"
    assert PERSISTED_PATH_MARKER not in content
