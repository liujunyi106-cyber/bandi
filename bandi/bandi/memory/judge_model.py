"""功能：记忆提炼与审批的判官——跟随全局活跃模型，不自建独立配置。

设计取向：用户在设置里把主模型换成哪个，草稿提炼就用哪个。这样「助理用
DeepSeek V4.1 在干活，审批草稿却由另一个模型生成」这种割裂不会发生；换模型时
提炼口径也一起换，不需要用户再单独配一次。

解析顺序：
  ① 全局活跃档案（`ProfileStore.active_profile()`）
  ② 用户配置里的默认模型（`BandiConfig.llm`）
  ③ 都没有 → 返回 None，调用方退化为规则提炼（不能因为没配模型就丢掉候选）
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


def resolve_judge_profile() -> dict[str, Any] | None:
    """功能：取当前全局活跃模型作为判官；取不到返回 None。"""
    try:
        from bandi.llm.profiles import ProfileStore

        profile = ProfileStore().active_profile()
        if profile is not None and str(profile.model or "").strip():
            return {
                "profile_id": profile.profile_id,
                "model": profile.model,
                "provider": profile.provider,
                "api_key": profile.api_key,
                "base_url": profile.base_url,
                "extra": dict(profile.extra or {}),
            }
    except Exception as exc:  # noqa: BLE001 - 解析失败只影响提炼质量
        logger.warning("judge profile resolve failed: %s", exc)

    # 回退：配置文件里的默认模型
    try:
        from bandi.core.config import BandiConfig

        config = BandiConfig.load()
        model = str(getattr(config.llm, "model", "") or "").strip()
        if model:
            return {
                "profile_id": "",
                "model": model,
                "provider": str(getattr(config.llm, "provider", "") or ""),
                "api_key": str(getattr(config.llm, "api_key", "") or ""),
                "base_url": str(getattr(config.llm, "base_url", "") or ""),
                "extra": {},
            }
    except Exception as exc:  # noqa: BLE001 - 配置读取失败只影响提炼质量，不许阻断维护
        logger.warning("judge fallback to config model failed: %s", exc)
    return None


def build_judge_complete() -> Any | None:
    """功能：把活跃模型包成同步 `complete(prompt)`；无可用模型返回 None。"""
    profile = resolve_judge_profile()
    if profile is None:
        return None
    complete = _http_complete(profile)
    if complete is None:
        return None
    logger.info(
        "memory judge follows global model: %s (%s)",
        profile.get("model"),
        profile.get("provider") or "openai_compat",
    )
    return complete


def _http_complete(profile: dict[str, Any]) -> Any | None:
    """功能：按 OpenAI 兼容协议直连判官模型（跟随全局模型时不依赖 provider 单例）。"""
    base_url = str(profile.get("base_url") or "").rstrip("/")
    api_key = str(profile.get("api_key") or "")
    model = str(profile.get("model") or "")
    if not base_url or not model:
        return None
    extra = profile.get("extra") or {}

    def _complete(prompt: str, *, max_tokens: int = 700) -> str:
        """功能：发一次非流式补全，返回正文；网络/协议错误直接抛出由上层降级。"""
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": max_tokens,
                "stream": False,
                **extra,
            }
        ).encode()
        headers = {
            "Content-Type": "application/json",
            # 部分网关按 User-Agent 拦截默认库标识，显式声明客户端
            "User-Agent": "bandi-desktop/1.2",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            f"{base_url}/chat/completions", data=payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
        choices = body.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    return _complete
