from __future__ import annotations

from pathlib import Path

import pytest

from bandi.tools.grep_tool import GrepTool


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", root)
    return root


@pytest.fixture
def tmp_tree(workspace_root: Path) -> str:
    d = workspace_root
    (d / "hello.py").write_text("def greet():\n    return 'hello'")
    (d / "main.py").write_text("import os\nprint('main')\n# todo")
    (d / "sub").mkdir()
    (d / "sub" / "helper.py").write_text("HELLO = 42")
    return str(d)


class TestGrepTool:
    @pytest.mark.asyncio
    async def test_grep_simple(self, tmp_tree: str) -> None:
        tool = GrepTool()
        result = await tool.execute({"pattern": "(?i)hello", "path": tmp_tree})
        assert "return 'hello'" in result
        assert "HELLO" in result

    @pytest.mark.asyncio
    async def test_grep_with_include(self, tmp_tree: str) -> None:
        tool = GrepTool()
        result = await tool.execute({"pattern": "hello", "path": tmp_tree, "include": "*.py"})
        assert "return 'hello'" in result

    @pytest.mark.asyncio
    async def test_grep_no_matches(self, tmp_tree: str) -> None:
        tool = GrepTool()
        result = await tool.execute({"pattern": "notfound", "path": tmp_tree})
        assert "no matches found" in result

    @pytest.mark.asyncio
    async def test_grep_invalid_regex(self, tmp_tree: str) -> None:
        tool = GrepTool()
        with pytest.raises(ValueError, match="invalid regex"):
            await tool.execute({"pattern": "[invalid", "path": tmp_tree})

    @pytest.mark.asyncio
    async def test_grep_path_traversal_rejected(self, workspace_root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("top secret")
        tool = GrepTool()
        with pytest.raises(ValueError, match="outside workspace"):
            await tool.execute({"pattern": "secret", "path": str(outside)})

    @pytest.mark.asyncio
    async def test_grep_skips_sensitive_dirs(self, tmp_tree: str, workspace_root: Path) -> None:
        """敏感目录（.env/.ssh/credentials 等）内容不得被 grep 检索返回。"""
        (workspace_root / ".env").write_text("API_KEY=super_secret_value")
        (workspace_root / "credentials").mkdir()
        (workspace_root / "credentials" / "token.txt").write_text("token_secret_value")
        tool = GrepTool()
        result = await tool.execute({"pattern": "secret", "path": tmp_tree})
        assert "API_KEY" not in result, "grep 不得泄漏 .env 内容"
        assert "token_secret_value" not in result, "grep 不得检索 credentials 目录"

    @pytest.mark.asyncio
    async def test_grep_skips_sensitive_extensions(
        self, tmp_tree: str, workspace_root: Path
    ) -> None:
        """敏感扩展名（.key/.pem/.pfx 等）文件不得被 grep 检索返回。"""
        (workspace_root / "server.key").write_text("PRIVATE_KEY secret_material")
        tool = GrepTool()
        result = await tool.execute({"pattern": "secret", "path": tmp_tree})
        assert "PRIVATE_KEY" not in result, "grep 不得泄漏 .key 文件内容"

    @pytest.mark.asyncio
    async def test_grep_skips_uppercase_sensitive_extensions(
        self, tmp_tree: str, workspace_root: Path
    ) -> None:
        """大写敏感扩展名（.KEY/.PEM）不得绕过敏感拦截。"""
        (workspace_root / "server.KEY").write_text("PRIVATE_KEY secret_material")
        (workspace_root / "cert.PEM").write_text("CERTIFICATE secret_material")
        tool = GrepTool()
        result = await tool.execute({"pattern": "secret", "path": tmp_tree})
        assert "PRIVATE_KEY" not in result, "grep 不得泄漏 .KEY 文件内容"
        assert "CERTIFICATE" not in result, "grep 不得泄漏 .PEM 文件内容"

    @pytest.mark.asyncio
    async def test_grep_skips_symlink_to_outside(
        self, tmp_tree: str, workspace_root: Path, tmp_path: Path
    ) -> None:
        """
        链接目标内容）——rglob 在 3.11/3.12 跟随目录符号链接。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("OUTSIDE_SECRET_MATERIAL")
        try:
            (workspace_root / "link_to_outside").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            import sys as _sys

            if _sys.platform != "win32":
                pytest.skip("symlink not supported on this platform")
            import _winapi

            try:
                _winapi.CreateJunction(str(outside), str(workspace_root / "link_to_outside"))
            except (OSError, NotImplementedError):
                pytest.skip("directory link not supported on this platform")
        tool = GrepTool()
        result = await tool.execute({"pattern": "OUTSIDE_SECRET", "path": tmp_tree})
        assert "OUTSIDE_SECRET_MATERIAL" not in result, "grep 不得读取工作区外链接目标内容"
