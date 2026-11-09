from __future__ import annotations

from unittest.mock import patch

from bandi.tools.base import ToolDependency
from bandi.tools.dependency_checker import DependencyChecker


class TestDependencyChecker:
    def setup_method(self) -> None:
        self.checker = DependencyChecker()

    def test_check_installed(self) -> None:
        dep = ToolDependency(type="binary", name="python")
        with patch("shutil.which", return_value="/usr/bin/python"):
            assert self.checker.check(dep) is True

    def test_check_missing(self) -> None:
        dep = ToolDependency(type="binary", name="nonexistent-tool")
        with patch("shutil.which", return_value=None):
            assert self.checker.check(dep) is False

    def test_check_executable(self) -> None:
        dep = ToolDependency(type="binary", name="python")
        with (
            patch("shutil.which", return_value="/usr/bin/python"),
            patch("os.access", return_value=True),
        ):
            assert self.checker.check(dep) is True

    def test_check_python_package_installed(self) -> None:
        dep = ToolDependency(type="python_package", name="json")
        with patch("importlib.util.find_spec") as mock_find:
            mock_find.return_value = object()
            assert self.checker.check(dep) is True

    def test_check_python_package_missing(self) -> None:
        dep = ToolDependency(type="python_package", name="nonexistent_pkg")
        with patch("importlib.util.find_spec", return_value=None):
            assert self.checker.check(dep) is False

    def test_check_env_var_exists(self) -> None:
        dep = ToolDependency(type="env_var", name="PATH")
        assert self.checker.check(dep) is True

    def test_check_env_var_missing(self) -> None:
        dep = ToolDependency(type="env_var", name="_UNLIKELY_ENV_VAR_23456")
        assert self.checker.check(dep) is False

    def test_check_file_exists(self) -> None:
        dep = ToolDependency(type="file", name=__file__)
        assert self.checker.check(dep) is True

    def test_check_file_missing(self) -> None:
        dep = ToolDependency(type="file", name="/nonexistent/path/to/file")
        assert self.checker.check(dep) is False

    def test_describe_missing(self) -> None:
        deps = [
            ToolDependency(type="binary", name="missing-tool", help_hint="Install it"),
            ToolDependency(type="binary", name="python", help_hint="Needed"),
        ]
        with patch("shutil.which", side_effect=[None, "/usr/bin/python"]):
            missing = self.checker.describe_missing(deps)

        assert len(missing) == 1
        assert "missing-tool" in missing[0]
        assert "binary" in missing[0]
        assert "Install it" in missing[0]
