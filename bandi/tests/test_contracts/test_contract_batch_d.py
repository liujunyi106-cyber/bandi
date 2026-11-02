# 契约原则：压缩不丢 meta / markdown 预算含 structured / LIKE 转义 /
# 兜底 prompt 含项目指令 / prefetch 不阻塞事件循环。
from __future__ import annotations

import asyncio
from unittest.mock import patch

from bandi.core.message import Message, MessageRole


def test_contract_compression_keeps_meta() -> None:
    """契约 1：压缩重建 Message 不得丢 meta——
    未传 meta（压缩后元数据丢失，如来源/指纹标记）。"""
    from bandi.memory.compression_pipeline import MicroCompressor

    compressor = MicroCompressor()
    msgs = [
        Message(
            role=MessageRole.TOOL,
            content="x" * 3000,
            tool_call_id="c1",
            name="grep",
            meta={"source": "tool", "flag": 1},
        ),
        Message(role=MessageRole.USER, content="hi", meta={"source": "user"}),
    ]
    out, _ = compressor.compress(msgs)
    tool_msg = next(m for m in out if m.role == MessageRole.TOOL)
    assert tool_msg.meta.get("source") == "tool", f"meta 必须保留，实际: {tool_msg.meta}"
    assert tool_msg.meta.get("flag") == 1


def test_contract_markdown_budget_includes_structured(tmp_path) -> None:
    """契约 2：save_visible_text 预算必须含 structured 块——
    BEGIN/END 标记切断，内容泄漏）。"""
    from bandi.memory.markdown_memory import (
        PROJECT_MEMORY_MAX_CHARS,
        MarkdownMemoryStore,
    )

    store = MarkdownMemoryStore(store_dir=str(tmp_path), project_root=str(tmp_path))
    store.ensure_memory_file()
    store.append_visible_entry("first entry")

    # 读回 structured 块（append 写入的格式）
    raw = (tmp_path / ".bandi" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "## [20" in raw, "append 必须写入时间戳条目"

    # 超大 visible 写入——structured 块必须完整保留且总长不超限
    store.save_visible_text("x" * (PROJECT_MEMORY_MAX_CHARS * 2))
    final = (tmp_path / ".bandi" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert len(final) <= PROJECT_MEMORY_MAX_CHARS + 200, f"文件总长不得严重超限，实际 {len(final)}"


def test_contract_global_store_like_escaped(tmp_path) -> None:
    """契约 3：global_store.search 必须转义 LIKE 通配符——
    `50%` 匹配字面 `50%` 而非任意前缀；`_` 匹配字面下划线。"""
    from bandi.memory.global_store import GlobalMemoryStore

    store = GlobalMemoryStore(str(tmp_path / "g.db"))
    store.put_global_memory("rate_50pct", "progress 50% complete")
    store.put_global_memory("rate_100pct", "progress 100% complete")
    store.put_global_memory("rate_50X", "progress 50X")

    hits = store.search("50%")
    keys = {h["key"] for h in hits}
    assert "rate_50pct" in keys, "字面 50% 必须命中"
    assert "rate_100pct" not in keys, "100% 不得被 50% 匹配（% 未转义则误命中）"

    store.put_global_memory("under_score", "a_b value")
    store.put_global_memory("underscore", "ab value")
    hits2 = store.search("a_b")
    keys2 = {h["key"] for h in hits2}
    assert "under_score" in keys2
    assert "underscore" not in keys2, "_ 必须作字面字符（未转义则匹配任意单字符）"


def test_contract_default_prompt_keeps_project_instructions() -> None:
    """契约 4：ContextAssembler 兜底路径必须含 project_instructions——"""
    from bandi.perception.context_assembler import ContextAssembler
    from bandi.perception.context_loader import ContextBundle

    assembler = ContextAssembler()
    bundle = ContextBundle(project_instructions="使用中文回复。")
    prompt = assembler._default_prompt("execute", bundle)
    assert "使用中文回复" in prompt, "兜底 prompt 必须含 project_instructions"


def test_contract_prefetch_not_blocking() -> None:
    """契约 5：MemoryPrefetchProvider 检索不得同步阻塞事件循环——
    search_all 是同步 IO，async 内直接调用冻结主循环（其他协程无法调度）。"""
    import time

    from bandi.perception.prefetch import MemoryPrefetchProvider

    class _SlowMemory:
        def search_all(self, query, limit=20):
            time.sleep(0.3)  # 模拟慢 DB
            return {"long_term": []}

    provider = MemoryPrefetchProvider(_SlowMemory(), max_chars=100)
    heartbeat_fired = {"ok": False}

    async def _run() -> None:
        async def _heartbeat() -> None:
            await asyncio.sleep(0.1)
            heartbeat_fired["ok"] = True  # 若事件循环被冻结 0.3s，此任务无法在 0.3s 内触发

        hb = asyncio.create_task(_heartbeat())
        await provider("hi", "s1", [])
        await hb

    asyncio.run(_run())
    assert heartbeat_fired["ok"], "事件循环被同步检索冻结（to_thread 未生效）"


def test_contract_message_from_dict_function_none() -> None:
    """契约 6：message_from_dict 对 function=None 的 tool_call 不得崩溃
    ——`tc.get("function")` 返回 None 时 `None.get` 抛 AttributeError。"""
    from bandi.core.message import message_from_dict

    msg = message_from_dict(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": None, "arguments": {}}],
        }
    )
    assert msg.tool_calls is not None and len(msg.tool_calls) == 1, (
        "function=None 必须读回 tool_call"
    )


def test_contract_policy_unknown_key_rejected() -> None:
    """契约 7：AttributeCondition 未知条件键必须拒绝——
    拼错键（resorce_type）静默通过 = 安全策略失效（恒真放行）。"""
    from bandi.core.permission.policy import AttributeCondition
    from bandi.core.permission.policy_engine import PermissionContext, Resource

    resource = Resource("tool", "write", "execute", {})  # 普通类：位置参数
    ctx = PermissionContext(user_id="u", role="admin", mode="execute")
    cond = AttributeCondition({"resorce_type": "tool"})  # 拼错
    assert cond.match(resource, ctx) is False, "未知键必须拒绝（防拼错静默放行）"

    # 合法键仍工作
    ok_cond = AttributeCondition({"resource_type": "tool", "action": "execute"})
    assert ok_cond.match(resource, ctx) is True


def test_contract_content_filter_word_boundary() -> None:
    """契约 8：英文关键词必须词边界匹配——`bomb` 不得匹配
    `bombard`、`shoot` 不得匹配 `shooter`（子串匹配误拦截）；
    中文关键词子串匹配保持。"""
    from bandi.safety.content_filter import ContentFilter

    filt = ContentFilter()
    # 不误拦截
    assert filt.check("bombard the enemy position").blocked is False, "bombard 不得触发 bomb"
    assert filt.check("the shooter ran away").blocked is False, "shooter 不得触发 shoot"
    # 真命中仍拦截
    assert filt.check("make a bomb now").blocked is True, "独立 bomb 必须触发"
    assert filt.check("枪击事件").blocked is True, "中文关键词保持子串匹配"


def test_contract_validate_params_integer_rejects_bool() -> None:
    """契约 9：validate_params integer 分支不得接受 bool——"""
    from bandi.tools.base import BaseTool, ToolCategory

    class _IntTool(BaseTool):
        name = "int_tool"
        description = "int"
        category = ToolCategory.UTIL
        parameters = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
        }

        async def execute(self, params, context=None):  # type: ignore[override]
            return "ok"

    tool = _IntTool()
    assert tool.validate_params({"count": True}), "True 不得通过 integer 校验"
    assert tool.validate_params({"count": 5}) == [], "整数 5 必须通过"


def test_contract_file_read_tool_accepts_context() -> None:
    """契约 10：FileReadTool.execute 必须接受 context 参数——"""
    import inspect

    from bandi.tools.file_ops import FileReadTool

    sig = inspect.signature(FileReadTool.execute)
    assert "context" in sig.parameters, "FileReadTool.execute 必须接受 context 参数"


def test_contract_web_fetch_byte_truncation() -> None:
    """契约 11：web_fetch 必须字节安全截断——字符切片在中文下
    超限 3 倍（截断形同虚设）。"""
    from unittest.mock import AsyncMock, MagicMock

    from bandi.tools.web_fetch import WebFetchTool

    text = "中" * 200  # 600 字节
    encoded = text.encode("utf-8")[:100]
    truncated = encoded.decode("utf-8", errors="ignore")
    assert len(truncated.encode("utf-8")) <= 100, "截断后必须 ≤ max_bytes（字节语义）"

    # 端到端：真实工具 + mock HTTP 响应
    tool = WebFetchTool(max_bytes=100)  # type: ignore[call-arg]
    resp = MagicMock()
    resp.text = text
    resp.headers = {"content-type": "text/html"}
    resp.url = "https://example.com"
    resp.raise_for_status = MagicMock()

    class _StreamCtx:
        async def __aenter__(self) -> MagicMock:
            return resp

        async def __aexit__(self, *args: object) -> bool:
            return False

    async def _body() -> object:
        yield text.encode("utf-8")

    resp.aiter_bytes = _body
    client = AsyncMock()
    client.stream = MagicMock(return_value=_StreamCtx())
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def _run() -> str:
        with (
            patch("bandi.tools.web_fetch._build_guarded_client", return_value=client),
            # 功能：mock SSRF 解析点——本契约只验证字节截断，不引入真实 DNS 网络依赖
            # （DNS 校验行为由 test_contract_web_fetch_dns.py 专项契约覆盖）
            patch(
                "bandi.tools.web_fetch._resolve_public_ips_async",
                AsyncMock(return_value=["93.184.216.34"]),
            ),
        ):
            return await tool.execute({"url": "https://example.com"})

    result = asyncio.run(_run())
    assert "truncated" in result, "超限内容必须带截断标记"


def test_contract_apply_patch_pure_addition() -> None:
    """契约 13：apply_patch 纯新增 hunk（只有 + 行）必须可应用——"""
    from bandi.tools.apply_patch import _apply_hunk

    text = "line1\nline2\n"
    hunk = "@@ -1,2 +1,4 @@\n line1\n line2\n+new line3\n+new line4\n"
    result, applied = _apply_hunk(text, hunk)
    assert applied, "纯新增 hunk 必须应用成功"
    assert "new line3" in result and "new line4" in result
    assert result.startswith("line1\nline2\n") or "line1\nline2\nnew" in result


def test_contract_file_edit_encoding_detection(tmp_path) -> None:
    """契约 14：file_edit 必须支持非 utf-8 编码文件——
    GBK 文件被强制 utf-8 读取编辑崩溃。"""
    from bandi.tools.file_edit import FileEditTool

    f = tmp_path / "gbk.txt"
    f.write_bytes("你好世界".encode("gbk"))
    tool = FileEditTool()
    with patch("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path):
        result = asyncio.run(
            tool.execute(
                {
                    "file_path": str(f),
                    "old_string": "你好",
                    "new_string": "再见",
                }
            )
        )
    assert "replaced" in result
    content = f.read_bytes().decode("gbk")
    assert "再见世界" in content, f"GBK 文件编辑后必须正确，实际: {content}"


def test_contract_reorg_from_dict_unknown_enum_defaults() -> None:
    """契约 15：ReorgProposal.from_dict 枚举未知值必须回退默认——
    脏数据（拼写错/旧版本枚举）抛 ValueError 崩溃反序列化链。"""
    from bandi.company.reorg_types import (
        ReorgProposal,
        ReorgProposalStatus,
        ReorgRiskLevel,
        ReorgScope,
    )

    proposal = ReorgProposal.from_dict(
        {"scope": "bogus_scope", "risk_level": "bogus_risk", "status": "bogus_status"}
    )
    assert proposal.scope == ReorgScope.ORG_MUTATION, "未知 scope 必须回退默认"
    assert proposal.risk_level == ReorgRiskLevel.MEDIUM, "未知 risk_level 必须回退默认"
    assert proposal.status == ReorgProposalStatus.PROPOSED, "未知 status 必须回退默认"


def test_contract_org_validate_changeset_role_none() -> None:
    """契约 16：validate_changeset 对 role=None 必须防御——
    脏数据（role 非 dict）抛 AttributeError 崩溃。"""
    from bandi.company.org_engine import OrgEngine
    from bandi.company.reorg_types import ReorgRoleChange

    org = OrgEngine()
    change = ReorgRoleChange(action="add", role=None)  # type: ignore[arg-type]
    from types import SimpleNamespace

    changeset = SimpleNamespace(role_changes=[change])
    errors = org.validate_changeset(changeset)
    assert errors, "role=None 必须报错（缺角色 id），不得抛 AttributeError"


def test_contract_communication_public_accessors() -> None:
    """契约 18：CommunicationManager 必须暴露 workspace/session_id
    公开属性——CLI 不再访问私有字段（封装违反）。"""
    from bandi.company.communication import CommunicationManager

    mgr = CommunicationManager(workspace="/tmp/w", project_id="p1", session_id="s1")
    assert mgr.workspace == "/tmp/w"
    assert mgr.session_id == "s1"
    assert mgr.project_id == "p1"


def test_contract_chinese_candidate_window_6() -> None:
    """
    5/6 字实体名漏召回。"""
    from bandi.eval.hallucination_checker import _collect_chinese_candidates

    candidates: set[str] = set()
    _collect_chinese_candidates(candidates, "某企业知识管理平台")
    assert any(len(g) == 6 for g in candidates), "6 字滑窗必须被收集"
