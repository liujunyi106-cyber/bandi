# 功能：远程 HTTP 媒体生成后端（env 门控真实 provider 骨架）
from __future__ import annotations

import os
from typing import Any

from bandi.generation.base import GenerationOutput, mime_for_kind


class RemoteGenerationBackend:
    """通过 env 凭证门控的 HTTP 媒体生成后端骨架。

    使用约定：
      - ``BANDI_<KIND>_GEN_URL`` —— 必填，生成服务地址
      - ``BANDI_<KIND>_GEN_API_KEY`` —— 可选，Bearer 凭证
    ``available()`` 在 URL 与 API_KEY 都齐备时返回 True；缺任一凭证即宣称不可用
    让 :class:`~bandi.generation.service.GenerationService` 透明降级到离线兜底。
    ``transport`` 可注入（测试用 ``httpx.MockTransport``），None 时用真实网络。
    """

    def __init__(
        self,
        kind: str,
        *,
        transport: Any | None = None,
    ) -> None:
        self.kind = kind
        self.name = f"remote_{kind}"
        self.kinds = (kind,)
        self._transport = transport

    @staticmethod
    def _creds(kind: str) -> tuple[str, str]:
        prefix = f"BANDI_{kind.upper()}_GEN_"
        return os.environ.get(f"{prefix}URL", "").strip(), os.environ.get(
            f"{prefix}API_KEY", ""
        ).strip()

    def available(self) -> bool:
        base_url, api_key = self._creds(self.kind)
        return bool(base_url and api_key)

    async def generate(self, *, kind: str, prompt: str, **params: Any) -> GenerationOutput:
        if kind != self.kind:
            return GenerationOutput.failure(kind, f"{self.name} produces only {self.kind!r}")
        import httpx

        base_url, api_key = self._creds(self.kind)
        if not base_url:
            return GenerationOutput.failure(kind, f"{self.name} not configured")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        timeout = float(params.pop("timeout", 120))
        async with httpx.AsyncClient(transport=self._transport, timeout=timeout) as client:
            resp = await client.post(
                base_url,
                headers=headers,
                json={"prompt": prompt, "params": {k: v for k, v in params.items()}},
            )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        return GenerationOutput(
            success=True,
            kind=kind,
            data=resp.content,
            mime=content_type or mime_for_kind(kind),
            filename=f"generated.{kind}",
            provider=self.name,
            model=str(params.get("model") or ""),
        )


__all__ = ["RemoteGenerationBackend"]
