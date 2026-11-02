# 功能：Agent 工厂 — 消除 CLI 和 API 之间 60 行重复组装代码
# 统一创建 ToolRegistry / Provider / ExecutionContext / AgentLoop

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from bandi.agent.base import ExecutionContext
from bandi.core.config import BandiConfig
from bandi.core.event_bus import EventBus
from bandi.core.permission.policy_engine import PolicyEngine
from bandi.core.user_interaction import UserInteraction
from bandi.llm.base import BaseProvider
from bandi.llm.mock import MockProvider
from bandi.llm.registry import ProviderRegistry
from bandi.observability.tracer import generate_trace_id
from bandi.ontology.triple_store import OntologyStore
from bandi.tools.executor import ToolExecutor
from bandi.tools.middleware import (
    CircuitBreakerMiddleware,
    OwnershipMiddleware,
    PermissionMiddleware,
    RateLimitMiddleware,
    RetryMiddleware,
    SlidingWindowRateLimiter,
    TelemetryMiddleware,
)
from bandi.tools.permissions_pipeline import PermissionsPipeline
from bandi.tools.pipeline import ToolPipeline
from bandi.tools.registry import ToolRegistry, normalize_role_tool_tags
from bandi.tools.selection import schema_name, select_relevant_schemas
from bandi.utils.db import connect_app_db

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bandi.tools.mcp_broker import MCPBroker

_REGISTRY_CACHE: dict[str, ProviderRegistry] = {}

# 默认账户库单例（岗位绑定解析用；仅在 user_id 非空且调用方未传库时构建）
_ACCOUNT_STORE_SINGLETON: Any | None = None


def _entity_judge_llm(provider: Any | None) -> Any | None:
    """功能：构造实体归一判定用的 LLM 入口——供 memory_assert 把新实体名归一到旧名。

    没有 provider 时返回 None，memory_assert 跳过归一（写入照常，只是不合并
    同义写法）——归一是精度增强，不该成为写入的硬前置。
    """
    if provider is None:
        return None

    async def _judge(prompt: str) -> str:
        resp = await provider.complete(
            [{"role": "user", "content": prompt}],
        )
        return resp.content or ""

    return _judge


def build_tool_registry(
    entity_store: Any | None = None,
    ontology_inference: bool = True,
    ontology_query_limit: int = 50,
    ontology_query_limit_max: int = 200,
    triple_ontology_store: Any | None = None,
    git_timeout: int = 0,
    web_search_timeout_tavily: int = 8,
    web_search_timeout_exa: int = 10,
    web_search_timeout_ddg: int = 15,
    web_search_circuit_fail_limit: int = 3,
    web_search_circuit_reset_secs: int = 60,
    bash_timeout_default: int = 30,
    bash_timeout_max: int = 60,
    python_exec_timeout_default: int = 30,
    python_exec_timeout_max: int = 120,
    python_exec_max_stdout_chars: int = 30000,
    python_exec_max_stderr_chars: int = 10000,
    browser_timeout_default: int = 30,
    browser_timeout_max: int = 120,
    browser_post_click_delay_ms: int = 500,
    browser_snapshot_preview_chars: int = 3000,
    browser_max_interactive_elements: int = 50,
    browser_element_text_max_chars: int = 60,
    web_fetch_timeout_default: int = 30,
    web_fetch_timeout_max: int = 60,
    web_fetch_max_bytes: int = 5242880,
    web_fetch_fallback_preview_chars: int = 2000,
    web_fetch_allow_private_networks: bool = False,
    web_search_max_results: int = 5,
    subagent_provider: Any | None = None,
    task_ledger: Any | None = None,
    feature_flags: dict[str, bool] | None = None,
    workflow_db_path: Any | None = None,
    subagent_max_concurrency: int = 10,
) -> ToolRegistry:
    """统一创建 ToolRegistry，注册所有内置工具。"""
    from bandi.agent.subagent_tool import SubAgentTool
    from bandi.tools.apply_patch import ApplyPatchTool
    from bandi.tools.bash import BashTool
    from bandi.tools.browser import BrowserTool
    from bandi.tools.canvas.canvas_publish import CanvasPublishTool
    from bandi.tools.canvas.genui_guide import GetGenuiGuideTool
    from bandi.tools.canvas.html_guide import GetHtmlCanvasGuideTool
    from bandi.tools.canvas.ui_components import EmitUiTreeTool, ListUiComponentsTool
    from bandi.tools.chart.chart_generator import ChartGeneratorTool
    from bandi.tools.collaboration import (
        AskPeerAndWaitTool,
        BroadcastIssueTool,
        CloseHumanReviewTool,
        InboxTool,
        ReplyMessageTool,
        RespondMeetingTool,
        RouteWorkTool,
        SendDmTool,
        StartMeetingTool,
    )
    from bandi.tools.ctx_inspect import CtxInspectTool
    from bandi.tools.data.data_aggregate import DataAggregateTool
    from bandi.tools.data.data_clean import DataCleanTool
    from bandi.tools.data.data_merge import DataMergeTool
    from bandi.tools.data.data_transform import DataTransformTool
    from bandi.tools.data.data_validate import DataValidateTool
    from bandi.tools.db.db_connection import DbConnectTool
    from bandi.tools.db.db_execute import DbExecuteTool
    from bandi.tools.db.db_inspect import DbInspectTool
    from bandi.tools.doc.archive_manager import ArchiveManagerTool
    from bandi.tools.doc.config_file_tool import ConfigFileTool
    from bandi.tools.doc.csv_processor import CSVProcessorTool
    from bandi.tools.doc.doc_classifier import DocClassifierTool
    from bandi.tools.doc.excel_reader import ExcelReaderTool
    from bandi.tools.doc.html_processor import HTMLProcessorTool
    from bandi.tools.doc.image_ocr import ImageOCRTool
    from bandi.tools.doc.markdown_processor import MarkdownProcessorTool
    from bandi.tools.doc.pdf_reader import PDFReaderTool
    from bandi.tools.doc.pdf_research import (
        CitationExtractorTool,
        PDFStructureTool,
        PDFTranslateTool,
        SectionSummarizerTool,
    )
    from bandi.tools.doc.text_processor import TextFileProcessorTool
    from bandi.tools.doc.word_reader import WordReaderTool
    from bandi.tools.doc_tool import DocumentReadTool
    from bandi.tools.exit_plan_mode import ExitPlanModeTool
    from bandi.tools.file_edit import FileEditTool
    from bandi.tools.file_ops import FileReadTool, FileWriteTool
    from bandi.tools.gen.checklist_tool import ChecklistGenerateTool
    from bandi.tools.gen.document_tool import DocumentGenerateTool
    from bandi.tools.gen.excel_generator import ExcelGenerateTool
    from bandi.tools.gen.slides_tool import SlidesGenerateTool
    from bandi.tools.gen.template_filler import TemplateFillerTool
    from bandi.tools.gen.template_tool import DocumentTemplateTool
    from bandi.tools.gen.theme_tool import ThemeDesignerTool
    from bandi.tools.git_ops import GitCommitTool, GitDiffTool, GitStatusTool
    from bandi.tools.glob_tool import GlobTool
    from bandi.tools.grep_tool import GrepTool
    from bandi.tools.integration.configure_settings import ConfigureSettingsTool
    from bandi.tools.integration.speech_to_text import SpeechToTextTool
    from bandi.tools.list_dir import ListDirTool
    from bandi.tools.manager_board import (
        DeleteWorkItemTool,
        ManagerBoardReadTool,
        ModifyWorkItemTool,
    )
    from bandi.tools.media.audio_generate import AudioGenerateTool
    from bandi.tools.media.image_generate import ImageGenerateTool
    from bandi.tools.media.video_generate import VideoGenerateTool
    from bandi.tools.python_exec import PythonExecTool
    from bandi.tools.resource_tool import ResourceConfigureTool
    from bandi.tools.skill_tool import SkillDiscoverTool, SkillTool
    from bandi.tools.team import TeamCreateTool, TeamListTool
    from bandi.tools.thread_task import SpawnThreadTaskTool
    from bandi.tools.todo import TodoReadTool, TodoWriteTool
    from bandi.tools.tool_search import ToolSearchTool
    from bandi.tools.user_input import UserInputTool
    from bandi.tools.util.cache_manager import CacheManagerTool
    from bandi.tools.util.conversation_history import ConversationHistoryTool
    from bandi.tools.util.cron_tools import CronCreateTool, CronDeleteTool, CronListTool
    from bandi.tools.util.date_calculator import DateCalculatorTool
    from bandi.tools.util.enter_plan_mode import EnterPlanModeTool
    from bandi.tools.util.folder_tool import FolderOperationsTool
    from bandi.tools.util.json_parser import JsonParserTool
    from bandi.tools.util.rule_matcher import RuleMatcherTool
    from bandi.tools.util.text_splitter import TextSplitterTool
    from bandi.tools.util.tool_argument_blob import ToolArgumentBlobTool
    from bandi.tools.web.image_download import WebImageDownloadTool
    from bandi.tools.web.image_search_tool import WebImageSearchTool
    from bandi.tools.web.login import WebLoginTool
    from bandi.tools.web.scraper import WebScraperTool
    from bandi.tools.web_fetch import WebFetchTool
    from bandi.tools.web_search import WebSearchTool
    from bandi.tools.workflow.chat_workflow import ChatWorkflowTool as _ChatWorkflowTool
    from bandi.tools.workflow.service import WorkflowService as _WorkflowService
    from bandi.tools.workflow.store import WorkflowStore as _WorkflowStore
    from bandi.tools.workflow.workflow_crud import (
        WorkflowCancelTool as _WorkflowCancelTool,
    )
    from bandi.tools.workflow.workflow_crud import (
        WorkflowListTool as _WorkflowListTool,
    )
    from bandi.tools.workflow.workflow_crud import (
        WorkflowPauseTool as _WorkflowPauseTool,
    )
    from bandi.tools.workflow.workflow_crud import (
        WorkflowResumeTool as _WorkflowResumeTool,
    )
    from bandi.tools.workflow.workflow_crud import (
        WorkflowRunTool as _WorkflowRunTool,
    )
    from bandi.tools.workflow.workflow_crud import (
        WorkflowStatusTool as _WorkflowStatusTool,
    )
    from bandi.tools.workflow.workflow_embed_emit import (
        WorkflowEmbedEmitTool as _WorkflowEmbedEmitTool,
    )
    from bandi.tools.workflow.workflow_save import WorkflowSaveTool as _WorkflowSaveTool

    registry = ToolRegistry(feature_flags=feature_flags)
    web_search_timeouts = {
        "tavily": web_search_timeout_tavily,
        "exa": web_search_timeout_exa,
        "ddg": web_search_timeout_ddg,
    }
    for tool_instance in [
        ApplyPatchTool(),
        BashTool(bash_timeout_default, bash_timeout_max),
        FileReadTool(),
        FileWriteTool(),
        FileEditTool(),
        GitStatusTool(git_timeout),
        GitDiffTool(git_timeout),
        GitCommitTool(git_timeout),
        GlobTool(),
        GrepTool(),
        ListDirTool(),
        DocumentReadTool(),
        SkillTool(provider=subagent_provider),
        SkillDiscoverTool(),
        ResourceConfigureTool(),
        WebFetchTool(
            web_fetch_timeout_default,
            web_fetch_timeout_max,
            web_fetch_max_bytes,
            web_fetch_fallback_preview_chars,
            web_fetch_allow_private_networks,
        ),
        WebSearchTool(
            web_search_timeouts,
            web_search_circuit_fail_limit,
            web_search_circuit_reset_secs,
            web_search_max_results,
        ),
        PythonExecTool(
            python_exec_timeout_default,
            python_exec_timeout_max,
            python_exec_max_stdout_chars,
            python_exec_max_stderr_chars,
        ),
        BrowserTool(
            browser_timeout_default,
            browser_timeout_max,
            browser_post_click_delay_ms,
            browser_snapshot_preview_chars,
            browser_max_interactive_elements,
            browser_element_text_max_chars,
        ),
        UserInputTool(),
        TodoReadTool(ledger=task_ledger),
        TodoWriteTool(ledger=task_ledger),
        SubAgentTool(
            provider=subagent_provider,
            default_max_concurrency=subagent_max_concurrency,
        ),
        CtxInspectTool(),
        ToolSearchTool(registry=registry),
        ExitPlanModeTool(),
        PDFReaderTool(),
        PDFStructureTool(),
        CitationExtractorTool(),
        SectionSummarizerTool(),
        PDFTranslateTool(),
        WordReaderTool(),
        ExcelReaderTool(),
        CSVProcessorTool(),
        TextFileProcessorTool(),
        MarkdownProcessorTool(),
        HTMLProcessorTool(),
        ConfigFileTool(),
        ArchiveManagerTool(),
        ImageOCRTool(),
        DocClassifierTool(),
        InboxTool(),
        SendDmTool(),
        AskPeerAndWaitTool(),
        ReplyMessageTool(),
        BroadcastIssueTool(),
        StartMeetingTool(),
        RespondMeetingTool(),
        RouteWorkTool(),
        CloseHumanReviewTool(),
        SpawnThreadTaskTool(),
        ManagerBoardReadTool(),
        ModifyWorkItemTool(),
        DeleteWorkItemTool(),
        TeamListTool(),
        TeamCreateTool(),
        ChecklistGenerateTool(),
        DocumentGenerateTool(),
        ExcelGenerateTool(),
        SlidesGenerateTool(),
        TemplateFillerTool(),
        DocumentTemplateTool(),
        ThemeDesignerTool(),
        ChartGeneratorTool(),
        ImageGenerateTool(),
        AudioGenerateTool(),
        VideoGenerateTool(),
        DateCalculatorTool(),
        JsonParserTool(),
        RuleMatcherTool(),
        TextSplitterTool(),
        CacheManagerTool(),
        ConversationHistoryTool(),
        CronCreateTool(),
        CronListTool(),
        CronDeleteTool(),
        EnterPlanModeTool(),
        FolderOperationsTool(),
        ToolArgumentBlobTool(),
        CanvasPublishTool(),
        GetGenuiGuideTool(),
        GetHtmlCanvasGuideTool(),
        ListUiComponentsTool(),
        EmitUiTreeTool(),
        DataAggregateTool(),
        DataCleanTool(),
        DataMergeTool(),
        DataTransformTool(),
        DataValidateTool(),
        DbConnectTool(),
        DbExecuteTool(),
        DbInspectTool(),
        ConfigureSettingsTool(),
        SpeechToTextTool(),
        WebImageDownloadTool(),
        WebImageSearchTool(),
        WebLoginTool(),
        WebScraperTool(),
    ]:
        registry.register_instance(tool_instance)

    # 功能：桌面控制工具按开关注册——关闭时四个工具完全不出现在工具面
    try:
        from bandi.core.config import BandiConfig as _ComputerConfig

        if bool(_ComputerConfig.load().tools.computer_use.enabled):
            from bandi.tools.computer_use import COMPUTER_TOOLS as _COMPUTER_TOOLS

            for _computer_tool in _COMPUTER_TOOLS:
                registry.register_instance(_computer_tool())
    except Exception:  # 配置读取失败按关闭处理（高危能力 fail-closed）
        logger.warning("computer_use 工具注册跳过：配置读取失败", exc_info=True)

    from pathlib import Path as _Path

    from bandi.knowledge.store import get_shared_store as _get_shared_store
    from bandi.memory.long_term import LongTermMemory as _LongTermMemory
    from bandi.tools.doc.knowledge_search import KnowledgeSearchTool as _KnowledgeSearchTool

    _db_dir = _Path.home() / ".bandi" / "data"
    _db_dir.mkdir(parents=True, exist_ok=True)
    _knowledge_store = _get_shared_store()
    _memory_long_term = _LongTermMemory(str(_db_dir / "long_term.db"))
    registry.register_instance(
        _KnowledgeSearchTool(
            memory=_memory_long_term,
            knowledge_store=_knowledge_store,
        )
    )
    from bandi.memory.episodic import EpisodicStore as _EpisodicStore
    from bandi.memory.global_store import GlobalMemoryStore as _GlobalMemoryStore
    from bandi.memory.working import ScratchpadStore as _ScratchpadStore
    from bandi.tools.memory_tools import (
        MemoryAssertTool as _MemoryAssertTool,
    )
    from bandi.tools.memory_tools import (
        MemoryForgetTool as _MemoryForgetTool,
    )
    from bandi.tools.memory_tools import (
        MemoryReadTool as _MemoryReadTool,
    )
    from bandi.tools.memory_tools import (
        MemorySearchTool as _MemorySearchTool,
    )

    registry.register_instance(_MemorySearchTool(db_dir=_db_dir, long_term=_memory_long_term))
    registry.register_instance(_MemoryReadTool(db_dir=_db_dir))
    # 结构化三元组写入——Agent 显式给三元组，代码只按已声明关系词汇校验
    registry.register_instance(
        _MemoryAssertTool(long_term=_memory_long_term, llm=_entity_judge_llm(subagent_provider))
    )
    # 遗忘全 scope：episodic/working/global 三 store 路径与
    # _build_memory_manager / _build_hierarchy_manager 的生产库同源
    registry.register_instance(
        _MemoryForgetTool(
            db_dir=_db_dir,
            long_term=_memory_long_term,
            episodic=_EpisodicStore(str(_db_dir / "episodic.db")),
            working=_ScratchpadStore(str(_db_dir / "working.db")),
            global_store=_GlobalMemoryStore(str(_db_dir / "hierarchy_global.db")),
        )
    )

    async def _workflow_tool_runner(tool_name: str, params: dict) -> Any:
        # 功能：工具节点执行器——registry 取工具→校验参数→执行；失败显式抛错使工作流进入 failed
        tool = registry.get(tool_name)
        if tool is None:
            raise RuntimeError(f"工作流引用的工具未注册或特性组未开启: {tool_name}")
        param_errors = tool.validate_params(params)
        if param_errors:
            raise RuntimeError(f"工具 {tool_name} 参数校验失败: {'; '.join(param_errors)}")
        result = await tool.execute(params)
        if isinstance(result, dict) and (
            not result.get("success", True) or result.get("ok") is False
        ):
            raise RuntimeError(f"工具 {tool_name} 执行失败: {result.get('error') or result}")
        return result

    _workflow_service = _WorkflowService(
        _WorkflowStore(
            str(workflow_db_path if workflow_db_path is not None else _db_dir / "workflow.db")
        ),
        provider=subagent_provider,
        tool_runner=_workflow_tool_runner,
    )
    for _workflow_tool in (
        _WorkflowListTool(_workflow_service),
        _WorkflowRunTool(_workflow_service),
        _WorkflowStatusTool(_workflow_service),
        _WorkflowCancelTool(_workflow_service),
        _WorkflowPauseTool(_workflow_service),
        _WorkflowResumeTool(_workflow_service),
        _WorkflowSaveTool(_workflow_service),
        _ChatWorkflowTool(registry),
        _WorkflowEmbedEmitTool(),
    ):
        registry.register_instance(_workflow_tool)

    # 三元组本体工具（TBox/ABox 纪律）：显式 `triple_ontology_store` 优先；
    # 兼容旧装配——`entity_store` 本身就是三元组 store 时同样注册。
    # 两者皆无（或非 OntologyStore）则不注册：缺席比半残好（execute 里 store 为 None 会崩）。
    ontology_target = triple_ontology_store or (
        entity_store if isinstance(entity_store, OntologyStore) else None
    )
    if isinstance(ontology_target, OntologyStore):
        from bandi.ontology.tools import OntologyToolContext
        from bandi.tools.ontology_tool_adapter import build_ontology_tools

        ontology_context = OntologyToolContext(
            store=ontology_target,
            inference=bool(ontology_inference),
            default_limit=int(ontology_query_limit),
            max_limit=int(ontology_query_limit_max),
        )
        for ontology_tool in build_ontology_tools(ontology_context):
            registry.register_instance(ontology_tool)

    for g in registry.check_feature_group_coverage():
        logger.warning(
            "配置 feature_flags.tool_groups 含未知组 %r：无任何工具声明该组，关闭不生效（请检查拼写）",
            g,
        )

    return registry


async def build_mcp_broker(
    bandi_config: BandiConfig, registry: ToolRegistry | None = None
) -> MCPBroker | None:
    """功能：按配置构建 MCP Broker；无配置返回 None，不连接不注册。

    有配置时：注册各 server → 连接 → 注册工具到 registry → 启动健康检查。
    """
    servers = bandi_config.tools.mcp_servers
    if not servers:
        return None

    from bandi.tools.mcp_broker import MCPBroker
    from bandi.tools.mcp_client import MCPClient, StdioTransport, StreamableHTTPTransport

    broker = MCPBroker()
    for server in servers:
        # 功能：单台服务器开关——停用的服务器不连接、不注册工具
        if not bool(getattr(server, "enabled", True)):
            logger.info("MCP server %s disabled by settings; skipped", server.name)
            continue
        if server.transport == "stdio":
            transport = (
                StdioTransport(server.command, server.args, env=server.env)
                if server.env
                else StdioTransport(server.command, server.args)
            )
        else:
            transport = StreamableHTTPTransport(server.url)
        broker.register(
            MCPClient(server.name, transport),
            pool_name=server.pool or server.name,
            prefix=server.prefix or None,
            allowed_tools=None if server.allowed_tools is None else set(server.allowed_tools),
        )

    errors = await broker.connect_all()
    for pool, pool_errors in errors.items():
        logger.warning("MCP pool %s connect errors: %s", pool, pool_errors)
    if registry is not None:
        await broker.register_all_tools(registry)
        from bandi.tools.mcp_resources import register_mcp_resource_tools

        register_mcp_resource_tools(registry, broker)
    broker.start_health_check()
    return broker


def _resolve_judge_provider(
    bandi_config: BandiConfig, main_provider: BaseProvider
) -> BaseProvider | None:
    """Judge / 反馈模型解析——反思验收（LLM-as-Judge）专用 provider：

    1) **反馈档案优先**（设置页「反馈模型」用户指定，跨 provider 完整覆盖 model/key/base_url）；
    2) 退回 `verification.judge_model`（同 provider 换模型名，消解「同模型自评」偏差）；
    3) 均未配置 → None（跟随主模型，零回归）；MockProvider 主模型（测试）不建 judge。

    修复：两条路径改走 `_build_provider_direct`（不再经 `with_resolved_llm`）
    避免激活档案反向覆盖 judge 模型——此前「judge_model + 已激活档案」= judge_model 被吞。
    """
    if isinstance(main_provider, MockProvider):
        return None
    from bandi.llm.profiles import resolve_feedback_llm_config

    feedback_llm = resolve_feedback_llm_config(bandi_config.llm)
    if feedback_llm is not None:
        return _build_provider_direct(bandi_config.model_copy(update={"llm": feedback_llm}))
    judge_model = (bandi_config.verification.judge_model or "").strip()
    if not judge_model:
        return None
    judge_cfg = bandi_config.model_copy(
        update={"llm": bandi_config.llm.model_copy(update={"default_model": judge_model})}
    )
    return _build_provider_direct(judge_cfg)


def build_provider(bandi_config: BandiConfig) -> BaseProvider:
    """创建 LLM Provider（无 API key 时回退 MockProvider）。

    with_circuit_breaker/RetryPolicy 生产零调用，LLM 熔断重试链路
    形同虚设（网络抖动直接抛错，无指数退避重试）。
    """
    from bandi.llm.profiles import with_resolved_llm

    bandi_config = with_resolved_llm(bandi_config)
    return _build_provider_direct(bandi_config)


def _build_provider_direct(bandi_config: BandiConfig) -> BaseProvider:
    # 功能：按**已解析**配置直接构建 provider（不再做档案解析——judge/feedback 专用路径
    # 由调用方负责档案覆盖；避免激活档案二次改写指定模型）
    if bandi_config.llm and bandi_config.llm.api_key:
        from bandi.llm.circuit_breaker import CircuitBreaker, RetryPolicy

        reg = ProviderRegistry(bandi_config)
        provider = reg.get_provider(
            bandi_config.llm.default_model or "deepseek-v4.1-flash",
            bandi_config,
            resolve_profile=False,
        )
        return provider.with_circuit_breaker(
            # 5xx/超时/连接类瞬时故障走指数退避 2/4/8/16 ≈ 30s 自愈窗口；
            # 4xx 立即抛出，分钟级宕机交人工重试
            CircuitBreaker(),
            RetryPolicy(max_retries=4, base_delay=2.0),
        )
    return MockProvider()


def build_tool_pipeline(
    permissions_pipeline: PermissionsPipeline | None = None,
    ratelimit_max_calls: int = 30,
    ratelimit_window_sec: int = 60,
    cb_threshold: int = 5,
    cb_recovery_timeout: float = 60.0,
    retry_max_retries: int = 2,
    retry_base_delay: float = 1.0,
    enable_ratelimit: bool = True,
    enable_circuit_breaker: bool = True,
    enable_retry: bool = True,
    enable_telemetry: bool = True,
    enable_ownership: bool = False,
    work_item_provider: Callable[[Any, Any], Any] | None = None,
    mcp_broker: MCPBroker | None = None,
) -> ToolPipeline:
    pipeline = ToolPipeline()
    pipeline.add(PermissionMiddleware(permissions_pipeline))
    if enable_ownership:
        pipeline.add(OwnershipMiddleware(work_item_provider=work_item_provider))
    if enable_ratelimit:
        pipeline.add(
            RateLimitMiddleware(SlidingWindowRateLimiter(ratelimit_max_calls, ratelimit_window_sec))
        )
    if enable_circuit_breaker:
        pipeline.add(CircuitBreakerMiddleware(cb_threshold, cb_recovery_timeout))
    # 消除 MCPConnectionMiddleware×Retry 级联放大（最坏 1×(1+2)）
    if enable_retry:
        pipeline.add(
            RetryMiddleware(
                max_retries=retry_max_retries, base_delay=retry_base_delay, mcp_broker=mcp_broker
            )
        )
    if enable_telemetry:
        pipeline.add(TelemetryMiddleware())
    return pipeline


def _default_account_store() -> Any:
    """功能：默认账户库（进程内单例）——本地单机形态下的唯一用户库。

    仅在 `user_id` 非空且调用方未显式传库时才被触达（`_resolve_account_store`）。
    """
    global _ACCOUNT_STORE_SINGLETON
    if _ACCOUNT_STORE_SINGLETON is None:
        from pathlib import Path

        from bandi.core.account import AccountStore

        _ACCOUNT_STORE_SINGLETON = AccountStore(str(Path.home() / ".bandi" / "data" / "account.db"))
    return _ACCOUNT_STORE_SINGLETON


def _resolve_account_store(user_id: str, account_store: object | None) -> object | None:
    """功能：解析岗位绑定所需的账户库——显式优先，缺省自建，失败 fail-open。

    原缺陷：公司链只传 `user_id` 不传 `account_store`，而 `effective_role_for`
    的启用条件是 `user_id and account_store is not None` → 整段绑定解析被跳过
    「用户 → 岗位 → 权限档」形同未接。此处补齐兜底，让**所有**传了 user_id 的调用点
    都能真正生效（CLI 空 user_id 路径零副作用不变）。
    """
    if account_store is not None:
        return account_store
    if not (user_id or "").strip():
        return None
    try:
        return _default_account_store()
    except Exception as e:  # noqa: BLE001 — 账户库不可用不得阻断执行（权限是加固项）
        logger.warning("默认账户库不可用，回落 agent_role：%s", e)
        return None


def build_policy_engine(
    pp: PermissionsPipeline,
    agent_role: str = "user",
    role_map: Mapping[str, str] | None = None,
    *,
    user_id: str = "",
    account_store: object | None = None,
) -> PolicyEngine:
    """功能：生产权限策略装配——内建策略 + 角色策略注册（policy_engine 单一构造点）。

    审计身份由 PolicyEngine 裁决（实体/角色 ASK）；工具级 deny/mode/shell
    子检查不在此处复述——由 PermissionMiddleware 的 pp.check 单点评估。
    6.5：DatasourcePolicy 已删除（无 resource_type="datasource" 消费方）。

    方案 A：把**组织岗位**解析成**内置权限档**后再注册策略，两条来源按优先级：
      1. `user_id` →（显式库，或本函数自解析的默认库）→ 该用户的**岗位绑定**；
      2. 回落 `agent_role`。
    两者都缺省时 `effective_role_for` 原样返回 → 与改造前行为**逐字节一致**。
    """
    from bandi.core.permission.builtin import EntityPolicy
    from bandi.core.permission.role_mapping import check_role_map, effective_role_for
    from bandi.core.permission.roles import register_builtin_role

    check_role_map(role_map)

    engine = PolicyEngine()
    engine.add(EntityPolicy())
    register_builtin_role(
        engine,
        effective_role_for(
            agent_role,
            user_id=user_id,
            role_map=role_map,
            account_store=_resolve_account_store(user_id, account_store),
        ),
    )
    return engine


def _build_auto_review_llm(provider: Any | None, bus: Any, session_id: str | None) -> Any:
    """功能：6.10 auto_review 复核 callable 构建——经 accounted_complete 计费
    通道调用 provider（旁路 LLM 计费纪律）；provider 缺失返回 None（保持禁用
    auto_review_decision 走 fail-safe escalate）。"""
    if provider is None:
        return None

    async def _review(messages: list[dict[str, Any]]) -> str:
        from bandi.llm.accounted import accounted_complete

        resp = await accounted_complete(
            provider,
            messages,
            label="auto_review",
            session_id=session_id or "default",
            bus=bus,
        )
        return str(getattr(resp, "content", "") or "")

    return _review


def _build_tool_summary_llm(provider: Any | None, bus: Any) -> Any:
    """功能：工具结果摘要 callable——accounted_complete 计费通道
    （label="tool_summary"）；provider 缺失返回 None（仅规则摘要）。"""
    if provider is None:
        return None

    async def _summary(prompt: str, session_id: str) -> str:
        from bandi.llm.accounted import accounted_complete

        resp = await accounted_complete(
            provider,
            [{"role": "user", "content": prompt}],
            label="tool_summary",
            session_id=session_id or "default",
            bus=bus,
        )
        return str(getattr(resp, "content", "") or "")

    return _summary


def get_provider_registry(bandi_config: BandiConfig) -> ProviderRegistry:
    """功能：进程级 provider 注册表缓存——同 provider+网关复用，凭证指纹留在实例键内。"""
    cache_key = f"{bandi_config.llm.provider}:{bandi_config.llm.base_url or ''}"
    registry = _REGISTRY_CACHE.get(cache_key)
    if registry is None:
        registry = ProviderRegistry(bandi_config)
        _REGISTRY_CACHE[cache_key] = registry
    return registry


def resolve_context_window(bandi_config: BandiConfig) -> int:
    """功能：按实际模型解析有效上下文窗口。

    放开语义（全分支目录优先）：
    - 目录收录该模型 → 一律采用目录窗口。（85% 压缩水位按模型真实窗口计算，
      不被静态配置锁死；大窗口模型如 1M 在 ~833K 才触发压缩。）
    - 目录未收录 → 显式配置（>0）兜底；无配置回落 `DEFAULT_CONTEXT_WINDOW`
      安全值（既不猜目录，也不退化为 0——0 会让 `effective_context_window`
      算成 1 token 致全链 PROMPT_TOO_LONG）。

    压缩块预算不受放开影响（`CompressionPipeline._effective_target` 取
    `min(绝对预算, 窗口×系数)`，大窗保持绝对预算不变）。
    """
    from bandi.core.config import DEFAULT_CONTEXT_WINDOW

    configured = int(getattr(bandi_config.agent, "context_window", 0) or 0)
    model = str(getattr(bandi_config.llm, "default_model", "") or "")
    if not model:
        return configured if configured > 0 else DEFAULT_CONTEXT_WINDOW
    try:
        from bandi.llm.model_catalog import ModelCatalog

        spec = ModelCatalog().get(model)
    except Exception:  # noqa: BLE001 — 目录不可用不阻断装配
        return configured if configured > 0 else DEFAULT_CONTEXT_WINDOW
    catalog_window = int(getattr(spec, "context_window", 0) or 0) if spec is not None else 0
    if catalog_window > 0:
        return catalog_window
    return configured if configured > 0 else DEFAULT_CONTEXT_WINDOW


def build_agent_context(
    user_input: str,
    mode: str = "execute",
    provider: Any | None = None,
    tool_registry: ToolRegistry | None = None,
    bus: EventBus | None = None,
    bandi_config: BandiConfig | None = None,
    entity_store: Any | None = None,
    triple_ontology_store: Any | None = None,
    mcp_broker: MCPBroker | None = None,
    user_interaction: UserInteraction | None = None,
    enable_ownership: bool = False,
    work_item: Any | None = None,
    session_id: str | None = None,
    communication: Any | None = None,
    agent_role: str = "user",
    project_root: str | None = None,
    role_tool_tags: list[str] | None = None,
    role_fs_scopes: list[str] | None = None,
    manager_capable: bool = True,
    # 功能：员工档案里 resource_type=tool 的绑定名单——非空即收窄工具面（空=不限制）
    resource_tool_allowlist: list[str] | None = None,
    extra_surface_tools: list[str] | None = None,
    skill_scope: dict[str, Any] | None = None,
    abort_controller: Any | None = None,
    user_id: str = "",
    account_store: object | None = None,
    relax_workspace_asks: bool = False,
) -> ExecutionContext:
    """统一创建 ExecutionContext。abort_controller 透传 ToolExecutor（工具级取消通道）。

    方案 A（续）：`user_id` + `account_store` 用于把**该用户的岗位绑定**解析成
    权限档（见 `core/permission/role_mapping.effective_role_for`）。两者缺省时
    完全不触发绑定查询 → 默认路径行为与改造前逐字节一致。
    """
    if bandi_config is None:
        bandi_config = BandiConfig.load()
    if provider is None:
        provider = build_provider(bandi_config)
    if tool_registry is None:
        t = bandi_config.tools
        tool_registry = build_tool_registry(
            entity_store,
            git_timeout=t.git_timeout,
            web_search_timeout_tavily=t.web_search_timeout_tavily,
            web_search_timeout_exa=t.web_search_timeout_exa,
            web_search_timeout_ddg=t.web_search_timeout_ddg,
            web_search_circuit_fail_limit=t.web_search_circuit_fail_limit,
            web_search_circuit_reset_secs=t.web_search_circuit_reset_secs,
            bash_timeout_default=t.bash_timeout_default,
            bash_timeout_max=t.bash_timeout_max,
            python_exec_timeout_default=t.python_exec_timeout_default,
            python_exec_timeout_max=t.python_exec_timeout_max,
            python_exec_max_stdout_chars=t.python_exec_max_stdout_chars,
            python_exec_max_stderr_chars=t.python_exec_max_stderr_chars,
            browser_timeout_default=t.browser_timeout_default,
            browser_timeout_max=t.browser_timeout_max,
            browser_post_click_delay_ms=t.browser_post_click_delay_ms,
            browser_snapshot_preview_chars=t.browser_snapshot_preview_chars,
            browser_max_interactive_elements=t.browser_max_interactive_elements,
            browser_element_text_max_chars=t.browser_element_text_max_chars,
            web_fetch_timeout_default=t.web_fetch_timeout_default,
            web_fetch_timeout_max=t.web_fetch_timeout_max,
            web_fetch_max_bytes=t.web_fetch_max_bytes,
            web_fetch_fallback_preview_chars=t.web_fetch_fallback_preview_chars,
            web_fetch_allow_private_networks=t.web_fetch_allow_private_networks,
            subagent_provider=provider,
            web_search_max_results=t.web_search_max_results,
            feature_flags=t.feature_flags.tool_groups,
            subagent_max_concurrency=t.subagent_max_concurrency,
        )
    if bus is None:
        bus = EventBus()

    # registry 尚未成型，后绑定）——子 agent 工具面经 tools_allowlist 过滤继承
    subagent_tool = tool_registry.get("subagent_spawn")
    if subagent_tool is not None and hasattr(subagent_tool, "bind_registry"):
        subagent_tool.bind_registry(tool_registry)
    skill_tool = tool_registry.get("skill_invoke")
    if skill_tool is not None and hasattr(skill_tool, "bind_registry"):
        skill_tool.bind_registry(tool_registry)

    from bandi.agent.mode_manager import ModeManager

    mode_manager = ModeManager(initial_mode=mode)

    perm_config = bandi_config.permissions
    # 员工执行链（公司模式）放宽工作区写类工具的「每次询问」：write/edit/bash 在
    # 项目工作区内由 fs/shell 守卫与 deny 规则兜底，无需逐次人工批复
    relaxed_ask_tools: set[str] | None = (
        None if perm_config.ask_tools is None else set(perm_config.ask_tools)
    )
    if relax_workspace_asks and relaxed_ask_tools is not None:
        relaxed_ask_tools -= {"bash", "write", "edit", "apply_patch"}
    from bandi.core.permission.rules import parse_permission_rule

    perm_rules = []
    for entry in perm_config.rules or []:
        parsed = parse_permission_rule(entry)
        if parsed is None:
            logger.warning("permission rule skipped (invalid): %r", entry)
        else:
            perm_rules.append(parsed)
    from bandi.tools.hooks import HookRunner, load_hook_rules

    hook_runner = HookRunner(
        load_hook_rules(bandi_config.hooks),
        provider=provider,
        project_root=project_root,
        session_id=session_id or "",
        bus=bus,
        user_id=user_id,
    )
    pp = PermissionsPipeline(
        deny_tools=None if perm_config.deny_tools is None else set(perm_config.deny_tools),
        ask_tools=relaxed_ask_tools,
        ask_risk_levels=perm_config.ask_risk_levels,
        bypass_tools=None if perm_config.bypass_tools is None else set(perm_config.bypass_tools),
        deny_commands=perm_config.deny_commands,
        user_interaction=user_interaction,
        project_root=project_root,
        rules=perm_rules,
    )
    if user_interaction is not None:
        _ui_tool = tool_registry.get("request_user_input")
        if _ui_tool is not None and hasattr(_ui_tool, "bind_ui"):
            _ui_tool.bind_ui(user_interaction)
        # 孤立实体守卫的人机协同入口：ontology_assert 确认通道（headless 不绑定 → 回滚）
        _ontology_assert_tool = tool_registry.get("ontology_assert")
        if _ontology_assert_tool is not None and hasattr(_ontology_assert_tool, "bind_ui"):
            _ontology_assert_tool.bind_ui(user_interaction)
    _policy_parts = [
        sorted(perm_config.deny_tools or []),
        sorted(perm_config.ask_tools or []),
        sorted(perm_config.bypass_tools or []),
        sorted(perm_config.deny_commands or []),
        sorted(role_tool_tags or []),
    ]
    policy_version = hashlib.sha256(repr(_policy_parts).encode()).hexdigest()[:16]
    tool_pipeline = build_tool_pipeline(
        permissions_pipeline=pp,
        mcp_broker=mcp_broker,
        enable_ownership=enable_ownership,
        work_item_provider=(lambda _ctx, _call: work_item) if enable_ownership else None,
    )
    # 单一构造点：内建策略 + 角色策略（工具级 deny/mode/shell 由
    # PermissionMiddleware 的 pp.check 单点评估，此处不复述）
    # （两者都缺省 = 原样返回 = 行为不变）
    policy_engine = build_policy_engine(
        pp,
        agent_role,
        perm_config.role_map,
        user_id=user_id,
        account_store=account_store,
    )
    # （write 覆盖写幂等）执行成功后结果落盘，checkpoint 恢复跨进程重放时命中直接返回
    from pathlib import Path as _Path

    from bandi.tools.cache import ToolResultCache
    from bandi.tools.idempotency import IdempotencyStore

    idem_store = IdempotencyStore(str(_Path.home() / ".bandi" / "idempotency"))
    # 完整），子代理工具的父链 extra 与 agent 动作派生的子代理不再恒 "default"
    _role_label = agent_role or "main"
    _agent_sid = str(session_id or "")
    agent_id = f"{_role_label}@{_agent_sid[-8:]}" if _agent_sid else _role_label
    executor = ToolExecutor(
        tool_registry,
        idempotency_store=idem_store,
        result_cache=ToolResultCache(),
        event_bus=bus,
        permissions_pipeline=pp,
        policy_engine=policy_engine,
        mode=mode_manager.mode,
        agent_id=agent_id,
        execution_timeout=bandi_config.tools.execution_timeout,
        output_preview_chars=bandi_config.tools.output_preview_chars,
        consecutive_failure_threshold=bandi_config.tools.consecutive_failure_threshold,
        chars_per_token=bandi_config.tools.chars_per_token,
        max_output_tokens=bandi_config.tools.max_output_tokens,
        temp_dir=bandi_config.tools.temp_dir,
        per_tool_max_output_tokens=bandi_config.tools.per_tool_max_output_tokens,
        tool_pipeline=tool_pipeline,
        communication=communication,
        agent_role=agent_role,
        user_id=user_id,
        project_root=project_root,
        role_tool_tags=role_tool_tags,
        role_fs_scopes=role_fs_scopes,
        skill_scope=skill_scope,
        policy_version=policy_version,
        provider_version=getattr(provider, "version", "") or "",
        max_concurrency=bandi_config.tools.max_concurrency,
        abort_controller=abort_controller,
        # 不再恒 escalate 人工）
        auto_review_llm=_build_auto_review_llm(provider, bus, session_id),
        summary_llm=_build_tool_summary_llm(provider, bus),
        hook_runner=hook_runner,
    )

    allowed_tools = mode_manager.filter_tools([t.name for t in tool_registry.list_tools()])
    # HookBefore 插最前（deny 短路 + updatedInput 改写，权限链继续执行不受影响）；
    # HookAfter 的 before 恒放行，after 链同序执行（结果追加 [hook context]）
    from bandi.tools.middleware import HookAfterMiddleware, HookBeforeMiddleware

    tool_pipeline.add(HookAfterMiddleware(hook_runner))
    tool_pipeline.add(HookBeforeMiddleware(hook_runner), index=0)
    allowed_tools = filter_tools_by_role_tags(
        allowed_tools, tool_registry, role_tool_tags, manager_capable=manager_capable
    )
    allowed_tools = _narrow_tools_by_resources(
        allowed_tools, tool_registry, resource_tool_allowlist, manager_capable=manager_capable
    )
    # 不含 request_user_input → Agent 无法提问，多选项卡永不触发）
    from bandi.tools.registry import with_always_available

    allowed_tools = with_always_available(allowed_tools)
    # u-fix plan 白名单只读性守卫——腐化成员（写盘/高危/ask-override）打观测日志
    from bandi.tools.permissions_pipeline import plan_mode_tool_violations

    _violations = plan_mode_tool_violations(tool_registry)
    if _violations:
        logging.getLogger(__name__).warning(
            "PLAN_MODE_TOOLS safety violations (plan mode skips approval layers): %s",
            "; ".join(_violations),
        )
    # context_window 按实际模型回填 + 输出 token 预留（cap 20K，2.10）传入
    # （model_copy 复制，避免污染共享的 BandiConfig.agent——同一 config 对象会被多次装配复用）。
    ctx = ExecutionContext(
        messages=[],
        provider=provider,
        config=bandi_config.agent.model_copy(
            update={
                "context_window": resolve_context_window(bandi_config),
                "reserved_output_tokens": (
                    int(getattr(bandi_config.agent, "reserved_output_tokens", 0) or 0)
                    or min(
                        max(int(getattr(bandi_config.llm, "max_output_tokens", 0) or 0), 0),
                        20_000,
                    )
                ),
            }
        ),
        bus=bus,
    )
    from bandi.utils.token_counter import set_active_model_for_provider

    set_active_model_for_provider(provider)
    from bandi.agent.recovery_manager import RecoveryManager

    ctx.recovery_manager = RecoveryManager(bus=bus)
    ctx.trace_id = generate_trace_id()
    # trace_id 进 meta：executor 经 bind_session_meta 可见，子 Agent 派生工具
    ctx.meta["trace_id"] = ctx.trace_id
    ctx.raw_input = user_input
    ctx.tool_executor = executor
    all_schemas = (
        tool_registry.list_schemas(allowed_tools=allowed_tools) if allowed_tools is not None else []
    )
    mcp_names = {str(name) for name in (extra_surface_tools or []) if str(name)}
    tools_config = getattr(bandi_config, "tools", None)
    selected = select_relevant_schemas(
        all_schemas,
        user_input,
        mcp_tool_names=mcp_names,
        mcp_lazy=bool(getattr(tools_config, "mcp_lazy_exposure", True)),
        mcp_max_tools=int(getattr(tools_config, "mcp_max_exposed_tools", 5) or 0),
    )
    if mcp_names:
        selected_names = {schema_name(schema) for schema in selected}
        selected.extend(
            schema
            for schema in all_schemas
            if schema_name(schema) in mcp_names and schema_name(schema) not in selected_names
        )
    ctx.active_tools = selected
    ctx.meta["policy_engine"] = policy_engine
    # 运行时角色进 meta：成本按角色归属（LLM_RESPONSE 事件透出）+ 可观测
    ctx.meta["agent_role"] = agent_role
    # 归因。形如 "<role>@<会话尾 8 位>"：同角色的并发会话可区分；无会话时回落角色名；
    # 角色也为空时回落 "main"（降级但不空）。值在 executor 构造前已生成（同源）。
    ctx.meta["agent_id"] = agent_id
    # 子代理父链据此不再是空串）；executor 已在构造时绑定，setter 幂等保留
    executor.set_agent_id(agent_id)
    hook_runner.set_agent_id(agent_id)
    # 充当 user_id。缺省为空串时消费方回落原值，行为不变。
    if user_id:
        ctx.meta["user_id"] = user_id
    ctx.meta["mode"] = mode_manager.mode
    ctx.meta["mode_manager"] = mode_manager
    ctx.meta["bandi_config"] = bandi_config
    # 上下文（agent 参数不可伪造；close_human_review 等据此判定）
    if work_item is not None:
        # （纯文本轮是否提名团队记忆）的唯一依据，agent 无法自证
        ctx.meta["work_item_id"] = str(getattr(work_item, "id", "") or "")
        _wi_meta = getattr(work_item, "metadata", None) or {}
        if _wi_meta.get("human_review_close_allowed"):
            ctx.meta["human_review_close_allowed"] = True
    # session_id 必须进 ctx.meta——pipeline.process 守卫
    # `if context_registry and session_id`（perception/pipeline.py:180）依赖它
    # 否则公司链路（seat_executor 传 member_session_id）记忆/偏好上下文收集不触发
    if session_id:
        ctx.session_id = session_id
        ctx.meta["session_id"] = session_id
    from bandi.llm.mock import MockProvider as _MockP

    if not isinstance(provider, _MockP):
        ctx.meta["provider_registry"] = get_provider_registry(bandi_config)

    from bandi.llm.mock import MockProvider

    # Verifier/HallucinationChecker 连带静默失效（阈值仍取 safety 段字段）
    if bandi_config.verification.enabled:
        from bandi.eval.hallucination_checker import HallucinationChecker
        from bandi.ontology.lookup import OntologyEntityLookup

        entity_lookup = (
            OntologyEntityLookup(triple_ontology_store)
            if triple_ontology_store is not None
            else None
        )
        ctx.hallucination_checker = HallucinationChecker(
            graph=entity_lookup,
            provider=provider if not isinstance(provider, MockProvider) else None,
            l2_threshold=bandi_config.safety.l2_threshold,
            block_threshold=bandi_config.safety.block_threshold,
        )

        from bandi.agent.verifier import Verifier, VerifierPolicy

        vp = bandi_config.verification
        ctx.verifier = Verifier(
            policy=VerifierPolicy(
                enabled=vp.enabled,
                min_todos_for_verification=vp.min_todos_for_verification,
                fail_open=False,
                stagnation_threshold=vp.stagnation_threshold,
                require_on_code_edits=vp.require_on_code_edits,
            ),
            provider=provider if not isinstance(provider, MockProvider) else None,
            judge_provider=_resolve_judge_provider(bandi_config, provider),
        )

    from pathlib import Path

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    if db_path.exists():
        from bandi.eval.badcase_tracker import BadCaseTracker
        from bandi.safety.pii_detector import PIIDetector

        conn = connect_app_db(db_path, row_factory=True)
        ctx.badcase_tracker = BadCaseTracker(conn, masker=PIIDetector().sanitize)

    if isinstance(provider, MockProvider):
        from bandi.core.message import LLMResponse, TokenUsage

        # 演示回显：超长输入（员工任务契约等）摘要展示；标注演示模式与切换方法
        preview = user_input if len(user_input) <= 240 else f"{user_input[:240]}…"
        provider.add_response(
            LLMResponse(
                content=(
                    f"Mock回复: {preview}\n\n"
                    "> 演示模式：未配置模型 API Key，以上为占位回显；"
                    "配置模型后可看到真实工作产出。"
                ),
                # 演示可观测性：Mock 也产出思考流（前端过程块「思考」节点数据源）——
                # 真实 LLM 下由 provider reasoning 提供，此处仅模拟工作思路形态
                reasoning=(
                    "先明确任务目标与产出要求，再梳理项目上下文与已有结论，"
                    "最后整理关键要点并给出结论。"
                ),
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
                finish_reason="stop",
            )
        )

    ctx.meta["hook_runner"] = hook_runner

    return ctx


def _narrow_tools_by_resources(
    tool_names: list[str],
    registry: ToolRegistry,
    allowlist: list[str] | None,
    *,
    manager_capable: bool = True,
) -> list[str]:
    # 功能：按员工档案的工具绑定收窄工具面——只收窄不放宽（与模式/角色标签取交集，
    # 绑定不会越权放开角色未授权的工具）。空名单 = 未配置 = 不过滤（opt-in）。
    # 协作/派单工具与角色标签路径同源豁免：员工被创建即具备组织结构性权利。
    if not allowlist:
        return tool_names
    allowed = {str(name).strip() for name in allowlist if str(name).strip()}
    if not allowed:
        return tool_names
    registered = {tool.name for tool in registry.list_tools()}
    unknown = sorted(allowed - registered)
    if unknown:
        logger.warning(
            "员工工具绑定含未注册工具，已忽略：%s（绑定名单=%s）", unknown, sorted(allowed)
        )
    exempt = ToolRegistry.COLLAB_ALWAYS_AVAILABLE_TOOLS
    if not manager_capable:
        exempt = exempt - ToolRegistry.MANAGER_ONLY_TOOLS
    kept = [name for name in tool_names if name in allowed or name in exempt]
    if not kept:
        logger.warning(
            "员工工具绑定与当前可用工具无交集 → 该员工工具面仅剩交互/协作通道（绑定=%s）",
            sorted(allowed),
        )
    return kept


def filter_tools_by_role_tags(
    tool_names: list[str],
    registry: ToolRegistry,
    role_tool_tags: list[str] | None,
    *,
    manager_capable: bool = True,
) -> list[str]:
    # 功能：role-scoped 工具过滤——按角色 tool_tags 裁剪工具名单；
    # 空列表 = 显式全禁（fail-closed，角色查询异常路径），不折叠为不过滤；
    # 全为非法值（非 ToolCategory 值域）= 该值从未表达过有效约束 ⇒ 视为未配置（不过滤）。
    # manager_capable=False（无派发面的员工卡）：管理者专属工具不进工具面——与角色
    # 标签无关的通用规则（换团队/未配置角色也生效）。
    names = list(tool_names)
    if not manager_capable:
        names = [n for n in names if n not in ToolRegistry.MANAGER_ONLY_TOOLS]
    if role_tool_tags is None:
        return names
    raw = {str(t).strip() for t in role_tool_tags if str(t).strip()}
    if not raw:
        return []
    valid, dropped = normalize_role_tool_tags(sorted(raw))
    if dropped:
        logger.error(
            "role tool_tags 含非法值（非 ToolCategory 值域）：%s；已忽略。合法值=%s，原始输入=%s",
            dropped,
            valid,
            sorted(raw),
        )
    if not valid:
        # 全非法 ⇒ 视为未配置（防员工零工具）；不得按「空名单=全禁」处理
        return tool_names
    tags = set(valid)
    tagged = {t.name for t in registry.list_tools() if any(tag in tags for tag in (t.tags or []))}
    if not tagged:
        logger.error(
            "role tool_tags %s 与注册工具标签无交集 → 该角色可用工具为 0（权限语义保留）",
            sorted(tags),
        )
    # 协作/派单工具豁免（组织结构性权利）：与执行门禁 executor._role_gate 同源
    exempt = ToolRegistry.COLLAB_ALWAYS_AVAILABLE_TOOLS
    if not manager_capable:
        exempt = exempt - ToolRegistry.MANAGER_ONLY_TOOLS
    return [n for n in tool_names if n in tagged or n in exempt]


async def run_agent(
    user_input: str,
    mode: str = "execute",
    session_store: Any | None = None,
    session_id: str | None = None,
    entity_store: Any | None = None,
    bandi_config: BandiConfig | None = None,
    bus: EventBus | None = None,
    work_item: Any | None = None,
    project_id: str | None = None,
    employee_id: str | None = None,
    group_conversation_id: str = "",
    dm_conversation_id: str = "",
    project_root: str | None = None,
    communication: Any | None = None,
    agent_role: str = "user",
    company_runtime: Any | None = None,
    role_tool_tags: list[str] | None = None,
    role_fs_scopes: list[str] | None = None,
    manager_capable: bool = True,
    skill_scope: dict[str, Any] | None = None,
    company_store: Any | None = None,
    company_thread_manager: Any | None = None,
    company_org_engine: Any | None = None,
    provider_config: dict[str, Any] | None = None,
    abort_controller: Any | None = None,
    user_id: str = "",
    account_store: Any | None = None,
    stream_handler: Any | None = None,
    user_interaction: UserInteraction | None = None,
    relax_workspace_asks: bool = False,
    mcp_allowed_servers: list[str] | None = None,
    approval_capture: dict[str, Any] | None = None,
) -> tuple[str, str | None, int, int]:
    """便利函数：委托 run_agent_turn 统一执行入口（记账/记忆/偏好/工具消息落库一致）。

    work_item 传入时启用 OwnershipMiddleware 并绑定当前工作项（公司模式越界写守卫）。
    agent_role 传入时工具上下文角色为运行时角色（公司链路为 role_id，非默认 user）。
    provider_config 传入时覆盖 config.llm 同名字段（换大脑：per-role/per-employee
    模型路由，键名对齐 LLMConfig 字段，如 default_model/api_key/base_url）。
    user_id + account_store 传入时启用「用户 → 权限档」解析（方案 A 续）；
    缺省不触发绑定查询，行为与改造前一致。
    stream_handler 传入时透传过程帧。
    user_interaction 传入时装配员工交互通道（多问卡/权限卡真挂起，员工链路由
    seat_executor 经 interaction_provider 构建）。
    approval_capture 传入时回填本轮审批锚点（tool_call_id/tool_name，供调度精确恢复）。
    """
    from bandi.core.agent_runner import AgentTurnResult, run_agent_turn

    result: AgentTurnResult = await run_agent_turn(
        user_input=user_input,
        mode=mode,
        session_id=session_id,
        session_store=session_store,
        entity_store=entity_store,
        bandi_config=bandi_config,
        stream_handler=stream_handler,
        project_id=project_id,
        employee_id=employee_id,
        group_conversation_id=group_conversation_id,
        dm_conversation_id=dm_conversation_id,
        project_root=project_root,
        work_item=work_item,
        communication=communication,
        agent_role=agent_role,
        company_runtime=company_runtime,
        role_tool_tags=role_tool_tags,
        role_fs_scopes=role_fs_scopes,
        manager_capable=manager_capable,
        skill_scope=skill_scope,
        company_store=company_store,
        company_thread_manager=company_thread_manager,
        company_org_engine=company_org_engine,
        provider_config=provider_config,
        abort_controller=abort_controller,
        user_id=user_id,
        account_store=account_store,
        user_interaction=user_interaction,
        relax_workspace_asks=relax_workspace_asks,
        mcp_allowed_servers=mcp_allowed_servers,
    )
    if approval_capture is not None:
        approval_capture["tool_call_id"] = result.approval_tool_call_id
        approval_capture["tool_name"] = result.approval_tool_name
    return (result.reply, result.terminal_reason, result.total_tokens, result.turn_index)
