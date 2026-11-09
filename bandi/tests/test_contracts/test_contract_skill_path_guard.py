# 功能：契约——路径片段安全校验公共件（核销：serializer / frame_store / branching
# 三处重复实现收敛为单点 `validate_path_segment`）。
# 钉行为不钉实现：断言「返回值与拒绝条件」——ASCII 与 Unicode 两种口径、长度上限
# 路径穿越拒绝、错误类型由调用方指定（各族错误语义不变）。
from __future__ import annotations

import pytest

from bandi.core.exceptions import BandiError
from bandi.skills.path_guard import DEFAULT_MAX_SEGMENT_LENGTH, validate_path_segment


class _FakeError(BandiError):
    """功能：契约替身错误（验证 error_cls 原样抛出）。"""


def test_p1_ascii_only_rejects_non_ascii() -> None:
    assert validate_path_segment("sk_ship", label="skill_id", error_cls=_FakeError, ascii_only=True)

    with pytest.raises(_FakeError) as excinfo:
        validate_path_segment("技能卡", label="skill_id", error_cls=_FakeError, ascii_only=True)
    assert "skill_id" in str(excinfo.value)


def test_p2_unicode_mode_accepts_chinese() -> None:
    assert validate_path_segment("会话-中文", label="scope_id", error_cls=_FakeError) == "会话-中文"
    assert validate_path_segment("agent-a", label="owner_id", error_cls=_FakeError) == "agent-a"


def test_p3_traversal_and_separators_rejected() -> None:
    for value in ("../evil", "a/b", "a\\b", ".", ".."):
        with pytest.raises(_FakeError):
            validate_path_segment(value, label="scope_id", error_cls=_FakeError)
        with pytest.raises(_FakeError):
            validate_path_segment(value, label="skill_id", error_cls=_FakeError, ascii_only=True)


def test_p4_empty_and_overlong_rejected() -> None:
    with pytest.raises(_FakeError):
        validate_path_segment("", label="scope_id", error_cls=_FakeError)

    overlong = "a" * (DEFAULT_MAX_SEGMENT_LENGTH + 1)
    with pytest.raises(_FakeError) as excinfo:
        validate_path_segment(overlong, label="scope_id", error_cls=_FakeError)
    assert "过长" in str(excinfo.value)

    boundary = "a" * DEFAULT_MAX_SEGMENT_LENGTH
    assert validate_path_segment(boundary, label="scope_id", error_cls=_FakeError) == boundary


def test_p5_windows_reserved_device_names_rejected() -> None:
    for value in ("CON", "con", "Nul", "PRN", "AUX", "COM1", "com9", "LPT1", "lpt9"):
        with pytest.raises(_FakeError) as excinfo:
            validate_path_segment(value, label="scope_id", error_cls=_FakeError)
        assert "保留设备名" in str(excinfo.value)
        with pytest.raises(_FakeError):
            validate_path_segment(value, label="skill_id", error_cls=_FakeError, ascii_only=True)

    assert validate_path_segment("console", label="scope_id", error_cls=_FakeError) == "console"
    assert validate_path_segment("com10", label="scope_id", error_cls=_FakeError) == "com10"
