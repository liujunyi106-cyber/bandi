from __future__ import annotations

from unittest.mock import MagicMock, patch

from bandi.core import agent_runner as ar
from bandi.observability.metrics import MetricsCollector


def test_contract_active_sessions_gauge_follows_register_unregister() -> None:
    # 契约：活跃会话 gauge 与 _active_contexts 注册表同步（注册 +1 / 注销 -1 / 缺失注销幂等）
    from bandi.observability import metrics as metrics_mod

    mc = MetricsCollector()
    with (
        patch.object(ar, "_active_contexts", {}),
        patch.object(metrics_mod.MetricsCollector, "_instance", mc),
    ):
        mc._active_sessions = MagicMock()
        gauge = mc._active_sessions

        ar._register_active_context("s1", object())
        gauge.set.assert_called_with(1)
        ar._register_active_context("s2", object())
        gauge.set.assert_called_with(2)
        ar._unregister_active_context("s1")
        gauge.set.assert_called_with(1)
        ar._unregister_active_context("no-such-session")
        gauge.set.assert_called_with(1)
