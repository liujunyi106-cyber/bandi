# 功能：能力路由（按能力画像选择 Provider）
"""``CapabilityRouter`` — 能力感知的候选选择。

给定 :class:`CapabilityContract`（任务 + 必需模态），路由器按运行时适配度排序匹配档案：

1. 显式 *钉选 Provider* 优先；
2. 更低 ``cost_tier`` 优先（本地/免费先于付费）；
3. 注册顺序作稳定决胜；
4. 始终可用的 *offline* 兜底垫底（存在时）。

被 chat fallback 与能力驱动 UI 共用。
"""

from __future__ import annotations

import logging

from bandi.llm.capabilities.profile import BackendClass, CapabilityContract, CapabilityProfile
from bandi.llm.capabilities.registry import CapabilityRegistry

logger = logging.getLogger(__name__)


class CapabilityRouter:
    """把注册表档案按契约排序。"""

    def __init__(self, registry: CapabilityRegistry, *, stats: object | None = None) -> None:
        self.registry = registry
        if stats is None:
            try:
                from bandi.llm.capabilities.provider_stats import get_provider_stats

                stats = get_provider_stats()
            except Exception as e:  # noqa: BLE001 - stats 尽力而为
                logger.debug("provider stats unavailable: %s", e)
                stats = None
        self.stats = stats

    def candidates(
        self,
        contract: CapabilityContract,
        *,
        preferred_provider: str | None = None,
        available_only: bool = True,
        offline_floor: bool = True,
    ) -> list[CapabilityProfile]:
        """返回满足 *contract* 的排序后档案。

        设置了 ``preferred_provider`` 时只返回该 Provider 的匹配档案（不强制可用性
        — 显式钉选应被遵守），随后接 offline 兜底。
        """
        matched = self.registry.query(contract=contract)
        offline = [p for p in matched if p.backend_class == BackendClass.OFFLINE]
        real = [p for p in matched if p.backend_class != BackendClass.OFFLINE]

        if preferred_provider:
            picked = [p for p in real if p.provider == preferred_provider]
            ranked = self._rank(picked, contract)
        else:
            if available_only:
                real = [p for p in real if p.available()]
            ranked = self._rank(real, contract)

        if offline_floor and offline:
            for p in offline:
                if p not in ranked:
                    ranked.append(p)
        return ranked

    def select(
        self,
        contract: CapabilityContract,
        *,
        preferred_provider: str | None = None,
        available_only: bool = True,
    ) -> CapabilityProfile | None:
        """返回单个最佳候选（或 None）。"""
        cands = self.candidates(
            contract,
            preferred_provider=preferred_provider,
            available_only=available_only,
        )
        return cands[0] if cands else None

    def provider_options(
        self,
        contract: CapabilityContract,
        *,
        include_offline: bool = True,
    ) -> list[str]:
        """可满足 *contract* 的（去重）Provider 名。

        故意忽略可用性以便列出每个可绑定的 Provider；``auto`` 由调用方负责前缀。
        """
        names: list[str] = []
        for p in self.registry.query(contract=contract):
            if not include_offline and p.backend_class == BackendClass.OFFLINE:
                continue
            if p.provider not in names:
                names.append(p.provider)
        if include_offline and "offline" in names:
            names = [n for n in names if n != "offline"] + ["offline"]
        return names

    def _rank(
        self,
        profiles: list[CapabilityProfile],
        contract: CapabilityContract | None = None,
    ) -> list[CapabilityProfile]:
        # 稳定排序：cost_tier 优先（便宜/本地先于付费），再按观测可靠度
        # （生产自优化），最后注册顺序。未观测 Provider 得中性分以保持排序稳定。
        task = contract.task.value if contract is not None else ""
        indexed = list(enumerate(profiles))

        def _reliability(profile: CapabilityProfile) -> float:
            if self.stats is None or not task:
                return 0.5
            try:
                return float(self.stats.reliability(task, profile.provider))
            except Exception as e:  # noqa: BLE001
                logger.debug("reliability stats failed: %s", e)
                return 0.5

        indexed.sort(
            key=lambda pair: (
                pair[1].cost_tier,
                -round(_reliability(pair[1]), 3),
                pair[0],
            )
        )
        return [p for _, p in indexed]


__all__ = ["CapabilityRouter"]
