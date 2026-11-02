from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bandi.tools.web_fetch import (
    WebFetchTool,
    _assert_request_public,
    _build_guarded_client,
    _PinningBackend,
    _resolve_public_ips,
    _SSRFCheckTransport,
)


class _StreamCtx:
    """批 3：web_fetch 改流式读取——测试 fake 的 stream 上下文。"""

    def __init__(self, resp: MagicMock) -> None:
        self._resp = resp

    async def __aenter__(self) -> MagicMock:
        return self._resp

    async def __aexit__(self, *args: object) -> bool:
        return False


async def _aiter(chunks: list[bytes]):  # type: ignore[no-untyped-def]
    for chunk in chunks:
        yield chunk


def _install_stream(mc: MagicMock, resp: MagicMock) -> None:
    """把 fake client 的响应装成流式形态（stream + aiter_bytes）。"""
    mc.stream = MagicMock(return_value=_StreamCtx(resp))
    body = str(getattr(resp, "text", "") or "")
    resp.aiter_bytes = lambda: _aiter([body.encode("utf-8")])


@pytest.fixture
def tool() -> WebFetchTool:
    return WebFetchTool()


class TestWebFetchTool:
    @pytest.mark.asyncio
    async def test_risk_level(self, tool: WebFetchTool) -> None:
        assert tool.risk_level == "low"
        assert tool.is_concurrency_safe is True

    @pytest.mark.asyncio
    async def test_invalid_url_rejected(self, tool: WebFetchTool) -> None:
        with pytest.raises((Exception, ValueError)):
            await tool.execute({"url": ""})

    @pytest.mark.asyncio
    @patch("bandi.tools.web_fetch.httpx.AsyncClient")
    async def test_binary_content_rejected(
        self, mock_client_cls: AsyncMock, tool: WebFetchTool
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "application/octet-stream"}
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mc = MagicMock()
        _install_stream(mc, mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mc
        result = await tool.execute({"url": "https://example.com/file.bin"})
        assert "unsupported content type" in result

    @pytest.mark.asyncio
    @patch("bandi.tools.web_fetch.httpx.AsyncClient")
    async def test_html_content(self, mock_client_cls: AsyncMock, tool: WebFetchTool) -> None:
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.status_code = 200
        mock_resp.text = "<html><body><h1>Hello</h1><p>World</p></body></html>"
        mock_resp.url = "https://example.com/"
        mock_resp.raise_for_status = MagicMock()
        mc = MagicMock()
        _install_stream(mc, mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mc
        result = await tool.execute({"url": "https://example.com/"})
        assert "Hello" in result
        assert "World" in result

    @pytest.mark.asyncio
    @patch("bandi.tools.web_fetch.httpx.AsyncClient")
    async def test_http_404(self, mock_client_cls: AsyncMock, tool: WebFetchTool) -> None:
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404 Not Found", request=MagicMock(), response=mock_resp
        )
        mc = MagicMock()
        _install_stream(mc, mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mc
        with pytest.raises(httpx.HTTPStatusError):
            await tool.execute({"url": "https://example.com/404"})

    @pytest.mark.asyncio
    @patch("bandi.tools.web_fetch.httpx.AsyncClient")
    async def test_timeout_capped_at_60(
        self, mock_client_cls: AsyncMock, tool: WebFetchTool
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.text = "<html>ok</html>"
        mock_resp.url = "https://example.com/"
        mock_resp.raise_for_status = MagicMock()
        mc = MagicMock()
        _install_stream(mc, mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mc
        await tool.execute({"url": "https://example.com/", "timeout": 999})
        _, kwargs = mock_client_cls.call_args
        assert kwargs["timeout"] == 60

    @pytest.mark.asyncio
    @patch("bandi.tools.web_fetch.httpx.AsyncClient")
    async def test_timeout_default_30(self, mock_client_cls: AsyncMock, tool: WebFetchTool) -> None:
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.text = "<html>ok</html>"
        mock_resp.url = "https://example.com/"
        mock_resp.raise_for_status = MagicMock()
        mc = MagicMock()
        _install_stream(mc, mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mc
        await tool.execute({"url": "https://example.com/"})
        _, kwargs = mock_client_cls.call_args
        assert kwargs["timeout"] == 30

    @pytest.mark.asyncio
    @patch("bandi.tools.web_fetch.httpx.AsyncClient")
    async def test_private_ip_blocked_by_default(
        self, mock_client_cls: AsyncMock, tool: WebFetchTool
    ) -> None:
        with pytest.raises(ValueError, match="private|localhost|internal"):
            await tool.execute({"url": "http://127.0.0.1/admin"})
        mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    @patch("bandi.tools.web_fetch.httpx.AsyncClient")
    async def test_private_hostname_blocked_by_default(
        self, mock_client_cls: AsyncMock, tool: WebFetchTool
    ) -> None:
        with pytest.raises(ValueError):
            await tool.execute({"url": "http://localhost/admin"})
        mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    @patch("bandi.tools.web_fetch.httpx.AsyncClient")
    async def test_allow_private_networks_enables_local(self, mock_client_cls: AsyncMock) -> None:
        tool = WebFetchTool(allow_private_networks=True)
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.text = "<html>ok</html>"
        mock_resp.url = "http://127.0.0.1/"
        mock_resp.raise_for_status = MagicMock()
        mc = MagicMock()
        _install_stream(mc, mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mc
        result = await tool.execute({"url": "http://127.0.0.1/"})
        assert "ok" in result

    @pytest.mark.asyncio
    @patch("bandi.tools.web_fetch.httpx.AsyncClient")
    async def test_public_url_allowed(self, mock_client_cls: AsyncMock, tool: WebFetchTool) -> None:
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.text = "<html>ok</html>"
        mock_resp.url = "https://example.com/"
        mock_resp.raise_for_status = MagicMock()
        mc = MagicMock()
        _install_stream(mc, mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mc
        out = await tool.execute({"url": "https://example.com/"})
        assert "ok" in out


class TestSSRFRedirect:
    """重定向绕过：follow_redirects 自动跟随 302 时，跳转目标必须同样经过 SSRF 校验。"""

    @staticmethod
    def _patch_public_dns(host: str = "public.example.com", ip: str = "93.184.216.34"):
        import bandi.tools.web_fetch as wf

        real_getaddrinfo = wf.socket.getaddrinfo

        def fake_getaddrinfo(h: str, *args: object, **kwargs: object):
            if h == host:
                return [(2, 1, 6, "", (ip, 0))]
            return real_getaddrinfo(h, *args, **kwargs)

        return patch("bandi.tools.web_fetch.socket.getaddrinfo", side_effect=fake_getaddrinfo)

    @pytest.mark.asyncio
    async def test_redirect_to_metadata_ip_blocked(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
            return httpx.Response(200, text="ok")

        with self._patch_public_dns():
            async with _build_guarded_client(
                30, False, transport=httpx.MockTransport(handler)
            ) as client:
                with pytest.raises(ValueError, match="private"):
                    await client.get("https://public.example.com/")

    @pytest.mark.asyncio
    async def test_redirect_to_localhost_blocked(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/admin"})
            return httpx.Response(200, text="ok")

        with self._patch_public_dns():
            async with _build_guarded_client(
                30, False, transport=httpx.MockTransport(handler)
            ) as client:
                with pytest.raises(ValueError, match="private"):
                    await client.get("https://public.example.com/")

    @pytest.mark.asyncio
    async def test_redirect_chain_public_allowed(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "a.example.com":
                return httpx.Response(302, headers={"Location": "https://b.example.com/"})
            return httpx.Response(200, text="final")

        with (
            self._patch_public_dns("a.example.com", "93.184.216.34"),
            self._patch_public_dns("b.example.com", "93.184.216.35"),
        ):
            async with _build_guarded_client(
                30, False, transport=httpx.MockTransport(handler)
            ) as client:
                resp = await client.get("https://a.example.com/")
                assert resp.text == "final"


class TestSSRFDnsRebinding:
    """DNS rebinding：校验时解析公网 IP，连接时解析私网 IP——连接层必须再次校验。"""

    @pytest.mark.asyncio
    async def test_pinned_transport_blocks_private_url(self) -> None:
        transport = _SSRFCheckTransport(allow_private=False)
        request = httpx.Request("GET", "http://169.254.169.254/latest/", content=b"")
        with pytest.raises(ValueError, match="private"):
            await transport.handle_async_request(request)

    @pytest.mark.asyncio
    async def test_pinning_backend_rejects_private_second_resolution(self) -> None:
        backend = _PinningBackend()
        with patch(
            "bandi.tools.web_fetch.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("10.0.0.5", 0))],
        ):
            with pytest.raises(ValueError, match="private"):
                await backend.connect_tcp("evil.example.com", 80)

    @pytest.mark.asyncio
    async def test_pinning_backend_connects_via_public_ip(self) -> None:
        # connect_tcp 解析公网 IP → 用 IP 直连（验证 rebinding 窗口关闭）
        backend = _PinningBackend()
        connected: list[str] = []

        async def fake_connect_tcp(host: str, *args: object, **kwargs: object):
            connected.append(host)
            raise OSError("connection closed (test)")

        with (
            patch(
                "bandi.tools.web_fetch.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
            ),
            patch.object(backend, "_inner") as mock_inner,
        ):
            mock_inner.connect_tcp = AsyncMock(side_effect=fake_connect_tcp)
            with pytest.raises(OSError):
                await backend.connect_tcp("example.com", 80)
        assert connected == ["93.184.216.34"]

    @pytest.mark.asyncio
    async def test_allow_private_skips_checks(self) -> None:
        sent: list[httpx.Request] = []

        async def recording_handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, text="ok")

        transport = _SSRFCheckTransport(
            allow_private=True, transport=httpx.MockTransport(recording_handler)
        )
        resp = await transport.handle_async_request(
            httpx.Request("GET", "http://127.0.0.1/", content=b"")
        )
        assert resp.status_code == 200
        assert sent and sent[0].url.host == "127.0.0.1"


class TestSSRFRequestHook:
    """event_hooks 每跳校验钩子：直接校验 request.url。"""

    @pytest.mark.asyncio
    async def test_hook_blocks_private_ip(self) -> None:
        request = httpx.Request("GET", "http://192.168.1.1/", content=b"")
        with pytest.raises(ValueError, match="private"):
            await _assert_request_public(request)

    @pytest.mark.asyncio
    async def test_hook_allows_public_ip(self) -> None:
        request = httpx.Request("GET", "http://93.184.216.34/", content=b"")
        await _assert_request_public(request)


class TestResolvePublicIps:
    def test_resolves_all_ips(self) -> None:
        with patch(
            "bandi.tools.web_fetch.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("93.184.216.34", 0)),
                (2, 2, 17, "", ("93.184.216.35", 0)),
            ],
        ):
            ips = _resolve_public_ips("example.com")
        assert ips == ["93.184.216.34", "93.184.216.35"]

    def test_rejects_mixed_private_ip(self) -> None:
        with patch(
            "bandi.tools.web_fetch.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("93.184.216.34", 0)),
                (2, 1, 6, "", ("10.0.0.5", 0)),
            ],
        ):
            with pytest.raises(ValueError, match="private"):
                _resolve_public_ips("example.com")

    def test_unresolvable_host_rejected(self) -> None:
        with patch(
            "bandi.tools.web_fetch.socket.getaddrinfo",
            side_effect=OSError("no such host"),
        ):
            with pytest.raises(ValueError, match="unable to resolve"):
                _resolve_public_ips("no-such-host.invalid")

    def test_rejects_ipv4_mapped_ipv6_loopback(self) -> None:
        """::ffff:127.0.0.1（IPv4-mapped）必须视为私网拦截，不得绕过 SSRF 防护。"""
        with patch(
            "bandi.tools.web_fetch.socket.getaddrinfo",
            return_value=[(10, 1, 6, "", ("::ffff:127.0.0.1", 0))],
        ):
            with pytest.raises(ValueError, match="private"):
                _resolve_public_ips("mapped-loopback.invalid")

    def test_rejects_ipv4_mapped_ipv6_private(self) -> None:
        """::ffff:10.0.0.5（IPv4-mapped 私网）必须拦截。"""
        with patch(
            "bandi.tools.web_fetch.socket.getaddrinfo",
            return_value=[(10, 1, 6, "", ("::ffff:10.0.0.5", 0))],
        ):
            with pytest.raises(ValueError, match="private"):
                _resolve_public_ips("mapped-private.invalid")

    def test_rejects_ipv4_mapped_ipv6_metadata(self) -> None:
        """::ffff:169.254.169.254（云 metadata）必须拦截。"""
        with patch(
            "bandi.tools.web_fetch.socket.getaddrinfo",
            return_value=[(10, 1, 6, "", ("::ffff:169.254.169.254", 0))],
        ):
            with pytest.raises(ValueError, match="private"):
                _resolve_public_ips("mapped-metadata.invalid")

    def test_rejects_unspecified_ipv6(self) -> None:
        """::/128（未指定地址）必须拦截。"""
        with patch(
            "bandi.tools.web_fetch.socket.getaddrinfo",
            return_value=[(10, 1, 6, "", ("::", 0))],
        ):
            with pytest.raises(ValueError, match="private"):
                _resolve_public_ips("unspecified.invalid")
