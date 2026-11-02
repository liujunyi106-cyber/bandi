from __future__ import annotations

import os
from collections.abc import Mapping

BANDI_COLLAB_RPC_HOST = "BANDI_COLLAB_RPC_HOST"
BANDI_COLLAB_RPC_PORT = "BANDI_COLLAB_RPC_PORT"
BANDI_COLLAB_RPC_TOKEN = "BANDI_COLLAB_RPC_TOKEN"
BANDI_COLLAB_RPC_TRANSPORT = "BANDI_COLLAB_RPC_TRANSPORT"


def rpc_env_available(env: Mapping[str, str] | None = None) -> bool:
    """检测 RPC 是否已配置（有 token + 完整 TCP 参数）。"""
    source = env if env is not None else os.environ
    token = str(source.get(BANDI_COLLAB_RPC_TOKEN, "")).strip()
    if not token:
        return False
    transport = str(source.get(BANDI_COLLAB_RPC_TRANSPORT, "")).strip().lower()
    if not transport:
        return False
    if transport == "tcp":
        host = str(source.get(BANDI_COLLAB_RPC_HOST, "")).strip()
        raw_port = str(source.get(BANDI_COLLAB_RPC_PORT, "")).strip()
        try:
            port = int(raw_port)
        except ValueError:
            return False
        return bool(host) and 0 < port <= 65535
    return False
