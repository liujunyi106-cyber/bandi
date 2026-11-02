#
#
# 机制（沿用 既有实现，不新造）：
# `BaseTool.param_aliases: dict[alias, canonical]`（**正式键优先**，别名不覆盖正式值）
# + `normalize_params()` 在执行面 **validate 之前**归一（`executor.py`）
# + schema 的 properties 必须**同步声明**别名（否则模型看不到、提供商严格校验会拒）。
#
# 逐条不变量见各用例 docstring。

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from bandi.tools.base import BaseTool

# 文件类工具的正式键：本项目 doc 系工具统一用 `file_path`
CANONICAL = "file_path"


def _production_tools() -> dict[str, BaseTool]:
    # 功能：真实装配的工具池（非手搓清单）——新增工具自动纳入本契约
    from functools import lru_cache

    from bandi.agent.factory import build_tool_registry

    @lru_cache(maxsize=1)
    def _registry() -> Any:
        return build_tool_registry()

    return {t.name: t for t in _registry().list_tools()}


def _file_path_tools() -> dict[str, BaseTool]:
    # 功能：schema 里 **required 含 file_path** 的工具 = 需要别名防线的族
    out: dict[str, BaseTool] = {}
    for name, tool in _production_tools().items():
        params = getattr(tool, "parameters", {}) or {}
        # ⚠️ 按「声明了属性」而非「required 含它」界定：csv_processor 等工具的 file_path
        # 随 operation 分支才必填 ⇒ 按 required 界定会漏掉它们（本契约初版即如此，被抓红）
        if CANONICAL in (params.get("properties") or {}):
            out[name] = tool
    return out


# ── ① 族级覆盖：所有 file_path 工具都必须配别名 ─────────────────────────────


def test_every_required_file_path_tool_declares_aliases() -> None:
    """契约①（族级）：凡 `required` 含 `file_path` 的工具，必须声明别名。

    这是 「后续批次再补」的替代机制——以后有人新增 `required:["file_path"]`
    的工具却忘了别名，这里直接红（枚举生产工具池，不靠人工维护名单）。
    """
    tools = _file_path_tools()
    assert tools, "生产工具池里应有文件类工具（装配失败会致本契约失去意义）"
    missing = sorted(name for name, tool in tools.items() if not tool.param_aliases)
    assert not missing, f"以下文件类工具未声明 param_aliases（弱模型传别名会被拒）：{missing}"


def test_alias_keys_are_declared_in_schema() -> None:
    """契约①：**别名声明的键必须在 schema properties 里可见**。

    只加 `param_aliases` 不改 schema = 模型看不到该键、提供商严格校验仍会拒
    （的原始症状就是 `'file_path' was unexpected`）。
    逐工具检查（canonical 可为 file_path 或 path，本用例不假设具体是哪个）。
    """
    problems: list[str] = []
    for name, tool in _file_path_tools().items():
        props = (getattr(tool, "parameters", {}) or {}).get("properties") or {}
        for alias in tool.param_aliases or {}:
            if alias not in props:
                problems.append(f"{name}: 别名 {alias} 未在 schema properties 声明")
    assert not problems, "别名不可见（模型看不到）：\n" + "\n".join(problems)


def test_alias_spellings_collapse_to_single_canonical() -> None:
    """契约①（行为口径）：`file_path`/`path`/`file` 三种拼法各自传入后
    归一结果必须**恰好剩一个文件键且值不变**（不产生并存歧义）。

    以行为断言而非「别名必须指向 file_path」——`file_ops`/`doc_tool` 的 canonical 是
    `path`，写死 `file_path` 会把它们误判为违规（本用例初版即犯此错）。
    """
    keys = ("file_path", "path", "file")
    problems: list[str] = []
    for name, tool in _file_path_tools().items():
        for spelling in keys:
            out = tool.normalize_params({spelling: "X"})
            present = [k for k in keys if k in out]
            if len(present) != 1 or out[present[0]] != "X":
                problems.append(f"{name}: 以 {spelling} 传参 ⇒ 归一后文件键 {present or '无'}")
    assert not problems, "别名归一产生歧义：\n" + "\n".join(problems)


def test_doc_tool_classes_are_all_covered() -> None:
    """契约①（族级·新增工具防线）：`tools/doc/` 下所有带 file_path 参数的类都已覆盖。

    直接扫类定义（比工具池更早发现「写了新工具但没注册/没配别名」）。
    """
    import importlib
    import inspect
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "bandi" / "tools" / "doc"
    uncovered: list[str] = []
    for py in sorted(root.glob("*.py")):
        if py.name.startswith("_"):
            continue
        module = importlib.import_module(f"bandi.tools.doc.{py.stem}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, BaseTool) or obj.__module__ != module.__name__:
                continue
            params = getattr(obj, "parameters", {}) or {}
            if CANONICAL not in (params.get("properties") or {}):
                continue
            if not getattr(obj, "param_aliases", None):
                uncovered.append(f"{py.name}::{obj.__name__}")
    assert not uncovered, f"doc 系工具缺别名：{uncovered}"


# ── ② 归一语义（正式键优先 / 别名让位 / 无别名零开销）──────────────────────


def test_normalize_maps_alias_to_canonical() -> None:
    """"""
    tool = _file_path_tools()["csv_processor"]
    out = tool.normalize_params({"operation": "read", "path": "/tmp/a.csv"})
    assert out[CANONICAL] == "/tmp/a.csv"
    assert "path" not in out


def test_normalize_keeps_formal_key_authoritative() -> None:
    """契约②（破坏者）：正式键已给值时，别名**不得覆盖**它。"""
    tool = _file_path_tools()["csv_processor"]
    out = tool.normalize_params({CANONICAL: "/real.csv", "file": "/wrong.csv"})
    assert out[CANONICAL] == "/real.csv"
    assert "file" not in out


def test_normalize_is_noop_without_aliases() -> None:
    """契约②：无别名声明的工具**零开销**（原样返回同一对象，不拷贝）。"""
    from bandi.tools.bash import BashTool

    tool = BashTool()
    assert not tool.param_aliases
    args = {"command": "echo hi"}
    assert tool.normalize_params(args) is args


# ── ③ 破坏者：`path` 不是万能别名（config_file 的 path 是独立语义）──────────


def test_config_file_tool_does_not_alias_path() -> None:
    """契约③（破坏者）：`config_file_tool` 的 `path` 是**点路径**（如 `database.host`）
    与文件路径是两回事 ⇒ 绝不可把 `path` 别名成 `file_path`（否则 query 操作被打断）。"""
    tool = _production_tools()["config_file"]
    assert "path" not in (tool.param_aliases or {}), "path 是独立合法参数，不得作为别名"
    assert (tool.param_aliases or {}).get("file") == CANONICAL
    out = tool.normalize_params({"operation": "query", "path": "database.host"})
    assert out["path"] == "database.host", "点路径参数必须原样保留"
    assert CANONICAL not in out


# ── ④ 端到端：走真实 ToolExecutor，用别名键调用必须成功 ─────────────────────


def test_executor_accepts_alias_key_end_to_end(tmp_path: Path) -> None:
    """契约④（端到端·生产主链路）：经**真实 ToolExecutor** 用 `path` 别名调用
    CSV 工具必须成功——证明归一化确实挂在执行面（不是只声明了字段）。

    只测 `normalize_params` 单函数会漏掉「executor 没调用它」这类断线（家族）。
    """
    from unittest.mock import patch

    from bandi.core.message import ToolCall
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    csv_path = tmp_path / "alias.csv"
    csv_path.write_text("name,score\na,1\nb,2\n", encoding="utf-8")

    from bandi.tools.doc.csv_processor import CSVProcessorTool

    registry = ToolRegistry()
    registry.register_instance(CSVProcessorTool())
    executor = ToolExecutor(registry=registry, temp_dir=str(tmp_path / "tmp"))

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path),
    ):
        results = asyncio.run(
            executor.execute(
                [
                    ToolCall(
                        id="c-alias",
                        name="csv_processor",
                        # ⚠️ 关键：用**别名** path，而不是正式键 file_path
                        arguments={"operation": "read", "path": str(csv_path)},
                    )
                ]
            )
        )

    assert len(results) == 1
    assert results[0].success, f"别名调用应被归一化后成功，实际：{results[0].error}"
    assert results[0].output, "应返回 CSV 内容"
