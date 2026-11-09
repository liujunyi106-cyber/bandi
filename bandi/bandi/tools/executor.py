# 功能：工具执行器（ToolExecutor 并行/串行执行 + 权限中间件管道）
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time as time_module
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import ToolCall, ToolResult
from bandi.core.permission.models import Decision as PermissionDecision
from bandi.core.permission.models import PermissionContext, Resource
from bandi.tools.base import BaseTool, ToolPermissionContext
from bandi.tools.cache import ToolResultCache
from bandi.tools.errors import NonRetryableToolError, ToolAbortedError
from bandi.tools.permissions_pipeline import PermissionsPipeline
from bandi.tools.pipeline import ToolContext, ToolPipeline
from bandi.tools.registry import ToolRegistry
from bandi.tools.result_summary import summarize_tool_output

logger = logging.getLogger(__name__)

# 功能：交互类工具不套工具执行超时——等待用户答复由交互 TTL（1800s）兜底，
# 否则配置里的 120s 工具超时会先中断 request_user_input（卡片仍在、工具已失败）。
_INTERACTIVE_TOOL_NAMES: frozenset[str] = frozenset({"request_user_input"})

# 按 (事件循环, 上限) 共享的批内并发闸门——见 shared_concurrency_limiter
_SHARED_CONCURRENCY_LIMITERS: Any = WeakKeyDictionary()


def shared_concurrency_limiter(limit: int) -> asyncio.Semaphore:
    """功能：取本进程内共享的批内并发闸门（按事件循环 + 上限索引）。

    原实现每次执行批内并发分区时新建 `asyncio.Semaphore` → 只能限制**单批**：
    父 Agent、每个子代理、公司模式下每个员工会话的批次各自计一份
    组合峰值 = 批数 × 上限（承诺的「子代理复用同一全局闸门」未兑现）。
    改为按 loop 共享后，同一事件循环内所有工具批合计不超过 limit，形成真背压。

    `subagent_spawn` 的 `is_concurrency_safe=False` → 恒走串行分区、不持槽
    因此父子共用闸门**不构成死锁**（子代理的内部工具批仍能拿到槽）。
    按 loop 索引可避免跨事件循环复用同一 semaphore（Python 3.10+ 会报错）。
    """
    loop = asyncio.get_running_loop()
    per_loop = _SHARED_CONCURRENCY_LIMITERS.get(loop)
    if per_loop is None:
        per_loop = {}
        _SHARED_CONCURRENCY_LIMITERS[loop] = per_loop
    sem = per_loop.get(limit)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        per_loop[limit] = sem
    return sem


_MS_PER_SEC = 1000
_ERROR_MAX_CHARS = 2000

_TOOL_OUTPUT_TTL_DAYS = 7
_TOOL_OUTPUT_SWEPT = False


def cleanup_stale_tool_outputs(
    directory: Path, *, max_age_days: int = _TOOL_OUTPUT_TTL_DAYS
) -> int:
    # 功能：清理过期 `tool_output_*.txt`（按 mtime；返回删除数；目录缺失/异常静默）
    cutoff = time_module.time() - max_age_days * 86400
    removed = 0
    try:
        if not Path(directory).is_dir():
            return 0
        for path in Path(directory).glob("tool_output_*.txt"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed


def _clip_head_tail(text: str, limit: int) -> str:
    # 功能：文本超限截断——头 60% + 尾 40%（尾含 traceback 根因，不整体丢弃）；
    # 省略标注计入 limit，返回总长严格 ≤ limit
    if len(text) <= limit:
        return text
    marker = f"\n...[{len(text) - limit} chars omitted]...\n"
    body = max(limit - len(marker), 0)
    head_len = int(body * 0.6)
    tail_len = body - head_len
    tail = text[len(text) - tail_len :] if tail_len else ""
    return text[:head_len] + marker + tail


def _error_text(exc: BaseException) -> str:
    # 功能：失败 error 文本兜底——空消息异常回填类名（如 Windows SelectorEventLoop
    # 的无参 NotImplementedError，str 为空），失败原因不得对模型/用户不可见
    return str(exc) or type(exc).__name__


_NETWORK_ERROR_HINTS: tuple[str, ...] = (
    "network",
    "dns",
    "timed out",
    "timeout",
    "resolve",
    "unreachable",
    "curl (",
    "connection",
)
_NETWORK_TOOL_HINTS: tuple[str, ...] = (
    "web_",
    "websearch",
    "web_search",
    "web_fetch",
    "browser",
    "playwright",
    "fetch",
)


def _with_network_next_step(result: ToolResult) -> ToolResult:
    """功能：网络/检索类失败追加转向指引。

    模型在"网络不可用"时若无明确转向指令，会反复换工具诊断网络直到耗尽轮次。
    失败结果直接带上下一步路径（知识库/记忆/现有产物、缺失标「待取证」）。"""
    if result.success:
        return result
    text = f"{result.tool_name} {result.error or ''}".lower()
    if not any(hint in text for hint in _NETWORK_ERROR_HINTS) and not any(
        hint in text for hint in _NETWORK_TOOL_HINTS
    ):
        return result
    error = str(result.error or "")
    if "[next_step]" in error:
        return result
    result.error = error + (
        "\n[next_step] 网络/检索不可用：不要再诊断网络或重复同一调用；"
        "改用知识库、记忆、会话上下文与现有产物继续，缺失证据标注「待取证」，"
        "并写出本卡最终正文。"
    )
    return result


# -3：暂停工具的反复调用上限——超过才 should_abort 硬终止（防死循环烧钱）
_BLOCKED_RETRY_LIMIT = 3


def _extract_dict_error(output: Any) -> str | None:
    # 功能：识别工具未抛异常但返回错误形态 dict 的输出（{"error": ...} / success=False /
    # ok=False——python_exec 等工具的非零退出码形态，3.7）
    if not isinstance(output, dict):
        return None
    err = output.get("error")
    if isinstance(err, str) and err.strip():
        return err
    if output.get("success") is False:
        msg = output.get("message") or err or "工具返回失败状态"
        return str(msg)
    if output.get("ok") is False:
        for key in ("message", "stderr"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        return f"tool reported ok=false (exit_code={output.get('exit_code')})"
    return None


def _is_streaming_tool(tool: Any) -> bool:
    # 功能：判定工具是否 override 了 BaseTool.stream()（有自定义增量进度）。
    # 要求类型上存在真实 async-generator 的 stream 且非默认 BaseTool.stream——
    # 排除 MagicMock 自动属性（hasattr 恒真）与未覆盖默认实现/无 stream 的工具。
    cls_stream = getattr(type(tool), "stream", None)
    return cls_stream is not BaseTool.stream and inspect.isasyncgenfunction(cls_stream)


_REMOTE_SCAN_MAX_DEPTH = 8
_PATH_URI_SCHEME = "file://"


def _iter_string_values(value: Any, *, depth: int = 0) -> Iterator[str]:
    """功能：-② 递归遍历参数中的全部字符串（含嵌套 dict/list，封堵深层旁路）。"""
    if depth > _REMOTE_SCAN_MAX_DEPTH:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_string_values(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_string_values(item, depth=depth + 1)


def _path_candidate(text: str) -> str | None:
    """功能：-② 提取待把关的本地路径——盘符形态优先按本地路径处理（"://" 会把
    C://x 这类写法误当 scheme）；file:// URI 归一化为本地路径（远端 MCP server 多为
    本地进程，不可当普通 URL 放过）；其余含 scheme 的值非本地路径返回 None。"""
    if not text or len(text) > 4096:
        return None
    if len(text) > 1 and text[1] == ":" and text[0].isalpha():
        return text
    if "://" in text:
        if not text.lower().startswith(_PATH_URI_SCHEME):
            return None
        from urllib.parse import unquote, urlparse

        path = unquote(urlparse(text).path or "")
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path or None
    if text.startswith(("~", "./", "..", ".\\")):
        return text
    if "/" in text or "\\" in text:
        return text
    return None


# 功能：员工工作区免审批——bash 危险命令特征（命中即仍走审批；宁严勿漏）
_DANGEROUS_COMMAND_PATTERNS: tuple[str, ...] = (
    r"\brm\b",
    r"\bdel\b",
    r"\brd\b",
    r"\brmdir\b",
    r"\bformat\b",
    r"\breg\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\btaskkill\b",
    r"\bmkfs\b",
    r"\bdd\b",
    r"\bsudo\b",
    r"\brunas\b",
    r"\bwmic\b",
    r"\bschtasks\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bcurl\b[^\n|]*\|",
    r"\bwget\b[^\n|]*\|",
    r"invoke-expression",
    r"\s-enc(odedcommand)?\b",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bpip\s+uninstall\b",
    r"\bnpm\s+publish\b",
    r"\bstart-process\b",
    r"\bremove-item\b",
    r"\bset-content\b",
    r"\bout-file\b",
    r"\bcopy-item\b",
    r"\bmove-item\b",
    r"\bnew-item\b",
    r"\brename-item\b",
    r"\bclear-content\b",
    r"\badd-content\b",
)

# 功能：员工工作区免审批——文件写入类工具（目标路径须在工作区内）
_EMPLOYEE_AUTO_ALLOW_FILE_TOOLS: frozenset[str] = frozenset(
    {"write", "file_write", "file_edit", "edit"}
)
_EMPLOYEE_AUTO_ALLOW_PATH_KEYS: tuple[str, ...] = ("file_path", "path", "filename")


def _extract_citations(output: Any) -> list[dict[str, Any]]:
    """功能：从工具输出提取知识库引用（最多 8 条，非 citations 输出返回空）。"""
    if not isinstance(output, dict):
        return []
    citations = output.get("citations")
    if not isinstance(citations, list):
        return []
    return [item for item in citations[:8] if isinstance(item, dict)]


class ToolExecutor:  # 功能：工具执行引擎（并发/串行分区执行 + Pipeline 中间件 + 长输出截断落盘 + 连续失败检测 + 事件发射）
    def __init__(
        self,
        registry: ToolRegistry,
        max_output_tokens: int = 8192,
        temp_dir: str = "~/.bandi/tmp",
        event_bus: EventBus | None = None,
        consecutive_failure_threshold: int = 3,
        # 工具执行单次上限（超时返回错误结果给 LLM 续跑，不挂死执行轮）
        execution_timeout: int = 180,
        output_preview_chars: int = 200,
        chars_per_token: int = 4,
        permissions_pipeline: PermissionsPipeline | None = None,
        policy_engine: Any | None = None,
        mode: str = "execute",
        agent_id: str = "default",
        agent_role: str = "user",
        agent_scope: str = "default",
        tool_pipeline: ToolPipeline | None = None,
        result_cache: ToolResultCache | None = None,
        policy_version: str = "",
        communication: Any | None = None,
        project_root: str | None = None,
        per_tool_max_output_tokens: dict[str, int] | None = None,
        auto_review_llm: Any | None = None,
        summary_llm: Callable[[str, str], Awaitable[str]] | None = None,
        role_tool_tags: list[str] | None = None,
        role_fs_scopes: list[str] | None = None,
        # 是否有派发面——无下级员工的执行面不得调用管理者专属工具
        manager_capable: bool = True,
        # 员工档案工具绑定（resource_type=tool）——执行面与 schema 面同源收窄
        bound_tool_names: list[str] | None = None,
        skill_scope: dict[str, Any] | None = None,
        provider_version: str = "",
        idempotency_store: Any | None = None,
        abort_controller: Any | None = None,
        snapshot_root: str | None = None,
        # 批内并发上限：并发安全分区同时执行数上限（None=不限制，向后兼容）
        max_concurrency: int | None = None,
        read_state: dict[str, Any] | None = None,
        # 使子代理工具面继承父的 PreToolUse/PostToolUse）
        hook_runner: Any | None = None,
        # （缺省空串 = 未接入身份链路，消费处回落 agent_id 保持旧行为）
        user_id: str = "",
    ):
        self._registry = registry
        self.read_state: dict[str, Any] = read_state if read_state is not None else {}
        self._temp_dir = Path(temp_dir).expanduser()
        self._event_bus = event_bus
        self._consecutive_failure_threshold = consecutive_failure_threshold
        self._execution_timeout = execution_timeout
        self._output_preview_chars = output_preview_chars
        self._chars_per_token = chars_per_token
        self._max_output_chars = max_output_tokens * self._chars_per_token
        self._per_tool_max_output_tokens = per_tool_max_output_tokens or {}
        self._failure_count: dict[str, int] = {}
        # 同签名参数校验失败计数（失败阶梯：1 引导/2 升级/3 终止）
        self._param_fail_counts: dict[str, int] = {}
        # 高危写操作快照根目录（None=write_snapshot 模块默认 ~/.bandi/snapshots）
        self._snapshot_root = Path(snapshot_root).expanduser() if snapshot_root else None
        # -3：暂停工具的反复调用计数（硬撞保护——≥ _BLOCKED_RETRY_LIMIT 才终止）
        self._block_count: dict[str, int] = {}
        # workspace，与调用方 project_root 沙箱语义不一致）
        self._permissions_pipeline = permissions_pipeline or PermissionsPipeline(
            project_root=project_root
        )
        self._policy_engine = policy_engine
        self._mode = mode
        self._agent_id = str(agent_id or "default")
        self._agent_role = agent_role
        # 归一为 str——非 str 入参（None/数字）在缓存 scope 拼接处会 TypeError
        self._user_id = str(user_id or "")
        self._agent_scope = agent_scope
        self._pipeline = tool_pipeline
        self._result_cache = result_cache
        self._policy_version = policy_version
        self._communication = communication
        self._project_root = project_root
        self._hook_runner = hook_runner
        self._provider_version = provider_version
        self._summary_llm = summary_llm
        self._idem_keys: dict[str, str] = {}
        # 用确定性 key + 查/写此 store，进程 B 恢复命中不重放副作用
        self._idem_store = idempotency_store
        # 审批回执——无 idempotency store 时的进程内兜底（测试/未接线路径）
        self._approval_receipts: dict[str, ToolResult] = {}
        # 越界即不串缓存；策略变更（policy_version）自动失效旧缓存；
        # session_id/tool_version/provider_version 由 _cache_scope_for 动态并入
        self._base_cache_scope = self._compose_base_cache_scope()
        # 经 ToolPermissionContext.extra 透传给 ctx_inspect 类自查工具）
        self._runtime_snapshot: dict[str, Any] | None = None
        self._session_meta: dict[str, Any] | None = None
        self._auto_review_llm = auto_review_llm
        # 此处执行面 fail-closed 防幻觉调用未注入工具）
        # 空列表 = 显式全禁（fail-closed 哨兵；仅 None = 不过滤）——角色过滤
        # 查询异常路径返回空名单时必须真正拒绝全部工具（原 `if role_tool_tags`
        self._role_tool_tags = set(role_tool_tags) if role_tool_tags is not None else None
        self._manager_capable = manager_capable
        # 员工工具绑定：非空即收窄（None/空 = 未配置 = 不收窄），执行面与 schema 面同源
        self._bound_tool_names = (
            frozenset(str(n).strip() for n in bound_tool_names if str(n).strip())
            if bound_tool_names
            else None
        )
        # .6：角色级落盘目录白名单——经 extra 透传给 fs 工具窄化沙箱
        self._role_fs_scopes = list(role_fs_scopes) if role_fs_scopes else None
        # 技能作用域透传——skill_invoke / skill_discover 据此收窄可见技能，
        # 防止模型显式点名 invoke 别团队技能（自动注入收窄不足以覆盖显式调用）
        self._skill_scope = dict(skill_scope) if skill_scope else None
        self._abort_controller = abort_controller
        # 批内并发限流：>0 = 并发组 semaphore 上限；None/<=0 = 不限制（gather 全并发）。
        # 显式 0/负数属"主动关掉闸门"，必须告警——静默裸奔会让单批 N 个并发安全工具
        if max_concurrency is not None and int(max_concurrency) <= 0:
            logger.warning(
                "批内工具并发未设上限（max_concurrency=%s）—— 同批工具将全部并发执行，"
                "可能耗尽连接与文件句柄",
                max_concurrency,
            )
        self._max_concurrency = max_concurrency if (max_concurrency or 0) > 0 else None

    def _compose_base_cache_scope(self) -> str:
        # 功能：拼身份维度缓存 scope（agent/role/scope/mode/project_root/策略版本/user）——
        # 构造与 set_agent_id 共用单一拼装点，防两处口径漂移
        return "|".join(
            [
                self._agent_id,
                self._agent_role,
                self._agent_scope,
                self._mode,
                str(self._project_root or ""),
                self._policy_version,
                self._user_id,
            ]
        )

    @property
    def agent_id(self) -> str:
        """功能：真实 Agent 实例标识只读暴露（父链/缓存 scope 单一来源）。"""
        return getattr(self, "_agent_id", "default")

    def set_agent_id(self, agent_id: str) -> None:
        """功能：构建后绑定真实 agent_id——factory 中 id 生成晚于 executor 构造
        经此刷新身份维度（父链 extra 与缓存 scope），否则子代理父链恒 "default"。

        同时按旧 base scope 前缀清理结果缓存——切换身份后旧 scope 键不得再命中
        （身份隔离延伸；未记录 scope 的直接写入不受影响，零行为给未调用者）。
        """
        previous_scope = getattr(self, "_base_cache_scope", "")
        self._agent_id = str(agent_id or "default")
        self._base_cache_scope = self._compose_base_cache_scope()
        cache = getattr(self, "_result_cache", None)
        if cache is not None and previous_scope and previous_scope != self._base_cache_scope:
            cache.purge_scope_prefix(previous_scope + "|")

    def _cache_scope_for(self, tool: BaseTool | None) -> str:
        # 功能：按工具动态拼缓存 scope——base 六维 + session_id + tool_version + provider_version；
        # 工具未声明版本（""）保持既有键语义（向后兼容）
        session_id = (self._session_meta or {}).get("session_id", "")
        tool_version = getattr(tool, "tool_version", "") if tool is not None else ""
        return "|".join(
            [self._base_cache_scope, str(session_id), str(tool_version), self._provider_version]
        )

    @property
    def cache_scope(self) -> str:
        """缓存作用域（base 六维 + session + tool_version + provider_version）只读暴露。"""
        return self._cache_scope_for(None)

    def _role_gate(self, tc: ToolCall) -> ToolResult | None:
        # 无派发面员工不得调用管理者专属工具——schema 面已扣，执行面同源
        if (
            not getattr(self, "_manager_capable", True)
            and tc.name in ToolRegistry.MANAGER_ONLY_TOOLS
        ):
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error=f"tool {tc.name} requires a manager-capable role",
            )
        # 交互通道与协作/派单工具恒定豁免（与 schema 面 with_always_available 同源）
        exempt = set(ToolRegistry.ALWAYS_AVAILABLE_TOOLS) | set(
            ToolRegistry.COLLAB_ALWAYS_AVAILABLE_TOOLS
        )
        # 员工工具绑定：执行面必须校验，否则模型凭空点名绑定外工具仍会执行
        bound = getattr(self, "_bound_tool_names", None)
        if bound is not None and tc.name not in exempt and tc.name not in bound:
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error=f"tool {tc.name} not bound to this employee",
            )
        if self._role_tool_tags is None:
            return None
        # with_always_available 同口径；否则受限角色「schema 可见、执行被拒」
        # 提问能力在员工/子代理链不可达（schema 有 request_user_input 也调不动）
        if tc.name in exempt:
            return None
        tool = self._registry.get(tc.name)
        tool_tags = getattr(tool, "tags", None) or []
        if any(tag in self._role_tool_tags for tag in tool_tags):
            return None
        return ToolResult(
            tool_call_id=tc.id,
            tool_name=tc.name,
            success=False,
            output="",
            error=f"tool {tc.name} not allowed for role tags {sorted(self._role_tool_tags)}",
        )

    @staticmethod
    def _permission_action_for(tool: BaseTool) -> str:
        """功能：6.4 权限 action 解析——工具显式声明优先（bash=execute）
        否则按 read_only/destructive 推导 read/delete，其余 write；使权限档的
        read/write/delete/execute 四个分支全部可达。"""
        declared = getattr(tool, "permission_action", "")
        if declared:
            return declared
        if getattr(tool, "is_destructive", False):
            return "delete"
        if getattr(tool, "is_read_only", False):
            return "read"
        return "write"

    @property
    def _effective_user_id(self) -> str:
        """功能：审批/策略/工具上下文的用户标识——真实 user_id 优先，缺省回落
        agent_id（未接入身份链路的旧行为）；getattr 防御测试替身绕过 __init__。"""
        return getattr(self, "_user_id", "") or getattr(self, "_agent_id", "default")

    def _approval_dims(self) -> dict[str, str]:
        """功能：6.9 授权五维键取值——user/role/project/policy_version 经
        ApprovalStore 复合键消费（此前只传 sid，维度声明在生产不生效）；
        getattr 防御：测试替身可能绕过 __init__（无身份属性时回退默认）。"""
        meta = getattr(self, "_session_meta", None) or {}
        return {
            "user_id": self._effective_user_id,
            "role": getattr(self, "_agent_role", "user"),
            "project_id": str(meta.get("project_id", "") or ""),
            "policy_version": getattr(self, "_policy_version", ""),
        }

    def set_runtime_snapshot(self, snapshot: dict[str, Any]) -> None:
        # 功能：绑定当轮运行时快照（states.agent_runtime_snapshot 产出）
        self._runtime_snapshot = snapshot

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def bind_session_meta(self, meta: dict[str, Any]) -> None:
        self._session_meta = meta
        # .2 修复：交互式审批记账的会话键下发权限管道（CLI/SSE 拒绝追踪）
        bind = getattr(self._permissions_pipeline, "bind_session_id", None)
        if callable(bind):
            bind(str((meta or {}).get("session_id") or self._agent_id))

    async def execute(
        self, tool_calls: list[ToolCall]
    ) -> list[ToolResult]:  # 功能：按并发安全分区执行工具调用，返回结果列表（含缓存+幂等性去重）
        if not tool_calls:
            return []

        gated: dict[str, ToolResult] = {}
        pending_calls: list[ToolCall] = []
        for tc in tool_calls:
            denial = self._role_gate(tc)
            if denial is not None:
                gated[tc.id] = denial
            else:
                pending_calls.append(tc)

        unique_calls: list[ToolCall] = []
        dedup_map: dict[str, ToolCall] = {}
        seen_index: dict[tuple[str, str], ToolCall] = {}
        for tc in pending_calls:
            arg_hash = json.dumps(tc.arguments, sort_keys=True, default=str)
            key = (tc.name, arg_hash)
            tool = self._registry.get(tc.name)
            eligible = self._is_dedup_eligible(tool)
            if eligible and key in seen_index:
                dedup_map[tc.id] = seen_index[key]
            else:
                seen_index[key] = tc
                unique_calls.append(tc)
                dedup_map[tc.id] = tc

        # 批量审批：同一批 ≥2 条需人工确认的调用合成一张卡，一次决策应用到全部
        unique_calls, batch_denied = await self._preflight_batch_approval(unique_calls)
        results_by_id: dict[str, ToolResult] = {**gated, **batch_denied}

        def _cancelled_result(tc: ToolCall) -> ToolResult:
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error="cancelled: batch aborted (should_abort in same batch)",
                meta={"error_class": "cancelled"},
            )

        def _skipped_result(tc: ToolCall) -> ToolResult:
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error="skipped: batch aborted (should_abort before dispatch)",
                meta={"error_class": "cancelled"},
            )

        segments = self._segments(unique_calls)
        aborted = False

        for is_concurrent, seg_calls in segments:
            if aborted:
                for tc in seg_calls:
                    results_by_id[tc.id] = _skipped_result(tc)
                continue

            if not is_concurrent:
                for idx, tc in enumerate(seg_calls):
                    try:
                        results_by_id[tc.id] = await self._execute_one(tc)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("tool %s failed: %s", tc.name, e)
                        self._record_failure(tc.name)
                        results_by_id[tc.id] = ToolResult(
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            success=False,
                            output="",
                            error=_error_text(e),
                        )
                    current = results_by_id.get(tc.id)
                    if current is not None and current.should_abort:
                        for rest in seg_calls[idx + 1 :]:
                            results_by_id[rest.id] = _skipped_result(rest)
                        aborted = True
                        break
                continue

            if self._max_concurrency is not None:
                # 批内并发限流——semaphore 只作用于并发安全分区；serial 组语义不变。
                # 闸门按 (loop, 上限) 共享：父/子代理与多 Agent 的批次合计受限
                sem = shared_concurrency_limiter(self._max_concurrency)

                async def _limited(tc: ToolCall) -> ToolResult:
                    async with sem:
                        return await self._execute_one(tc)

                task_list = [_limited(tc) for tc in seg_calls]
            else:
                task_list = [self._execute_one(tc) for tc in seg_calls]
            task_map: dict[asyncio.Task[ToolResult], ToolCall] = {
                asyncio.ensure_future(coro): tc for coro, tc in zip(task_list, seg_calls)
            }
            pending: set[asyncio.Task[ToolResult]] = set(task_map)

            while pending:
                done_batch, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                abort_seen = False
                for t in done_batch:
                    tc = task_map[t]
                    if t.cancelled():
                        results_by_id[tc.id] = _cancelled_result(tc)
                        continue
                    exc = t.exception()
                    if exc is not None:
                        self._record_failure(tc.name)
                        results_by_id[tc.id] = ToolResult(
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            success=False,
                            output="",
                            error=_error_text(exc),
                        )
                        continue
                    r = t.result()
                    results_by_id[tc.id] = r
                    if r.should_abort:
                        abort_seen = True
                if abort_seen and pending:
                    for p in pending:
                        p.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for t in pending:
                        results_by_id[task_map[t].id] = _cancelled_result(task_map[t])
                    pending = set()
            if abort_seen:
                aborted = True

        # 按原始调用顺序重建结果——dedup 映射指向执行过的唯一调用，按 tool_call_id 定位
        ordered: list[ToolResult] = []
        try:
            for tc in tool_calls:
                map_tc = dedup_map.get(tc.id)
                is_dedup_hit = map_tc is not None and map_tc is not tc
                base = results_by_id.get(map_tc.id) if map_tc is not None else gated.get(tc.id)
                if base is not None and base.tool_call_id != tc.id:
                    ordered.append(
                        ToolResult(
                            tool_call_id=tc.id,
                            tool_name=base.tool_name,
                            success=base.success,
                            output=base.output,
                            error=base.error,
                            duration_ms=base.duration_ms,
                            should_abort=base.should_abort,
                            meta=dict(base.meta or {}),
                        )
                    )
                elif base is not None:
                    ordered.append(base)
                else:
                    ordered.append(
                        ToolResult(
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            success=False,
                            output="",
                            error="tool returned no result",
                        )
                    )
                if is_dedup_hit:
                    hit = ordered[-1]
                    await self._emit_event(
                        EventType.TOOL_COMPLETED,
                        {
                            "tool_name": tc.name,
                            "tool_call_id": tc.id,
                            "duration_ms": 0,
                            "success": hit.success,
                            "deduped": True,
                            "citations": _extract_citations(hit.output),
                            "output_preview": str(hit.output or "")[
                                : self._output_preview_chars
                            ].replace("\n", " "),
                        },
                    )
            # 最大且非豁免结果落盘替换为预览（小结果保完整；读类豁免）
            self._enforce_batch_output_budget(ordered)
            return ordered
        finally:
            # 重试复用仅限批内，长生命周期 executor 不泄漏
            for tc in tool_calls:
                self._idem_keys.pop(tc.id, None)

    def _is_dedup_eligible(self, tool: BaseTool | None) -> bool:
        # 功能：去重仅限只读或显式幂等工具——有副作用工具复用结果会丢真实执行；
        # dedup_scope="none" 显式禁去重（读外部状态工具结果会变）
        if tool is None:
            return False
        if getattr(tool, "dedup_scope", "turn") == "none":
            return False
        return bool(getattr(tool, "is_read_only", False) or getattr(tool, "is_idempotent", False))

    def _consume_approval_grant(self, tc: ToolCall) -> bool:
        """功能：消费审批授权（人工/机审放行后的 re-issue）——命中即写权限确认缓存
        使本次调用穿透 ask 门与 policy ASK（ApprovalStore=授权单一事实源）。
        此前 headless 批准只写 store，pp.check 的 ask 层无通道再拒 → 工具永不执行；
        一次性授权消费即焚、会话级授权复用由 store.is_granted 语义承担。"""
        from bandi.tools.approval import get_approval_store, params_digest

        store = get_approval_store()
        sid = (self._session_meta or {}).get("session_id") or self._agent_id
        granted = store.is_granted(
            sid, tc.name, params_digest(tc.arguments), **self._approval_dims()
        )
        if not granted:
            # 兼容兜底：人工审批端点（apply_decision）写入的是 legacy 无维度
            # grant（端点无 agent 身份上下文）——带维度消费 miss 后回查旧键。
            # 第三兜底（has_any_grant 跨会话 wildcard 放权）已删除——
            # 授权只在批准它的会话内生效，不得外溢到全进程其它会话。
            granted = store.is_granted(sid, tc.name, params_digest(tc.arguments))
        if not granted:
            return False
        self._permissions_pipeline.cache_approved(tc.name, tc.arguments)
        return True

    def _approval_once_granted(self, tc: ToolCall) -> bool:
        """本次调用是否由「仅本次」授权放行（决定是否写/查回执）。

        session 授权重复调用是预期行为，不参与回执——否则第二次相同调用会
        返回旧结果而不是真实执行。
        """
        from bandi.tools.approval import get_approval_store, params_digest

        store = get_approval_store()
        sid = (self._session_meta or {}).get("session_id") or self._agent_id
        digest = params_digest(tc.arguments)
        return store.is_once_granted(
            sid, tc.name, digest, **self._approval_dims()
        ) or store.is_once_granted(sid, tc.name, digest)

    def _approval_receipt_key(self, tc: ToolCall) -> str:
        """审批回执确定性键——同会话同工具同参数恒定，跨重放/跨进程可重算。"""
        from bandi.tools.approval import params_digest

        sid = str((self._session_meta or {}).get("session_id") or self._agent_id)
        # 前缀用下划线而非冒号——幂等存储按 key 落文件名，Windows 不允许 `:`
        return (
            "approval_receipt_"
            + hashlib.sha256(f"{sid}:{tc.name}:{params_digest(tc.arguments)}".encode()).hexdigest()[
                :24
            ]
        )

    def _approval_receipt_get(self, key: str) -> ToolResult | None:
        if self._idem_store is not None:
            return self._idem_store.get(key)
        return self._approval_receipts.get(key)

    def _approval_receipt_set(self, key: str, result: ToolResult) -> None:
        # 只存成功结果：失败由模型/用户重试，不能把一次失败固化成幂等回执。
        if not result.success:
            return
        if self._idem_store is not None:
            self._idem_store.set(key, result)
        else:
            self._approval_receipts[key] = result

    async def _preflight_batch_approval(
        self, tool_calls: list[ToolCall]
    ) -> tuple[list[ToolCall], dict[str, ToolResult]]:
        # 功能：批量审批预检——同批 ≥2 条需人工确认的调用合并成一张卡，一次决策应用到全部
        pp = self._permissions_pipeline
        if pp is None or len(tool_calls) < 2:
            return tool_calls, {}
        if not getattr(pp, "user_interaction_available", False):
            return tool_calls, {}
        ui = getattr(pp, "user_interaction", None)
        if ui is None or not hasattr(ui, "ask_permission_batch"):
            return tool_calls, {}
        from bandi.safety.denial_tracker import get_denial_tracker
        from bandi.tools.approval import get_approval_store, params_digest

        sid = (self._session_meta or {}).get("session_id") or self._agent_id
        store = get_approval_store()
        picked: list[ToolCall] = []
        items: list[dict[str, Any]] = []
        for tc in tool_calls:
            tool = self._registry.get(tc.name)
            if tool is None:
                continue
            try:
                needs_approval = bool(pp.needs_approval(tc, tool, self._mode))
            except AttributeError:
                continue
            internal = getattr(pp, "needs_internal_confirmation", None)
            if not needs_approval and callable(internal) and internal(tc, tool):
                needs_approval = True
            if not needs_approval:
                continue
            if self._employee_workspace_auto_allow(tc):
                # 缓存写入失败不影响本次放行与后续逐条询问
                try:
                    pp.cache_approved(tc.name, tc.arguments)
                except Exception:
                    logger.debug("employee auto-allow cache write failed", exc_info=True)
                continue
            if pp.is_confirm_cached(tc.name, tc.arguments):
                continue
            if store.is_granted(sid, tc.name, params_digest(dict(tc.arguments))):
                continue
            picked.append(tc)
            items.append(
                {
                    "tool_call_id": tc.id,
                    "tool_name": tc.name,
                    "args": dict(tc.arguments),
                    "risk": str(getattr(tool, "risk_level", "medium") or "medium"),
                }
            )
        if len(picked) < 2:
            return tool_calls, {}
        decision = await ui.ask_permission_batch(items)
        tracker = get_denial_tracker()
        if bool(decision.get("granted")):
            scope = "session" if str(decision.get("scope") or "") == "session" else "once"
            for tc in picked:
                # 缓存写入失败只影响下次免问，授权仍按 store 生效
                try:
                    pp.cache_approved(tc.name, tc.arguments)
                except Exception:
                    logger.debug("batch approval cache write failed", exc_info=True)
                store.grant(
                    sid,
                    tc.name,
                    scope=scope,
                    params_digest="" if scope == "session" else params_digest(dict(tc.arguments)),
                )
                tracker.record_approval(sid, tc.name)
            return tool_calls, {}
        denied: dict[str, ToolResult] = {}
        for tc in picked:
            tracker.record_denial(sid, tc.name)
            denied[tc.id] = ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error=(
                    f"user denied running {tc.name} (batch approval): "
                    "do not retry; propose an alternative"
                ),
                meta={"error_class": "permission_denied", "batch": True},
            )
        remaining = [tc for tc in tool_calls if tc.id not in denied]
        return remaining, denied

    async def _approval_gate(self, tc: ToolCall, tool: BaseTool) -> ToolResult | None:
        """
        （turn 将以 AWAITING_APPROVAL 暂停）。

        触发条件 = pipeline 判定需审批 且 无同步确认通道（headless/API）；
        CLI 有 UI 时零介入（同步询问路径不变）。auto_review reviewer 先机审
        escalate 落人工 pending。
        """
        pp = self._permissions_pipeline
        if pp is None:
            return None
        verdict = None
        try:
            explain = getattr(pp, "explain_approval", None)
            if callable(explain):
                verdict = explain(tc, tool, self._mode)
                needs = bool(verdict.ask_user)
            else:
                needs = pp.needs_approval(tc, tool, self._mode)
        except AttributeError:
            return None
        if not needs:
            return None

        # 员工工作区免审批：员工会话（role-session）内
        # 文件写入/bash 且目标全落在本 run 工作区内、无敏感/危险特征 → 自动放行。
        # 该判定先于 UI/headless 分叉：cache_approved 让 pipeline 的 UI 询问
        # （_check_ui）直接缓存命中放行——有交互通道时不再弹权限卡；工作区外/
        # 危险操作仍走审批（fail-closed；缓存写失败同样回退审批）
        if self._employee_workspace_auto_allow(tc):
            try:
                pp.cache_approved(tc.name, tc.arguments)
                return None
            except Exception:  # 缓存失败回退审批（防 ask 层二次拒绝）
                logger.debug("employee auto-allow cache write failed", exc_info=True)

        if not getattr(pp, "user_interaction_available", False):
            pass  # headless——进入审批流
        else:
            return None

        # .2：连续拒绝追踪的**生产短路**。
        # 同一会话对同一工具被连续拒绝达阈值后不再重复追问——否则模型会「再试一次 →
        # 死链）里，生产从未生效；此处把它接进真正的审批入口。
        from bandi.safety.denial_tracker import get_denial_tracker

        denial_sid = (self._session_meta or {}).get("session_id") or self._agent_id
        tracker = get_denial_tracker()
        if tracker.should_auto_deny(denial_sid, tc.name):
            # 每次自动拒绝记一次拒绝（系统代用户拒）——累计阈值随死缠重试可达
            tracker.record_denial(denial_sid, tc.name)
            # 累计拒绝达阈值 → 硬终止信号（复用 states 的「先总结后终止」机制；
            # 只影响本会话本轮——多 Agent 各自会话键互不污染）
            hard_stop = tracker.should_abort(denial_sid, tc.name)
            await self._emit_event(
                EventType.APPROVAL_DENIED,
                {
                    "tool_name": tc.name,
                    "tool_call_id": tc.id,
                    "decided_by": "denial_tracker",
                    "rationale": "consecutive denials exceeded threshold",
                    "should_abort": hard_stop,
                },
            )
            error = (
                f"工具 {tc.name} 被重复拒绝已达上限——已中止本轮。"
                "请停止重试该工具：向用户说明受阻原因，或请其调整权限策略后再继续。"
                if hard_stop
                else (
                    f"tool {tc.name} auto-denied after repeated denials — do not retry; "
                    "ask the user to adjust the permission policy instead"
                )
            )
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error=error,
                should_abort=hard_stop,
            )

        from bandi.tools.approval import (
            APPROVAL_REQUIRED_PREFIX,
            build_approval_question,
            describe_tool_intent,
            get_approval_store,
        )
        from bandi.tools.auto_review import auto_review_decision

        store = get_approval_store()
        sid = (self._session_meta or {}).get("session_id") or self._agent_id
        from bandi.tools.approval import params_digest

        if store.get_policy(sid) == "never":
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error=f"tool {tc.name} denied by approval policy (never)",
            )

        params = dict(tc.arguments)
        from bandi.tools.approval import PendingApproval

        pending = PendingApproval(
            tool_call_id=tc.id,
            tool_name=tc.name,
            params_digest=params_digest(params),
            reason=(
                verdict.reason
                if verdict is not None and verdict.reason
                else "requires user approval"
            ),
            detail=describe_tool_intent(tc.name, params),
            rule_pattern=(verdict.rule_pattern if verdict is not None else ""),
            rule_source=(verdict.rule_source if verdict is not None else ""),
            # 服务端不可变参数快照（决策/审计以它为准，不信任客户端回传）
            params=dict(params),
        )
        if store.get_reviewer(sid) == "auto_review":
            decision, rationale = await auto_review_decision(
                pending, params, llm_call=self._auto_review_llm
            )
            if decision == "allow":
                store.grant(
                    sid,
                    tc.name,
                    params_digest=params_digest(params),
                    scope="once",
                    **self._approval_dims(),
                )
                # 同调用继续执行：写确认缓存使 pp.check 的 ask 层放行
                self._permissions_pipeline.cache_approved(tc.name, tc.arguments)
                # .2 修复：机审放行同口径清零连续拒绝计数（与人工批准一致）
                get_denial_tracker().record_approval(sid, tc.name)
                await self._emit_event(
                    EventType.APPROVAL_GRANTED,
                    {
                        "tool_name": tc.name,
                        "tool_call_id": tc.id,
                        "decided_by": "auto_review",
                        "rationale": rationale,
                        "session_id": sid,
                    },
                )
                return None
            if decision == "deny":
                store.pop_pending(sid)
                # .2：机审拒绝同样计入拒绝追踪（与人工拒绝同一口径）
                get_denial_tracker().record_denial(sid, tc.name)
                await self._emit_event(
                    EventType.APPROVAL_DENIED,
                    {
                        "tool_name": tc.name,
                        "tool_call_id": tc.id,
                        "decided_by": "auto_review",
                        "rationale": rationale,
                        "session_id": sid,
                    },
                )
                return ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    success=False,
                    output="",
                    error=f"denied by auto_review: {rationale}",
                )
            # escalate → 落入人工 pending

        store.set_pending(sid, pending)
        await self._emit_event(
            EventType.APPROVAL_REQUESTED,
            {
                "tool_name": tc.name,
                "tool_call_id": tc.id,
                "params_digest": pending.params_digest,
                "reason": pending.reason,
                "session_id": sid,
                "decided_by": "pipeline",
            },
        )
        question = build_approval_question(
            tool_call_id=tc.id,
            tool_name=tc.name,
            reason=pending.reason,
            detail=describe_tool_intent(tc.name, params),
            rule_pattern=pending.rule_pattern,
            rule_source=pending.rule_source,
        )
        return ToolResult(
            tool_call_id=tc.id,
            tool_name=tc.name,
            success=False,
            output="",
            error=f"{APPROVAL_REQUIRED_PREFIX}{json.dumps(question, ensure_ascii=False)}",
        )

    async def _enqueue_approval(self, tc: ToolCall, reason: str) -> ToolResult:
        """功能：政策引擎判 ASK 时落入审批 pending 并返回 APPROVAL_REQUIRED。

        ASK 语义是「需人工决策」而非工具失败——此前与 DENY 同一分支直接
        success=False，角色/资源策略的审批入口被吞（既不落 pending 也无审批卡片）。
        与权限管道 ask 共用同一 ApprovalStore，保证审批只有一个事实源。
        """
        from bandi.tools.approval import (
            APPROVAL_REQUIRED_PREFIX,
            PendingApproval,
            build_approval_question,
            describe_tool_intent,
            get_approval_store,
            params_digest,
        )

        store = get_approval_store()
        sid = (self._session_meta or {}).get("session_id") or self._agent_id
        digest = params_digest(dict(tc.arguments))
        store.set_pending(
            sid,
            PendingApproval(
                tool_call_id=tc.id,
                tool_name=tc.name,
                params_digest=digest,
                reason=reason,
                # 服务端不可变参数快照
                params=dict(tc.arguments),
            ),
        )
        await self._emit_event(
            EventType.APPROVAL_REQUESTED,
            {
                "tool_name": tc.name,
                "tool_call_id": tc.id,
                "params_digest": digest,
                "reason": reason,
                "session_id": sid,
                "decided_by": "policy_ask",
            },
        )
        question = build_approval_question(
            tool_call_id=tc.id,
            tool_name=tc.name,
            reason=reason,
            detail=describe_tool_intent(tc.name, dict(tc.arguments)),
        )
        return ToolResult(
            tool_call_id=tc.id,
            tool_name=tc.name,
            success=False,
            output="",
            error=f"{APPROVAL_REQUIRED_PREFIX}{json.dumps(question, ensure_ascii=False)}",
        )

    def _resume_enabled(self, tool: BaseTool, tc: ToolCall | None = None) -> bool:
        # 功能：工具是否声明跨进程恢复（retry_policy.cross_process_resume=True）；
        # （write 的 append 模式非法幂等——同参重放会吞真实追加）
        rp = getattr(tool, "retry_policy", None)
        if not (rp is not None and getattr(rp, "cross_process_resume", False)):
            return False
        checker = getattr(tool, "resume_eligible", None)
        if checker is not None and tc is not None:
            try:
                return bool(checker(tc.arguments))
            except Exception:  # noqa: BLE001 — 钩子异常按不可恢复处理（fail-closed）
                return False
        return True

    def _log_effect(
        self, tool: BaseTool, tc: ToolCall, idem_key: str, status: str, error: str = ""
    ) -> None:
        # 功能：副作用日志——记录本次调用的步骤键与终态，供崩溃后对齐执行进度；
        # 无 session_id（CLI/单测裸路径）不记，避免污染生产库
        meta = self._session_meta or {}
        session_id = str(meta.get("session_id") or "")
        if not session_id:
            return
        from bandi.tools.effect_log import get_effect_log

        get_effect_log().record(
            session_id=session_id,
            turn_index=int(meta.get("turn_index") or 0),
            step_key=idem_key,
            tool_name=tc.name,
            args_hash=self._param_signature(tc),
            read_only=bool(getattr(tool, "is_read_only", False)),
            status=status,
            error=error,
        )

    def _idempotency_key_for(self, tool: BaseTool, tc: ToolCall) -> str:
        # 功能：幂等键两段式——cross_process_resume 工具用确定性 key（tool+args+项目根哈希
        # 跨进程可重算、跨运行环境隔离）；其余用调用实例级 uuid（新调用新 key，防外部去重吞真实再次执行）
        if self._resume_enabled(tool, tc):
            arg_hash = json.dumps(tc.arguments, sort_keys=True, default=str)
            scope = self._project_root or ""
            # 缺维度会被幂等存储判重放返回旧结果（假成功）；同会话跨进程仍确定性
            sid = (self._session_meta or {}).get("session_id") or ""
            return hashlib.sha256(f"{tc.name}:{sid}:{scope}:{arg_hash}".encode()).hexdigest()[:16]
        key = self._idem_keys.get(tc.id) or uuid.uuid4().hex
        self._idem_keys[tc.id] = key
        return key

    @staticmethod
    def _param_signature(tc: ToolCall) -> str:
        """功能：工具调用参数指纹（工具名+参数 JSON SHA256 前缀），失败阶梯的计数键。"""
        try:
            raw = json.dumps(tc.arguments or {}, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = repr(tc.arguments)
        return f"{tc.name}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    def _employee_workspace_auto_allow(self, tc: ToolCall) -> bool:
        """功能：员工工作区免审批判定——员工会话（role-session）内文件写入/bash 且
        目标全落在本 run 工作区（project_root）内、无敏感路径与危险命令特征。"""
        sid = str((self._session_meta or {}).get("session_id") or "")
        if not sid.startswith("role-session::"):
            return False
        root = str(self._project_root or "").strip()
        if not root:
            return False
        name = str(tc.name or "")
        if name in _EMPLOYEE_AUTO_ALLOW_FILE_TOOLS:
            return self._workspace_paths_allowed(tc.arguments or {}, root)
        if name == "bash":
            return self._workspace_bash_allowed(tc.arguments or {}, root)
        return False

    def _workspace_paths_allowed(self, arguments: dict[str, Any], root: str) -> bool:
        """功能：写入参数中的目标路径必须全部落在工作区内且非敏感（缺路径 fail-closed）。"""
        from bandi.tools.fs_tool import sensitive_path_reason

        values = [
            str(arguments.get(key) or "").strip()
            for key in _EMPLOYEE_AUTO_ALLOW_PATH_KEYS
            if str(arguments.get(key) or "").strip()
        ]
        if not values:
            return False
        try:
            root_path = Path(root).expanduser().resolve()
        except (OSError, ValueError):
            return False
        for value in values:
            try:
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = root_path / path
                target = path.resolve()
            except (OSError, ValueError):
                return False
            if not target.is_relative_to(root_path):
                return False
            if sensitive_path_reason(target):
                return False
        return True

    def _workspace_bash_allowed(self, arguments: dict[str, Any], root: str) -> bool:
        """功能：bash 免审批判定——无危险命令特征、无越界绝对路径/上级目录穿越、
        非敏感路径（任一存疑 fail-closed 回审批）。"""
        from bandi.tools.fs_tool import sensitive_path_reason

        command = str(arguments.get("command") or arguments.get("cmd") or "").strip()
        if not command:
            return False
        lowered = command.lower()
        for pattern in _DANGEROUS_COMMAND_PATTERNS:
            if re.search(pattern, lowered):
                return False
        if re.search(r"(^|[\s\"'(])\.\.([\\/]|$|[\s\"')])", command):
            return False
        # 变量展开路径无法静态校验目录边界（$env:/$HOME/%VAR%），一律回审批。
        if re.search(r"\$env:|\$home|\$\{?[A-Za-z_]|%[A-Za-z_][A-Za-z0-9_]*%", command, re.I):
            return False
        # 裸敏感文件名（.env/id_rsa 等）无路径分隔符，也必须回审批。
        if re.search(
            r"(^|[\s\"'=])(\.env|\.npmrc|\.netrc|id_rsa|id_ed25519|id_ecdsa|credentials|secrets)(\b|$)",
            command,
            re.I,
        ):
            return False
        try:
            root_path = Path(root).expanduser().resolve()
        except (OSError, ValueError):
            return False
        for token in re.findall(r"[A-Za-z]:[\\/][^\s\"'|;&]+|~[^\s\"'|;&]*|/[^\s\"'|;&]+", command):
            try:
                path = Path(token).expanduser()
                target = path.resolve() if path.is_absolute() else (root_path / path).resolve()
            except (OSError, ValueError):
                return False
            if not target.is_relative_to(root_path):
                return False
            if sensitive_path_reason(target):
                return False
        return True

    def _remote_sensitive_path_deny(self, tool: BaseTool, arguments: dict[str, Any]) -> str | None:
        """功能：-② MCP 远端工具敏感路径本地把关——参数中路径形态值（含嵌套
        dict/list，file:// URI 先归一化为本地路径）经 project_root（缺省 workspace）
        解析后命中敏感路径即拒绝（fail-closed）；仅作用于 mcp_remote 标记工具。"""
        if not getattr(tool, "mcp_remote", False):
            return None
        from bandi.tools.fs_tool import sensitive_path_reason, workspace_root

        base = Path(self._project_root) if self._project_root else workspace_root()
        for raw in _iter_string_values(arguments):
            candidate = _path_candidate(raw.strip())
            if candidate is None:
                continue
            try:
                path = Path(candidate).expanduser()
                if not path.is_absolute():
                    path = base / path
                target = path.resolve()
            except (OSError, ValueError):
                continue
            reason = sensitive_path_reason(target)
            if reason:
                return f"denied: {reason} (remote tool path policy, G2)"
        return None

    async def _resolve_blob_arguments(
        self, arguments: dict[str, Any], context: ToolPermissionContext
    ) -> dict[str, Any]:
        """收口：blob 引用参数还原——`blob:<id>` 值经 resolve_blob_text
        读取真实内容（读后即焚），替换后返回新 arguments；非引用键原样保留。"""
        from bandi.tools.util.tool_argument_blob import resolve_blob_text

        out: dict[str, Any] = {}
        for k, v in arguments.items():
            if isinstance(v, str) and v.startswith("blob:"):
                out[k] = await resolve_blob_text(context, v[len("blob:") :])
            else:
                out[k] = v
        return out

    async def _execute_one(self, tc: ToolCall) -> ToolResult:
        tool = self._registry.get(tc.name)

        # 连续失败检测：超出阈值后**暂停该工具**（不再执行，失败结果与指引回喂 LLM——
        # 反复调用（≥ _BLOCKED_RETRY_LIMIT 次）才 should_abort 终止，防死循环烧钱）
        if self._failure_count.get(tc.name, 0) >= self._consecutive_failure_threshold:
            self._block_count[tc.name] = self._block_count.get(tc.name, 0) + 1
            blocked_hits = self._block_count[tc.name]
            hard_stop = blocked_hits >= _BLOCKED_RETRY_LIMIT
            error = (
                f"工具 {tc.name} 在被暂停后仍被反复调用（第 {blocked_hits} 次）——已中止本轮。"
                if hard_stop
                else (
                    f"工具 {tc.name} 已连续失败 {self._failure_count.get(tc.name, 0)} 次，"
                    "本轮已暂停该工具（不再执行）；请立即改用其他工具完成目标，"
                    "或在无法完成时总结现状（不要重复调用本工具）。"
                )
            )
            logger.warning(
                "tool %s paused after %d consecutive failures (blocked retries: %d)",
                tc.name,
                self._failure_count.get(tc.name, 0),
                blocked_hits,
            )
            await self._emit_event(
                EventType.TOOL_ERROR,
                {
                    "tool_name": tc.name,
                    "tool_call_id": tc.id,
                    "error_message": error,
                    "should_abort": hard_stop,
                },
            )
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error=error,
                should_abort=hard_stop,
            )

        if tool is None:
            self._record_failure(tc.name)
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error=(
                    f"unknown tool: {tc.name}"
                    "\n[失败引导] 该工具不存在。请改用其他可用工具，或直接回答用户；"
                    "不要重复调用不存在的工具。"
                ),
            )

        # 模式闸门前置：模式不可用的报错优先于参数校验失败——模型须先看到模式限制
        mode_verdict = self._permissions_pipeline.check_mode_available(tc, tool, self._mode)
        if not mode_verdict.allow:
            logger.warning("tool %s blocked by mode gate: %s", tc.name, mode_verdict.reason)
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error=(
                    f"{mode_verdict.reason}"
                    "\n[失败引导] 当前模式下该工具不可用。请改用当前模式可用的工具，"
                    "并向用户说明当前模式限制；不要重复调用本工具。"
                ),
            )

        # 归一化后的参数同步用于后续幂等 key/工具执行；非 BaseTool（第三方/MCP）
        # 实现无 normalize_params 时跳过
        normalizer = getattr(tool, "normalize_params", None)
        if normalizer is not None:
            normalized_args = normalizer(tc.arguments)
            if normalized_args is not tc.arguments:
                tc.arguments = normalized_args

        param_errors = tool.validate_params(tc.arguments)
        if param_errors:
            logger.warning("tool %s param validation failed: %s", tc.name, param_errors)
            base = f"参数校验失败: {'; '.join(param_errors)}"
            clarify = (
                "若缺失信息无法从上下文确定，必须先调用 request_user_input "
                "向用户澄清后再发起调用，禁止编造参数值。"
            )
            sig = self._param_signature(tc)
            fail_n = self._param_fail_counts.get(sig, 0) + 1
            self._param_fail_counts[sig] = fail_n
            if fail_n >= 3:
                error = (
                    f"{base}\n[失败终止] 同一调用已连续 {fail_n} 次参数校验失败："
                    "不要再调用该工具，直接向用户说明缺失的信息。"
                )
            elif fail_n == 2:
                error = (
                    f"{base}\n[失败引导] 已第 {fail_n} 次校验失败："
                    f"禁止再次以相同参数调用；{clarify}"
                )
            else:
                error = f"{base}\n[失败引导] {clarify}"
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error=error,
            )
        # 参数校验通过即清零同签名失败阶梯（模型已产出合法参数 = 恢复信号）
        self._param_fail_counts.pop(self._param_signature(tc), None)

        # -②：MCP 远端工具敏感路径本地剔除——远端参数名无统一约定
        # （filename/directory 等不在通用路径参数元组），对全部顶层字符串值做扫描
        remote_deny = self._remote_sensitive_path_deny(tool, tc.arguments)
        if remote_deny is not None:
            logger.warning("remote tool %s denied: %s", tc.name, remote_deny)
            return ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=False,
                output="",
                error=remote_deny,
            )

        # 审批授权消费（人工/机审放行后 re-issue）：命中即写权限确认缓存并放行
        # 不再重入 ask 门与政策 ASK（ApprovalStore=授权单一事实源）。
        granted = self._consume_approval_grant(tc)
        receipt_key = ""
        if granted and self._approval_once_granted(tc):
            # 同 approval 二次执行不得重放副作用——命中回执直接返回原结果。
            # 仅 once 授权适用：session 授权的重复调用应真实执行。
            receipt_key = self._approval_receipt_key(tc)
            prior = self._approval_receipt_get(receipt_key)
            if prior is not None:
                prior.tool_call_id = tc.id
                logger.info("tool %s served from approval receipt (key=%s)", tc.name, receipt_key)
                return prior
        if not granted:
            gate = await self._approval_gate(tc, tool)
            if gate is not None:
                return gate
            # 交互式审批（UI 卡）/机审在闸门内写入 grant——重新消费一次，
            # 使本次「首次执行」也落回执（覆盖执行成功但结果未送达的重放场景）。
            # 仅 once 授权参与回执；session 授权的重复执行语义不变。
            granted = self._consume_approval_grant(tc)
            if granted and self._approval_once_granted(tc):
                receipt_key = self._approval_receipt_key(tc)
                prior = self._approval_receipt_get(receipt_key)
                if prior is not None:
                    prior.tool_call_id = tc.id
                    return prior

        if self._policy_engine is not None:
            resource = Resource(
                type=tool.resource_type,
                name=tc.name,
                # 原硬编码 "execute" 使角色策略 read/write/delete 分支全为死档
                action=self._permission_action_for(tool),
                attributes={"arguments": tc.arguments, "tool_obj": tool},
            )
            pctx = PermissionContext(
                user_id=self._effective_user_id,
                role=self._agent_role,
                mode=self._mode,
                scope=self._agent_scope,
            )
            decision = self._policy_engine.check(resource, pctx)
            if decision.decision == PermissionDecision.ASK and not granted:
                # 已授权（granted）的 re-issue 不重入审批
                return await self._enqueue_approval(
                    tc,
                    decision.reason or f"policy requires approval: {tc.name}",
                )
            if decision.decision == PermissionDecision.DENY:
                return ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    success=False,
                    output="",
                    error=decision.reason or f"denied by policy: {tc.name}",
                )

        # 权限策略变更后缓存结果绕过新策略。此处仅当 `_pipeline is None` 时
        # 直接查缓存；有 pipeline 时缓存命中下沉到 run_tool 回调（权限中间件之后）
        if self._pipeline is not None:
            ctx = ToolContext(
                user_id=self._effective_user_id,
                agent_id=self._agent_id,
                role=self._agent_role,
                scope=self._agent_scope,
                mode=self._mode,
                extra={
                    "tool": tool,
                    "permissions_pipeline": self._permissions_pipeline,
                    "tool_call_id": tc.id,
                    "event_bus": getattr(self, "_event_bus", None),
                    "session_id": (getattr(self, "_session_meta", None) or {}).get(
                        "session_id", ""
                    ),
                },
            )

            async def run_tool(_ctx: ToolContext, _call: ToolCall) -> ToolResult:
                cached = await self._cached_result(tc, tool)
                if cached is not None:
                    return cached
                return await self._run_with_single_flight(tc, tool, receipt_key)

            result = await self._pipeline.execute(ctx, tc, run_tool)
            # 与 _fail_result 同口径发 TOOL_ERROR 事件——拒绝可观测（原静默返回）
            if not result.success and (result.meta or {}).get("middleware_blocked"):
                await self._emit_event(
                    EventType.TOOL_ERROR,
                    {
                        "tool_name": tc.name,
                        "tool_call_id": tc.id,
                        "error_message": result.error or "blocked by middleware",
                        "should_abort": bool(result.should_abort),
                    },
                )
            return _with_network_next_step(result)
        else:
            # 无 Pipeline 时走原有权限检查+执行路径
            verdict = await self._permissions_pipeline.check(
                tc,
                tool,
                self._mode,
                agent_id=self._agent_id,
                role=self._agent_role,
                scope=self._agent_scope,
            )
            if not verdict.allow:
                return ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    success=False,
                    output="",
                    error=verdict.reason or f"permission denied: {tc.name}",
                )
            cached = await self._cached_result(tc, tool)
            if cached is not None:
                return cached
            return await self._run_with_single_flight(tc, tool, receipt_key)

    async def _cached_result(self, tc: ToolCall, tool: BaseTool) -> ToolResult | None:
        if self._result_cache is None or tool.cache_ttl <= 0:
            return None
        cache_key = self._result_cache.build_key(
            tc.name, tc.arguments, scope=self._cache_scope_for(tool)
        )
        cached = self._result_cache.get(cache_key)
        if cached is None:
            return None
        return await self._as_cache_hit(tc, cached)

    async def _as_cache_hit(self, tc: ToolCall, cached: ToolResult) -> ToolResult:
        """功能：把缓存结果按「命中」语义落到本调用——重映射 tool_call_id + 补发事件。

        修复：等待者复用赢家结果时保留 meta（如 error_class）与 should_abort。
        修复：失败命中与赢家 `_fail_result` 同口径发 TOOL_ERROR（带 error_message）
        ——否则等待者失败不进防篡改审计且 UI 看不到原因；成功命中仍发 TOOL_COMPLETED。
        """
        logger.debug("cache hit for tool %s", tc.name)
        output = str(cached.output) if cached.output is not None else ""
        if cached.success:
            output_preview = output[: self._output_preview_chars].replace("\n", " ")
            await self._emit_event(
                EventType.TOOL_COMPLETED,
                {
                    "tool_name": tc.name,
                    "tool_call_id": tc.id,
                    "duration_ms": 0,
                    "success": True,
                    "output_preview": output_preview,
                    "from_cache": True,
                    "citations": _extract_citations(cached.output),
                },
            )
        else:
            await self._emit_event(
                EventType.TOOL_ERROR,
                {
                    "tool_name": tc.name,
                    "tool_call_id": tc.id,
                    "error_message": str(cached.error or ""),
                    "should_abort": bool(cached.should_abort),
                },
            )
        return ToolResult(
            tool_call_id=tc.id,
            tool_name=cached.tool_name,
            success=cached.success,
            output=output,
            error=cached.error,
            duration_ms=0.0,
            should_abort=cached.should_abort,
            meta=dict(cached.meta or {}),
        )

    async def _run_with_single_flight(
        self, tc: ToolCall, tool: BaseTool, receipt_key: str = ""
    ) -> ToolResult:
        """功能：缓存未命中时按 key 单飞执行——同 key 并发只真跑一次，等待者按命中复用。

        只给「可缓存」工具（result_cache 存在 + cache_ttl>0）接单飞；等待者拿到的是
        赢家产物，须经 `_as_cache_hit` 重映射自己的 tool_call_id 后再落账（防串号）。
        receipt_key：审批回执键（空=非审批授权路径，不落回执）。
        """
        if self._result_cache is None or tool.cache_ttl <= 0:
            return await self._run_and_record(tc, tool, receipt_key)
        cache_scope = self._cache_scope_for(tool)
        cache_key = self._result_cache.build_key(tc.name, tc.arguments, scope=cache_scope)
        result, computed = await self._result_cache.get_or_compute_with_status(
            cache_key,
            lambda: self._run_and_record(tc, tool, receipt_key),
            ttl=tool.cache_ttl,
            scope=cache_scope,
        )
        if computed:
            return result
        return await self._as_cache_hit(tc, result)

    async def _invoke_stream(  # 功能：override stream() 的工具——迭代增量进度，中间事件发 TOOL_PROGRESS
        self, tool: BaseTool, tc: ToolCall, kwargs: dict[str, Any]
    ) -> Any:
        output: Any = None
        async for ev in tool.stream(tc.arguments, **kwargs):
            if ev.type == "complete":
                output = ev.data.get("result")
            else:
                await self._emit_event(
                    EventType.TOOL_PROGRESS,
                    {
                        "tool_name": tc.name,
                        "tool_call_id": tc.id,
                        "progress_type": ev.type,
                        "data": ev.data,
                    },
                )
        return output if output is not None else ""

    async def _invoke_or_execute(  # 功能：工具调用统一入口——stream override 走增量，否则走原 execute
        self, tool: BaseTool, tc: ToolCall, kwargs: dict[str, Any]
    ) -> Any:
        if _is_streaming_tool(tool):
            return await self._invoke_stream(tool, tc, kwargs)
        return await tool.execute(tc.arguments, **kwargs)

    def _snapshot_write_targets(self, tc: ToolCall, tool: BaseTool) -> None:
        """功能：高危写操作执行前快照既有目标文件——非只读 + 风险达 MEDIUM 且参数中
        存在已存在的文件目标才触发；快照失败仅告警，绝不阻断执行。"""
        from bandi.safety.write_snapshot import (
            create_snapshot,
            extract_target_paths,
            snapshot_worthy,
        )

        try:
            if not snapshot_worthy(
                tc.name, tc.arguments, bool(getattr(tool, "is_read_only", False))
            ):
                return
            targets = extract_target_paths(tc.arguments, base_dir=self._project_root)
            if targets:
                create_snapshot(tc.name, targets, snapshot_root=self._snapshot_root)
        except Exception:
            logger.warning("write snapshot failed for %s", tc.name, exc_info=True)

    async def _run_and_record(
        self, tc: ToolCall, tool: BaseTool, receipt_key: str = ""
    ) -> ToolResult:
        # 高危写操作执行前快照既有目标文件（best-effort 兜底——失败不阻断执行）
        self._snapshot_write_targets(tc, tool)
        await self._emit_event(
            EventType.TOOL_INVOKED,
            {
                "tool_name": tc.name,
                "tool_call_id": tc.id,
                "is_concurrent": tool.is_concurrency_safe,
                "tool_input": tc.arguments or {},
            },
        )

        start = time_module.time()
        try:
            extra: dict[str, Any] = {}
            extra["read_state"] = self.read_state
            # 工具执行上下文带本调用 id（子代理派生工具据此挂嵌套过程树）
            extra["tool_call_id"] = tc.id
            if self._agent_id:
                extra["agent_id"] = self._agent_id
            if self._project_root:
                extra["project_root"] = self._project_root
            # .6：角色 fs scopes 透传——fs 工具据此窄化落盘白名单
            if self._role_fs_scopes:
                extra["role_fs_scopes"] = list(self._role_fs_scopes)
            if self._skill_scope is not None:
                extra["skill_scope"] = dict(self._skill_scope)
            # .1：角色工具门控名单透传——子代理派生（SubAgentTool）据此继承父角色
            # 门控，否则受限角色可借 subagent_spawn 绕过（None=不过滤；[]=显式全禁）
            if self._role_tool_tags is not None:
                extra["role_tool_tags"] = sorted(self._role_tool_tags)
            # PolicyEngine，否则受限权限档可经一次 spawn 在子代理内绕过硬 DENY
            if self._policy_engine is not None:
                extra["policy_engine"] = self._policy_engine
            # 父 trace 归属透传——子 Agent 派生工具据此串联父子调用链
            trace_id = (self._session_meta or {}).get("trace_id", "")
            if trace_id:
                extra["trace_id"] = trace_id
            # HookBefore/After（否则子代理工具调用绕过用户配置的 PreToolUse deny）
            if self._hook_runner is not None:
                extra["hook_runner"] = self._hook_runner
            if self._runtime_snapshot:
                extra["runtime_snapshot"] = self._runtime_snapshot
            mode_manager = (self._session_meta or {}).get("mode_manager")
            if mode_manager is not None:
                extra["mode_manager"] = mode_manager
            # close_human_review 等需系统授权位的工具据此判定（agent 参数不可伪造）
            if (self._session_meta or {}).get("human_review_close_allowed"):
                extra["human_review_close_allowed"] = True
            if getattr(tool, "needs_context", False):
                extra["session_id"] = (self._session_meta or {}).get(
                    "session_id", ""
                ) or self._agent_id
                extra.setdefault(
                    "project_root",
                    self._project_root or (self._session_meta or {}).get("project_root", ""),
                )
                # memory_search 拿不到 project/employee 维度 → 检索只命中全局层
                # （既功能失效，又是跨 agent 可见的隔离漏口）
                extra.setdefault("project_id", (self._session_meta or {}).get("project_id", ""))
                extra.setdefault("employee_id", (self._session_meta or {}).get("employee_id", ""))
                extra["turn_index"] = int((self._session_meta or {}).get("turn_index") or 0)
                # 员工独立工作区——bash 默认 cwd / 浏览器 profile 的基准。
                # 非员工会话（个人助理）不注入，保持既有行为。
                employee_id = str(extra.get("employee_id") or "")
                if employee_id:
                    from bandi.tools.fs_tool import employee_workspace_root

                    emp_ws = employee_workspace_root(
                        self._project_root or extra.get("project_root") or "",
                        str(extra.get("project_id") or ""),
                        employee_id,
                    )
                    if emp_ws is not None:
                        extra["employee_workspace"] = str(emp_ws)
            blob_dir = (self._session_meta or {}).get("blob_data_dir")
            if blob_dir:
                extra["blob_data_dir"] = blob_dir
            # 其余调用实例级 uuid（重试复用、新批新 key，防外部去重吞真实再次执行）
            idem_key = self._idempotency_key_for(tool, tc)
            extra["idempotency_key"] = idem_key
            self._log_effect(tool, tc, idem_key, "started")
            # 跨进程恢复：声明工具先查持久化存储——进程 A 已执行成功，进程 B 命中
            # 直接返回持久化结果，不重新执行（防副作用重放）
            if self._idem_store is not None and self._resume_enabled(tool, tc):
                prior = self._idem_store.get(idem_key)
                if prior is not None:
                    prior.tool_call_id = tc.id
                    self._log_effect(tool, tc, idem_key, "replayed")
                    logger.info(
                        "tool %s resumed from idempotency store (key=%s)", tc.name, idem_key
                    )
                    return prior
            ctx = ToolPermissionContext(
                user_id=self._effective_user_id,
                role=self._agent_role,
                mode=self._mode,
                permissions_pipeline=self._permissions_pipeline,
                communication=self._communication,
                extra=extra,
                event_bus=getattr(self, "_event_bus", None),
            )
            # （读后即焚），下游工具不再看到引用占位；无 blob 引用零开销直通
            if any(isinstance(v, str) and v.startswith("blob:") for v in tc.arguments.values()):
                # 还原上下文补全 session/blob 目录（不受 needs_context 门控影响——
                # 写读两侧必须同 session 目录，读后即焚语义才成立）
                from dataclasses import replace as _dc_replace

                restore_ctx = _dc_replace(
                    ctx,
                    extra={
                        **ctx.extra,
                        "session_id": (self._session_meta or {}).get("session_id", "")
                        or self._agent_id,
                        **({"blob_data_dir": blob_dir} if blob_dir else {}),
                    },
                )
                tc.arguments = await self._resolve_blob_arguments(tc.arguments, restore_ctx)
            kwargs = (
                {"context": ctx} if "context" in inspect.signature(tool.execute).parameters else {}
            )
            controller = self._abort_controller
            if controller is not None and controller.should_abort:
                return await self._fail_result(
                    tc,
                    f"aborted: {controller.reason or 'user_interrupt'}",
                    (time_module.time() - start) * _MS_PER_SEC,
                    error_class="cancelled",
                )
            if getattr(tool, "cancellable", False) and controller is not None:
                exec_task = asyncio.create_task(self._invoke_or_execute(tool, tc, kwargs))
                abort_task = asyncio.create_task(controller.wait())
                done, pending = await asyncio.wait(
                    {exec_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if abort_task in done and exec_task not in done:
                    return await self._fail_result(
                        tc,
                        f"aborted: {controller.reason or 'user_interrupt'}",
                        (time_module.time() - start) * _MS_PER_SEC,
                        error_class="cancelled",
                    )
                output = exec_task.result()
            else:
                # 功能：交互工具（向用户提问）不设执行超时——等答复由交互 TTL 兜底
                tool_timeout = (
                    None if tool.name in _INTERACTIVE_TOOL_NAMES else self._execution_timeout
                )
                output = await asyncio.wait_for(
                    self._invoke_or_execute(tool, tc, kwargs), timeout=tool_timeout
                )
            duration = (time_module.time() - start) * _MS_PER_SEC

            # 按失败处理，防假成功回填污染对话上下文；保留原始 payload 供模型观察。
            dict_error = _extract_dict_error(output)
            if dict_error is not None:
                return await self._fail_result(
                    tc, dict_error, duration, output=str(output), error_class="non_retryable"
                )

            output_str = str(output) if output is not None else ""

            self._reset_failure(tc.name)

            limit_chars = self._resolve_max_output_tokens(tc.name, tool) * self._chars_per_token
            if len(output_str) > limit_chars:
                summary = await summarize_tool_output(
                    output_str,
                    tc.name,
                    llm=self._summary_llm,
                    session_id=(self._session_meta or {}).get("session_id") or self._agent_id,
                )
                persisted = self._persist_output(tc, output_str)
                final_output = summary
                if persisted:
                    final_output += (
                        f"\n\n[full output persisted: {persisted} — use read with offset/limit]"
                    )
            else:
                final_output = output_str
            # 置于截断之后保证边界标记完整
            final_output, _ = self._wrap_external(tool, final_output, "")
            result = ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                success=True,
                output=final_output,
                duration_ms=duration,
            )
            if receipt_key:
                # 审批授权路径执行成功后落回执；网络失败/重开页面后的
                # re-issue 命中同一 key，直接返回本结果而不重跑副作用。
                self._approval_receipt_set(receipt_key, result)
            if self._result_cache is not None and tool.cache_ttl > 0:
                # 冗余双写幂等：单飞路径已写过一次，此处直写兜底（见 cache.set 注释）
                cache_scope = self._cache_scope_for(tool)
                cache_key = self._result_cache.build_key(tc.name, tc.arguments, scope=cache_scope)
                self._result_cache.set(cache_key, result, ttl=tool.cache_ttl, scope=cache_scope)
            # 跨进程恢复：成功结果写入幂等存储（进程 B 同 key 恢复时直接复用）
            if self._idem_store is not None and self._resume_enabled(tool, tc):
                self._idem_store.set(idem_key, result)
            self._log_effect(tool, tc, idem_key, "succeeded")

            output_preview = (output_str or "")[: self._output_preview_chars].replace("\n", " ")
            await self._emit_event(
                EventType.TOOL_COMPLETED,
                {
                    "tool_name": tc.name,
                    "tool_call_id": tc.id,
                    "duration_ms": duration,
                    "success": True,
                    "output_preview": output_preview,
                    "citations": _extract_citations(output),
                },
            )
            # 功能：技能实际加载时上报（Skill 使用组数据源）——F 模式只在此触发
            if tc.name == "skill_invoke" and isinstance(output, dict):
                loaded_skill = str(output.get("name") or "").strip()
                if loaded_skill and output.get("success") is not False:
                    await self._emit_event(
                        EventType.SKILL_INJECTED,
                        {"skills": [loaded_skill], "tool_call_id": tc.id},
                    )
            return result
        except NonRetryableToolError as e:
            logger.debug("tool %s non-retryable failure: %s", tc.name, e)
            duration = (time_module.time() - start) * _MS_PER_SEC
            return await self._fail_result(
                tc, _error_text(e), duration, error_class="non_retryable"
            )
        except ToolAbortedError as e:
            logger.info("tool %s aborted: %s", tc.name, e)
            duration = (time_module.time() - start) * _MS_PER_SEC
            result = await self._fail_result(tc, _error_text(e), duration, error_class="aborted")
            result.should_abort = True
            return result
        except TimeoutError as e:
            # 决定（只读/显式可重试工具放行，其余禁重试）——传参超时不再落通用异常
            logger.debug("tool %s timeout: %s", tc.name, e)
            duration = (time_module.time() - start) * _MS_PER_SEC
            return await self._fail_result(tc, _error_text(e), duration, error_class="timeout")
        except Exception as e:  # noqa: BLE001
            logger.debug("tool %s execution failed: %s", tc.name, e)
            duration = (time_module.time() - start) * _MS_PER_SEC
            return await self._fail_result(tc, _error_text(e), duration)

    async def _fail_result(
        self,
        tc: ToolCall,
        error: str,
        duration: float,
        output: str = "",
        error_class: str | None = None,
    ) -> ToolResult:
        # 功能：构造失败 ToolResult（连续失败记账 + TOOL_ERROR 事件 + 中止判定 +
        if len(error) > _ERROR_MAX_CHARS:
            error = _clip_head_tail(error, _ERROR_MAX_CHARS)
        failed_tool = self._registry.get(tc.name)
        if output:
            # （per_tool_max_output_tokens / MCP output_budget 同样生效）
            limit_chars = (
                self._resolve_max_output_tokens(tc.name, failed_tool) * self._chars_per_token
                if failed_tool is not None
                else self._max_output_chars
            )
            truncated_output, full_output = self._truncate_output(output, limit_chars)
            if truncated_output is not None:
                persisted = self._persist_output(tc, full_output)
                if persisted:
                    hint = f"\n[full output persisted: {persisted} — use read with offset/limit]"
                    # 提示计入输出预算：重截断体腾出提示空间，追加后总长仍 ≤ 预算
                    body, _ = self._truncate_output(output, max(limit_chars - len(hint), 0))
                    output = (body if body is not None else truncated_output) + hint
                else:
                    output = truncated_output
        # 工具的 error/output 同样清洗 + 边界包装（远端 MCP error 文本不得裸奔）
        output, error = self._wrap_external(failed_tool, output, error)
        self._record_failure(tc.name)
        error = self._compose_failure_text(tc.name, error, _ERROR_MAX_CHARS)
        # -3 修复：失败回喂 LLM（不再因失败计数终止整轮）——终止仅发生在
        # 「工具被暂停后仍被反复调用」的硬撞路径（见 _execute_one 前置拦截）
        should_abort = False
        meta: dict[str, Any] = {"error_class": error_class} if error_class else {}
        result = ToolResult(
            tool_call_id=tc.id,
            tool_name=tc.name,
            success=False,
            output=output,
            error=error,
            duration_ms=duration,
            should_abort=should_abort,
            meta=meta,
        )
        await self._emit_event(
            EventType.TOOL_ERROR,
            {
                "tool_name": tc.name,
                "tool_call_id": tc.id,
                "error_message": error,
                "should_abort": should_abort,
            },
        )
        return result

    def _wrap_external(self, tool: BaseTool | None, output: str, error: str) -> tuple[str, str]:
        """功能：6.14 外部内容统一出口包装——external_content=True 工具的
        output/error 均做特殊 token/伪造边界清洗 + 随机 id 边界包裹；成功
        dict-error、异常三类出口共用同一 helper（空文本无内容可隔离，原样返回）。"""
        if tool is None or not getattr(tool, "external_content", False):
            return output, error
        from bandi.safety.external_content import wrap_external_content

        if output:
            output = wrap_external_content(output, source=tool.name)
        if error:
            error = wrap_external_content(error, source=tool.name)
        return output, error

    def _partition(self, calls: list[ToolCall]) -> tuple[list[ToolCall], list[ToolCall]]:
        concurrent, serial = [], []
        for call in calls:
            tool = self._registry.get(call.name)
            if tool and tool.is_concurrency_safe:
                concurrent.append(call)
            else:
                serial.append(call)
        return concurrent, serial

    def _segments(self, calls: list[ToolCall]) -> list[tuple[bool, list[ToolCall]]]:
        # 功能：按声明序把批切成连续同类段（并发段/串行段）——保住「写后读」次序
        # 段内并发或串行、段间严格顺序执行（正确性优先于最大并发度）。
        segments: list[tuple[bool, list[ToolCall]]] = []
        for call in calls:
            tool = self._registry.get(call.name)
            is_concurrent = bool(tool and tool.is_concurrency_safe)
            if segments and segments[-1][0] == is_concurrent:
                segments[-1][1].append(call)
            else:
                segments.append((is_concurrent, [call]))
        return segments

    def _resolve_max_output_tokens(self, tool_name: str, tool: BaseTool) -> int:
        override = self._per_tool_max_output_tokens.get(tool_name)
        if override:
            return override
        budget = getattr(tool, "output_budget", None)
        if isinstance(budget, int) and budget > 0:
            return budget
        attr = getattr(tool, "max_result_tokens", None)
        if attr:
            return attr
        return self._max_output_chars // self._chars_per_token

    def _truncate_output(self, output: str, max_chars: int | None = None) -> tuple[str | None, str]:
        # 功能：超长截断——头 60% + 尾 40% 保留（尾部根因不丢弃）；省略标注计入
        # max_chars，返回总长严格 ≤ max_chars
        limit = max_chars if max_chars is not None else self._max_output_chars
        if len(output) <= limit:
            return None, output
        marker = f"\n...[{len(output) - limit} chars omitted]...\n"
        body = max(limit - len(marker), 0)
        head_len = int(body * 0.6)
        tail_len = body - head_len
        tail = output[len(output) - tail_len :] if tail_len else ""
        truncated = output[:head_len] + marker + tail
        return truncated, output

    def _output_dir(self) -> Path:
        # 功能：工具输出落盘目录（project_root 内 .bandi/tool_outputs 优先——read
        # 允许根内可读回；无 project_root 回退 temp_dir）
        if getattr(self, "_project_root", None):
            try:
                return Path(self._project_root) / ".bandi" / "tool_outputs"
            except TypeError:
                pass
        return self._temp_dir

    def _persist_output(self, tc: ToolCall, full_output: str) -> str | None:
        # 功能：全文落盘并返回路径（路径回传模型；失败返回 None 不阻断）。
        global _TOOL_OUTPUT_SWEPT
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        filename = f"tool_output_{tc.name}_{ts}_{suffix}.txt"
        base_dir = self._output_dir()
        dump_path = base_dir / filename
        try:
            from bandi.safety.pii_detector import sanitize_for_persist

            base_dir.mkdir(parents=True, exist_ok=True)
            # 磁盘全文也不暴露敏感值；模型读回取回通道不受影响——值是占位符）
            dump_path.write_text(sanitize_for_persist(full_output), encoding="utf-8")
            logger.info("full tool output saved to %s", dump_path)
            if not _TOOL_OUTPUT_SWEPT:
                _TOOL_OUTPUT_SWEPT = True
                cleanup_stale_tool_outputs(self._temp_dir)
                if base_dir != self._temp_dir:
                    cleanup_stale_tool_outputs(base_dir)
            return str(dump_path)
        except OSError as e:
            logger.warning("failed to persist tool output: %s", e)
            return None

    def _persist_output_name(self, tool_name: str, full_output: str) -> str | None:
        # 功能：按工具名落盘（聚合帽复用——_persist_output 仅消费 tc.name）
        from types import SimpleNamespace

        return self._persist_output(SimpleNamespace(name=tool_name), full_output)

    def persist_folded_output(self, tool_name: str, content: str) -> str | None:
        """功能：折叠视图原文落盘——内容寻址命名（同内容只落一次
        幂等），与工具输出同目录同 TTL（tool_output_* 清理覆盖）；写前过落盘
        PII 脱敏。失败返回 None（调用方回退「重新调用」出路）。"""
        import hashlib

        from bandi.safety.pii_detector import sanitize_for_persist

        digest = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()[:12]
        base_dir = self._output_dir()
        dump_path = base_dir / f"tool_output_fold_{tool_name}_{digest}.txt"
        if dump_path.exists():
            return str(dump_path)
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            dump_path.write_text(sanitize_for_persist(content), encoding="utf-8")
            logger.info("folded tool output saved to %s", dump_path)
            return str(dump_path)
        except OSError as e:
            logger.warning("failed to persist folded tool output: %s", e)
            return None

    # 结果落盘为头尾预览，非均摊截断；小结果保完整）；读类豁免（其语义为按需分页读）
    _BATCH_OUTPUT_MAX_CHARS = 50000
    _BATCH_PREVIEW_CHARS = 2000
    _BATCH_EXEMPT_TOOLS = frozenset({"read", "grep", "glob"})

    def _enforce_batch_output_budget(self, results: list[ToolResult]) -> None:
        """功能：单轮工具结果聚合帽：超帽时按输出长度降序把最大且非豁免的结果
        落盘、上下文替换为头尾预览+完整路径（内容不丢，模型可按路径续读）。"""
        total = sum(len(r.output or "") for r in results)
        if total <= self._BATCH_OUTPUT_MAX_CHARS:
            return
        for r in sorted(results, key=lambda x: len(x.output or ""), reverse=True):
            if total <= self._BATCH_OUTPUT_MAX_CHARS:
                break
            if r.tool_name in self._BATCH_EXEMPT_TOOLS or not (r.output or ""):
                continue
            preview, full = self._truncate_output(r.output, self._BATCH_PREVIEW_CHARS)
            if preview is None:
                continue
            path = self._persist_output_name(r.tool_name, full)
            hint = f"\n[完整输出: {path}]" if path else ""
            total -= len(r.output) - len(preview)
            r.output = preview + hint

    def _append_failure_guidance(self, tool_name: str, error: str) -> str:
        """功能：按连续失败次数给换路引导——首次失败即提示核对参数/改用其他工具，
        第二次预告暂停，达阈值明示已暂停。此前引导只在暂停后才注入，前两次失败是
        零引导区，模型在无引导下会反复用相同参数重试同一工具。"""
        lowered = error.lower()
        scope_markers = (
            "outside role fs scopes",
            "access denied",
            "sensitive path rejected",
            "control-plane path rejected",
            "sensitive file type rejected",
        )
        if any(marker in lowered for marker in scope_markers):
            return (
                error + "\n[路径限制] 该路径不在当前角色工作区，重试同一路径不会成功。"
                "请直接使用本轮附件，或在员工工作区内选择文件；不要再次读取被拒绝的路径。"
            )
        count = self._failure_count.get(tool_name, 0)
        if count <= 0:
            return error
        if count == 1:
            hint = (
                f"\n[失败引导] 工具 {tool_name} 首次失败。先核对参数与路径是否正确；"
                "若确认无误，请改用其他工具或换一条路径完成目标，不要用相同参数重试。"
            )
        elif count == 2:
            hint = (
                f"\n[失败引导] 工具 {tool_name} 已连续失败 2 次。请立即改用其他工具或换路径；"
                "再失败一次该工具将被暂停。"
            )
        else:
            hint = (
                f"\n[失败引导] 工具 {tool_name} 已连续失败 {count} 次，达到暂停阈值；"
                "本轮该工具已被暂停，请立即改用其他工具完成目标，或在无法完成时总结现状。"
            )
        return error + hint

    def _compose_failure_text(self, tool_name: str, error: str, limit: int) -> str:
        """功能：拼接失败正文与换路引导，且总量不超过上限。

        引导是拼接在正文之后的——若先截断正文再拼，最终消息会越界
        （正文顶到上限 + 引导几十字）。这里给引导预留预算，正文按剩余额度
        截断，保证成品长度恒 ≤ limit。
        """
        guidance = self._append_failure_guidance(tool_name, "")
        if not guidance:
            return error[:limit]
        budget = max(0, limit - len(guidance))
        if len(error) <= budget:
            return error + guidance
        # 截断标记本身也占额度，否则成品仍会越界
        marker_budget = 48
        body = error[: max(0, budget - marker_budget)]
        marker = f"…（已截断，原长度 {len(error)} 字符）"
        if len(marker) > marker_budget:
            marker = "…（已截断）"
        return body + marker + guidance

    def _record_failure(self, tool_name: str) -> None:  # 功能：递增工具失败计数
        from bandi.tools.retry_state import RETRY_IN_FLIGHT

        if RETRY_IN_FLIGHT.get():
            return
        self._failure_count[tool_name] = self._failure_count.get(tool_name, 0) + 1
        logger.warning("tool %s failure count: %d", tool_name, self._failure_count[tool_name])

    def _reset_failure(self, tool_name: str) -> None:  # 功能：工具执行成功后重置失败计数
        self._failure_count.pop(tool_name, None)
        self._block_count.pop(tool_name, None)

    async def _emit_event(
        self, event_type: EventType, data: dict
    ) -> None:  # 功能：发射 EventBus 事件（TOOL_INVOKED/COMPLETED/ERROR/审批事件）
        # getattr 防御：测试替身可能 monkeypatch 掉 __init__（实例无任何属性）
        # 事件发射失败不得阻断审批/工具主链路
        event_bus = getattr(self, "_event_bus", None)
        if event_bus is None:
            return
        session_id = (getattr(self, "_session_meta", None) or {}).get("session_id", "")
        data = {"session_id": session_id, **data}
        await event_bus.emit(Event(type=event_type, data=data))
