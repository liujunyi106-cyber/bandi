"""
契约族：角色工具面默认值与读取失败语义。

历史：本文件原钉「查询异常 → 空列表（全禁）」与「未配置 → None（不过滤）」。
2026-11-07 按对齐：参考实现 `org_engine.py` 未配置回落显式默认
工具集；opencode 权限未命中转人工而非静默拒绝。因此改为：
- 读取角色异常 → **上抛**（任务失败），不返回空集让员工带 0 工具继续跑；
- 自定义角色未配置 → **默认面**（不是 None＝不过滤、也不是空集＝全禁）。

本契约钉的行为族（生产调用路径）：
- `company/seat_executor.py::_role_tool_tags`

断言「最终产物」：
- 查询异常 → 抛出（不吞异常、不降级）；
- 角色存在且 tool_tags 非空 → 正常返回白名单；
- tool_tags 混入值域外值 → 只丢非法项且告警；全非法视为未配置 → 默认面；
- 角色存在但未配置 tool_tags（None/空）→ 默认面。"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from bandi.company.seat_executor import EngineSeatExecutor


def _executor_with_engine(get_role) -> EngineSeatExecutor:
    engine = MagicMock()
    engine.get_role = get_role
    ex = EngineSeatExecutor.__new__(EngineSeatExecutor)  # 绕过重型构造，只填过滤依赖
    ex._org_engine = engine
    return ex


def test_contract_role_tags_exception_propagates() -> None:
    """读取失败必须上抛（任务失败），禁止降级为空集让员工带 0 工具继续跑。"""

    def boom(role_id):
        raise RuntimeError("org store unavailable")

    ex = _executor_with_engine(boom)
    with pytest.raises(RuntimeError):
        ex._role_tool_tags("role-1")


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

    全非法则视为未配置（与助理一致：不过滤）——不得沿用权限路径的「空名单=全禁」
    （填错一个字不该等于禁用全部工具）。
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
    assert ex2._role_tool_tags("role-1") is None, "全非法=视为未配置，与助理一致（不过滤）"


def test_contract_role_tags_unset_falls_back_to_assistant_parity() -> None:
    """角色存在但未配置 tool_tags → None（与助理同一口径，不落空集）。"""
    role = MagicMock()
    role.tool_tags = None
    ex = _executor_with_engine(lambda rid: role)
    assert ex._role_tool_tags("role-1") is None


def test_contract_role_tags_exception_logs_error(caplog) -> None:
    """读取失败必须伴随 ERROR 日志（可观测），且异常继续上抛。"""

    def boom(role_id):
        raise RuntimeError("org store unavailable")

    ex = _executor_with_engine(boom)
    with caplog.at_level(logging.ERROR, logger="bandi.company.seat_executor"):
        with pytest.raises(RuntimeError):
            ex._role_tool_tags("role-1")
    assert any("role" in r.message.lower() for r in caplog.records), "读取失败必须记录 ERROR"
