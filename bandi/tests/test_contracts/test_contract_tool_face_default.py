"""契约族：员工工具面与助理一致（未配置＝不过滤），读取失败＝报错。

背景（2026-11-08 复核）：助理与员工共用同一条装配管线
`agent/factory.py::build_agent_context`——唯一差别是 `role_tool_tags`
参数：助理传 None（不过滤），员工传角色配置。因此「员工未配置」的正确
语义是与助理一致（None），而不是另立一套默认面（按类别给会带出 70/112
个工具，按工具名另立清单又会与助理漂移）。

token 由另外三道闸控制：`tools.feature_flags.tool_groups` 组开关、
`select_relevant_schemas`（>35 才裁 + 打分取前 N）、`mcp_max_exposed_tools=5`。

本契约钉的行为族：
- `company/seat_executor.py::_role_tool_tags`

断言「最终产物」：
- 未配置 / 全非法 → None（与助理同一口径，不落空集、不自造默认面）；
- 有合法配置 → 只下发合法值；
- 读取角色异常 → **上抛**（装配期失败，由派发层延后/失败，不给空工具面）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bandi.company.seat_executor import EngineSeatExecutor


def _executor(get_role) -> EngineSeatExecutor:
    engine = MagicMock()
    engine.get_role = get_role
    ex = EngineSeatExecutor.__new__(EngineSeatExecutor)  # 只填过滤依赖，绕开重型构造
    ex._org_engine = engine
    return ex


def test_unset_role_equals_assistant_parity() -> None:
    """破坏者：自定义角色留空 → None（与助理同一口径），不得落空集。"""
    role = MagicMock()
    role.tool_tags = []
    tags = _executor(lambda _rid: role)._role_tool_tags("dev-1")
    assert tags is None, "未配置必须与助理一致（None＝不过滤），空集会让员工零工具"


def test_role_read_failure_raises_instead_of_empty() -> None:
    """破坏者：组织库读取失败 → 必须报错，不得静默降级成 0 工具继续跑。"""

    def boom(_rid):
        raise RuntimeError("org store unavailable")

    with pytest.raises(RuntimeError):
        _executor(boom)._role_tool_tags("dev-1")


def test_builtin_role_name_is_not_a_tool_face_baseline() -> None:
    """破坏者：岗位 id 恰好叫 member/admin 时不得被当成工具面基线（两套语义已删）。

    岗位模板只在创建员工时写入 tool_tags；运行时不按角色名收窄。
    """
    role = MagicMock()
    role.tool_tags = []
    assert _executor(lambda _rid: role)._role_tool_tags("member") is None
    assert _executor(lambda _rid: role)._role_tool_tags("admin") is None
