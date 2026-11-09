# 功能：canvas 工具族契约族——持久化产物/越界/embed 白名单/树校验/gate/装配/只读指引
# 行为族级覆盖（DoD-10）：canvas_publish 全 mode + emit_ui_tree 校验路径 + gate 双态
from __future__ import annotations

from typing import Any

import pytest


def _ctx(tmp_path: Any, **extra: Any) -> Any:
    from bandi.tools.base import ToolPermissionContext

    merged: dict[str, Any] = {"project_root": str(tmp_path)}
    merged.update(extra)
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute", extra=merged)


def _publish() -> Any:
    from bandi.tools.canvas import CanvasPublishTool

    return CanvasPublishTool()


def _emit() -> Any:
    from bandi.tools.canvas import EmitUiTreeTool

    return EmitUiTreeTool()


# ---------- 持久化产物族（DoD-10：html / embed_url / gen_ui 全 mode） ----------


@pytest.mark.asyncio
async def test_publish_html_single_file_writes_preview(tmp_path: Any) -> None:
    tool = _publish()
    result = await tool.execute(
        {"title": "中文 汇报页", "mode": "html", "html": "<html><body>你好</body></html>"},
        _ctx(tmp_path),
    )
    assert result["success"] is True
    assert result["mode"] == "html"
    assert result["preview_path"].endswith("/index.html")
    assert len(result["files"]) == 1
    out = tmp_path / result["preview_path"]
    assert out.is_file()
    assert "你好" in out.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_publish_html_single_non_index_file_falls_back_to_sole(tmp_path: Any) -> None:
    tool = _publish()
    result = await tool.execute(
        {
            "title": "单文件无入口",
            "mode": "html",
            "html_files": {"main.html": "<h1>唯一页面</h1>"},
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    assert result["preview_path"].endswith("/main.html")
    assert (tmp_path / result["preview_path"]).is_file()
    assert "唯一页面" in (tmp_path / result["preview_path"]).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_publish_html_multi_file_map_with_entry(tmp_path: Any) -> None:
    tool = _publish()
    result = await tool.execute(
        {
            "title": "多文件页",
            "mode": "html",
            "html_files": {"assets/style.css": "body{color:red}", "main.html": "<h1>主页面</h1>"},
            "html_bundle_entry": "main.html",
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    assert result["preview_path"].endswith("/main.html")
    assert len(result["files"]) == 2
    assert (tmp_path / result["preview_path"]).is_file()
    assert (tmp_path / "canvas" / "多文件页" / "assets" / "style.css").is_file()


@pytest.mark.asyncio
async def test_publish_embed_url_allowlist(tmp_path: Any) -> None:
    tool = _publish()
    ok = await tool.execute(
        {"title": "嵌入地图", "mode": "embed_url", "embed_url": "https://maps.google.com/maps?q=1"},
        _ctx(tmp_path),
    )
    assert ok["success"] is True
    assert "iframe" in (tmp_path / ok["preview_path"]).read_text(encoding="utf-8")

    vimeo = await tool.execute(
        {"title": "嵌入视频", "mode": "embed_url", "embed_url": "https://vimeo.com/12345"},
        _ctx(tmp_path),
    )
    assert vimeo["success"] is True
    youtube = await tool.execute(
        {
            "title": "嵌入油管",
            "mode": "embed_url",
            "embed_url": "https://www.youtube.com/embed/abc123",
        },
        _ctx(tmp_path),
    )
    assert youtube["success"] is True

    bad = await tool.execute(
        {"title": "x", "mode": "embed_url", "embed_url": "https://evil.example.com/embed"},
        _ctx(tmp_path),
    )
    assert bad["success"] is False
    assert "allow-listed" in str(bad.get("error", ""))
    for impostor in (
        "https://maps.google.com.evil.example/embed",
        "https://evil.example.com/?u=google.com/maps",
        "https://notyoutube.com/watch?v=abc",
    ):
        r = await tool.execute(
            {"title": "x", "mode": "embed_url", "embed_url": impostor}, _ctx(tmp_path)
        )
        assert r["success"] is False, f"伪装域应被拒绝: {impostor!r}"
        assert "allow-listed" in str(r.get("error", ""))


@pytest.mark.asyncio
async def test_publish_gen_ui_persists_tree_json(tmp_path: Any) -> None:
    tool = _publish()
    result = await tool.execute(
        {
            "title": "组件快照",
            "mode": "gen_ui",
            "ui_tree": {"kind": "Heading", "props": {"level": 1, "text": "标题"}},
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    assert result["preview_path"].endswith("/ui_tree.json")
    assert '"root"' in (tmp_path / result["preview_path"]).read_text(encoding="utf-8")


# ---------- 路径安全族（破坏者：越界/绝对路径 fail-closed，DoD-3） ----------


@pytest.mark.asyncio
async def test_publish_traversal_key_rejected(tmp_path: Any) -> None:
    tool = _publish()
    result = await tool.execute(
        {
            "title": "越界",
            "mode": "html",
            "html_files": {"../../evil.html": "<h1>bad</h1>"},
        },
        _ctx(tmp_path),
    )
    assert result["success"] is False
    assert not (tmp_path.parent / "evil.html").exists()


@pytest.mark.asyncio
async def test_publish_unsafe_keys_fail_closed(tmp_path: Any) -> None:
    tool = _publish()
    for bad_key in (
        "http://evil.example/x.html",
        "data:text/html,x",
        "/abs/index.html",
        "a/../b.html",
    ):
        result = await tool.execute(
            {"title": "不安全键", "mode": "html", "html_files": {bad_key: "<h1>x</h1>"}},
            _ctx(tmp_path),
        )
        assert result["success"] is False, f"key 应被拒绝: {bad_key!r}"
        assert any((tmp_path / "canvas").glob("**/*")) is False or result.get("files") is None


# ---------- 树校验族（emit_ui_tree 破坏者视角） ----------


@pytest.mark.asyncio
async def test_emit_valid_tree_ok() -> None:
    result = await _emit().execute(
        {
            "tree": {
                "kind": "DesignSurface",
                "props": {"preset": "minimal"},
                "children": [{"kind": "Heading", "props": {"level": 1, "text": "仪表盘"}}],
            }
        },
        _ctx("."),
    )
    assert result["success"] is True
    assert result["tree"]["kind"] == "DesignSurface"


@pytest.mark.asyncio
async def test_emit_unknown_kind_fails() -> None:
    result = await _emit().execute({"tree": {"kind": "NotReal", "props": {}}}, _ctx("."))
    assert result["success"] is False
    assert "unknown component kind" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_emit_stray_node_key_fails() -> None:
    result = await _emit().execute(
        {"tree": {"kind": "Card", "title": "漂移键", "props": {}}}, _ctx(".")
    )
    assert result["success"] is False
    assert "unexpected node keys" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_emit_deep_tree_fails() -> None:
    node: dict[str, Any] = {"kind": "Card", "props": {}}
    for _ in range(20):
        node = {"kind": "Card", "props": {}, "children": [node]}
    result = await _emit().execute({"tree": node}, _ctx("."))
    assert result["success"] is False
    assert "max depth" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_emit_node_count_budget_enforced() -> None:
    children = [{"kind": "Card", "props": {}, "nodeId": str(i)} for i in range(250)]
    root = {"kind": "Stack", "props": {}, "children": children}
    result = await _emit().execute({"tree": root}, _ctx("."))
    assert result["success"] is False
    assert "node" in str(result.get("error", "")).lower()


# ---------- 指引族（只读，无副作用） ----------


@pytest.mark.asyncio
async def test_get_genui_guide_readonly() -> None:
    from bandi.tools.canvas import GetGenuiGuideTool

    result = await GetGenuiGuideTool().execute({}, _ctx("."))
    assert result["success"] is True
    assert "workflow" in result["guide"]
    assert "anti_patterns" in result["guide"]


@pytest.mark.asyncio
async def test_get_html_canvas_guide_readonly() -> None:
    from bandi.tools.canvas import GetHtmlCanvasGuideTool

    result = await GetHtmlCanvasGuideTool().execute({}, _ctx("."))
    assert result["success"] is True
    assert "delivery" in result["guide"]
    assert "reference_template" in result["guide"]


# ---------- 装配族（factory 注册 + capability + gate fail-closed） ----------


def test_canvas_tools_registered_with_gates() -> None:
    from bandi.agent.factory import build_tool_registry
    from bandi.tools.base import ToolCategory

    registry = build_tool_registry(feature_flags={"canvas": True, "web": True})
    pub = registry.get("canvas_publish")
    emit = registry.get("emit_ui_tree")
    guide = registry.get("get_genui_guide")
    assert pub is not None and pub.category == ToolCategory.CANVAS
    assert emit is not None and emit.category == ToolCategory.CANVAS
    assert guide is not None
    assert pub.to_tool_meta()["capability_tags"] == ["file_write"]
    assert emit.to_tool_meta()["capability_tags"] == []

    closed = build_tool_registry(feature_flags={"canvas": False, "web": True})
    assert closed.get("canvas_publish") is None
    assert closed.get("emit_ui_tree") is None
    assert closed.get("get_genui_guide") is None
