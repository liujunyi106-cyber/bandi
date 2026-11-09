# 功能：契约 — 角色 tool_tags 值域归一化
# 占位符字面量 ["optional","tool","tags"] ⇒ LLM 无从得知合法值。非法值（工具名/中文/
# （108 → 0）。
# 契约：
# ① 正常化只做「解析 + 丢弃」，合法值 ⊆ ToolCategory；
# ② 消费侧「全非法」必须视为**未配置**（不过滤），不得沿用权限路径的「空名单=全禁」；
# ③ `[]`（权限 fail-closed）与「全非法」（输入解析失败）语义**必须区分**；
# ⑤ 提示词必须给出 ToolCategory 枚举且不再含占位符。

from __future__ import annotations

from functools import lru_cache
from typing import Any

import pytest

from bandi.agent.factory import build_tool_registry, filter_tools_by_role_tags
from bandi.tools.base import ToolCategory
from bandi.tools.registry import (
    VALID_ROLE_TOOL_TAGS,
    normalize_role_tool_tags,
)

# 契约里出现的「LLM 可能真的这么填」的样本值（全部非 ToolCategory）
LLM_PLAUSIBLE_JUNK = (
    ["optional", "tool", "tags"],  # 照抄提示词占位符
    ["read", "write", "bash"],  # 填了工具名
    ["搜索", "文件"],  # 填了中文
    ["admin"],  # 填了内置「角色名」而非「类别名」
)


@lru_cache(maxsize=1)
def _registry() -> Any:
    """生产装配的真实 registry。**必须缓存**：单次构建约 5s（108 个工具）
    用例内反复构建会让整个契约文件超时。"""
    return build_tool_registry()


def _names() -> list[str]:
    return [t.name for t in _registry().list_tools()]


# ── ① 值域与归一化 ────────────────────────────────────────────────────


def test_valid_domain_is_tool_category() -> None:
    """契约①：合法值域必须由 ToolCategory 派生（单一来源，防实现/提示词漂移）。"""
    assert VALID_ROLE_TOOL_TAGS == {c.value for c in ToolCategory}
    assert "admin" not in VALID_ROLE_TOOL_TAGS, "内置角色名不是工具类别，不得视为合法值"


def test_normalize_keeps_valid_and_reports_dropped() -> None:
    """契约①：归一化返回 (合法值升序去重, 被丢弃值升序去重)。"""
    assert normalize_role_tool_tags(["code", "code", "web"]) == (["code", "web"], [])
    assert normalize_role_tool_tags(["code", "中文", "read"]) == (["code"], ["read", "中文"])
    assert normalize_role_tool_tags(None) == ([], [])
    assert normalize_role_tool_tags([]) == ([], [])
    assert normalize_role_tool_tags(["", "  "]) == ([], [])  # 空白归一后丢弃


# ── ② 消费侧：非法值不得静默禁用员工 ──────────────────────────────────


def test_filter_all_invalid_treats_as_unconfigured() -> None:
    """契约②（回归核心）：全为非法值时不得滤成 0 个工具——视为未配置 ⇒ 不过滤。"""
    names = _names()
    for junk in LLM_PLAUSIBLE_JUNK:
        kept = filter_tools_by_role_tags(names, _registry(), junk)
        assert kept == names, f"非法值 {junk} 不得把工具滤空（实测曾 108→0，员工无法工作）"


def test_filter_empty_list_still_fail_closed() -> None:
    """契约③（破坏者视角）：`[]` = 权限路径显式全禁，**不得**被归一化放宽为不过滤。"""
    names = _names()
    assert filter_tools_by_role_tags(names, _registry(), []) == []
    assert filter_tools_by_role_tags(names, _registry(), None) == names


def test_filter_valid_tags_still_narrow() -> None:
    """契约①：合法值仍必须真实收窄（防「修 A 破 B」——把权限过滤整体废掉）。"""
    names = _names()
    kept = filter_tools_by_role_tags(names, _registry(), ["code"])
    assert 0 < len(kept) < len(names), f"合法类别 code 应收窄工具面，实际 {len(kept)}/{len(names)}"
    assert kept != names


def test_filter_valid_but_no_matching_tool_does_not_escalate() -> None:
    """契约④（破坏者视角）：合法类别但当前无对应工具 ⇒ 结果为空
    **不得**回退全量（否则「限定某类别」被升级成「放开全部」= 权限升级）。"""
    names = _names()
    rare = [c.value for c in ToolCategory if c.value not in {"code", "web", "data", "util"}]
    for tag in rare:
        kept = filter_tools_by_role_tags(names, _registry(), [tag])
        assert kept != names or not kept, f"类别 {tag} 若无工具则必须为空，不得放开全量"


# ── ③ seat 侧：定义点归一化 + 回落链路 ────────────────────────────────


def _seat(org: Any) -> Any:
    from bandi.company.seat_executor import EngineSeatExecutor

    ex = EngineSeatExecutor.__new__(EngineSeatExecutor)
    ex._org_engine = org
    return ex


def test_seat_role_all_invalid_tags_equals_assistant_parity() -> None:
    """契约②：角色只配了非法值 ⇒ 视为未配置 ⇒ 回落默认面（不是空集，也不是不过滤）。"""
    from bandi.company.org_engine import OrgEngine

    org = OrgEngine()
    org.register_role("dev-1", "Dev", tool_tags=["read", "write"])
    # 存储层保持原样（org.json 往返保真契约要求，见 final_decider 契约）
    assert org.get_role("dev-1").tool_tags == ["read", "write"]
    assert _seat(org)._role_tool_tags("dev-1") is None, (
        "全非法值视为未配置，与助理一致（None＝不过滤）"
    )


def test_seat_role_mixed_tags_keeps_only_valid() -> None:
    """契约①：混填时只下发合法值（非法值丢弃，不误伤合法约束）。"""
    from bandi.company.org_engine import OrgEngine

    org = OrgEngine()
    org.register_role("dev-2", "Dev2", tool_tags=["code", "中文", "read"])
    assert _seat(org)._role_tool_tags("dev-2") == ["code"]


def test_seat_builtin_fallback_and_exception_path_preserved() -> None:
    """契约③：无配置 + 内置角色名 ⇒ 内置岗位基线；角色查询异常 ⇒ 上抛（任务失败）。"""
    from bandi.company.org_engine import OrgEngine

    org = OrgEngine()
    org.register_role("member", "M", tool_tags=[])
    assert _seat(org)._role_tool_tags("member") is None, "岗位名不再充当工具面基线"

    class _Boom:
        def get_role(self, _rid: str) -> Any:
            raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        _seat(_Boom())._role_tool_tags("any")


# ── ④ 生产路径：员工实际拿到的工具数不为零 ────────────────────────────


def test_production_path_employee_keeps_tools_with_junk_tags() -> None:
    """契约（生产装配路径）：用真实 OrgEngine + 真实 registry，模拟 LLM 填了各种
    非法 tool_tags —— 员工最终可用工具数必须 > 0（这是本批唯一会真坏事的功能故障）。"""
    from bandi.company.org_engine import OrgEngine

    names = _names()
    for idx, junk in enumerate(LLM_PLAUSIBLE_JUNK):
        org = OrgEngine()
        rid = f"role-{idx}"
        org.register_role(rid, "R", tool_tags=list(junk))
        tags = _seat(org)._role_tool_tags(rid)
        kept = filter_tools_by_role_tags(names, _registry(), tags)
        assert kept, f"tool_tags={junk} ⇒ 员工零工具（tags={tags}）"


# ── ⑤ 提示词：值域必须可见 ─────────────────────────────────────────────


def test_secretary_prompt_exposes_tag_enum() -> None:
    """契约⑤：组织构建提示词必须给出 ToolCategory 枚举，且不再含占位符
    （提示词是 LLM 侧的唯一值域来源，缺它就是本缺陷的根因）。"""
    from bandi.company.secretary import SecretaryService

    prompt = SecretaryService._system_prompt()
    assert '"optional", "tool", "tags"' not in prompt, "占位符必须移除（原根因）"
    for value in sorted(VALID_ROLE_TOOL_TAGS):
        assert value in prompt, f"枚举缺少合法值 {value}"
    assert "OMIT" in prompt, "必须给出「不确定就留空」的指引"
