"""契约族：role_map 装配期自检（B6-C2）。

行为不变式：
4. 接线点：`build_policy_engine`（CLI/API 共用装配单点）触发自检。
"""

from __future__ import annotations

import logging
from typing import Any

from bandi.core.permission.role_mapping import (
    check_role_map,
    validate_role_map,
)


class TestValidateRoleMapHardening:
    def test_non_mapping_input_fails_safe(self) -> None:
        """"""
        problems = validate_role_map(["engineer", "member"])  # type: ignore[arg-type]
        assert problems and "类型" in problems[0]

    def test_bad_target_reported(self) -> None:
        problems = validate_role_map({"engineer": "superuser"})
        assert any("superuser" in p for p in problems)

    def test_good_map_clean(self) -> None:
        assert validate_role_map({"engineer": "member"}) == []
        assert validate_role_map(None) == []


class TestCheckRoleMap:
    def test_bad_map_warns_once_per_fingerprint(self, caplog: Any) -> None:
        role_map = {"engineer": "superuser", "pm": "member"}
        with caplog.at_level(logging.WARNING, logger="bandi.core.permission.role_mapping"):
            check_role_map(role_map)
            first = [r for r in caplog.records if "superuser" in r.getMessage()]
            assert first, f"坏映射应告警：{[r.getMessage() for r in caplog.records]}"
            caplog.clear()
            check_role_map(role_map)
            assert not [r for r in caplog.records if "superuser" in r.getMessage()], (
                "同一映射指纹不得重复告警（防每轮刷屏）"
            )

    def test_good_map_logs_describe_without_warning(self, caplog: Any) -> None:
        with caplog.at_level(logging.INFO, logger="bandi.core.permission.role_mapping"):
            check_role_map({"engineer": "power_user"})
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("engineer→power_user" in r.getMessage() for r in caplog.records), (
            f"describe 摘要应入日志：{[r.getMessage() for r in caplog.records]}"
        )

    def test_empty_map_silent(self, caplog: Any) -> None:
        with caplog.at_level(logging.INFO, logger="bandi.core.permission.role_mapping"):
            check_role_map(None)
        assert not caplog.records, "未启用映射应完全静默"


class TestBuildPolicyEngineWiring:
    def test_build_policy_engine_triggers_selfcheck(self, caplog: Any) -> None:
        """"""
        from bandi.agent.factory import build_policy_engine
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        with caplog.at_level(logging.WARNING, logger="bandi.core.permission.role_mapping"):
            build_policy_engine(
                PermissionsPipeline(),
                "user",
                {"engineer": "no-such-tier"},
                user_id="",
            )
        assert any("no-such-tier" in r.getMessage() for r in caplog.records), (
            f"装配期自检应告警：{[r.getMessage() for r in caplog.records]}"
        )
