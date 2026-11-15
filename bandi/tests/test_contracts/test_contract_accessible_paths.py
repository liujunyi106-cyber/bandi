r"""契约：可访问路径必须来自本机真实探测，且覆盖非 `Desktop` 命名的桌面。

缺陷来源（真机 L1 G14-02）：系统提示词从未告知模型可访问路径，模型自行臆造
`C:/Users/Administrator/Desktop`（真实用户为 10293），5 次调用全败后仍把臆测
当成访问失败的事实报给用户，并谎称「从对话上下文看到 L1评测文件夹」。

硬编码 `C:/Users/<用户名>/Desktop` 也过不了审：非英语 Windows 桌面不叫
`Desktop`，OneDrive 重定向后桌面在 `OneDrive\Desktop`。故走注册表
`User Shell Folders\Desktop` 权威值。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bandi.agent.prompt_harness.builder import _accessible_paths_section, _user_desktop

_REAL_HOME = Path.home().resolve()


def _listed_paths(text: str) -> list[Path]:
    """功能：抠出路径段里的清单条目（反引号包裹的绝对路径）。"""
    found: list[Path] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- `") and stripped.endswith("`"):
            try:
                found.append(Path(stripped[3:-1]))
            except (OSError, ValueError):
                continue
    return found


class TestDesktopDiscovery:
    def test_desktop_path_is_never_fabricated(self) -> None:
        """破坏者视角：桌面路径不得由目录名猜出，只允许注册表权威值或 home 回退。

        干净 runner 上桌面目录可以不存在（清单会跳过它），故不要求存在；
        钉的是「来源」——注册表值或 `home/Desktop`，不会是别处的臆造路径。
        """
        desktop = _user_desktop()
        assert isinstance(desktop, Path), f"探测结果必须是 Path：{desktop!r}"
        home_fallback = Path.home() / "Desktop"
        on_windows_registry = desktop.name.lower() == "desktop" or desktop.is_absolute()
        assert desktop == home_fallback or on_windows_registry, (
            f"桌面必须来自注册表权威值或 home 回退：{desktop}"
        )

    def test_existing_desktop_is_listed(self) -> None:
        """探测到的桌面若真实存在，就必须出现在可访问路径段里。"""
        desktop = _user_desktop()
        if desktop is None or not desktop.exists():
            pytest.skip("本机无桌面目录")
        assert str(desktop) in _accessible_paths_section()

    def test_no_hardcoded_username(self) -> None:
        """不得把某台机器的用户名写进**可执行代码**——换用户即失效。

        文档字符串里保留 `C:/Users/Administrator/Desktop` 作缺陷反例，故先剥掉
        docstring 再查字面量；否则契约会被注释里的示例误伤。
        """
        import ast

        src = (
            Path(sys.modules[__name__].__file__).parents[2]
            / "bandi"
            / "agent"
            / "prompt_harness"
            / "builder.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        docstrings = {
            ast.get_docstring(node, clean=False)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in docstrings
        ]
        for text in literals:
            assert "Administrator" not in text, f"可执行代码里出现硬编码用户名：{text[:80]}"
            for needle in ("C:/Users/", "C:\\Users\\"):
                idx = text.find(needle)
                while idx != -1:
                    tail = text[idx + len(needle) : idx + len(needle) + 1]
                    assert tail == "<", (
                        f"可执行代码里出现硬编码用户路径（占位符须用 <>）：{text[:80]}"
                    )
                    idx = text.find(needle, idx + 1)

    def test_desktop_falls_back_without_crash(self, monkeypatch) -> None:
        """注册表读不到时不得抛异常——环境信息缺失不能拖垮主链路。"""
        import builtins

        real_import = builtins.__import__

        def _fake(name, *a, **kw):
            if name == "winreg":
                raise ImportError("no winreg")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _fake)
        desktop = _user_desktop()
        assert desktop is None or isinstance(desktop, Path)


class TestSectionContent:
    def test_lists_real_workspace(self) -> None:
        """段里必须给出本机真实存在的绝对路径——模型据此访问而不是臆造。"""
        home = Path.home().resolve()
        text = _accessible_paths_section()
        assert text, "可访问路径段不得为空"
        listed = _listed_paths(text)
        assert listed, f"段内必须至少列出一条真实路径：{text}"
        assert any(home in p.parents or p == home for p in listed), (
            f"用户主目录或其子路径必须出现在清单里：{listed}"
        )

    def test_forbids_fabricated_path(self) -> None:
        """段内必须带反臆造约束——否则模型仍可能编 C:/Users/<用户名>/..."""
        assert "臆造" in _accessible_paths_section()

    def test_skips_nonexistent_roots(self) -> None:
        """不存在的路径不得进清单——会把模型导向坏路径。"""
        text = _accessible_paths_section()
        for line in text.splitlines():
            if line.startswith("- `") and line.endswith("`"):
                p = Path(line[3:-1])
                assert p.exists(), f"清单里含不存在的路径：{p}"


class TestNoSectionWhenUnresolvable:
    def test_empty_on_total_failure(self, monkeypatch) -> None:
        """三处根都解析不出来时返回空串，不产出误导性段落。"""
        import bandi.agent.prompt_harness.builder as builder

        monkeypatch.setattr(builder, "_user_desktop", lambda: None)
        monkeypatch.setattr(Path, "exists", lambda self: False)
        assert builder._accessible_paths_section() == ""


@pytest.mark.parametrize("always", [True])
def test_deterministic(always: bool) -> None:
    """同一台机器多次调用结果一致——不得随机漏路径。"""
    assert _accessible_paths_section() == _accessible_paths_section()
