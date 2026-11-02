from __future__ import annotations

from bandi.tools.collaboration_rpc import (
    BANDI_COLLAB_RPC_HOST,
    BANDI_COLLAB_RPC_PORT,
    BANDI_COLLAB_RPC_TOKEN,
    BANDI_COLLAB_RPC_TRANSPORT,
    rpc_env_available,
)


class TestRpcEnvAvailable:
    def test_no_env_returns_false(self) -> None:
        assert rpc_env_available({}) is False
        assert rpc_env_available(None) is False

    def test_with_token_and_tcp_host_returns_true(self) -> None:
        env = {
            BANDI_COLLAB_RPC_TOKEN: "abc",
            BANDI_COLLAB_RPC_TRANSPORT: "tcp",
            BANDI_COLLAB_RPC_HOST: "127.0.0.1",
            BANDI_COLLAB_RPC_PORT: "9999",
        }
        assert rpc_env_available(env) is True

    def test_missing_token_returns_false(self) -> None:
        env = {
            BANDI_COLLAB_RPC_TRANSPORT: "tcp",
            BANDI_COLLAB_RPC_HOST: "127.0.0.1",
            BANDI_COLLAB_RPC_PORT: "9999",
        }
        assert rpc_env_available(env) is False
