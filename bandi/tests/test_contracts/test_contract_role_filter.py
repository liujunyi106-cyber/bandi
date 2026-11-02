"""
契约族：角色工具过滤 fail-closed（六-）。

背景：seat_executor._role_tool_tags 查询 OrgRole.tool_tags 异常时返回
None（=不过滤，fail-open）——存储故障可被利用绕过角色限制。

本契约钉的行为族（生产调用路径）：
- `company/seat_executor.py::_role_tool_tags`（角色过滤查询）

断言「最终产物」：
- 查询异常 → 返回空列表（fail-closed：该角色不执行任何工具）+ 告警
  不得返回 None（fail-open）；
- 角色存在且 tool_tags 非空 → 正常返回白名单；
- tool_tags 混入值域外值（治理）→ 只丢非法项且告警；全非法视为未配置；
- 角色存在但未配置 tool_tags（None/空）→ None（未配置 = 不过滤，合法语义）。
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from bandi.company.seat_executor import EngineSeatExecutor


def _executor_with_engine(get_role) -> EngineSeatExecutor:
    engine = MagicMock()
    engine.get_role = get_role
    ex = EngineSeatExecutor.__new__(EngineSeatExecutor)  # 绕过重型构造，只填过滤依赖
    ex._org_engine = engine
    return ex


def test_contract_role_tags_exception_is_fail_closed() -> None:
    """查询异常必须 fail-closed（空列表=全禁），禁止 fail-open（None=不过滤）。"""

    def boom(role_id):
        raise RuntimeError("org store unavailable")

    ex = _executor_with_engine(boom)
    tags = ex._role_tool_tags("role-1")
    assert tags == [], f"查询异常必须返回空列表（fail-closed），实际 {tags!r}"


def test_contract_role_tags_normal_whitelist() -> None:
    """角色存在且 tool_tags 非空 → 返回白名单（过滤语义不变）。

    值域是 ToolCategory（治理：tool_tags 由 LLM 生成，可能落在值域外
    直接下发会让过滤求交为空、员工拿到 0 个工具），故夹具用类别值而非工具名。
    """
    role = MagicMock()
    role.tool_tags = ["doc", "util"]
    ex = _executor_with_engine(lambda rid: role)
    assert ex._role_tool_tags("role-1") == ["doc", "util"]


def test_contract_role_tags_mixed_invalid_drops_only_invalid(caplog) -> None:
    """破坏者：混入工具名/中文等值域外值 → 只丢非法项，合法项照常下发且告警。

    全非法则视为未配置（回落内置标签，custom role 为 None=不过滤）——
    不得沿用权限路径的「空名单=全禁」（填错一个字不该等于禁用全部工具）。
    """
    role = MagicMock()
    role.tool_tags = ["doc", "read", "读取文件"]
    ex = _executor_with_engine(lambda rid: role)
    with caplog.at_level(logging.WARNING, logger="bandi.company.seat_executor"):
        assert ex._role_tool_tags("role-1") == ["doc"]
    assert any("非法值" in r.message for r in caplog.records), "丢弃非法值必须告警"

    all_bad = MagicMock()
    all_bad.tool_tags = ["read", "glob"]
    ex2 = _executor_with_engine(lambda rid: all_bad)
    assert ex2._role_tool_tags("role-1") is None, "全非法=视为未配置，不得全禁"


def test_contract_role_tags_unset_means_no_filter() -> None:
    """角色存在但未配置 tool_tags（None）→ None（未配置=不过滤，合法）。"""
    role = MagicMock()
    role.tool_tags = None
    ex = _executor_with_engine(lambda rid: role)
    assert ex._role_tool_tags("role-1") is None


def test_contract_role_tags_exception_logs_warning(caplog) -> None:
    """fail-closed 必须伴随告警（degraded attention），不允许静默降级。"""

    def boom(role_id):
        raise RuntimeError("org store unavailable")

    ex = _executor_with_engine(boom)
    with caplog.at_level(logging.ERROR, logger="bandi.company.seat_executor"):
        ex._role_tool_tags("role-1")
    assert any(
        "fail-closed" in r.message.lower() or "role" in r.message.lower() for r in caplog.records
    ), "查询异常必须记录 ERROR 告警"
