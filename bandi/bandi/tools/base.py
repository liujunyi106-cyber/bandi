# 功能：工具抽象基类（BaseTool 声明式元数据 + 权限上下文）
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker


class ToolCategory(StrEnum):
    DOC = "doc"
    WEB = "web"
    DATA = "data"
    GEN = "gen"
    IMAGE = "image"
    MEDIA = "media"
    CHART = "chart"
    INTEGRATION = "integration"
    UTIL = "util"
    CANVAS = "canvas"
    WORKFLOW = "workflow"
    CODE = "code"
    SKILLS = "skills"


class ToolCapability(StrEnum):
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    NETWORK = "network"
    CODE_EXEC = "code_exec"
    SHELL = "shell"
    DATABASE = "database"
    LLM_CALL = "llm_call"
    DANGEROUS = "dangerous"

    @classmethod
    def validate_all(cls, values: list[str]) -> None:
        # 功能：注册门禁——未知标签 fail-closed（防手滑输错静默通过）
        known = {c.value for c in cls}
        unknown = [v for v in values if v not in known]
        if unknown:
            raise ValueError(f"未知 capability_tags: {unknown}；合法集合 = {sorted(known)}")


@dataclass(frozen=True)
class RetryPolicy:
    # cross_process_resume 仅为元数据开关——本版不做跨进程幂等键，false 默认/true 不改变本进程行为。
    retryable: bool
    max_retries: int = 2
    backoff_seconds: float = 1.0
    cross_process_resume: bool = False


@dataclass
class ToolDependency:
    type: str
    name: str
    help_hint: str = ""


@dataclass
class ToolPermissionContext:
    user_id: str = ""
    role: str = ""
    mode: str = "execute"
    extra: dict[str, Any] | None = None
    # 权限管道引用——工具内部 WARN 级命令经此走用户确认通道
    permissions_pipeline: Any | None = None
    # run_agent → build_agent_context → ToolExecutor 逐层透传，None = 非公司链路
    communication: Any | None = None
    # 父事件总线引用（executor 注入）——子 Agent 派生工具（subagent_spawn）复用
    # 使子链路事件/成本落到父链而非自建无订阅总线
    event_bus: Any | None = None


PermissionVerdict = tuple[bool, str]


@dataclass
class ToolResult:
    output: Any = None
    metadata: dict[str, Any] | None = None
    error: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.error is None

    @classmethod
    def ok(cls, output: Any = None, metadata: dict[str, Any] | None = None) -> ToolResult:
        return cls(output=output, metadata=metadata)

    @classmethod
    def fail(cls, error: str, output: Any = None) -> ToolResult:
        return cls(output=output, error=error)


@dataclass(frozen=True)
class ToolProgressEvent:
    # 类型：status=阶段状态 / output=增量输出 / complete=最终结果 / error=进度错误
    type: str
    data: dict[str, Any]
    timestamp: float | None = None


_VALIDATOR_CACHE: dict[int, tuple[dict[str, Any], Draft202012Validator]] = {}

# 实例级注册（不污染全局 FormatChecker 类），供 _param_validator 复用
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-zA-Z0-9-]{1,63}(?<!-)(\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*\.?$"
)


def _check_uri(value: Any) -> bool:
    # 功能：format: uri 校验——rfc3986 语义（允许相对引用），禁止空白/控制字符
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 0x20 for c in value):
        return False
    parsed = urlparse(value)
    # scheme 或 netloc 或 path 至少存在一个非空（空串非法）
    return bool(parsed.scheme or parsed.netloc or parsed.path)


def _check_date_time(value: Any) -> bool:
    # 功能：format: date-time 校验——ISO 8601 完整时间戳（含日期+时间部分；
    if not isinstance(value, str) or ("T" not in value and " " not in value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _check_hostname(value: Any) -> bool:
    # 功能：format: hostname 校验——RFC 1123 标签规则（≤63 字符，字母数字连字符，首尾非连字符）
    return isinstance(value, str) and bool(_HOSTNAME_RE.match(value))


def _check_path(value: Any) -> bool:
    # 功能：format: path 校验（五-建议补齐）——非空、无 NUL、非纯空白
    # 不超 4096（纵深防御：非法字符由路径安全层负责，此处只拦结构级非法）
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\x00" not in value
        and len(value) <= 4096
    )


_FORMAT_CHECKER = FormatChecker()
_FORMAT_CHECKER.checkers["uri"] = (_check_uri, ValueError)
_FORMAT_CHECKER.checkers["date-time"] = (_check_date_time, ValueError)
_FORMAT_CHECKER.checkers["hostname"] = (_check_hostname, ValueError)
_FORMAT_CHECKER.checkers["path"] = (_check_path, ValueError)


def _param_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """# 功能：按 schema 对象缓存 JSON Schema 校验器（类属性 parameters 引用稳定）"""
    entry = _VALIDATOR_CACHE.get(id(schema))
    if entry is not None and entry[0] is schema:
        return entry[1]
    # FormatChecker：启用 format 关键字校验（含 uri/date-time/hostname 自定义注册
    # 默认 Draft202012 仅注解不校验）
    validator = Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)
    _VALIDATOR_CACHE[id(schema)] = (schema, validator)
    return validator


class BaseTool(ABC):
    name: str = ""
    description: str = ""
    category: ToolCategory = ToolCategory.UTIL
    aliases: tuple[str, ...] = ()
    # 参数别名（弱模型参数名漂移容错）：别名键 → 正式键。schema 面须同步
    # 在 parameters.properties 声明别名属性（additionalProperties=False 下模型可见可用）
    param_aliases: dict[str, str] = {}
    is_concurrency_safe: bool = False
    enabled: bool = True
    risk_level: str = "low"
    is_read_only: bool = False
    is_idempotent: bool = False
    is_destructive: bool = False
    # "none" = 禁止去重——读外部状态（文件/DB/API）的工具结果会变
    # 复用旧结果丢真实读取
    dedup_scope: str = "turn"
    # 旧缓存自动失效；空串 = 未声明（兼容旧键语义）
    tool_version: str = ""
    needs_context: bool = False
    cancellable: bool = False
    permissions_override: str | None = None
    # 显式 retryable=False 即使只读/名单外也禁重试（声明权威）
    retry_policy: RetryPolicy | None = None
    cache_ttl: float = 0.0
    # config per_tool_max_output_tokens 可覆盖此值）
    max_result_tokens: int | None = None
    user_facing_name: str = ""
    tags: list[str] = []
    resource_type: str = "tool"
    feature_group: str | None = None
    capability_tags: list[str] = []
    # is_read_only/is_destructive 推导（readonly 角色的 read 分支可达）
    permission_action: str = ""
    external_content: bool = False
    # -②：远端执行标记（MCP 注册产物）——参数中路径值经本地敏感路径把关
    mcp_remote: bool = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "parameters" not in cls.__dict__:
            cls.parameters = {}
        if "dependencies" not in cls.__dict__:
            cls.dependencies = []
        if not cls.user_facing_name:
            cls.user_facing_name = cls.name

    @abstractmethod
    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> str | dict[str, Any]: ...

    async def stream(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ):
        # 功能：默认流式实现——单次委托 execute()，产出完整结果事件。
        # 工具可 override 此方法增量产出中间进度；executor 依据
        # `type(tool).stream is not BaseTool.stream` 判定是否走流式路径。
        result = await self.execute(params, context=context)
        yield ToolProgressEvent(type="complete", data={"result": result}, timestamp=time.time())

    async def check_permissions(
        self, params: dict[str, Any], ctx: ToolPermissionContext | None = None
    ) -> PermissionVerdict:
        return True, ""

    def resume_eligible(self, arguments: dict[str, Any]) -> bool:
        # 功能：跨进程恢复按参数动态资格（5.2）——默认参与；写类工具在非幂等
        # 参数模式（如 write 的 append）覆盖返回 False，防同参重放吞真实副作用
        return True

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        schema = self.parameters
        if not schema or not isinstance(schema, dict):
            return []

        validator = _param_validator(schema)
        return [
            f"{'.'.join(map(str, err.absolute_path)) or '<root>'}: {err.message}"
            for err in validator.iter_errors(params)
        ]

    def normalize_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        别名不覆盖正式值）；无别名声明的工具原样返回（零开销不拷贝）。"""
        if not self.param_aliases or not params:
            return params
        out = dict(params)
        for alias, canonical in self.param_aliases.items():
            if alias in out:
                if canonical not in out:
                    out[canonical] = out[alias]
                out.pop(alias, None)
        return out

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_tool_meta(self) -> dict[str, Any]:
        # 功能：能力管理面元数据（capability_tags/feature_group 不进 OpenAI schema
        # 经 list_tool_meta 单独暴露，供 ToolSearch/前端能力管理消费）
        # risk_level/mcp 是能力管理页的展示字段（判断「会不会弹权限卡」「是不是远端工具」）
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "feature_group": self.feature_group,
            "capability_tags": list(self.capability_tags),
            "risk_level": self.risk_level,
            "mcp": bool(self.mcp_remote),
        }
