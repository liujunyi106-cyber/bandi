# 功能：链接快照契约——公网校验、重定向拦截、HTML 清洗与正文抽取。

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bandi.knowledge import link_snapshot
from bandi.knowledge.link_snapshot import SnapshotError, fetch_snapshot, sanitize_html


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/private":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.8/secret")
            self.end_headers()
            return
        if self.path == "/not-html":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            return
        if self.path == "/gzip":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(b"\x1f\x8b")
            return
        body = (
            "<html><head><title>示例页</title></head><body>"
            "<script>alert(1)</script><h1>标题</h1><p>正文</p>"
            '<iframe src="https://evil"></iframe>'
            '<a href="javascript:alert(1)">坏链接</a>'
            '<img src="https://example.com/a.png" onerror="alert(1)">'
            "</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture()
def public_server(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.setattr(link_snapshot, "_ALLOWED_PORTS", {port, 80, 443})
    monkeypatch.setattr(link_snapshot, "_assert_public_host", lambda host, target: ["127.0.0.1"])
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def test_private_address_is_rejected():
    with pytest.raises(SnapshotError, match="内网"):
        fetch_snapshot("http://127.0.0.1/")


def test_private_hostname_is_rejected(monkeypatch):
    monkeypatch.setattr(
        link_snapshot.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.8", 80))],
    )
    with pytest.raises(SnapshotError, match="内网"):
        fetch_snapshot("http://internal.example/")


def test_sanitize_strips_scripts_and_event_handlers():
    html, text, title = sanitize_html(
        "<title>标题</title><script>alert(1)</script><h1>正文</h1>"
        '<a href="javascript:alert(1)" onclick="x()">链</a>'
        '<img src="https://example.com/a.png" onerror="alert(1)">'
    )
    assert title == "标题"
    assert "正文" in text
    assert "alert(1)" not in html
    assert "javascript:" not in html
    assert "onerror" not in html
    assert "onclick" not in html


def test_fetch_snapshot_follows_redirect_and_returns_text(public_server):
    snapshot = fetch_snapshot(f"{public_server}/redirect")
    assert "标题" in snapshot.html
    assert "正文" in snapshot.text
    assert snapshot.title == "示例页"
    assert snapshot.size > 0


def test_fetch_snapshot_rejects_private_redirect(public_server, monkeypatch):
    def guarded(host: str, port: int) -> list[str]:
        if host == "10.0.0.8":
            raise SnapshotError("不允许抓取内网或本机地址")
        return ["127.0.0.1"]

    monkeypatch.setattr(link_snapshot, "_assert_public_host", guarded)
    with pytest.raises(SnapshotError, match="内网"):
        fetch_snapshot(f"{public_server}/private")


def test_fetch_snapshot_rejects_non_html(public_server):
    with pytest.raises(SnapshotError, match="HTML"):
        fetch_snapshot(f"{public_server}/not-html")


def test_fetch_snapshot_rejects_compressed_body(public_server):
    with pytest.raises(SnapshotError, match="压缩"):
        fetch_snapshot(f"{public_server}/gzip")
