# 功能：配置模型（LLM/Tools/Session/Verifier/熔断/重试 Pydantic 配置）
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator

from bandi.core.exceptions import ConfigError
from bandi.core.security.secret_protect import is_protected, protect, unprotect

logger = logging.getLogger(__name__)


def _atomic_dump_yaml(target: Path, data: dict[str, Any]) -> None:
    # 功能：配置文件原子写（临时文件 + fsync + replace），防截断/跨进程混写。
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            yaml.dump(data, handle, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


# models.dev 自动同步常量（定义在本层避免 llm↔core 循环导入；模型目录层引用之）
MODELS_DEV_URL: str = "https://models.dev/api.json"
DEFAULT_CACHE_PATH: str = "~/.bandi/models_cache.json"
# 成本台账库路径（**单一来源**）：写入方（agent_runner）/ 读取方（api 端点）/ 重算工具共用
# 否则设置页「用量」恒为空（历史上三处各自拼路径）。
# ⚠️ 必须经 `Path.home()` 拼接，**不得**写成 `"~/.bandi/..."` 再 `.expanduser()`——
# 后者读 HOME/USERPROFILE 环境变量，**不受 `monkeypatch.setattr(Path, "home", ...)` 影响**
_COST_DB_PARTS: tuple[str, ...] = (".bandi", "data", "cost.db")


def default_cost_db_path() -> Path:
    # 功能：成本台账 SQLite 路径（单一来源；见上方 ⚠️ 关于 Path.home 的说明）
    return Path.home().joinpath(*_COST_DB_PARTS)


# 时使用。定义为常量而非字段默认值：字段默认必须是哨兵 0（未声明），否则静态默认
DEFAULT_CONTEXT_WINDOW: int = 128000


class LLMConfig(
    BaseModel
):  # 功能：LLM 提供方配置（模型/供应商/API Key/地址/超时/思考模式/预算/降级链）
    # DeepSeek-.1-Flash（原生多模态）**两种端点各认一个名字**，目录两条都收录：
    # 默认取 (2)：降级链第二项保留 (1) 作双命名端点兜底——若端点拒绝其一，链上另一项
    default_model: str = "deepseek-v4.1-flash"
    provider: str = (
        ""  # openai | deepseek | openai_compat | anthropic | gemini，空=按模型名自动匹配
    )
    api_key: str = ""
    base_url: str = ""
    timeout: int = 60
    # max_output_tokens 正值校验——0/负值会让请求每轮显式失败
    max_output_tokens: int = Field(default=8192, gt=0)
    # deepseek: low/high/max ｜ openai: none/low/medium/high/xhigh（空档落 none）
    # anthropic / gemini / moonshot / zhipu：适配层尚未接线 ⇒ 目录不列挡位
    thinking_mode: str = ""
    budget_limit: float = -1.0
    # 功能：OpenAI 兼容端点的附加请求头（网关强制头/客户端标识；空 = 不发送）
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # 功能：单次流式调用整体超时（秒）——高思考档单次生成可达数分钟，
    # 防挂起缺省 240s；档案/config 可放宽（如 900）
    stream_timeout: float = Field(default=240.0, gt=0)
    # 降级链默认**空**：本地产品不预设降级（分发时无任何已配模型，预设链只会让每次
    # 失败白烧数次注定打不通的跨厂商请求）。用户显式配置即生效——机制保留，默认不启用。
    fallback_chain: list[str] = Field(default_factory=list)
    # 安全：记录加载时 api_key 原值是否为 dpapi: 密文——save 据此重新加密，防明文回写
    _api_key_protected: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _unprotect_api_key(self) -> "LLMConfig":
        # 功能：加载后自动解密 dpapi: 前缀的 api_key 并记录受保护状态（解密失败保留原值）
        self._api_key_protected = is_protected(self.api_key)
        self.api_key = unprotect(self.api_key)
        return self


class TaskLedgerConfig(BaseModel):  # 功能：任务台账配置（启停/上限/双持久化开关/事件开关）
    enabled: bool = True
    max_items: int = 24
    persist_to_session: bool = True
    persist_to_work_item: bool = True
    emit_runtime_events: bool = True


class MCPConfig(
    BaseModel
):  # 功能：MCP 服务配置（单 server：连接池名/传输/命令或 URL/工具前缀/白名单）
    name: str = ""
    pool: str = ""
    transport: str = "stdio"  # stdio | streamable_http
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    prefix: str = ""
    # -③：允许注册的远端工具白名单（含前缀全名）；缺省 None = 不受限；显式空数组 = 全部拒绝（fail-closed）
    allowed_tools: list[str] | None = None
    # 功能：单台服务器开关——False 时不连接、不注册任何工具（旧配置缺省视为启用）
    enabled: bool = True
    # 功能：分类与主页链接（设置页分组展示与外链跳转用；空串 = 不分组/无链接）
    category: str = ""
    homepage: str = ""


class WebSearchConfig(BaseModel):  # 功能：联网检索凭据（落盘前 DPAPI 加密；env 为兜底）
    tavily_api_key: str = ""
    brave_api_key: str = ""


class StorageConfig(BaseModel):  # 功能：数据保留策略（超期会话先归档再删除）
    retention_days: int = 90


class ComputerUseConfig(
    BaseModel
):  # 功能：桌面控制（截屏/点击/打字/滚动）——默认关闭，独立 venv 运行
    enabled: bool = False
    # 功能：建运行时用的 Python 解释器（空 = 自动检测）
    interpreter: str = ""
    # 功能：运行时 venv 目录（空 = ~/.bandi/runtime/computer-venv）
    venv_dir: str = ""
    # 功能：敏感窗口黑名单豁免——关闭即允许操作，默认不豁免
    allow_sensitive_windows: bool = False


class FeatureFlagsConfig(
    BaseModel
):  # 功能：特性门控配置（CC-24 tool_groups：组名→开关，未列出的组默认开放）
    tool_groups: dict[str, bool] = Field(default_factory=dict)


class ToolsConfig(BaseModel):  # 功能：工具系统配置（临时目录/超时/预算）
    task_ledger: TaskLedgerConfig = Field(default_factory=TaskLedgerConfig)
    mcp_servers: list[MCPConfig] = Field(default_factory=list)
    # 功能：MCP 自动连接开关——默认开启；无 servers 配置时不产生连接。
    mcp_autostart: bool = True
    # 功能：MCP 工具懒暴露——普通对话不注入 MCP schema，命中 MCP 意图时才暴露。
    mcp_lazy_exposure: bool = True
    # 功能：单轮最多暴露的 MCP 工具数——控制 schema token 上限。
    mcp_max_exposed_tools: int = Field(default=5, ge=0, le=50)
    # 功能：联网检索凭据（配置优先、环境变量兜底）
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    computer_use: ComputerUseConfig = Field(default_factory=ComputerUseConfig)
    feature_flags: FeatureFlagsConfig = Field(default_factory=FeatureFlagsConfig)
    temp_dir: str = "~/.bandi/tmp"
    execution_timeout: int = 120
    output_preview_chars: int = 200
    # 工具输出截断预算（单次工具输出 > 该值即截断落盘）——executor 装配透传
    max_output_tokens: int = 8192
    per_tool_max_output_tokens: dict[str, int] = Field(
        default_factory=lambda: {
            # 单位 token）——读类大档/命令类中档/网络类中档；未列工具走默认 8192
            "read": 25000,
            "grep": 25000,
            "glob": 15000,
            "bash": 10000,
            "python_exec": 10000,
            "web_fetch": 12000,
            "web_search": 8000,
        }
    )
    consecutive_failure_threshold: int = 3
    chars_per_token: int = 4
    # 显式 0 = 不限（保留逃生口，但会产生告警）；None = 沿用旧行为（同样不限、不告警）。
    max_concurrency: int | None = 8
    # 子代理 fan-out 默认并行上限（SubAgentTool 未显式传 max_concurrent 时生效）。
    # 默认 10——子代理是独立会话
    # 单会话内资源占用低于批内工具，故高于工具侧上限。显式传 0 = 不限（告警）。
    subagent_max_concurrency: int = 10
    bash_timeout_default: int = 30
    bash_timeout_max: int = 60
    git_timeout: int = 15
    python_exec_timeout_default: int = 30
    python_exec_timeout_max: int = 120
    python_exec_max_stdout_chars: int = 30000
    python_exec_max_stderr_chars: int = 10000
    browser_timeout_default: int = 30
    browser_timeout_max: int = 120
    browser_post_click_delay_ms: int = 500
    browser_snapshot_preview_chars: int = 3000
    browser_max_interactive_elements: int = 50
    browser_element_text_max_chars: int = 60
    web_search_timeout_tavily: int = 8
    web_search_timeout_exa: int = 10
    web_search_timeout_ddg: int = 15
    web_search_circuit_fail_limit: int = 3
    web_search_circuit_reset_secs: int = 60
    web_search_max_results: int = 5
    web_fetch_timeout_default: int = 30
    web_fetch_timeout_max: int = 60
    web_fetch_max_bytes: int = 5242880
    web_fetch_fallback_preview_chars: int = 2000
    web_fetch_allow_private_networks: bool = False


class SessionConfig(BaseModel):  # 功能：Session 持久化配置（预留扩展锚点）
    pass


class VerifierPolicyConfig(BaseModel):  # 功能：验证器策略配置（启停/触发条件/停滞检测阈值）
    enabled: bool = True
    min_todos_for_verification: int = 3
    require_on_code_edits: bool = True
    stagnation_threshold: int = 3
    judge_model: str = ""


class AgentConfig(
    BaseModel
):  # 功能：Agent 模式/迭代上限/压缩阈值/上下文窗口/Token 预算/折叠阈值配置
    mode: str = "execute"  # plan | execute（双模式，u 日拍板）
    max_iterations: int = 15
    stream_to_ui: bool = True
    compression_threshold: float = 0.85
    # 0 = 「未显式配置」哨兵 → 装配时按模型目录回填真实窗口（`resolve_context_window`）；
    # 目录未收录该模型时回落 `DEFAULT_CONTEXT_WINDOW`。显式配置（>0）保持只收紧口径
    # （min(配置, 目录)）。切勿改回静态 128000——那会与用户显式配置不可区分
    # 使 1M 模型在 ~91.8K 就触发压缩。
    context_window: int = 0
    # 输出 token 预留（压缩阈值按 context_window - reserved 生效）——装配时按
    # LLMConfig.max_output_tokens 回填（cap 20000）；0=不预留（向后兼容默认）
    reserved_output_tokens: int = 0
    token_budget_limit: int = -1  # Token 预算上限（-1=不限制）
    # 请求前折叠视图阈值（chars）：旧轮超长工具输出折叠为占位符（只影响请求
    # 视图、不写 ctx.messages）；<=0 关闭。见 bandi/agent/request_view.py。
    # 「摘要+落盘路径」回传长度 ⇒ 已摘要消息必被折叠、出路丢失；抬到 4000）
    fold_tool_view_chars: int = 4000
    # 0=未显式配置 → 上下文默认 5；>0 显式覆盖
    loop_detection_window: int = 0


class SystemConfig(BaseModel):  # 功能：系统路径配置（临时目录/路径展开工具）
    temp_dir: str = "~/.bandi/tmp"

    @classmethod
    def _expand(cls, v: str) -> str:
        if v.startswith("~"):
            return str(Path(v).expanduser())
        return v

    @classmethod
    def resolve_path(cls, v: str) -> str:  # 功能：展开路径中的 ~ 为完整用户目录
        return cls._expand(v)


class PermissionsConfig(
    BaseModel
):  # 功能：权限管道配置（拒绝/询问/白名单工具 + 危险命令 + 风险等级策略）
    # {"pattern": ..., "action": "allow|ask|deny"}；加载器逐层 concat 并自动标注
    # source（user/project/local/flag）
    rules: list[dict[str, Any]] = Field(default_factory=list)
    deny_tools: list[str] = Field(default_factory=list)
    ask_tools: list[str] = Field(
        default_factory=lambda: [
            "bash",
            "write",
            "ontology_define",
            "ontology_assert",
            "memory_assert",
        ]
    )
    bypass_tools: list[str] = Field(
        default_factory=lambda: ["read", "web_search", "glob", "grep", "ontology_query"]
    )
    deny_commands: list[str] = Field(
        default_factory=lambda: ["rm -rf /", "format *", "del /f /s", "rd /s /q"]
    )
    ask_risk_levels: list[str] = Field(default_factory=lambda: ["high", "critical"])
    # 目标值须是 admin/power_user/member/readonly 之一（非法值被忽略并告警
    # 见 core/permission/role_mapping.py）。
    role_map: dict[str, str] = Field(default_factory=dict)
    # 安全加固：岗位绑定读取失败时的处置。False（默认）= 告警并回落 agent_role
    # （现状行为）；True = 抛 BandiError fail-closed——防一次 DB 故障把受限用户
    # 按高权限 agent_role 静默放行（见 core/permission/role_mapping.py）。
    strict_role_binding: bool = False


class SafetyConfig(
    BaseModel
):  # 功能：安全护栏配置（注入模式/敏感词/PII 规则/输出规范/幻觉检测阈值）
    enabled: bool = True
    # 「未配置」与「显式清空」不可分，清空语义永远不可达（or None 回落）
    prompt_guard_patterns: list[tuple[str, str]] | None = None
    content_filter_categories: dict[str, list[str]] | None = None
    max_output_chars: int = 8000
    max_output_lines: int = 500
    l2_threshold: float = 0.3
    block_threshold: float = 0.7


class PrefetchConfig(
    BaseModel
):  # 功能：记忆预取配置（并行预取启停/超时放弃等待/focused 节开关/单节字符上限）
    enabled: bool = True
    timeout_secs: float = 1.0
    max_chars: int = 4000
    focused_memory: bool = True


class ModelsDevConfig(
    BaseModel
):  # 功能：models.dev 元数据自动同步配置（方案 C：启动拉 + 本地缓存 + TTL，断网兜底）
    enabled: bool = True
    url: str = MODELS_DEV_URL
    cache_ttl_hours: float = 24.0
    timeout_secs: float = 5.0
    cache_path: str = DEFAULT_CACHE_PATH


class MemoryMaintenanceConfig(BaseModel):
    """功能：记忆/会话生命周期维护配置（调度器每日维护的地基参数）。"""

    enabled: bool = True
    interval_hours: float = 24.0
    session_ttl_days: int = 90
    forget_older_than_days: int = 120
    promote_limit: int = 48


class ExecutionIsolationConfig(BaseModel):
    """功能：员工执行隔离——local 加固或 docker 容器（不可用时 fail-closed）"""

    mode: str = "local_hardened"
    image: str = "python:3.12-slim"
    network: str = "none"
    memory_mb: int = Field(default=1024, ge=128, le=16384)
    cpu_seconds: int = Field(default=300, ge=1, le=3600)


class CompanyConfig(BaseModel):
    """功能：公司调度并发配置（同时在跑的工作项上限与单 tick 预算）。"""

    max_concurrent: int = Field(default=3, ge=1, le=16)
    tick_budget_sec: float = Field(default=300.0, ge=0.0, le=86400.0)
    max_items_per_tick: int = Field(default=64, ge=1, le=512)


class BandiConfig(BaseModel):  # 功能：配置对象（聚合所有子配置 + YAML 加载/合并/校验 + 多环境支持）
    env: str = "dev"
    system: SystemConfig = Field(default_factory=SystemConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    verification: VerifierPolicyConfig = Field(default_factory=VerifierPolicyConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    prefetch: PrefetchConfig = Field(default_factory=PrefetchConfig)
    models_dev: ModelsDevConfig = Field(default_factory=ModelsDevConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    execution_isolation: ExecutionIsolationConfig = Field(
        default_factory=lambda: ExecutionIsolationConfig()
    )
    memory_maintenance: MemoryMaintenanceConfig = Field(default_factory=MemoryMaintenanceConfig)
    company: CompanyConfig = Field(default_factory=CompanyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    # （无 BANDI_TRUST_PROJECT_HOOKS 时 project/local 来源条目被剔除）
    hooks: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    preferred_language: str = "zh-CN"

    _loaded_sources: list[str] = PrivateAttr(default_factory=list)

    @property
    def loaded_sources(self) -> list[str]:
        # 功能：本进程实际加载的配置来源列表（来源分层诊断面）
        return list(self._loaded_sources)

    @classmethod
    def load(cls) -> "BandiConfig":
        # 功能：四层来源加载（user/env-profile → project → local → flag）+ 环境变量覆盖
        merged: dict[str, Any] = {}
        sources: list[str] = []
        rules: list[dict[str, Any]] = []
        hooks: dict[str, list[dict[str, Any]]] = {}

        def _read_layer(path: Path, source: str) -> None:
            nonlocal merged
            if not path.exists():
                return
            try:
                with open(path, encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError) as e:
                from bandi.utils.errors import error_text

                raise ConfigError(f"failed to load {path}: {error_text(e)}") from e
            if not isinstance(raw, dict):
                return
            rules.extend(cls._collect_permission_rules(raw, source))
            cls._collect_hooks(raw, source, hooks)
            merged = cls.deep_merge(merged, raw)
            sources.append(source)

        home = cls._bandi_home()
        _read_layer(home / "config.yaml", "user")
        env_name = merged.get("env") or os.environ.get("BANDI_ENV", "dev")
        _read_layer(home / f"config.{env_name}.yaml", f"env:{env_name}")
        project_config_path = cls._find_project_config()
        if project_config_path is not None:
            _read_layer(project_config_path, "project")
        _read_layer(Path.cwd() / ".bandi" / "config.local.yaml", "local")
        flag_path = os.environ.get("BANDI_CONFIG", "").strip()
        if flag_path:
            _read_layer(Path(flag_path), "flag")
        merged = cls.deep_merge(merged, cls._env_overrides("BANDI_"))
        try:
            from bandi.core.mcp_file import read_servers_raw

            raw_servers = read_servers_raw()
            mcp_servers = (
                [MCPConfig.model_validate(item).model_dump() for item in raw_servers]
                if raw_servers is not None
                else None
            )
        except Exception as exc:  # noqa: BLE001 - mcp.json 损坏不得阻断主配置加载
            logger.warning("mcp.json ignored: %s", exc)
            mcp_servers = None
        if mcp_servers is not None:
            merged.setdefault("tools", {})
            merged["tools"]["mcp_servers"] = mcp_servers
        if rules:
            merged.setdefault("permissions", {})
            merged["permissions"]["rules"] = rules
        if hooks:
            trust_project = cls._trust_project_hooks()
            filtered: dict[str, list[dict[str, Any]]] = {}
            for event, entries in hooks.items():
                kept = [
                    entry
                    for entry in entries
                    if trust_project
                    or entry.get("source") in ("user", "flag")
                    or str(entry.get("source") or "").startswith("env:")
                ]
                if kept:
                    filtered[event] = kept
            merged["hooks"] = filtered
        try:
            config = cls.model_validate(merged)
        except ValidationError as e:
            from bandi.utils.errors import error_text

            raise ConfigError(f"config validation failed: {error_text(e)}") from e
        config._loaded_sources = sources
        return config

    @staticmethod
    def _collect_permission_rules(raw: dict[str, Any], source: str) -> list[dict[str, Any]]:
        # 功能：收集单层 permissions.rules 并标注来源（字符串/dict 双形态透传）
        permissions = raw.get("permissions")
        if not isinstance(permissions, dict):
            return []
        entries = permissions.get("rules")
        if not isinstance(entries, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in entries:
            if isinstance(entry, str) and entry.strip():
                out.append({"pattern": entry.strip(), "source": source})
            elif isinstance(entry, dict) and entry.get("pattern"):
                item = dict(entry)
                item["source"] = source
                out.append(item)
        return out

    @staticmethod
    def _collect_hooks(
        raw: dict[str, Any], source: str, acc: dict[str, list[dict[str, Any]]]
    ) -> None:
        # 功能：收集单层 hooks 条目（event -> 列表，逐条标注来源）
        hooks = raw.get("hooks")
        if not isinstance(hooks, dict):
            return
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict):
                    item = dict(entry)
                    item["source"] = source
                    acc.setdefault(str(event), []).append(item)

    @staticmethod
    def _trust_project_hooks() -> bool:
        # 功能：项目级 hooks 信任门禁——仅用户显式设置环境变量时执行项目 hooks
        return os.environ.get("BANDI_TRUST_PROJECT_HOOKS", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    def save(self, path: Path | str | None = None, *, patch: dict[str, Any] | None = None) -> None:
        """功能：将配置写入 YAML 文件。

        默认路径（user config.yaml）语义分两档——
        - `patch` 给定：读-改-写（只更新指定字段，不把多层合并对象的其他层值落盘）；
        - `patch` 缺失：仅允许「单层/新建配置」全量写（`loaded_sources` 空或仅 user）。
          多层合并对象全量写会把 env/project/local 层配置提升为全局（权限面污染）
          显式拒绝（fail-closed）。
        """
        target = (
            Path(path) if isinstance(path, str) else (path or self._bandi_home() / "config.yaml")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if patch is not None:
            base: dict[str, Any] = {}
            if target.exists():
                try:
                    with open(target, encoding="utf-8") as f:
                        raw = yaml.safe_load(f) or {}
                except (OSError, yaml.YAMLError) as e:
                    from bandi.utils.errors import error_text

                    raise ConfigError(f"failed to load {target}: {error_text(e)}") from e
                if isinstance(raw, dict):
                    base = raw
            merged_patch = self.deep_merge(base, dict(patch))
            llm_patch = merged_patch.get("llm")
            protected = bool(getattr(self.llm, "_api_key_protected", False))
            if (
                protected
                and isinstance(llm_patch, dict)
                and llm_patch.get("api_key")
                and not is_protected(str(llm_patch["api_key"]))
            ):
                llm_patch["api_key"] = protect(str(llm_patch["api_key"]))
            _atomic_dump_yaml(target, merged_patch)
            return
        extra_sources = [source for source in self._loaded_sources if source != "user"]
        if extra_sources:
            raise ConfigError(
                "合并配置对象禁止全量回写（来源: "
                + ", ".join(extra_sources)
                + "）——请使用 save(patch={...}) 显式指定变更字段"
            )
        data = self.model_dump()
        # 安全：原值为 dpapi: 密文时重新加密落盘——load 已解密内存值，直接 dump 会把
        # 一次 save（model switch、PUT /api/settings/model）变成保护静默失效
        if bool(getattr(self.llm, "_api_key_protected", False)) and self.llm.api_key:
            data["llm"]["api_key"] = protect(self.llm.api_key)
        _atomic_dump_yaml(target, data)

    @staticmethod
    def _bandi_home() -> Path:
        return Path.home() / ".bandi"

    @staticmethod
    def _find_project_config() -> Path | None:
        cwd = Path.cwd()
        candidate = cwd / ".bandi" / "config.yaml"
        if not candidate.exists():
            return None
        # 双载会令 permissions.rules 逐层 concat 出现重复项、并让合并对象多出
        # 伪 project 来源（save 防护误伤）。同一文件只作 user 层。
        try:
            if candidate.resolve() == (Path.home() / ".bandi" / "config.yaml").resolve():
                return None
        except OSError:
            return None
        return candidate

    @staticmethod
    def _env_overrides(prefix: str) -> dict:
        overrides = {}
        for key, value in os.environ.items():
            if key.startswith(prefix):
                remainder = key[len(prefix) :].lower()
                if remainder == "config":
                    continue
                # BANDI_API_KEY 同 llm.api_key（老产品惯用名）
                if remainder == "api_key":
                    overrides["llm"] = {"api_key": value}
                    continue
                # BANDI_AGENT_MAX_ITERATIONS → agent.max_iterations
                # BANDI_LLM_BASE_URL → llm.base_url
                parts = remainder.split("_", 1)
                if len(parts) == 2:
                    class_prefix = parts[0]
                    field_name = parts[1]
                    if class_prefix not in overrides:
                        overrides[class_prefix] = {}
                    overrides[class_prefix][field_name] = value
                else:
                    overrides[parts[0]] = value
        return overrides

    @staticmethod
    def deep_merge(
        base: dict, override: dict
    ) -> dict:  # 功能：递归合并两个字典，子字典内层合并而非覆盖
        result = dict(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = BandiConfig.deep_merge(result[key], value)
            elif value is not None:
                result[key] = value
        return result
