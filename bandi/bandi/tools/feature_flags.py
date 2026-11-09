# 功能：FeatureGate 特性门控解析（按 feature_group 条件加载判定）
from __future__ import annotations

from collections.abc import Mapping


class FeatureGate:
    """功能：把 config 的 tool_groups 映射解析为布尔门控——未列出的组默认开放（零配置全开）。"""

    def __init__(self, flags: Mapping[str, bool] | None = None) -> None:
        flags = dict(flags or {})
        for group, value in flags.items():
            if not isinstance(value, bool):
                raise ValueError(
                    f"feature group {group!r} 的值必须为 bool，实际为 {type(value).__name__}"
                )
        self._flags = flags

    def is_enabled(self, group: str | None) -> bool:
        # feature_group=None 的工具不参与门控（无声明 = 永远放行）
        if group is None:
            return True
        return self._flags.get(group, True)

    @property
    def flags(self) -> dict[str, bool]:
        return dict(self._flags)
