#
# `mkdir("CON")` 直接 OSError（保留设备名）；`foo.` 与 `foo` 在 NTFS 上同一名字
# （尾随点碰撞）；`../` 类穿越写出沙箱。
#
# ① `skills/path_guard.validate_path_segment` —— 项目 id / 技能卡 id（白名单正则 + 保留名）
# ② `company/comms._safe_path_segment` —— session/role/meeting id（**含客户端传入**）
# 本契约把「同一清单」钉成参数化族级用例：两个入口都必须拒绝同一组攻击输入
# 且都必须放行同一组合法输入（含中文会话名，不得误伤）。

from __future__ import annotations

from typing import Any

import pytest

from bandi.company.comms import _safe_path_segment
from bandi.skills.path_guard import validate_path_segment

# 攻击输入：穿越 / 绝对路径 / 盘符 / 保留设备名（含扩展名形态）/ 尾随点与空格 / 空
DANGEROUS = [
    "",
    "   ",
    ".",
    "..",
    "../etc",
    "..\\windows",
    "a/b",
    "a\\b",
    "C:\\temp",
    "C:/temp",
    "/abs",
    "\\abs",
    "CON",
    "con",
    "PRN",
    "AUX",
    "NUL",
    "nul.txt",
    "COM1",
    "LPT9",
    "foo.",
    "foo ",
    " foo",
]

# 合法输入：普通 id + 中文名（本项目真实形态——会话名/员工名可为中文）
LEGIT = ["ok-1", "sess_abc", "proj-2026-09-28", "中文会话名", "员工张三"]


def _path_guard_call(value: str) -> Any:
    from bandi.core.exceptions import BandiError

    return validate_path_segment(value, label="seg", error_cls=BandiError)


ENTRIES = {
    "skills/path_guard": _path_guard_call,
    "company/comms": lambda value: _safe_path_segment(value, "seg"),
}


@pytest.mark.parametrize("entry", sorted(ENTRIES))
@pytest.mark.parametrize("bad", DANGEROUS)
def test_every_path_segment_entry_rejects_checklist(entry: str, bad: str) -> None:
    """契约（族级·破坏者）：两个入口都必须拒绝清单里的每一种形态。

    只测其中一个入口（例如此前只测 path_guard）会漏掉 comms 这个「客户端可传
    session_id」的入口——「同族逐入口核对」的正是这种漏。
    """
    with pytest.raises(Exception) as exc:
        ENTRIES[entry](bad)
    assert "seg" in str(exc.value), "异常信息须含字段名（便于定位）"


@pytest.mark.parametrize("entry", sorted(ENTRIES))
@pytest.mark.parametrize("good", LEGIT)
def test_legit_ids_pass_in_every_entry(entry: str, good: str) -> None:
    """契约（破坏者反向）：合法 id 不得误伤——**中文名必须放行**（本项目真实形态）。"""
    assert ENTRIES[entry](good) == good


def test_trailing_dot_is_rejected_not_silently_stripped() -> None:
    """契约（关键语义）：尾随点必须**报错**而非静默 strip。

    静默 strip 会让 `foo.` 与 `foo` 落到同一目录（NTFS 等价名）⇒ 两个不同 id 静默串台
    比抛错危险得多。这条钉住「拒绝」这个选择本身。
    """
    with pytest.raises(ValueError):
        _safe_path_segment("foo.", "session_id")
    assert _safe_path_segment("foo", "session_id") == "foo"


def test_comms_builds_no_directory_for_reserved_session_id(tmp_path: Any) -> None:
    """契约（端到端）：保留名的 session_id **不得**建出任何目录（OSError 的那条路）。"""
    from bandi.company import comms

    with pytest.raises(ValueError):
        comms._comms_root(str(tmp_path), "proj", "CON")
    assert not (tmp_path / comms.COMMS_ROOT_NAME).exists()
