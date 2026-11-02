# 功能：MCP 客户端（stdio/streamable-http 传输 + 工具注册）
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.registry import ToolRegistry
from bandi.tools.subprocess_tree import (
    kill_process_tree,
    reap_process,
    release_process_job,
    spawn_managed_subprocess,
)

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"

_MCP_RISK_LEVELS = ("low", "medium", "high", "critical")

# 功能：MCP 子进程环境清洗——对齐 deepseek-harness scrubbedParentEnv / Hermes
# _SAFE_ENV_KEYS 的做法。远端 MCP server 是第三方进程，凭据不应随环境整包外泄。
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "USERNAME",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_TIME",
        "TZ",
        # Windows 运行必需
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERPROFILE",
        "OS",
    }
)
# 功能：凭据形状的键一律不继承（大小写不敏感，子串匹配覆盖 NESTED_* 与数字后缀变体）
_CREDENTIAL_KEY_PATTERN = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|AUTH|SESSION_ID|PRIVATE)",
    re.IGNORECASE,
)


def scrub_child_env(
    extra: dict[str, str] | None = None, *, inherit_all: bool = False
) -> dict[str, str]:
    """功能：构造 MCP stdio 子进程环境。

    ``inherit_all=False``（默认）时只放行运行必需键 + 非凭据形状的普通变量；
    凭据形状键（``*KEY*`` / ``*TOKEN*`` / ``*SECRET*`` …）被丢弃。``extra`` 中的键是
    调用方的**刻意授权**，即使形似凭据也透传（对齐 deepseek-harness「键名即授权」）。
    ``inherit_all=True`` 时完整继承父进程环境（兼容依赖完整环境的 server）。
    """
    if inherit_all:
        base = dict(os.environ)
    else:
        base = {
            key: value
            for key, value in os.environ.items()
            if key in _CHILD_ENV_ALLOWLIST or not _CREDENTIAL_KEY_PATTERN.search(key)
        }
    if extra:
        base.update({str(k): str(v) for k, v in extra.items()})
    return base


class MCPError(Exception):
    pass


class MCPTransport(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send_request(self, method: str, params: dict | None = None) -> dict: ...

    @abstractmethod
    async def send_notification(self, method: str, params: dict | None = None) -> None: ...

    @property
    @abstractmethod
    def available(self) -> bool: ...


class StdioTransport(MCPTransport):
    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        inherit_all: bool = False,
    ):
        self._command = command
        self._args = args or []
        self._extra_env = dict(env or {})
        self.env_scrubbed = not inherit_all
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        # 连接摘除标志：读循环退出（server 崩溃/stdout 关闭）即置位
        # available 随之 False——broker 停止向死连接路由新请求
        self._closed = False

    async def connect(self) -> None:
        self._closed = False
        self._process = await spawn_managed_subprocess(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrub_child_env(self._extra_env, inherit_all=not self.env_scrubbed),
        )
        self._reader = self._process.stdout
        self._writer = self._process.stdin
        self._read_task = asyncio.create_task(self._read_loop())
        # 管道缓冲即阻塞挂起（子进程整体卡死）
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        while self._process and self._process.stderr and not self._process.stderr.at_eof():
            try:
                await self._process.stderr.readline()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — stderr 消费失败不影响主协议
                break

    def _fail_pending(self, reason: str) -> None:
        """
        CONNECTION_CLOSED 语义——绝不悬挂到超时）。幂等：重复调用安全。"""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(MCPError(reason))
        self._pending.clear()

    async def _read_loop(self) -> None:
        try:
            while self._reader and not self._reader.at_eof():
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace").strip())
                    rid = msg.get("id")
                    if rid is not None and rid in self._pending:
                        self._pending[rid].set_result(msg)
                except json.JSONDecodeError:
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("MCP stdio read loop error")
                    continue
        finally:
            # server 崩溃/stdout 关闭：摘除连接并立即失败全部 pending
            # in-flight send_request 马上收到 MCPError 而非等 超时
            self._closed = True
            self._fail_pending("MCP server connection closed")

    async def disconnect(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
        if self._writer:
            self._writer.close()
        if self._process:
            if self._process.returncode is None:
                kill_process_tree(self._process)
                await reap_process(self._process)
            # 否则重连循环累积 _JOB_HANDLES 泄漏；pid 防御（测试替身可能无 pid）
            proc_pid = getattr(self._process, "pid", None)
            if isinstance(proc_pid, int):
                release_process_job(proc_pid)
        self._fail_pending("transport disconnected")

    async def send_request(self, method: str, params: dict | None = None) -> dict:
        if self._closed:
            raise MCPError("transport closed")
        self._req_id += 1
        req_id = self._req_id
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            if self._writer:
                line = json.dumps(payload, ensure_ascii=False) + "\n"
                self._writer.write(line.encode("utf-8"))
                await self._writer.drain()
            result = await asyncio.wait_for(future, timeout=30)
            if "error" in result:
                raise MCPError(result["error"].get("message", str(result["error"])))
            return result.get("result", {})
        finally:
            self._pending.pop(req_id, None)

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        if self._writer:
            line = (
                json.dumps(
                    {"jsonrpc": "2.0", "method": method, "params": params or {}}, ensure_ascii=False
                )
                + "\n"
            )
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()

    @property
    def available(self) -> bool:
        return not self._closed and self._process is not None and self._process.returncode is None


class StreamableHTTPTransport(MCPTransport):
    def __init__(self, session_url: str):
        self._session_url = session_url.rstrip("/")
        self._send_url = ""
        self._client: httpx.AsyncClient | None = None
        self._connected = False
        self._req_id = 0

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(timeout=30)
        async with self._client.stream("GET", f"{self._session_url}/sse") as resp:
            if resp.status_code != 200:
                raise MCPError(f"SSE connection failed: {resp.status_code}")
            async for line in resp.aiter_lines():
                if line.startswith("event: ") and "endpoint" in line:
                    continue
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    if isinstance(data, dict) and data.get("endpoint"):
                        self._send_url = data["endpoint"]
                        break
        if not self._send_url:
            self._send_url = f"{self._session_url}/message"
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        if self._client:
            await self._client.aclose()

    async def send_request(self, method: str, params: dict | None = None) -> dict:
        if not self._client:
            raise MCPError("HTTP transport not connected")
        self._req_id += 1
        req_id = self._req_id
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        if self._send_url.startswith("http"):
            url = self._send_url
        else:
            url = f"{self._session_url}{self._send_url}"
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
            result = resp.json()
            if "error" in result:
                raise MCPError(result["error"].get("message", str(result["error"])))
            if "result" in result:
                return result["result"]
            return {}
        except httpx.HTTPError as exc:
            raise MCPError(f"HTTP transport request failed: {exc}") from exc

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        if not self._client:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        url = (
            f"{self._send_url}"
            if self._send_url.startswith("http")
            else f"{self._session_url}{self._send_url}"
        )
        try:
            await self._client.post(url, json=payload)
        except httpx.RequestError:
            pass

    @property
    def available(self) -> bool:
        return self._connected


class MCPClient:
    def __init__(self, name: str, transport: MCPTransport):
        self._name = name
        self._transport = transport
        self._initialized = False
        self._server_info: dict = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def available(self) -> bool:
        return self._transport.available and self._initialized

    async def connect(self) -> None:
        await self._transport.connect()
        result = await self._transport.send_request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "bandi", "version": "0.1.0"},
            },
        )
        self._server_info = result
        self._initialized = True
        await self._transport.send_notification("notifications/initialized")

    async def disconnect(self) -> None:
        self._initialized = False
        await self._transport.disconnect()

    async def reconnect(self, max_retries: int = 3) -> bool:
        import random

        for attempt in range(1, max_retries + 1):
            try:
                await self.disconnect()
                await self.connect()
                return True
            except (ConnectionError, OSError, httpx.RequestError) as e:
                logger.warning("MCP reconnect attempt %d/%d failed: %s", attempt, max_retries, e)
                if attempt < max_retries:
                    await asyncio.sleep(2**attempt + random.uniform(0, 1))
        return False

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._transport.send_request("tools/list")
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._transport.send_request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        return result if isinstance(result, dict) else {"result": result}

    @property
    def capabilities(self) -> dict[str, Any]:
        # initialize 响应的 capabilities 字段；缺省回落空（无 capabilities 即不支持扩展）
        caps = (
            self._server_info.get("capabilities") if isinstance(self._server_info, dict) else None
        )
        return caps if isinstance(caps, dict) else {}

    async def list_resources(self) -> list[dict[str, Any]]:
        result = await self._transport.send_request("resources/list")
        resources = result.get("resources")
        return resources if isinstance(resources, list) else []

    async def read_resource(self, uri: str) -> list[dict[str, Any]]:
        result = await self._transport.send_request("resources/read", {"uri": uri})
        contents = result.get("contents")
        return contents if isinstance(contents, list) else []


def _bool_or_default(value: Any, default: bool = False) -> bool:
    """# 功能：仅接受布尔值，其余一律回落默认（fail-closed）"""
    return value if isinstance(value, bool) else default


def _normalize_mcp_metadata(tool_def: dict[str, Any]) -> dict[str, Any]:
    """
    MCP 动态工具元数据归一化——未知即 fail-closed：
    - 标准 MCP annotations hints（readOnlyHint/destructiveHint/idempotentHint）
    - inputSchema.x-bandi 私有扩展显式覆盖（read_only/idempotent/destructive/
      concurrency_safe/risk/cache_ttl/retryable）
    - 非法/越界扩展值回落默认（read_only/idempotent/destructive/concurrency_safe=False
      risk="medium"、cache_ttl=0.0、retryable=False）
    - retryable=true 必须具备只读或幂等基础，否则拒绝（重试会重放副作用）。"""
    hints = tool_def.get("annotations") or {}
    if not isinstance(hints, dict):
        hints = {}
    schema = tool_def.get("inputSchema") or {}
    ext = schema.get("x-bandi") if isinstance(schema, dict) else {}
    if not isinstance(ext, dict):
        ext = {}

    read_only = _bool_or_default(hints.get("readOnlyHint"))
    if isinstance(ext.get("read_only"), bool):
        read_only = ext["read_only"]

    idempotent = _bool_or_default(hints.get("idempotentHint"))
    if isinstance(ext.get("idempotent"), bool):
        idempotent = ext["idempotent"]

    destructive = _bool_or_default(hints.get("destructiveHint"))
    if isinstance(ext.get("destructive"), bool):
        destructive = ext["destructive"]

    concurrency_safe = read_only  # 只读工具隐式并发安全（cc-haha 语义）
    if isinstance(ext.get("concurrency_safe"), bool):
        concurrency_safe = ext["concurrency_safe"]

    risk = ext.get("risk")
    risk_level = risk if risk in _MCP_RISK_LEVELS else "medium"

    ttl_raw = ext.get("cache_ttl")
    cache_ttl = 0.0
    if isinstance(ttl_raw, (int, float)) and not isinstance(ttl_raw, bool):
        cache_ttl = max(float(ttl_raw), 0.0)

    retryable = ext.get("retryable") is True and (read_only or idempotent)
    if ext.get("retryable") is True and not retryable:
        logger.warning(
            "MCP tool %s declares retryable=true without read_only/idempotent; force non-retryable",
            tool_def.get("name", "?"),
        )

    # tool_version（实现版本，进缓存键）/ output_budget（输出预算，None=不限）/
    # cancellable（是否支持取消）/ needs_context（是否需要 session·project 上下文）/
    # provenance（来源标注，默认 mcp:<client> 由 register 侧补充）
    tv_raw = ext.get("tool_version")
    tool_version = tv_raw if isinstance(tv_raw, str) else ""

    budget_raw = ext.get("output_budget")
    output_budget = None
    if isinstance(budget_raw, (int, float)) and not isinstance(budget_raw, bool) and budget_raw > 0:
        output_budget = int(budget_raw)

    cancellable = _bool_or_default(ext.get("cancellable"))
    needs_context = _bool_or_default(ext.get("needs_context"))
    prov_raw = ext.get("provenance")
    provenance = prov_raw if isinstance(prov_raw, str) and prov_raw else ""

    return {
        "is_read_only": read_only,
        "is_idempotent": idempotent,
        "is_destructive": destructive,
        "is_concurrency_safe": concurrency_safe,
        "risk_level": risk_level,
        "cache_ttl": cache_ttl,
        "retryable": retryable,
        "tool_version": tool_version,
        "output_budget": output_budget,
        "cancellable": cancellable,
        "needs_context": needs_context,
        "provenance": provenance,
    }


def _apply_write_heuristics(metadata: dict[str, Any], tool_name: str, description: str) -> None:
    """-① 启发式写盘标记：名称/描述含写盘动词的远端工具强制 risk=high + 非只读
    ——服务器元数据缺失/误报时，破坏性写盘不再仅靠执行前确认兜底。"""
    hints = ("write", "edit", "delete", "remove", "rename", "move", "create")
    blob = f"{tool_name} {description}".lower()
    if any(w in blob for w in hints):
        metadata["risk_level"] = "high"
        metadata["is_read_only"] = False


async def register_mcp_tools(
    registry: ToolRegistry,
    client: MCPClient,
    prefix: str | None = None,
    allowed_tools: set[str] | None = None,
) -> list[str]:
    if not client.available:
        logger.warning("MCP client %s not available, skipping tool registration", client.name)
        return []

    tools = await client.list_tools()

    # 先清后注（替换式）：重连/重注册时按命名空间卸载旧工具，避免新增实例
    # 触发 _guard_collision（registry 不同实例同名 = fail-closed）。
    # 记账法精确清理本 namespace —— 不匹配子串前缀（fs/fs_backup 互不误删）；
    namespace = prefix or client.name
    touched = getattr(client, "_bandi_registered_namespaces", None)
    if not isinstance(touched, set):
        touched = set()
        try:
            client._bandi_registered_namespaces = touched
        except Exception:  # noqa: BLE001 — 冻结对象降级为仅清当前 namespace
            touched = {namespace}
    touched.add(namespace)
    for ns in touched:
        registry.unregister_prefix(ns)

    registered: list[str] = []
    for tool_def in tools:
        tool_name = tool_def.get("name", "")
        if not tool_name:
            continue
        full_name = f"{prefix}_{tool_name}" if prefix else f"{client.name}_{tool_name}"
        # -③ 白名单：配置 allowed_tools 时未列名远端工具一律不注册（fail-closed）
        if (
            allowed_tools is not None
            and full_name not in allowed_tools
            and tool_name not in allowed_tools
        ):
            logger.info("MCP tool %s skipped (not in allowed_tools)", full_name)
            continue
        description = tool_def.get("description", "")
        input_schema = tool_def.get("inputSchema", {})
        if not isinstance(input_schema, dict):
            logger.warning("MCP tool %s has non-dict inputSchema; skipped", full_name)
            continue
        try:
            Draft202012Validator.check_schema(input_schema)
        except Exception as exc:  # noqa: BLE001 — 校验失败只跳过该工具，不拖垮同批注册
            logger.warning("MCP tool %s has invalid inputSchema; skipped: %s", full_name, exc)
            continue

        cls_name = f"MCPTool_{full_name.replace('-', '_').replace('.', '_')}"
        metadata = _normalize_mcp_metadata(tool_def)
        # -① 启发式写盘标记（服务器元数据缺失/误报时兜底确认通道）
        _apply_write_heuristics(metadata, tool_name, description)
        mcp_tool = type(
            cls_name,
            (BaseTool,),
            {
                "name": full_name,
                "description": description,
                "parameters": input_schema,
                "category": ToolCategory.INTEGRATION,
                # -②：远端标记——executor 对远端工具参数做敏感路径本地把关
                "mcp_remote": True,
                # 触发确认通道，无确认时 fail-closed
                "risk_level": metadata["risk_level"],
                "is_read_only": metadata["is_read_only"],
                "is_idempotent": metadata["is_idempotent"],
                "is_destructive": metadata["is_destructive"],
                "is_concurrency_safe": metadata["is_concurrency_safe"],
                "cache_ttl": metadata["cache_ttl"],
                "retryable": metadata["retryable"],
                # cancellable/needs_context 供上层取消/上下文注入决策；
                # provenance 缺省回落 "mcp:<client>" 来源标注
                "tool_version": metadata["tool_version"],
                "output_budget": metadata["output_budget"],
                "cancellable": metadata["cancellable"],
                "needs_context": metadata["needs_context"],
                "provenance": metadata["provenance"] or f"mcp:{client.name}",
                "external_content": True,
                "_mcp_client": client,
                "_mcp_tool_name": tool_name,
                "execute": _make_mcp_execute(client, tool_name),
            },
        )
        # 不再是死声明；远端来源在工具面即透明
        provenance = metadata["provenance"] or f"mcp:{client.name}"
        if provenance and provenance not in mcp_tool.description:
            mcp_tool.description = f"{mcp_tool.description} [来源: {provenance}]"
        registry.register(mcp_tool, prefix=namespace)
        registered.append(full_name)

    logger.info("Registered %d MCP tools from %s", len(registered), client.name)
    return registered


def _make_mcp_execute(client: MCPClient, tool_name: str):
    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str | dict[str, Any]:
        result = await client.call_tool(tool_name, params)
        text: str | None = None
        if isinstance(result, dict):
            content = result.get("content", result)
            if isinstance(content, list):
                texts = [item.get("text", str(item)) for item in content if isinstance(item, dict)]
                text = "\n".join(texts)
        if isinstance(result, dict) and result.get("isError") is True:
            return {"ok": False, "error": text or "MCP tool reported isError=true"}
        if text is not None:
            return text
        return result

    return execute
