"""契约：`/compact` fork 出的新会话继承项目归属（与 SessionManager.fork 同口径）。

旧行为只 `create_session(title, mode)`，新会话丢掉 project_root/project_id/origin，
压缩后继续跑会脱离项目目录与项目线程；现行为继承同三个键并记录来源会话 id。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bandi.core.message import Message, MessageRole
from bandi.core.session_compact import compact_session
from bandi.memory.session.store import SessionStore


class _StubProvider:
    # 功能：压缩测试用 provider——计数按 4 字符/token，摘要固定文本
    def count_tokens(self, text: str) -> int:
        return max(1, len(text or "") // 4)

    async def complete(self, messages: list[dict], **_kwargs) -> object:
        return type(
            "Resp",
            (),
            {
                "content": (
                    "## 目标\n压缩测试\n## 所有用户消息\n用户要求保留项目归属\n## 下一步\n继续执行"
                )
            },
        )()


def _seed_session(store: SessionStore) -> str:
    sid = store.create_session(
        title="待压缩会话",
        project_root=str(Path("/work/proj")),
        project_id="proj-1",
        origin="employee_dm",
    )
    for index in range(6):
        store.append_message(
            sid,
            Message(
                role=MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
                content=f"第 {index} 条消息：" + "项目上下文内容 " * 40,
            ),
        )
    return sid


@pytest.mark.asyncio
async def test_compact_replaces_in_place_and_keeps_project_metadata(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "sessions.db"))
    sid = _seed_session(store)

    result = await compact_session(store, sid, _StubProvider())

    assert result.success, f"压缩应成功：{result.reason}"
    assert result.new_session_id == sid, "压缩必须在当前会话内完成"
    session = store.get_session(sid)
    assert session is not None, "原会话必须保留"
    meta = json.loads(session["metadata"] or "{}")
    assert meta.get("project_root") == str(Path("/work/proj"))
    assert meta.get("project_id") == "proj-1"
    assert meta.get("origin") == "employee_dm"
    assert any(entry.get("entry_type") == "summary" for entry in store.get_full_branch(sid)), (
        "当前会话必须写入压缩边界摘要"
    )
    store.close()


@pytest.mark.asyncio
async def test_manual_compact_short_history_still_produces_checkpoint(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "manual-short.db"))
    sid = store.create_session(title="短会话")
    for index, text in enumerate(["你好", "在吗", "今天适合喝咖啡吗", "继续", "记住了", "谢谢"]):
        store.append_message(
            sid,
            Message(
                role=MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
                content=text,
            ),
        )

    result = await compact_session(store, sid, _StubProvider())

    assert result.success, f"手动压缩短会话必须产出检查点：{result.reason}"
    assert result.new_session_id == sid
    assert result.after_messages < result.before_messages, "手动压缩必须真实折叠消息"
    full = store.get_full_branch(sid)
    assert any("compaction_card" in str(entry.get("metadata") or "") for entry in full), (
        "短会话手动压缩必须写入压缩卡"
    )
    store.close()
