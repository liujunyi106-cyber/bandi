# 功能：参数别名契约——弱模型参数名漂移容错（根因：write(file_path=...) 被
# jsonschema additionalProperties 拒）。别名参数与正式参数等价，schema 面可见可用。
from typing import Any

import pytest

from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool
from bandi.tools.doc_tool import DocumentReadTool
from bandi.tools.file_ops import FileReadTool, FileWriteTool
from bandi.tools.permissions_pipeline import PermissionsPipeline
from bandi.tools.registry import ToolRegistry


class _ApprovingUI:
    async def ask_permission(self, *args: Any, **kwargs: Any) -> bool:
        return True


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_instance(FileWriteTool())
    registry.register_instance(FileReadTool())
    registry.register_instance(DocumentReadTool())
    return registry


async def _run(registry: ToolRegistry, tmp_path, calls: list[ToolCall]) -> list[Any]:
    from bandi.tools.executor import ToolExecutor

    pp = PermissionsPipeline(bypass_tools={"write", "read"}, user_interaction=_ApprovingUI())
    executor = ToolExecutor(registry, permissions_pipeline=pp, project_root=str(tmp_path))
    return await executor.execute(calls)


@pytest.mark.asyncio
class TestFileParamAliases:
    async def test_write_via_file_path_alias(self, tmp_path) -> None:
        # 弱模型 file_path 命名（失败形态）必须等价 path 成功落盘
        results = await _run(
            _registry(),
            tmp_path,
            [
                ToolCall(
                    id="c1",
                    name="write",
                    arguments={"file_path": "notes/report.md", "content": "hi"},
                )
            ],
        )
        assert results[0].success, results[0].error
        assert (tmp_path / "notes" / "report.md").read_text(encoding="utf-8") == "hi"

    async def test_read_via_file_path_alias(self, tmp_path) -> None:
        (tmp_path / "report.md").write_text("hello world", encoding="utf-8")
        results = await _run(
            _registry(),
            tmp_path,
            [ToolCall(id="c2", name="read", arguments={"file_path": "report.md"})],
        )
        assert results[0].success, results[0].error
        assert "hello world" in results[0].output

    async def test_schema_exposes_alias_property(self) -> None:
        # 模型可见面：别名属性必须在 properties 中声明（否则 additionalProperties 拒）
        for tool_class in (FileWriteTool, FileReadTool):
            props = tool_class.parameters["properties"]
            assert "file_path" in props, f"{tool_class.__name__} 应暴露 file_path 别名属性"

    async def test_canonical_key_wins_on_conflict(self, tmp_path) -> None:
        # 破坏者视角：别名与正式键同时提供时正式键优先（别名不覆盖）
        results = await _run(
            _registry(),
            tmp_path,
            [
                ToolCall(
                    id="c3",
                    name="write",
                    arguments={
                        "file_path": "from-alias.md",
                        "path": "from-canonical.md",
                        "content": "z",
                    },
                )
            ],
        )
        assert results[0].success, results[0].error
        assert (tmp_path / "from-canonical.md").is_file()
        assert not (tmp_path / "from-alias.md").exists()

    async def test_read_document_aliases(self, tmp_path) -> None:
        (tmp_path / "brief.md").write_text("# brief\nnote body", encoding="utf-8")
        for alias_key in ("doc_path", "file_path", "file"):
            results = await _run(
                _registry(),
                tmp_path,
                [
                    ToolCall(
                        id=f"cd-{alias_key}",
                        name="read_document",
                        arguments={alias_key: "brief.md"},
                    )
                ],
            )
            assert results[0].success, f"{alias_key}: {results[0].error}"
            assert "note body" in results[0].output, f"{alias_key}: 未返回文档内容"

    async def test_unrelated_aliases_stay_unaffected(self, tmp_path) -> None:
        # 未声明别名的工具不得被别名机制误伤
        class NoAliasTool(BaseTool):
            name: str = "no_alias"
            parameters: dict[str, Any] = {
                "type": "object",
                "required": ["target"],
                "properties": {"target": {"type": "string"}},
            }

            async def execute(self, params: dict[str, Any], context=None) -> str:
                return f"ok:{params['target']}"

        registry = _registry()
        registry.register_instance(NoAliasTool())
        results = await _run(
            registry,
            tmp_path,
            [ToolCall(id="c4", name="no_alias", arguments={"target": "x"})],
        )
        assert results[0].success
