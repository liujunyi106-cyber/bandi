# 功能：Token 计数（TokenCounter 多编码器估算 + 校准系数 + 误差观测 + 分项计量）
from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import Any

_ROLE_KEYS = {"system", "user", "assistant", "tool"}

_AUTO_CALIB_MIN_OBSERVATIONS = 8
_CALIB_RATIO_RANGE = (0.3, 3.0)
_CALIB_UPDATE_MIN_DELTA = 0.02


def format_omission_marker(omitted_chars: int) -> str:
    """功能：省略规模标注统一格式（截断产物必须可辨识省略多少，不得静默硬切）。"""
    return f"...[已省略 {omitted_chars} 字符]..."


def set_active_model_for_provider(provider: Any) -> None:
    """功能：按 provider 装配 active model（装配点接线）——空名/None 防御为空
    校准不误命中其他模型（装配点唯一调用方）。"""
    TokenCounter.set_active_model(getattr(provider, "model_name", "") or "")


class TokenCounter:
    _encoder = None
    _lock = threading.Lock()
    _calibrations: dict[str, float] = {}
    # 误差观测窗口（estimated vs provider usage 实际值——校准系数的数据源）
    _observations: list[dict[str, Any]] = []
    # 类级最近设置值（跨任务/线程读取兜底；任务内以 ContextVar 为准——并发子代理
    # 各自 set_active_model 不再互相覆盖校准键）
    _active_model: str = ""
    _active_model_var: ContextVar[str] = ContextVar("bandi_active_model", default="")
    # tiktoken 编码消除）；超限整表清空（缓存无幂等语义）
    _encode_cache: dict[str, int] = {}
    _ENCODE_CACHE_MAX = 4096

    @classmethod
    def _get_encoder(cls):
        if cls._encoder is None:
            try:
                import tiktoken

                cls._encoder = tiktoken.get_encoding("cl100k_base")
            except (ImportError, OSError):
                cls._encoder = False
        return cls._encoder

    @classmethod
    def set_active_model(cls, model: str) -> None:
        """功能：设置当前生效模型名——任务上下文隔离（并发子代理互不覆盖）
        同时写类级值供非设置上下文/跨线程读取兜底。"""
        cls._active_model_var.set(model or "")
        cls._active_model = model or ""

    @classmethod
    def active_model(cls) -> str:
        """功能：当前生效模型名——优先任务上下文，缺省回落类级最近设置值。"""
        return cls._active_model_var.get() or cls._active_model

    @classmethod
    def set_calibration(cls, model: str, ratio: float) -> None:
        """功能：按模型设置校准系数（count = raw * ratio）。"""
        with cls._lock:
            cls._calibrations[model] = ratio

    @classmethod
    def record_observation(cls, *, model: str, estimated: int, actual: int) -> None:
        """功能：记录一次误差观测（估算值 vs provider usage 实际值）。"""
        if estimated <= 0 or actual <= 0:
            return
        with cls._lock:
            cls._observations.append({"model": model, "estimated": estimated, "actual": actual})
            del cls._observations[:-500]
        cls._maybe_auto_calibrate(model)

    @classmethod
    def _maybe_auto_calibrate(cls, model: str) -> None:
        """功能：观测窗口达阈值后自动应用建议校准（护栏限幅）——数据源到消费方
        闭环内联，无需外部消费者；极端比率（护栏外）不写入防计量失真。"""
        with cls._lock:
            pairs = [
                o for o in cls._observations[-100:] if o["model"] == model and o["estimated"] > 0
            ]
        if len(pairs) < _AUTO_CALIB_MIN_OBSERVATIONS:
            return
        suggested = sum(o["actual"] / o["estimated"] for o in pairs) / len(pairs)
        low, high = _CALIB_RATIO_RANGE
        if not (low <= suggested <= high):
            return
        with cls._lock:
            current = cls._calibrations.get(model, 1.0)
        if abs(suggested - current) > _CALIB_UPDATE_MIN_DELTA:
            cls.set_calibration(model, suggested)

    @classmethod
    def suggested_calibration(cls, model: str) -> float | None:
        """功能：由观测窗口计算建议校准系数（actual/estimated 均值），无数据返回 None。"""
        with cls._lock:
            pairs = [
                o for o in cls._observations[-100:] if o["model"] == model and o["estimated"] > 0
            ]
        if not pairs:
            return None
        return sum(o["actual"] / o["estimated"] for o in pairs) / len(pairs)

    @classmethod
    def _ratio(cls) -> float:
        model = cls.active_model()
        if not model:
            return 1.0
        with cls._lock:
            return cls._calibrations.get(model, 1.0)

    @classmethod
    def count(cls, text: str) -> int:
        enc = cls._get_encoder()
        if enc:
            raw = cls._encode_cache.get(text)
            if raw is None:
                raw = len(enc.encode(text))
                if len(cls._encode_cache) >= cls._ENCODE_CACHE_MAX:
                    cls._encode_cache.clear()
                cls._encode_cache[text] = raw
        else:
            raw = len(text) // 4
        return round(raw * cls._ratio())

    @classmethod
    def estimate(cls, text: str) -> int:
        # tiktoken 驱动，预算/成本判定与精确计量同源
        return cls.count(text)

    @classmethod
    def batch_count(cls, texts: list[str]) -> list[int]:
        enc = cls._get_encoder()
        if enc:
            return [len(enc.encode(t)) for t in texts]
        return [len(t) // 4 for t in texts]

    @classmethod
    def _longest_prefix_within(cls, text: str, max_tokens: int) -> str:
        """功能：二分求预算内最长前缀（预算<=0 空串；充足者原样）。"""
        if max_tokens <= 0:
            return ""
        if cls.count(text) <= max_tokens:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if cls.count(text[:mid]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo]

    @classmethod
    def _longest_suffix_within(cls, text: str, max_tokens: int, max_len: int) -> str:
        """功能：二分求预算内最长后缀（max_len 额外上限，防与头部重叠）。"""
        if max_tokens <= 0 or max_len <= 0:
            return ""
        lo, hi = 0, min(len(text), max_len)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if cls.count(text[len(text) - mid :]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        return text[len(text) - lo :] if lo else ""

    @classmethod
    def truncate_to_token_budget(cls, text: str, max_tokens: int) -> str:
        """功能：按 token 预算二分截断最长前缀（超长中文防打穿预算；预算充足
        原样返回；预算<=0 空串）。保前缀语义——通用输出截断用。"""
        return cls._longest_prefix_within(text, max_tokens)

    @classmethod
    def truncate_head_tail_to_token_budget(
        cls, text: str, max_tokens: int, *, head_ratio: float = 0.6
    ) -> str:
        """功能：按 token 预算头尾保留截断（头 head_ratio + 尾其余）——与
        executor._clip_head_tail 同口径的 token 语义版；省略标注带省略规模
        计入预算，产物严格 ≤ max_tokens。压缩源截断专用：只保前缀会丢最新对话。"""
        if max_tokens <= 0:
            return ""
        if cls.count(text) <= max_tokens:
            return text
        reserve = f"\n{format_omission_marker(len(text))}\n"
        body_tokens = max(0, max_tokens - cls.count(reserve))
        if body_tokens <= 0:
            return ""
        head_budget = int(body_tokens * head_ratio)
        head = cls._longest_prefix_within(text, head_budget)
        tail = cls._longest_suffix_within(
            text, body_tokens - head_budget, max_len=len(text) - len(head)
        )
        marker = f"\n{format_omission_marker(len(text) - len(head) - len(tail))}\n"
        result = f"{head}{marker}{tail}"
        # 数字 token 边界致微超时收缩字符并同步标注（保证严格 ≤ 预算）
        while cls.count(result) > max_tokens and (tail or head):
            if tail:
                tail = tail[1:]
            else:
                head = head[:-1]
            marker = f"\n{format_omission_marker(len(text) - len(head) - len(tail))}\n"
            result = f"{head}{marker}{tail}"
        return result

    @classmethod
    def breakdown(cls, messages: list) -> dict[str, int]:
        """功能：按 role 分项计量（system/user/assistant/tool），tool_calls 参数
        计入 assistant 侧——分项之和与逐条 count 总量一致。生产 provider_messages
        为 list[dict]，Message 对象与 dict 两种形态均直接可消费。"""
        result = {k: 0 for k in _ROLE_KEYS}
        for m in messages:
            if isinstance(m, dict):
                role = str(m.get("role", "") or "")
                content = m.get("content") or ""
                tool_calls = m.get("tool_calls") or []
                args = [
                    str(tc.get("arguments", "") or "") for tc in tool_calls if isinstance(tc, dict)
                ]
            else:
                role = getattr(getattr(m, "role", None), "value", str(getattr(m, "role", "")))
                content = m.content or ""
                tool_calls = getattr(m, "tool_calls", None)
                args = [str(getattr(tc, "arguments", "") or "") for tc in tool_calls or []]
            key = role if role in _ROLE_KEYS else "assistant"
            result[key] += cls.count(content)
            for a in args:
                result[key] += cls.count(a)
        return result
