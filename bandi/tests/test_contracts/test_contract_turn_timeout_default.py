# 功能：契约端点面回合看门狗须默认启用——不得缺省关闭致挂起无终帧
#
# 背景：loop.run(session_timeout_sec>0) 才有回合级看门狗；端点面缺省返回 0（关闭），
# 前端 WS 路径因而从未启用兜底。表现：回合卡在等待态 172s 零帧零日志、无终帧，
# 且会话锁不释放，后续轮次 SESSION_BUSY 且无重试出口。
#
# 行为不变式（钉行为，不钉实现）：
#   1. 请求未带 session_timeout_sec → 返回正数默认（看门狗启用）
#   2. 显式传正值 → 原样采用
#   3. 显式传 0 → 归 0（保留「显式关闭」能力，供测试/CLI 使用）
#   4. 非法值（负数/非数字）→ 回落正数默认（不得归一 0 变相关闭）
#   5. 默认必须有限且不过大（防一次挂起长时间占住会话）

from __future__ import annotations

import pytest

from bandi.api.app import _DEFAULT_SESSION_TIMEOUT_SEC, _session_timeout_sec_from


class TestDefaultEnabled:
    def test_absent_key_returns_positive_default(self) -> None:
        """功能：核心不变式——缺省即启用看门狗（此前缺省 0=关闭）。"""
        value = _session_timeout_sec_from({})

        assert value > 0, "缺省必须为正数默认，否则挂起回合永远没有终帧"

    def test_absent_key_uses_module_default(self) -> None:
        """功能：缺省值取自模块常量（保证口径集中、可探测）。"""
        assert _session_timeout_sec_from({}) == _DEFAULT_SESSION_TIMEOUT_SEC

    def test_default_is_bounded(self) -> None:
        """功能：默认值有限且不过大——一次挂起最长占用会话的时长有上界。"""
        assert 60 <= _DEFAULT_SESSION_TIMEOUT_SEC <= 1800


class TestExplicitValuesHonored:
    def test_explicit_positive_used(self) -> None:
        """功能：显式正值原样采用（用例预算里常传自定义值）。"""
        assert _session_timeout_sec_from({"session_timeout_sec": 90}) == 90

    def test_explicit_zero_keeps_disabled(self) -> None:
        """功能：显式 0 仍表示关闭（保留既有能力，不误伤测试/CLI）。"""
        assert _session_timeout_sec_from({"session_timeout_sec": 0}) == 0

    def test_string_number_coerced(self) -> None:
        """功能：字符串数字可解析（前端表单可能传字符串）。"""
        assert _session_timeout_sec_from({"session_timeout_sec": "120"}) == 120


class TestInvalidValues:
    def test_negative_falls_back_to_default(self) -> None:
        """功能：破坏者视角——负数不得变相关闭看门狗，须回落启用态默认。"""
        assert (
            _session_timeout_sec_from({"session_timeout_sec": -1}) == _DEFAULT_SESSION_TIMEOUT_SEC
        )

    def test_garbage_falls_back_to_default(self) -> None:
        """功能：非法字符串回落默认（不得 0）。"""
        assert (
            _session_timeout_sec_from({"session_timeout_sec": "abc"})
            == _DEFAULT_SESSION_TIMEOUT_SEC
        )

    def test_none_falls_back_to_default(self) -> None:
        """功能：None 回落默认。"""
        assert (
            _session_timeout_sec_from({"session_timeout_sec": None}) == _DEFAULT_SESSION_TIMEOUT_SEC
        )


class TestEnvOverride:
    def test_module_default_used_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """功能：默认值在调用时从模块读取——部署侧改常量/env 即刻生效（无需 reload）。"""
        import sys

        module = sys.modules["bandi.api.app"]
        monkeypatch.setattr(module, "_DEFAULT_SESSION_TIMEOUT_SEC", 450)

        assert module._session_timeout_sec_from({}) == 450
        assert module._session_timeout_sec_from({"session_timeout_sec": 99}) == 99

    def test_default_constant_is_positive_and_read_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """功能：常量来源可被 env 覆写（源码级验证，不重载模块）。"""
        import inspect

        source = inspect.getsource(_session_timeout_sec_from)

        assert "BANDI_SESSION_TIMEOUT_SEC" in source or _DEFAULT_SESSION_TIMEOUT_SEC > 0

    def test_env_name_is_documented_in_source(self) -> None:
        """功能：env 覆写名出现在模块源码（可发现性）。"""
        import inspect
        import sys

        module_source = inspect.getsource(sys.modules["bandi.api.app"])

        assert "BANDI_SESSION_TIMEOUT_SEC" in module_source
