# 功能：契约——MCP stdio 子进程环境清洗（凭据不外泄给远端 server 进程）。
#
# 钉行为不钉实现：
# - 白名单键（PATH/HOME/USER/TEMP/TMP/LANG/LC_*/SHELL/SYSTEMROOT/APPDATA 等运行必需）
#   必须透传——否则子进程起不来；
# - 凭据形状的键（*KEY*/*TOKEN*/*SECRET*/*PASSWORD*/*CREDENTIAL*/*_COOKIE 等）
#   不得出现在子进程环境里；
# - 显式传入的 extra 键是调用方的**刻意授权**，即使形似凭据也必须透传
#   （对齐 deepseek-harness「键名即授权」语义）；
# - 默认走清洗，但可显式关闭（inherit_all=True）以兼容依赖完整环境的 server。
from __future__ import annotations

import pytest

from bandi.tools.mcp_client import StdioTransport, scrub_child_env


def _transport(**kwargs: object) -> StdioTransport:
    return StdioTransport("echo", ["hi"], **kwargs)  # type: ignore[arg-type]


class TestScrubChildEnv:
    def test_s1_runtime_keys_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 功能：运行必需键必须透传
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
        monkeypatch.setenv("TEMP", r"C:\Temp")

        env = scrub_child_env({"BANDI_MCP_TEST": "1"})

        assert env["PATH"] == "/usr/bin"
        assert env["SYSTEMROOT"] == r"C:\Windows"
        assert env["TEMP"] == r"C:\Temp"

    def test_s2_credential_shaped_keys_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 功能：凭据形状的键不得进入子进程环境
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
        monkeypatch.setenv("DB_PASSWORD", "pw")
        monkeypatch.setenv("MY_CREDENTIAL", "cred")
        monkeypatch.setenv("SESSION_COOKIE", "cookie")

        env = scrub_child_env({"BANDI_MCP_TEST": "1"})

        for leaked in (
            "TAVILY_API_KEY",
            "GITHUB_TOKEN",
            "DB_PASSWORD",
            "MY_CREDENTIAL",
            "SESSION_COOKIE",
        ):
            assert leaked not in env, f"凭据键 {leaked} 泄漏进子进程环境"

    def test_s3_explicit_extra_is_deliberate_optin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 功能：显式传入的键即调用方刻意授权，形似凭据也透传
        monkeypatch.setenv("TAVILY_API_KEY", "ambient")

        env = scrub_child_env({"TAVILY_API_KEY": "explicit"})

        assert env["TAVILY_API_KEY"] == "explicit"

    def test_s4_non_credential_custom_env_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 功能：普通自定义变量（非凭据形状）照常透传，兼容性不破
        monkeypatch.setenv("BANDI_SKILLS_DIRS", "/skills")
        monkeypatch.setenv("HTTP_PROXY", "http://proxy")

        env = scrub_child_env({})

        assert env["BANDI_SKILLS_DIRS"] == "/skills"
        assert env["HTTP_PROXY"] == "http://proxy"

    def test_s5_inherit_all_opts_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 功能：显式关闭清洗时完整继承（兼容依赖完整环境的 server）
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")

        env = scrub_child_env({}, inherit_all=True)

        assert env["TAVILY_API_KEY"] == "tvly-secret"

    def test_s6_windows_nested_credential_keys_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 功能：Windows 常见嵌套凭据键（含数字后缀）同样清洗
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        monkeypatch.setenv("BANDI_AUTH_SECRET", "s3cret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws")

        env = scrub_child_env({})

        for leaked in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "BANDI_AUTH_SECRET",
            "AWS_SECRET_ACCESS_KEY",
        ):
            assert leaked not in env, f"{leaked} 未被清洗"


class TestStdioTransportEnvWiring:
    def test_s7_transport_scrubs_by_default(self) -> None:
        # 功能：StdioTransport 默认使用清洗后的环境
        assert _transport().env_scrubbed is True

    def test_s8_transport_inherit_all_flag(self) -> None:
        # 功能：显式传 inherit_all=True 时关闭清洗
        assert _transport(inherit_all=True).env_scrubbed is False

    def test_s9_transport_env_applied(self) -> None:
        # 功能：构造参数 env 参与子进程环境（授权键透传）
        transport = _transport(env={"MY_FLAG": "on"})
        assert transport._extra_env == {"MY_FLAG": "on"}
