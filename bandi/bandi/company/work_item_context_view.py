# 功能：工作项上下文视图——运行时元数据投影

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypeVar

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.metadata_ownership import is_work_item_owned_key
from bandi.company.work_item import WorkItem

_V = TypeVar("_V")


class WorkItemContextView:
    __slots__ = ("_wi_meta", "_rt_meta", "_has_work_item")

    def __init__(
        self, work_item: WorkItem | None = None, runtime: CompanyRuntime | None = None
    ) -> None:
        wi_meta = getattr(work_item, "metadata", None) if work_item is not None else None
        snapshot_fn = getattr(runtime, "metadata_snapshot", None) if runtime is not None else None
        rt_meta = snapshot_fn() if callable(snapshot_fn) else None
        self._wi_meta: dict[str, Any] = dict(wi_meta) if isinstance(wi_meta, dict) else {}
        self._rt_meta: dict[str, Any] = dict(rt_meta) if isinstance(rt_meta, dict) else {}
        self._has_work_item = work_item is not None

    def get(self, key: str, default: _V | None = None) -> _V | None:
        if key in self._wi_meta:
            return self._wi_meta[key]
        if self._has_work_item and is_work_item_owned_key(key):
            return default
        return self._rt_meta.get(key, default)

    def get_list(self, key: str) -> list[Any]:
        v = self.get(key, None)
        if isinstance(v, (list, tuple)):
            return list(v)
        return []

    def get_dict(self, key: str) -> dict[str, Any]:
        v = self.get(key, None)
        if isinstance(v, dict):
            return dict(v)
        return {}

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.get(key, default)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
        return default

    def has(self, key: str) -> bool:
        return key in self._wi_meta or (
            key in self._rt_meta and (not self._has_work_item or not is_work_item_owned_key(key))
        )

    def as_dict(self) -> dict[str, Any]:
        combined: dict[str, Any] = {}
        if not self._has_work_item:
            combined.update(deepcopy(self._rt_meta))
            return combined
        for key in self._rt_meta:
            if not is_work_item_owned_key(key):
                combined[key] = deepcopy(self._rt_meta[key])
        for key, value in self._wi_meta.items():
            combined[key] = deepcopy(value)
        return combined

    @property
    def metadata_keys(self) -> list[str]:
        keys = set(self._wi_meta.keys())
        if self._has_work_item:
            for k in self._rt_meta:
                if not is_work_item_owned_key(k):
                    keys.add(k)
        else:
            keys.update(self._rt_meta.keys())
        return sorted(keys)

    @property
    def has_work_item(self) -> bool:
        return self._has_work_item

    def __repr__(self) -> str:
        return f"WorkItemContextView(wi_keys={len(self._wi_meta)}, rt_keys={len(self._rt_meta)})"
