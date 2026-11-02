# 功能：契约——tools 域复查加固 C：Windows cmd 递归删除 / symlink 写路径统一 /
# 模板 url SSRF 与事件循环 / 流式读上限 / 输出落盘 best-effort。
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

# ---------- Windows cmd 风格递归删除 ----------


def test_contract_windows_cmd_recursive_delete_denied() -> None:
    """契约（Windows 破坏命令·破坏者）：cmd 风格 `rd /s` / `del /s` 递归删除必须
    DENY——修复前 flag 集合只有 PowerShell 风格（-recurse/-r/-force/-f），/s 漏网。"""
    from bandi.tools.bash import BashTool

    result = BashTool._match_patterns("rd", ["/s", "/q", "C:\\data"])
    assert result is not None and result.risk_level.name == "DENY", f"rd /s 必须 DENY: {result}"

    result2 = BashTool._match_patterns("del", ["/s", "/f", "C:\\data\\*"])
    assert result2 is not None and result2.risk_level.name == "DENY", f"del /s 必须 DENY: {result2}"


def test_contract_windows_del_quiet_not_denied() -> None:
    """回归：非递归 `del /q /f a.txt` 维持 WARN（不得误升级为 DENY）。"""
    from bandi.tools.bash import BashTool

    result = BashTool._match_patterns("del", ["/q", "/f", "a.txt"])
    assert result is not None and result.risk_level.name == "WARN", f"应维持 WARN: {result}"


# ---------- file_edit / apply_patch 路径统一（role_fs_scopes 强制） ----------


@pytest.mark.asyncio
async def test_contract_file_edit_role_scope_enforced(tmp_path: Path, monkeypatch) -> None:
    """契约（写路径统一·破坏者）：角色 fs_scopes 外的路径必须拒绝——修复前
    file_edit 手搓检查不含 role_fs_scopes（对比 fs 工具），scope 外文件被放行写入。"""
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)

    (tmp_path / "sub").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("abc", encoding="utf-8")

    from bandi.tools.base import ToolPermissionContext
    from bandi.tools.file_edit import FileEditTool

    ctx = ToolPermissionContext(extra={"role_fs_scopes": ["sub"]})
    with pytest.raises(ValueError):
        await FileEditTool().execute(
            {"file_path": "outside.txt", "old_string": "a", "new_string": "b"},
            context=ctx,
        )
    assert outside.read_text(encoding="utf-8") == "abc", "scope 外文件不得被写入"

    inner = tmp_path / "sub" / "inner.txt"
    inner.write_text("abc", encoding="utf-8")
    result = await FileEditTool().execute(
        {"file_path": "sub/inner.txt", "old_string": "a", "new_string": "z"},
        context=ctx,
    )
    assert "replaced" in str(result), f"scope 内路径必须可用: {result}"


@pytest.mark.asyncio
async def test_contract_apply_patch_role_scope_enforced(tmp_path: Path, monkeypatch) -> None:
    """契约（同族）：apply_patch 同样不得写 role fs_scopes 之外的文件。"""
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)

    (tmp_path / "sub").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("abc\n", encoding="utf-8")

    from bandi.tools.apply_patch import ApplyPatchTool
    from bandi.tools.base import ToolPermissionContext
    from bandi.tools.errors import NonRetryableToolError

    ctx = ToolPermissionContext(extra={"role_fs_scopes": ["sub"]})
    patch = "--- a/outside.txt\n+++ b/outside.txt\n@@ -1 +1 @@\n-abc\n+xyz\n"
    with pytest.raises(NonRetryableToolError):
        await ApplyPatchTool().execute({"patch": patch}, context=ctx)
    assert outside.read_text(encoding="utf-8") == "abc\n", "scope 外文件不得被 patch 写入"


# ---------- _persist_output best-effort ----------


def test_contract_persist_output_mkdir_failure_is_best_effort(tmp_path: Path) -> None:
    """契约（best-effort 落盘·破坏者）：temp_dir 不可创建时 _persist_output 不得
    冒泡异常——修复前 mkdir 在 try 之外，工具已成功却被标记失败。"""
    from bandi.core.message import ToolCall
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    ex = ToolExecutor(ToolRegistry())
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    ex._temp_dir = blocker / "sub"

    ex._persist_output(ToolCall(id="t1", name="demo", arguments={}), "payload")


# ---------- 模板 url SSRF + 事件循环不阻塞 ----------


@pytest.mark.asyncio
async def test_contract_template_url_private_blocked() -> None:
    """契约（SSRF·破坏者）：template_source=url 指向回环/私网必须拒绝——修复前
    无地址校验（可直接拉取 127.0.0.1 内部服务）。"""
    from bandi.tools.gen.template_filler import TemplateFillerTool

    result = await TemplateFillerTool().execute(
        {
            "template_source": "url",
            "template_url": "http://127.0.0.1:1/t",
            "variables": {},
        }
    )
    assert result.get("success") is False, f"回环地址必须拒绝: {result}"
    assert "private" in str(result.get("error", "")).lower(), (
        f"拒绝理由须为 SSRF 防护而非连接错误: {result.get('error')}"
    )


@pytest.mark.asyncio
async def test_contract_template_url_fetch_not_blocking_loop(monkeypatch) -> None:
    """契约（事件循环·破坏者）：url 拉取必须在线程池执行——同步 urlopen 阻塞
    事件循环（最长 ）导致心跳任务停摆。"""
    import time as _time
    import urllib.request as _urllib

    class _FakeResp:
        def read(self) -> bytes:
            _time.sleep(0.3)
            return b"hello"

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def _fake_urlopen(url: str, timeout: int = 30) -> _FakeResp:
        return _FakeResp()

    async def _noop_assert(url: str) -> None:
        return None

    monkeypatch.setattr(_urllib, "urlopen", _fake_urlopen)
    monkeypatch.setattr("bandi.tools.web_fetch._assert_public_url", _noop_assert)

    beats = 0

    async def _heartbeat() -> None:
        nonlocal beats
        while True:
            await asyncio.sleep(0.02)
            beats += 1

    from bandi.tools.gen.template_filler import TemplateFillerTool

    hb = asyncio.create_task(_heartbeat())
    try:
        result = await TemplateFillerTool().execute(
            {
                "template_source": "url",
                "template_url": "http://example.com/t",
                "variables": {},
            }
        )
    finally:
        hb.cancel()
        with pytest.raises(asyncio.CancelledError):
            await hb

    assert result.get("success") is True, result
    assert beats >= 5, f"事件循环被同步拉取阻塞（心跳仅 {beats} 次）"


# ---------- 流式读上限 helper ----------


@pytest.mark.asyncio
async def test_contract_read_capped_bounds_memory() -> None:
    """契约（资源上限·破坏者）：读流必须在上限内驻留内存——修复前 communicate()
    全量读入，GB 级输出 OOM。"""
    from bandi.tools.subprocess_tree import read_capped

    class _Stream:
        def __init__(self) -> None:
            self._chunks = [b"A" * 1024] * 10
            self._i = 0

        async def read(self, n: int) -> bytes:
            if self._i >= len(self._chunks):
                return b""
            chunk = self._chunks[self._i]
            self._i += 1
            return chunk

    data, truncated = await read_capped(_Stream(), 4096)
    assert len(data) <= 4096, f"驻留上限违反: {len(data)}"
    assert truncated is True

    data2, truncated2 = await read_capped(_Stream(), 100_000)
    assert len(data2) == 10 * 1024 and truncated2 is False


# ---------- close_human_review 授权口径统一 ----------


def test_contract_close_human_review_requires_authorization(tmp_path: Path) -> None:
    """契约（授权一致·破坏者）：无系统授权时 close_human_review 必须拒绝——
    修复前工具路径无任何检查（agent 可自行关闭人工评审绕过审批）。"""
    import asyncio

    from bandi.company.communication import CommunicationManager
    from bandi.tools.base import ToolPermissionContext
    from bandi.tools.collaboration import CloseHumanReviewTool

    comms = CommunicationManager(workspace=str(tmp_path), project_id="p1", session_id="s1")
    ctx = ToolPermissionContext(
        user_id="worker", role="worker", mode="execute", communication=comms
    )
    result = asyncio.run(CloseHumanReviewTool().execute({"summary": "s"}, ctx))
    assert "error" in result, f"无授权必须拒绝: {result}"
    assert "authorization" in str(result.get("error", "")), f"拒绝理由须为授权缺失: {result}"

    ctx_ok = ToolPermissionContext(
        user_id="worker",
        role="worker",
        mode="execute",
        communication=comms,
        extra={"human_review_close_allowed": True},
    )
    result_ok = asyncio.run(CloseHumanReviewTool().execute({"summary": "s"}, ctx_ok))
    assert result_ok.get("ok") is True, f"授权后必须成功: {result_ok}"


# ---------- markdown chars/bytes 口径 ----------


@pytest.mark.asyncio
async def test_contract_markdown_chars_written_is_chars(tmp_path: Path, monkeypatch) -> None:
    """契约（口径一致·破坏者）：create/append 的 chars_written/total_chars 必须是
    字符数——修复前 create 用 st_size 冒充（中文内容 3 倍虚报）。"""
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)

    from bandi.tools.doc.markdown_processor import MarkdownProcessorTool

    tool = MarkdownProcessorTool()
    content = "## 标题\n中文内容\n"
    r = await tool.execute({"operation": "write", "file_path": "a.md", "content": content})
    assert r["chars_written"] == len(content)

    r2 = await tool.execute({"operation": "create", "file_path": "b.md", "title": "中文标题"})
    assert r2.get("success") is True, r2
    written = (tmp_path / "b.md").read_text(encoding="utf-8")
    assert r2["chars_written"] == len(written), (
        f"chars_written 必须为字符数（实际 {r2['chars_written']} vs len {len(written)}）"
    )

    r3 = await tool.execute(
        {"operation": "append", "file_path": "a.md", "content": "追加中文段落\n"}
    )
    assert r3.get("success") is True, r3
    final = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert r3["total_chars"] == len(final), (
        f"total_chars 必须为字符数（实际 {r3['total_chars']} vs len {len(final)}）"
    )


# ---------- cron 校验器与调度器同源 ----------


def test_contract_cron_validator_matches_scheduler() -> None:
    """契约（口径一致·破坏者）：校验器必须与调度器 CronExpr 同源——修复前放行
    6 段/Quartz（?LW#）导致「登记成功但永不触发」的定时炸弹。"""
    from bandi.tools.util.cron_tools import _is_valid_cron

    assert _is_valid_cron("*/5 * * * *") is True
    assert _is_valid_cron("0 9 * * 1-5") is True
    assert _is_valid_cron("0 0 * * * *") is False, "6 段（秒级）调度器不支持，必须拒绝"
    assert _is_valid_cron("0 0 L * *") is False, "Quartz L 语法调度器不支持，必须拒绝"
    assert _is_valid_cron("0 0 ? * *") is False, "Quartz ? 语法调度器不支持，必须拒绝"
    assert _is_valid_cron("not a cron") is False


# ---------- BrowserPool 单例生命周期 ----------


@pytest.mark.asyncio
async def test_contract_browser_pool_singleton_lifecycle() -> None:
    """契约（单例生命周期·回归锚）：get_instance 重复返回同一实例、首次参数生效
    close() 后重建新实例；Semaphore 惰性创建（不在 get_instance 绑定事件循环）。"""
    from bandi.tools.web.browser_pool import BrowserPool

    BrowserPool._instance = None
    a = BrowserPool.get_instance(max_pages=2)
    b = BrowserPool.get_instance()
    assert a is b
    assert a._max_pages == 2
    assert a._sem is None, "Semaphore 应惰性创建（绑定首次 acquire 的事件循环）"

    await a.close()
    assert BrowserPool._instance is None
    c = BrowserPool.get_instance()
    assert c is not a
    BrowserPool._instance = None
