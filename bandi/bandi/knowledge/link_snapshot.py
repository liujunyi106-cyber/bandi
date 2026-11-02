# 功能：链接节点服务端快照——公网校验、固定 IP 抓取、HTML 清洗与正文抽取。

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urljoin, urlsplit

try:
    import certifi
except ImportError:  # pragma: no cover - certifi 随 httpx 安装
    certifi = None  # type: ignore[assignment]

MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
SNAPSHOT_TIMEOUT = 10.0
MAX_REDIRECTS = 3
_ALLOWED_PORTS = {80, 443, 8080, 8443}
_UA = "BandiLinkSnapshot/1.0 (+desktop)"
_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.IGNORECASE)


class SnapshotError(RuntimeError):
    # 功能：快照失败的可读原因——API 层转 422，不把底层异常暴露给前端。
    pass


@dataclass(frozen=True)
class Snapshot:
    html: str
    text: str
    title: str
    final_url: str
    size: int


def _assert_public_host(host: str, port: int) -> list[str]:
    # 功能：解析域名并拒绝内网/回环/链路本地/保留地址，返回公网 IP 列表。
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SnapshotError("域名解析失败") from exc
    addresses: list[str] = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise SnapshotError("域名解析结果无效") from exc
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise SnapshotError("不允许抓取内网或本机地址")
        addresses.append(raw)
    if not addresses:
        raise SnapshotError("域名解析为空")
    return addresses


def _parse_target(url: str) -> tuple[str, str, int, str, str]:
    # 功能：校验 URL 并返回 (scheme, host, port, path, 固定 IP)；仅允许 http(s) 公网。
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SnapshotError("只支持 http(s) 链接")
    if parts.username or parts.password:
        raise SnapshotError("链接不能包含账号密码")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise SnapshotError("链接缺少域名")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise SnapshotError("链接端口无效") from exc
    if port not in _ALLOWED_PORTS:
        raise SnapshotError("只允许 80/443/8080/8443 端口")
    addresses = _assert_public_host(host, port)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return scheme, host, port, path, addresses[0]


class _PinnedHTTPConnection(HTTPConnection):
    # 功能：固定解析后的公网 IP 建连，规避 DNS 重绑定与重定向内网访问。
    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(HTTPSConnection):
    # 功能：固定 IP + SNI 主机名建连，证书仍按原域名校验。
    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        ca_file = certifi.where() if certifi is not None else None
        super().__init__(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(cafile=ca_file),
        )
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _host_header(host: str, scheme: str, port: int) -> str:
    # 功能：Host 头保留非默认端口，默认端口不带后缀。
    default = 443 if scheme == "https" else 80
    return host if port == default else f"{host}:{port}"


def _fetch_once(
    scheme: str, host: str, port: int, path: str, ip: str
) -> tuple[int, str, str, str, bytes]:
    # 功能：单跳抓取——返回 (状态码, 重定向地址, content-type, content-encoding, 正文)。
    connection: HTTPConnection | HTTPSConnection
    if scheme == "https":
        connection = _PinnedHTTPSConnection(host, ip, port, SNAPSHOT_TIMEOUT)
    else:
        connection = _PinnedHTTPConnection(host, ip, port, SNAPSHOT_TIMEOUT)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Host": _host_header(host, scheme, port),
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        location = response.getheader("Location") or ""
        content_type = response.getheader("Content-Type") or ""
        content_encoding = (response.getheader("Content-Encoding") or "").lower()
        declared = response.getheader("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("页面超过 2MB，已停止抓取")
        body = response.read(MAX_SNAPSHOT_BYTES + 1)
        if len(body) > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("页面超过 2MB，已停止抓取")
        return response.status, location, content_type, content_encoding, body
    finally:
        connection.close()


def _decode(body: bytes, content_type: str) -> str:
    # 功能：按响应 charset 解码，缺省 UTF-8（容错 BOM/非法字节）。
    match = _CHARSET_RE.search(content_type)
    charset = match.group(1) if match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8-sig", errors="replace")


_ALLOWED_TAGS = {
    "a",
    "article",
    "b",
    "blockquote",
    "br",
    "code",
    "dd",
    "div",
    "dl",
    "dt",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "s",
    "section",
    "small",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_VOID_TAGS = {"br", "hr", "img"}
_SKIP_TAGS = {"script", "style", "iframe", "object", "embed", "form", "input", "button", "svg"}
_BLOCK_TAGS = {
    "article",
    "blockquote",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
}


class _Sanitizer(HTMLParser):
    # 功能：白名单清洗 + 正文抽取——丢弃脚本/样式/表单，文本与安全标签保留。
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag not in _ALLOWED_TAGS:
            return
        safe_attrs: list[str] = []
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "a":
            href = values.get("href", "").strip()
            if href.startswith(("http://", "https://")):
                safe_attrs.append(f'href="{escape(href, quote=True)}"')
                safe_attrs.append('rel="noreferrer noopener"')
        elif tag == "img":
            src = values.get("src", "").strip()
            if src.startswith(("http://", "https://", "data:image/")):
                safe_attrs.append(f'src="{escape(src, quote=True)}"')
            alt = values.get("alt", "").strip()
            if alt:
                safe_attrs.append(f'alt="{escape(alt, quote=True)}"')
        suffix = f" {' '.join(safe_attrs)}" if safe_attrs else ""
        self.parts.append(f"<{tag}{suffix}>")
        if tag in _VOID_TAGS:
            self.parts.append("")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.parts.append(f"</{tag}>")
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self.parts.append(escape(data))
        self.text_parts.append(data)


def sanitize_html(raw: str) -> tuple[str, str, str]:
    # 功能：清洗为可嵌入的 HTML 片段，并返回 (html, 正文, 标题)。
    parser = _Sanitizer()
    parser.feed(raw)
    parser.close()
    title = " ".join("".join(parser.title_parts).split())
    text = " ".join(" ".join(parser.text_parts).split())
    body = "".join(parser.parts).strip()
    wrapped = (
        '<article style="max-width:880px;margin:0 auto;padding:18px 22px;'
        "font:14px/1.75 system-ui,-apple-system,'Segoe UI',sans-serif;color:#1f2328;"
        'word-break:break-word">'
        f"{body}</article>"
    )
    return wrapped, text, title


def fetch_snapshot(url: str) -> Snapshot:
    # 功能：抓取公网 HTML 并清洗；每跳重定向都重新校验公网与端口。
    current = url.strip()
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, path, ip = _parse_target(current)
        status, location, content_type, content_encoding, body = _fetch_once(
            scheme, host, port, path, ip
        )
        if status in {301, 302, 303, 307, 308}:
            if not location:
                raise SnapshotError("重定向缺少目标地址")
            current = urljoin(current, location)
            continue
        if status != 200:
            raise SnapshotError(f"页面返回 {status}")
        if content_encoding and content_encoding not in {"identity", ""}:
            raise SnapshotError("暂不支持压缩响应")
        if "html" not in content_type.lower():
            raise SnapshotError("目标不是 HTML 页面")
        raw = _decode(body, content_type)
        html, text, title = sanitize_html(raw)
        return Snapshot(html=html, text=text, title=title, final_url=current, size=len(body))
    raise SnapshotError("重定向次数过多")
