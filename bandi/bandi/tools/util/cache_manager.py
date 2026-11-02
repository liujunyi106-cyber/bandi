# 功能：cache_manager — SQLite 持久化 KV（TTL/namespace/标签/批量/计数；store 注入）
from __future__ import annotations

from typing import Any

from bandi.tools.base import BaseTool, ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.util.store import UtilStore


class CacheManagerTool(BaseTool):
    """功能：缓存读写与维护（16 个 op）；所有写操作经 store 的 to_thread 落盘。"""

    name = "cache_manager"
    description = "持久化 KV 缓存：get/set/delete/exists/clear/stats/info/keys/mget/mset/mdelete/get_by_tag/delete_by_tag/touch/increment/decrement"
    category = ToolCategory.UTIL
    feature_group = "util"
    is_read_only = False
    is_concurrency_safe = True
    capability_tags: list[str] = [ToolCapability.DATABASE.value]
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "get",
                    "set",
                    "delete",
                    "exists",
                    "clear",
                    "stats",
                    "info",
                    "keys",
                    "mget",
                    "mset",
                    "mdelete",
                    "get_by_tag",
                    "delete_by_tag",
                    "touch",
                    "increment",
                    "decrement",
                ],
                "description": "要执行的操作",
            },
            "key": {"type": "string", "description": "缓存键"},
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "mget/mdelete 键列表",
            },
            "value": {"description": "缓存值"},
            "values": {"type": "object", "description": "mset 批量值"},
            "namespace": {"type": "string", "description": "命名空间（默认 default）"},
            "ttl": {"type": "number", "description": "过期秒数（None=永不过期）"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表"},
            "tag": {"type": "string", "description": "标签查询/删除"},
            "amount": {"type": "number", "description": "increment/decrement 步长"},
            "pattern": {"type": "string", "description": "keys 键名通配子串"},
        },
        "required": ["operation"],
    }

    def __init__(self, store: UtilStore | None = None) -> None:
        self._store = store or UtilStore()

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        op = params.get("operation", "get")
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            raise ValueError(f"Unknown operation: {op}")
        return await handler(params)

    def _ns(self, params: dict[str, Any]) -> str:
        return params.get("namespace", "default")

    async def _op_set(self, params: dict[str, Any]) -> dict[str, Any]:
        key = params.get("key")
        if not key:
            raise ValueError("key is required")
        await self._store.cache_set(
            self._ns(params),
            key,
            params.get("value"),
            params.get("ttl"),
            params.get("tags"),
        )
        return {"ok": True, "key": key}

    async def _op_get(self, params: dict[str, Any]) -> dict[str, Any]:
        key = params.get("key")
        if not key:
            raise ValueError("key is required")
        entry = self._store.cache_get(self._ns(params), key)
        if entry is None:
            return {"found": False, "key": key, "value": None}
        return {
            "found": True,
            "key": key,
            "value": entry["value"],
            "source": "sqlite",
            "tags": entry["tags"],
        }

    async def _op_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        key = params.get("key")
        if not key:
            raise ValueError("key is required")
        deleted = await self._store.cache_delete(self._ns(params), key)
        return {"deleted": deleted, "key": key}

    async def _op_exists(self, params: dict[str, Any]) -> dict[str, Any]:
        key = params.get("key")
        if not key:
            raise ValueError("key is required")
        return {"exists": self._store.cache_exists(self._ns(params), key), "key": key}

    async def _op_clear(self, params: dict[str, Any]) -> dict[str, Any]:
        cleared = await self._store.cache_clear()
        return {"cleared": cleared}

    async def _op_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"stats": self._store.cache_stats()}

    async def _op_info(self, params: dict[str, Any]) -> dict[str, Any]:
        key = params.get("key")
        if not key:
            raise ValueError("key is required")
        info = self._store.cache_info(self._ns(params), key)
        if info is None:
            return {"found": False, "key": key, "ttl_remaining": None}
        return {"found": True, "key": key, **info}

    async def _op_keys(self, params: dict[str, Any]) -> dict[str, Any]:
        keys = self._store.cache_keys(self._ns(params), params.get("pattern", ""))
        return {"keys": keys, "count": len(keys)}

    async def _op_mget(self, params: dict[str, Any]) -> dict[str, Any]:
        keys = params.get("keys", [])
        if not keys:
            raise ValueError("keys is required")
        return self._store.cache_mget(self._ns(params), keys)

    async def _op_mset(self, params: dict[str, Any]) -> dict[str, Any]:
        values = params.get("values") or {}
        if not values:
            raise ValueError("values is required")
        ttl = params.get("ttl")
        tags = params.get("tags")
        for key, value in values.items():
            await self._store.cache_set(self._ns(params), key, value, ttl, tags)
        return {"ok": True, "set": len(values), "keys": list(values)}

    async def _op_mdelete(self, params: dict[str, Any]) -> dict[str, Any]:
        keys = params.get("keys", [])
        if not keys:
            raise ValueError("keys is required")
        deleted_count = 0
        for key in keys:
            deleted_count += int(await self._store.cache_delete(self._ns(params), key))
        return {"deleted_count": deleted_count, "keys": keys}

    async def _op_get_by_tag(self, params: dict[str, Any]) -> dict[str, Any]:
        tag = params.get("tag")
        if not tag:
            raise ValueError("tag is required")
        entries = self._store.cache_get_by_tag(self._ns(params), tag)
        return {"entries": entries, "count": len(entries)}

    async def _op_delete_by_tag(self, params: dict[str, Any]) -> dict[str, Any]:
        tag = params.get("tag")
        if not tag:
            raise ValueError("tag is required")
        deleted_count = await self._store.cache_delete_by_tag(self._ns(params), tag)
        return {"deleted_count": deleted_count, "tag": tag}

    async def _op_touch(self, params: dict[str, Any]) -> dict[str, Any]:
        key = params.get("key")
        ttl = params.get("ttl")
        if not key:
            raise ValueError("key is required")
        if ttl is None:
            raise ValueError("ttl is required")
        success = await self._store.cache_touch(self._ns(params), key, float(ttl))
        return {"success": success, "key": key, "ttl_remaining": float(ttl)}

    async def _op_increment(self, params: dict[str, Any]) -> dict[str, Any]:
        key = params.get("key")
        if not key:
            raise ValueError("key is required")
        amount = params.get("amount", 1)
        value = await self._store.cache_increment(self._ns(params), key, float(amount))
        return {"value": value, "key": key}

    async def _op_decrement(self, params: dict[str, Any]) -> dict[str, Any]:
        key = params.get("key")
        if not key:
            raise ValueError("key is required")
        amount = params.get("amount", 1)
        value = await self._store.cache_increment(self._ns(params), key, -float(amount))
        return {"value": value, "key": key}
