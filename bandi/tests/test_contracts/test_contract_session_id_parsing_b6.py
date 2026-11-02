"""契约族：role-session 解析收敛为单一实现（B6-A7）。

行为不变式（对拍口径——同输入同输出，与收敛前两份散落实现逐例一致）：
1. `parse_member_session_id` 为唯一解析入口（生成侧同文件 `session_scoping.py`）；
2. `project_from_session_id` / `role_from_session_id` 委托统一解析，覆盖
   3/4/5 段（project::role::employee / +team 或 scope / +team+scope）与
   `default` 归属、非该形态、畸形段等边界；
3. API 侧 `_role_from_role_session` 与统一实现同结果（对拍）。
"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.company.session_scoping import (
    is_member_session_id,
    member_session_prefix,
    parse_member_session_id,
    project_from_session_id,
    role_from_session_id,
)


class TestParseMemberSessionId:
    def test_three_segment_shape(self) -> None:
        parts = parse_member_session_id("role-session::proj-a::engineer::emp-1")
        assert parts is not None
        assert parts.project == "proj-a"
        assert parts.role == "engineer"
        assert parts.employee == "emp-1"

    def test_five_segment_shape_with_scope_and_team(self) -> None:
        parts = parse_member_session_id("role-session::proj-a::chat::team-1::engineer::emp-1")
        assert parts is not None
        assert parts.project == "proj-a"
        assert parts.role == "engineer"
        assert parts.employee == "emp-1"

    def test_non_member_prefix_returns_none(self) -> None:
        assert parse_member_session_id("sess-123") is None
        assert parse_member_session_id("") is None
        assert parse_member_session_id(None) is None


class TestProjectFromSessionIdEquivalence:
    """对拍：与收敛前实现逐例同输出。"""

    @pytest.mark.parametrize(
        ("session_id", "expected"),
        [
            ("role-session::proj-a::engineer::emp-1", "proj-a"),
            ("role-session::proj-a::chat::team-1::engineer::emp-1", "proj-a"),
            ("role-session::default::engineer::emp-1", ""),  # default=无归属
            ("role-session::proj-a", "proj-a"),  # 单段（旧实现取首段）
            ("普通会话 id", ""),
            ("", ""),
        ],
    )
    def test_equivalence_battery(self, session_id: str, expected: str) -> None:
        assert project_from_session_id(session_id) == expected


class TestRoleFromSessionIdEquivalence:
    """对拍：与 API 侧旧 `_role_from_role_session` 逐例同输出。"""

    @pytest.mark.parametrize(
        ("session_id", "expected"),
        [
            ("role-session::proj-a::engineer::emp-1", "engineer"),
            ("role-session::proj-a::chat::team-1::engineer::emp-1", "engineer"),
            ("role-session::proj-a::engineer", "proj-a"),  # 两段（旧实现取倒数第二段）
            ("role-session::proj-a", ""),  # 单段（旧实现要求 ≥2 段）
            ("普通会话 id", ""),
            ("", ""),
        ],
    )
    def test_equivalence_battery(self, session_id: str, expected: str) -> None:
        assert role_from_session_id(session_id) == expected


class TestSingleImplementation:
    def test_wrappers_delegate_to_parser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """收敛：两个包装器必须经由统一解析入口（单一实现）。"""
        from bandi.company import session_scoping

        calls: list[str] = []
        original = session_scoping.parse_member_session_id

        def _spy(session_id: Any) -> Any:
            calls.append(str(session_id))
            return original(session_id)

        monkeypatch.setattr(session_scoping, "parse_member_session_id", _spy)
        project_from_session_id("role-session::p::r::e")
        role_from_session_id("role-session::p::r::e")
        assert len(calls) >= 2, f"两个包装器都须走统一解析：{calls}"

    def test_app_helper_matches_unified(self) -> None:
        """API 侧入口与统一实现同结果（对拍）。"""
        import importlib

        app_mod = importlib.import_module("bandi.api.app")

        for session_id in (
            "role-session::proj-a::engineer::emp-1",
            "role-session::proj-a::chat::team-1::engineer::emp-1",
            "role-session::proj-a::engineer",
            "role-session::proj-a",
            "普通会话 id",
        ):
            assert app_mod._role_from_role_session(session_id) == role_from_session_id(
                session_id
            ), session_id


class TestPrefixHelpers:
    def test_is_member_session_id(self) -> None:
        assert is_member_session_id("role-session::p::r::e") is True
        assert is_member_session_id("sess-1") is False
        assert is_member_session_id("") is False

    def test_member_session_prefix(self) -> None:
        assert member_session_prefix("proj-a") == "role-session::proj-a::"
