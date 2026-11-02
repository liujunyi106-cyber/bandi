# 功能：models.dev 模型元数据自动同步（启动拉取/本地缓存/TTL/断网兜底）
# 方案 C：启动检查缓存新鲜度 → 不新鲜则拉取 models.dev → 断网回退旧缓存/硬编码。
# **cache_read / cache_write** 缓存单价（USD→CNY 换算）——厂商调缓存价无需改代码。
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.core.config import DEFAULT_CACHE_PATH, MODELS_DEV_URL
from bandi.llm.model_catalog import BUILTIN_MODELS, USD_TO_CNY, ModelSpec

Fetcher = Callable[[], dict[str, Any]]


@dataclass
class ModelsDevSyncResult:  # 功能：一次同步的产物（数据来源 + 被更新模型名表）
    source: str  # network | cache | builtin
    updated: list[str] = field(default_factory=list)


def _extract_reasoning_efforts(entry: dict[str, Any]) -> list[str]:
    # models.dev reasoning_options 里 type=effort 的 values 表（如 low/high/max）
    efforts: list[str] = []
    for opt in entry.get("reasoning_options") or []:
        if isinstance(opt, dict) and opt.get("type") == "effort":
            values = opt.get("values")
            if isinstance(values, list):
                efforts.extend(str(v) for v in values)
    return efforts


def _extract_vision(entry: dict[str, Any]) -> bool:
    # 输入模态含 image 即判定支持视觉
    modalities = entry.get("modalities") or {}
    return "image" in (modalities.get("input") or [])


def flatten_models(providers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    # 展平 models.dev providers 结构 → {model_id: {reasoning, reasoning_efforts, context_window, max_output_tokens, tool_call, vision}}
    flat: dict[str, dict[str, Any]] = {}
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models") or {}
        if not isinstance(models, dict):
            continue
        for model_id, entry in models.items():
            if not isinstance(entry, dict):
                continue
            limit = entry.get("limit") or {}
            flat[model_id] = {
                "reasoning": bool(entry.get("reasoning", False)),
                "reasoning_efforts": _extract_reasoning_efforts(entry),
                "context_window": limit.get("context"),
                "max_output_tokens": limit.get("output"),
                "tool_call": bool(entry.get("tool_call", False)),
                "vision": _extract_vision(entry),
                "cost": entry.get("cost"),
            }
    return flat


def merge_remote_specs(
    target: dict[str, ModelSpec], remote: dict[str, dict[str, Any]]
) -> list[str]:
    # 就地 merge：仅同名模型被更新；远端显式字段才覆盖；不新增模型；pricing/provider 不动。返回更新模型名表
    updated: list[str] = []
    for name, fields in remote.items():
        spec = target.get(name)
        if spec is None:
            continue
        changed = False
        ctx = fields.get("context_window")
        if ctx is not None and int(ctx) != spec.context_window:
            spec.context_window = int(ctx)
            changed = True
        out = fields.get("max_output_tokens")
        if out is not None and int(out) != spec.max_output_tokens:
            spec.max_output_tokens = int(out)
            changed = True
        if "reasoning" in fields and bool(fields["reasoning"]) != spec.supports_reasoning:
            spec.supports_reasoning = bool(fields["reasoning"])
            changed = True
        efforts = fields.get("reasoning_efforts")
        if efforts is not None and list(efforts) != list(spec.reasoning_efforts):
            spec.reasoning_efforts = list(efforts)
            changed = True
        if "tool_call" in fields and bool(fields["tool_call"]) != spec.supports_tool_calling:
            spec.supports_tool_calling = bool(fields["tool_call"])
            changed = True
        if "vision" in fields and bool(fields["vision"]) != spec.supports_vision:
            spec.supports_vision = bool(fields["vision"])
            changed = True
        # ⚠️ 仅在**本地未提供**（=0）时填充——本地 CNY 表仍是权威，input/output 一律不动；
        # models.dev 为 USD/百万 tokens ⇒ USD_TO_CNY 换算。
        cost = fields.get("cost")
        if isinstance(cost, dict):
            pricing = spec.pricing
            read_usd = cost.get("cache_read")
            write_usd = cost.get("cache_write")
            if read_usd is not None and not pricing.cache_read_per_1m:
                pricing.cache_read_per_1m = round(float(read_usd) * USD_TO_CNY, 4)
                changed = True
            if write_usd is not None and not pricing.cache_write_per_1m:
                pricing.cache_write_per_1m = round(float(write_usd) * USD_TO_CNY, 4)
                changed = True
        if changed:
            updated.append(name)
    return updated


#
# 动机：同一模型经不同网关转售，价格可能不同（deepseek 官方 $0.15/$0.6，而 OpenRouter
# 转售 $0.3/$1.2）。**不硬编码任何网关价**——models.dev 的 providers[provider].models
# 本身就带每家 provider 的 cost（USD/百万），随 TTL 自动刷新 ⇒ 天然「实时」。
#
# 兼容性：本模块的 `flatten_models` 按 model_id 跨 provider 合并（后者覆盖前者）
# **丢失了 provider 维度**，故此处另建 provider 维度索引。

# 我们的 provider 名 → models.dev 的 provider key（仅补差异项）
# ⚠️ 只映射**确定同义**的名字。`openai_compat` 是「未知 OpenAI 兼容网关」的兜底名
# 不等于 openrouter ⇒ 不映射（宁可取不到实时价回落目录价，也不能猜错 provider 价）。
_PROVIDER_ALIASES: dict[str, str] = {
    "mimo": "xiaomi",
    "gemini": "google",
}

_INDEX_LOCK = threading.Lock()
# 构建锁：保证「预热线程」与「首次查询」不会各解析一遍 5MB 缓存（同一份工作只做一次；
# 后到者等前者建完即命中 mtime 缓存）
_BUILD_LOCK = threading.Lock()
_INDEX_CACHE: tuple[float, dict[str, dict[str, dict[str, Any]]]] | None = None


def _default_cache_path() -> Path:
    from bandi.core.config import DEFAULT_CACHE_PATH

    return Path(str(DEFAULT_CACHE_PATH)).expanduser()


def provider_price_index(
    cache_path: str | Path | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """功能：`{provider_key: {model_id: {"cost": {...}, "limit": {...}, "name": ...}}}`。

    按缓存文件 mtime 记忆（models.dev 缓存 5MB+，不得每次调用重解析）；任何异常
    一律返回空表——**观测/计价元数据故障绝不影响主链**。
    """
    global _INDEX_CACHE
    path = Path(cache_path) if cache_path is not None else _default_cache_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _INDEX_LOCK:
        if _INDEX_CACHE is not None and _INDEX_CACHE[0] == mtime:
            return _INDEX_CACHE[1]
    with _BUILD_LOCK:
        # 双检：等锁期间可能已被预热线程建好
        with _INDEX_LOCK:
            if _INDEX_CACHE is not None and _INDEX_CACHE[0] == mtime:
                return _INDEX_CACHE[1]
        try:
            payload = load_cache(path) or {}
            providers = _providers_from(payload)
            index: dict[str, dict[str, dict[str, Any]]] = {}
            for key, provider in providers.items():
                if not isinstance(provider, dict):
                    continue
                models = provider.get("models") or {}
                if not isinstance(models, dict):
                    continue
                index[str(key)] = {
                    mid: {
                        "cost": (entry.get("cost") if isinstance(entry, dict) else None),
                        "limit": (entry.get("limit") if isinstance(entry, dict) else None),
                        "name": (entry.get("name") if isinstance(entry, dict) else None),
                    }
                    for mid, entry in models.items()
                }
        except Exception:  # noqa: BLE001 — 元数据不可用即退化为「无实时价」
            return {}
        with _INDEX_LOCK:
            _INDEX_CACHE = (mtime, index)
        return index


def warm_provider_price_index() -> None:
    """功能：**后台预热** (provider, model) 实时价索引（daemon 线程，不阻塞调用方）。

    为什么需要：5MB models.dev 缓存的首次解析约 0.6s。若不预热，这笔开销会落在
    **首个 LLM 响应的记账路径**（`CostTracker.record` → `_resolve_pricing`）上，属用户可
    感知的偶发卡顿；预热后首个响应直接命中 mtime 缓存（~0ms）。
    与主链解耦：预热失败/未完成都不影响任何调用（查不到就回落目录价）。
    """
    threading.Thread(target=provider_price_index, name="models-dev-price-warm", daemon=True).start()


#
# 动机：档案/自定义模型（第三方网关、openrouter 长尾）不在 BUILTIN_MODELS ⇒ 前端思考
# 挡位拿不到能力数据（默认禁用）。models.dev 已收录 225 个 provider 的 reasoning /
# reasoning_options，本函数按模型名（末段归一）实时解析——与目录同步（flatten）同源
# 保证「档案模型解析结果 == 目录同名条目解析结果」的一致性。

_FLAT_LOCK = threading.Lock()
_FLAT_BUILD_LOCK = threading.Lock()
_FLAT_CACHE: tuple[float, dict[str, dict[str, Any]]] | None = None


def _flatten_cached(cache_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """功能：按缓存 mtime 记忆的 flatten_models 展平表；任何异常 → 空表（观测元数据故障不影响主链）。"""
    global _FLAT_CACHE
    path = Path(cache_path) if cache_path is not None else _default_cache_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _FLAT_LOCK:
        if _FLAT_CACHE is not None and _FLAT_CACHE[0] == mtime:
            return _FLAT_CACHE[1]
    with _FLAT_BUILD_LOCK:
        with _FLAT_LOCK:
            if _FLAT_CACHE is not None and _FLAT_CACHE[0] == mtime:
                return _FLAT_CACHE[1]
        try:
            payload = load_cache(path) or {}
            flat = flatten_models(_providers_from(payload))
        except Exception:  # noqa: BLE001 — 元数据不可用即视为无能力数据
            return {}
        with _FLAT_LOCK:
            _FLAT_CACHE = (mtime, flat)
        return flat


def lookup_model_capabilities(
    model: str, cache_path: str | Path | None = None
) -> dict[str, Any] | None:
    """功能：按模型名查 models.dev 能力（思考挡位）。

    命中顺序：模型 id 精确 → 末段匹配（网关常带 `vendor/` 前缀，如 `xiaomi/mimo-v2.6-flash`
    → `mimo-v2.6-flash`）；末段匹配多义即放弃（宁可无挡位，不可错挡位）。
    返回 `{"reasoning": bool, "reasoning_efforts": list[str]}`；无数据 → None。
    """
    if not model:
        return None
    flat = _flatten_cached(cache_path)
    entry = flat.get(model)
    if entry is None:
        tail = model.rsplit("/", 1)[-1]
        candidates = [mid for mid in flat if mid.rsplit("/", 1)[-1] == tail]
        if len(candidates) != 1:
            return None
        entry = flat[candidates[0]]
    return {
        "reasoning": bool(entry.get("reasoning")),
        "reasoning_efforts": [str(v) for v in (entry.get("reasoning_efforts") or [])],
    }


def resolve_provider_key(provider: str) -> str:
    # 功能：我们的 provider 名 → models.dev provider key（别名映射；未映射原样返回）
    key = (provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(key, key)


def lookup_provider_price(
    provider: str, model: str, cache_path: str | Path | None = None
) -> dict[str, Any] | None:
    """功能：查 (provider, model) 的**实时价与窗口**，价一律折算为 CNY/百万。

    命中顺序：模型 id 精确 → 末段匹配（网关常带 `vendor/` 前缀，如 OpenRouter 的
    `xiaomi/mimo-v2.6-flash` → 其自有 id `mimo-v2.6-flash`）。末段匹配**只在同一
    provider 内**进行，且要求唯一——多义时返回 None（宁可无价，不可错价）。
    无 cost 字段即视为无价（返回 None）。
    """
    if not provider or not model:
        return None
    index = provider_price_index(cache_path)
    bucket = index.get(resolve_provider_key(provider))
    if not bucket:
        return None
    entry = bucket.get(model)
    matched_id = model
    if entry is None:
        tail = model.rsplit("/", 1)[-1]
        candidates = [mid for mid in bucket if mid.rsplit("/", 1)[-1] == tail]
        if len(candidates) != 1:
            return None
        matched_id = candidates[0]
        entry = bucket[matched_id]
    cost = entry.get("cost")
    if not isinstance(cost, dict):
        return None
    limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}

    def _cny(value: Any) -> float:
        if value is None:
            return 0.0
        return round(float(value) * USD_TO_CNY, 4)

    return {
        "provider": resolve_provider_key(provider),
        "model_id": matched_id,
        "input_per_1m": _cny(cost.get("input")),
        "output_per_1m": _cny(cost.get("output")),
        "cache_read_per_1m": _cny(cost.get("cache_read")),
        "cache_write_per_1m": _cny(cost.get("cache_write")),
        "currency": "CNY",
        "context_window": limit.get("context"),
        "max_output_tokens": limit.get("output"),
    }


def save_cache(path: str | Path, payload: dict[str, Any]) -> None:
    # 缓存落盘（payload 含 providers 与 fetched_at）
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def load_cache(path: str | Path) -> dict[str, Any] | None:
    # 读缓存；缺失/损坏返回 None（不抛）
    p = Path(path).expanduser()
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_cache_fresh(path: str | Path, ttl_hours: float, now: float | None = None) -> bool:
    # 缓存存在且 mtime 距今 < ttl_hours → 新鲜（ttl<=0 恒不新鲜）
    p = Path(path).expanduser()
    if ttl_hours <= 0 or not p.exists():
        return False
    now_s = time.time() if now is None else now
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return False
    return (now_s - mtime) < ttl_hours * 3600.0


def _providers_from(data: dict[str, Any]) -> dict[str, Any]:
    # 兼容两种 payload：直接 provider map 或 {"providers": ..., "fetched_at": ...}
    inner = data.get("providers")
    if isinstance(inner, dict):
        return inner
    return data


def _apply_and_result(providers: dict[str, Any], source: str) -> ModelsDevSyncResult:
    updated = merge_remote_specs(BUILTIN_MODELS, flatten_models(providers))
    return ModelsDevSyncResult(source=source, updated=updated)


def _default_fetcher(timeout_secs: float, url: str = MODELS_DEV_URL) -> dict[str, Any]:
    # 真实拉取 models.dev/api.json（仅断网/无自定义 fetcher 时使用）
    import httpx

    resp = httpx.get(url, timeout=timeout_secs)
    resp.raise_for_status()
    return resp.json()


def sync_models_dev(
    *,
    cache_path: str | Path | None = None,
    fetcher: Fetcher | None = None,
    ttl_hours: float = 24.0,
    timeout_secs: float = 5.0,
    now: float | None = None,
    url: str | None = None,
) -> ModelsDevSyncResult:
    # 主同步：新鲜缓存→cache；否则拉取→network；失败→旧缓存兜底(cache)/无缓存(builtin)
    path = (
        Path(cache_path).expanduser()
        if cache_path is not None
        else Path(DEFAULT_CACHE_PATH).expanduser()
    )
    if is_cache_fresh(path, ttl_hours, now):
        cached = load_cache(path)
        if cached:
            return _apply_and_result(_providers_from(cached), "cache")
    try:
        data = (
            _default_fetcher(timeout_secs, url or MODELS_DEV_URL) if fetcher is None else fetcher()
        )
    except Exception:  # noqa: BLE001
        stale = load_cache(path)
        if stale:
            return _apply_and_result(_providers_from(stale), "cache")
        return ModelsDevSyncResult(source="builtin", updated=[])
    providers = _providers_from(data)
    save_cache(path, {"providers": providers, "fetched_at": datetime.now(UTC).isoformat()})
    return _apply_and_result(providers, "network")


_sync_lock = threading.Lock()
_sync_done = False
_sync_last: ModelsDevSyncResult | None = None


def ensure_models_dev_sync(
    *,
    cache_path: str | Path | None = None,
    fetcher: Fetcher | None = None,
    ttl_hours: float = 24.0,
    timeout_secs: float = 5.0,
    force: bool = False,
    _synchronize: bool = True,
    url: str | None = None,
) -> ModelsDevSyncResult | None:
    # 进程门：同进程只同步一次（force 重跑）；供 CLI main/serve 启动接线
    global _sync_done, _sync_last
    if _synchronize:
        with _sync_lock:
            if _sync_done and not force:
                return _sync_last
        result = sync_models_dev(
            cache_path=cache_path,
            fetcher=fetcher,
            ttl_hours=ttl_hours,
            timeout_secs=timeout_secs,
            url=url,
        )
        with _sync_lock:
            _sync_done = True
            _sync_last = result
        return result
    return None


__all__ = [
    "DEFAULT_CACHE_PATH",
    "MODELS_DEV_URL",
    "ModelsDevSyncResult",
    "ensure_models_dev_sync",
    "flatten_models",
    "is_cache_fresh",
    "load_cache",
    "merge_remote_specs",
    "save_cache",
    "sync_models_dev",
    "warm_provider_price_index",
]


def trigger_models_dev_sync() -> ModelsDevSyncResult | None:
    """功能：按配置触发 models.dev 元数据同步（CLI 入口与 API server 启动共用；
    enabled 关闭跳过、异常静默不阻断启动）。返回同步结果供调用方展示/记录。"""
    try:
        from bandi.core.config import BandiConfig

        cfg = BandiConfig.load().models_dev
        if not cfg.enabled:
            return None
        # 同步成功后预热实时价索引：把一次性解析开销留在启动期（见 warm_provider_price_index）
        warm_provider_price_index()
        return ensure_models_dev_sync(
            cache_path=cfg.cache_path,
            ttl_hours=cfg.cache_ttl_hours,
            timeout_secs=cfg.timeout_secs,
            url=cfg.url,
            _synchronize=True,
        )
    except Exception:  # noqa: BLE001 — 同步失败静默兜底（BUILTIN_MODELS 不变）
        return None
