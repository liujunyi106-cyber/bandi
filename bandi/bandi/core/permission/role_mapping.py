#
# 背景：本仓有两套并行的「角色」概念，语义不同因而**不合并**：
# * `core/permission/roles.py` 的 4 个**内置权限档**（admin / power_user / member /
# readonly）——回答「这个档能不能对这类资源做 read/write/delete/execute」
# 经 `PolicyEngine` 在**工具执行面**生效（`tools/executor.py` 的 policy 检查）；
# * 组织**岗位**（`company/org_engine.py` 的 `OrgRole`）——回答「这个人担任哪个岗位」
#
#
# 这正是方案 A 相对方案 C（合并两套体系）的核心优势：纯增量、可回退。

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from functools import lru_cache

from bandi.core.exceptions import BandiError

logger = logging.getLogger(__name__)

# 内置权限档名——必须与 `core/permission/roles.py::_BUILTIN` 的键一致（全小写）。
BUILTIN_ROLE_NAMES = frozenset({"admin", "power_user", "member", "readonly"})

# 权限档强弱序（数值越大权限越高）。多岗位合并时取**最高档**——
# 绑定语义是「授权」（`AccountStore.bind_role`），取最低档会让兼任角色互相抵消。
_ROLE_RANK: dict[str, int] = {
    "readonly": 0,
    "member": 1,
    "power_user": 2,
    "admin": 3,
}


def resolve_role(role: str, role_map: Mapping[str, str] | None = None) -> str:
    """把组织角色解析成内置权限档名。

    权限档」，与既有契约（`register_builtin_role` 对未知角色不注册任何策略）一致——
    既不会意外放行，也不会意外拦截。

    Args:
        role: 组织角色 id（如 `"engineer"`），或已是权限档名（如 `"member"`）。

    Returns:
        权限档名或原 `role`。
    """
    if not role_map:
        return role
    target = role_map.get(role)
    if target is None:
        return role
    normalized = str(target).strip().lower()
    if normalized not in BUILTIN_ROLE_NAMES:
        # 非法目标**不静默改名**：忽略该条并告警，保持原样（fail-safe）
        logger.warning(
            "role_map[%r] = %r 不是内置权限档（可选 %s）——该条映射被忽略",
            role,
            target,
            sorted(BUILTIN_ROLE_NAMES),
        )
        return role
    return normalized


def validate_role_map(role_map: Mapping[str, str] | None) -> list[str]:
    """

    供装配期/CLI 自检调用——把配置错误在启动时暴露，而不是等到某次工具调用才静默失效。
    """
    if role_map is None:
        return []
    if not isinstance(role_map, Mapping):
        return [f"role_map 配置类型错误：期望映射（dict），实际 {type(role_map).__name__}"]
    problems: list[str] = []
    for key, value in role_map.items():
        if not str(key).strip():
            problems.append("存在空的角色键")
            continue
        if str(value).strip().lower() not in BUILTIN_ROLE_NAMES:
            problems.append(
                f"{key!r} -> {value!r}：目标不是内置权限档 {sorted(BUILTIN_ROLE_NAMES)}"
            )
    return problems


# 每轮执行都会调用，无去重会按轮刷屏）
_checked_role_maps: set[str] = set()
_checked_role_maps_lock = threading.Lock()


def check_role_map(role_map: Mapping[str, str] | None) -> list[str]:
    """装配期自检：validate 逐条告警 + describe 入日志（进程内按指纹去重）。"""
    fingerprint = (
        repr(sorted((role_map or {}).items())) if isinstance(role_map, Mapping) else repr(role_map)
    )
    with _checked_role_maps_lock:
        if fingerprint in _checked_role_maps:
            return []
        _checked_role_maps.add(fingerprint)
    problems = validate_role_map(role_map)
    for problem in problems:
        logger.warning("角色映射配置问题：%s", problem)
    if isinstance(role_map, Mapping) and role_map:
        logger.info("%s", describe_role_map(role_map))
    return problems


def describe_role_map(role_map: Mapping[str, str] | None) -> str:
    """"""
    if not role_map:
        return "角色映射：未启用（所有角色保持无权限档）"
    pairs = ", ".join(f"{k}→{v}" for k, v in sorted(role_map.items()))
    return f"角色映射：{pairs}"


@lru_cache(maxsize=1)
def strict_role_binding_enabled() -> bool:
    """读取 `permissions.strict_role_binding` 开关（默认 False=保留回落行为）。

    进程内缓存（配置为启动期加载语义）；配置读取失败 fail-open 回落 False 并告警——
    权限加固不得因配置文件异常让主链路不可用。测试改配置后需 `cache_clear()`。
    """
    try:
        from bandi.core.config import BandiConfig

        return bool(BandiConfig.load().permissions.strict_role_binding)
    except Exception as e:  # noqa: BLE001 — 配置不可读不得阻断执行
        logger.warning("读取 permissions.strict_role_binding 失败，按默认 False 处理：%s", e)
        return False


def effective_role_for(
    agent_role: str = "user",
    *,
    user_id: str = "",
    role_map: Mapping[str, str] | None = None,
    account_store: object | None = None,
    strict: bool | None = None,
) -> str:
    """解析一次执行真正应使用的内置权限档。

    解析链（前者优先）：
      1. `user_id` 在账户体系里的**组织岗位绑定**（`AccountStore.list_user_roles`）
      3. 都没有 → `agent_role` 原样返回（= 无权限档，行为与改造前一致）。

    **零副作用保证**：`user_id` 为空时**完全不触碰** `account_store`（不建库、不查询）
    因此默认路径与改造前逐字节一致。

    绑定读取失败的处置（安全加固）：
      * 恒发 `logger.warning`（含 user_id 与异常）；
      * `strict=False`（或未启用）→ 告警后回落 `agent_role`（现状行为）；
      * `strict=True` → 抛 `BandiError` fail-closed（防 DB 故障静默把受限用户
        按高权限 `agent_role` 放行）；`strict=None` 时读 config
        `permissions.strict_role_binding`（默认 False）。

    Args:
        agent_role: 显式传入的角色（默认 `"user"`，即无权限档）。
        user_id: 当前用户标识。空串 = 不启用绑定解析。
        role_map: `{组织岗位: 权限档}`。
        account_store: 提供 `list_user_roles(user_id) -> list[str]` 的对象；缺省则不解析绑定。
        strict: 覆盖配置的严格模式开关（None = 读配置；True/False 显式权威）。
    """
    if user_id and account_store is not None:
        lister = getattr(account_store, "list_user_roles", None)
        if callable(lister):
            try:
                role_ids = lister(user_id) or []
            except Exception as exc:  # 绑定读取失败按策略处置（观测/权限是加固项）
                logger.warning("读取用户 %r 的岗位绑定失败：%s", user_id, exc)
                if strict is None:
                    strict = strict_role_binding_enabled()
                if strict:
                    raise BandiError(
                        f"读取用户 {user_id!r} 的岗位绑定失败，严格模式拒绝回落"
                        f"（fail-closed）：{exc}"
                    ) from exc
                role_ids = []
            tiers = [
                tier
                for tier in (resolve_role(str(r), role_map) for r in role_ids)
                if tier in _ROLE_RANK
            ]
            if tiers:
                return max(tiers, key=lambda t: _ROLE_RANK[t])
    return resolve_role(agent_role, role_map)
