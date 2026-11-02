# 功能：工具注册表（ToolRegistry 登记/查找 + 作用域包装）
from __future__ import annotations

import logging
from typing import Any

from jsonschema import Draft202012Validator

from bandi.tools.base import BaseTool, ToolCapability, ToolCategory
from bandi.tools.dependency_checker import DependencyChecker
from bandi.tools.feature_flags import FeatureGate

logger = logging.getLogger(__name__)

_ROLE_TAGS: dict[str, set[str]] = {
    "admin": {c.value for c in ToolCategory},
    "power_user": {"code", "web", "data", "util"},
    "member": {"web", "data", "util"},
    "readonly": {"web", "util"},
}


def builtin_role_tool_tags(role: str) -> list[str] | None:
    """功能：6.12 内置角色默认工具标签单一来源（原 registry._ROLE_TAGS 的
    category 派生与 seat 的 role.tool_tags 两套同义不同源易漂移）——
    过滤与 seat 回退共用；未知角色返回 None（不启用过滤）。"""
    tags = _ROLE_TAGS.get(role)
    return sorted(tags) if tags else None


def with_always_available(names: list[str]) -> list[str]:
    """功能：并入全域交互通道（ALWAYS_AVAILABLE_TOOLS）——域白名单 / 角色 tool_tags /
    plan 模式只读白名单都只裁剪**能力**，交互通道（向用户提问）必须始终可用。

     复现：plan 模式下 allowed = PLAN_MODE_TOOLS（只读集）
    不含 request_user_input → 秘书回复「当前可用工具里未注册」，多选项卡永不触发。
    """
    missing = [name for name in ToolRegistry.ALWAYS_AVAILABLE_TOOLS if name not in names]
    return [*names, *missing] if missing else names


# 功能：角色 tool_tags 的合法值域 = ToolCategory（工具注册时按类别打标签）
VALID_ROLE_TOOL_TAGS: frozenset[str] = frozenset(c.value for c in ToolCategory)


def normalize_role_tool_tags(raw: Any) -> tuple[list[str], list[str]]:
    """功能：角色 tool_tags 值域归一化（**输入解析路径**，区别于权限路径）。

    组织构建的 `role.tool_tags` 由 LLM 生成（`company/secretary.py`），而提示词
    原先给的是占位符字面量 ⇒ 实际值常落在 ToolCategory 值域之外（工具名 / 中文 /
    照抄占位符）。非法值会让 `filter_tools_by_role_tags` 求交为空 ⇒ **该角色员工
    拿到 0 个工具**（108 个工具全被滤掉）。

    返回 `(合法值升序去重, 被丢弃值升序去重)`。
    ⚠️ 语义边界：本函数只做「解析 + 丢弃」，**不**决定权限。调用方遇到「全不匹配」
    必须按「未配置（= 不过滤）」处理，**不得**沿用权限路径的「空名单 = 全禁」语义
    ——否则 LLM 填错一个字就等于把该员工全部工具禁掉。
    """
    if not raw:
        return [], []
    valid: set[str] = set()
    dropped: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        (valid if text in VALID_ROLE_TOOL_TAGS else dropped).add(text)
    return sorted(valid), sorted(dropped)


def _assert_valid_schema(tool: BaseTool) -> None:
    # 功能：注册门禁——非法参数 schema fail-closed，拒绝带病注册
    schema = tool.parameters
    if not isinstance(schema, dict):
        raise ValueError(f"tool {tool.name} parameters 必须是 dict，实际为 {type(schema).__name__}")
    if not schema:
        return
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError(f"tool {tool.name} parameters schema 非法: {exc}") from exc


class ScopedToolRegistry:
    def __init__(self, registry: ToolRegistry, tags: set[str]) -> None:
        self._registry = registry
        self._tags = tags

    def list_schemas(
        self, mode: str | None = None, allowed_tools: list[str] | None = None
    ) -> list[dict[str, Any]]:
        tools = self.list_tools()
        if allowed_tools is not None:
            tools = [t for t in tools if t.name in allowed_tools]
        if mode == "plan":
            tools = [t for t in tools if t.name in ToolRegistry.PLAN_MODE_ALLOWED]
        return [t.to_openai_schema() for t in tools]

    def list_tools(self) -> list[BaseTool]:
        return [t for t in self._registry.list_tools() if any(tag in self._tags for tag in t.tags)]

    def get(self, name: str) -> BaseTool | None:
        t = self._registry.get(name)
        if t and any(tag in self._tags for tag in t.tags):
            return t
        return None


class ToolRegistry:
    # is_read_only（契约 test_recovery_readonly_names_... 钉住）。
    # -3 修复：plan 模式=只读模式——白名单直接由此派生（原 10 项白名单过窄
    READONLY_TOOL_NAMES: frozenset[str] = frozenset(
        {
            "chat_workflow",
            "citation_extractor",
            "configure_settings",
            "conversation_history",
            "cron_list",
            "ctx_inspect",
            "data_aggregate",
            "data_merge",
            "data_validate",
            "date_calculator",
            "db_inspect",
            "doc_classifier",
            "emit_ui_tree",
            "excel_reader",
            "get_genui_guide",
            "get_html_canvas_guide",
            "git_diff",
            "git_status",
            "glob",
            "grep",
            "image_ocr",
            "inbox",
            "json_parser",
            "knowledge_search",
            "list_dir",
            "list_ui_components",
            "memory_read",
            "memory_search",
            "ontology_query",
            "pdf_structure",
            "pdf_translate",
            "read",
            "read_document",
            "rule_matcher",
            "section_summarizer",
            "skill_discover",
            "skill_invoke",
            "speech_to_text",
            "text_splitter",
            "todo_read",
            "tool_search",
            "web_fetch",
            "web_image_search",
            "web_scraper",
            "web_search",
            "word_reader",
            "workflow_embed_emit",
            "workflow_list",
            "workflow_status",
        }
    )

    PLAN_MODE_TOOLS: tuple[str, ...] = tuple(sorted(READONLY_TOOL_NAMES)) + (
        "enter_plan_mode",
        "exit_plan_mode",
        "todo_write",
        # 计划语义下的补充放行（真机实测：全拦会让 plan 模式做不出计划）
        # 1) 读 PDF 正文——pdf_structure 只给结构，出方案前要看内容
        "pdf_reader",
        # 2) 记忆写入——「A 依赖 B」这类关系事实是规划的输入，
        #    只读白名单把 memory_assert 挡了，计划永远记不下依赖
        "memory_assert",
        # 3) 协作/派单——「这事该谁做」「问下 XX」本身就是规划动作；
        #    与 COLLAB_ALWAYS_AVAILABLE_TOOLS 同源（组织结构性权利）
        "team_list",
        "send_dm",
        "ask_peer_and_wait",
        "route_work",
        "spawn_thread_task",
        "broadcast_issue",
        # 4) 经理 board 巡检——规划前先看现有子卡
        "manager_board_read",
    )
    # 注意：`bash` / `python_exec`（risk=high）刻意**不放行**——它们能绕过
    # 只读约束（重定向写文件、任意执行），放进 plan 模式等于撕开只读口子。
    # `team_create` 同理：建团队是写操作，plan 模式只允许「看有什么团队」。

    # （perception/domains.yaml 的 allowed_tools）、角色 tool_tags、plan 模式只读白名单
    # 都只裁剪「能力」，若不放行 request_user_input，Agent 无法向用户提问
    # 根因 = plan 模式 allowed 列表为 PLAN_MODE_TOOLS，不含交互通道）。
    ALWAYS_AVAILABLE_TOOLS: tuple[str, ...] = ("request_user_input",)

    # 协作/派单工具在角色 tool_tags 裁剪下的豁免清单——组织结构性权利（员工被创建
    # 即为协作），自定义角色 tags 不含 workflow 时不得失去协作/派单能力。
    # schema 过滤（factory.filter_tools_by_role_tags）与执行门禁（executor._role_gate）
    # 两层同源消费；空名单 fail-closed 语义不受影响。
    COLLAB_ALWAYS_AVAILABLE_TOOLS: frozenset[str] = frozenset(
        {
            "inbox",
            "send_dm",
            "ask_peer_and_wait",
            "reply_message",
            "broadcast_issue",
            "start_meeting",
            "respond_meeting",
            "route_work",
            "close_human_review",
            "spawn_thread_task",
            "manager_board_read",
            "modify_work_item",
            "delete_work_item",
        }
    )

    # 管理者专属工具（board 巡检/改卡/删卡）——无下级（无派发面）的卡不投放：
    # 没有派发对象时它们只会诱导员工以管理者口吻规划。
    MANAGER_ONLY_TOOLS: frozenset[str] = frozenset(
        {"manager_board_read", "modify_work_item", "delete_work_item"}
    )

    # 三处过滤面，但执行期判据 PermissionsPipeline._check_mode 只看本常量 →
    # （而非调用 with_always_available：类体不可引用自身属性），保持单一来源。
    PLAN_MODE_ALLOWED: frozenset[str] = frozenset(PLAN_MODE_TOOLS) | frozenset(
        ALWAYS_AVAILABLE_TOOLS
    )

    def __init__(
        self,
        dep_checker: DependencyChecker | None = None,
        feature_flags: dict[str, bool] | None = None,
    ):
        self._tools: dict[str, BaseTool] = {}
        self._dep_checker = dep_checker or DependencyChecker()
        self._feature_gate = FeatureGate(feature_flags)
        self._generation = 0
        self._schemas_cache: dict[tuple, list[dict[str, Any]]] = {}
        # unregister_prefix 按记账精确删除——禁止用「子串前缀」匹配（fs/fs_backup
        # 互为前缀时会把其他 client 的工具一起误删）
        self._prefix_members: dict[str, set[str]] = {}
        self._registered_groups: set[str] = set()

    def _apply_registration_gate(self, instance: BaseTool) -> None:
        # 功能：注册期门禁——capability_tags 合法性与 feature_group 门控判定。
        # getattr 降级：历史工具/测试桩未声明 → 未声明语义（不参与门控），与 BaseTool 默认一致
        capability_tags = getattr(instance, "capability_tags", [])
        feature_group = getattr(instance, "feature_group", None)
        ToolCapability.validate_all(capability_tags)
        if feature_group is not None:
            self._registered_groups.add(feature_group)
            if not self._feature_gate.is_enabled(feature_group):
                instance.enabled = False
                logger.warning(
                    "tool %s disabled (feature_group=%r gate off)",
                    instance.name,
                    feature_group,
                )

    def _apply_dep_gate(self, tool: BaseTool) -> None:
        # 功能：缺依赖禁用门闸——register 与 register_instance 同一体制（生产装配
        # getattr 降级：历史工具/测试桩未声明 dependencies → 未声明语义（不受限）
        missing = self._dep_checker.describe_missing(getattr(tool, "dependencies", None) or [])
        if missing:
            tool.enabled = False
            logger.warning(
                "tool %s disabled (missing deps):\n%s",
                tool.name,
                "\n".join(missing),
            )

    def _bump_generation(self) -> None:
        self._generation += 1
        self._schemas_cache.clear()

    def _guard_collision(self, name: str, instance: BaseTool) -> None:
        # 功能：同名注册碰撞 fail-closed——不同对象覆盖 = 静默替换风险（MCP 双注册/装配重复）
        existing = self._tools.get(name)
        if existing is not None and existing is not instance:
            raise ValueError(
                f"tool {name} 已被不同实例注册（原 {type(existing).__name__}），"
                "禁止同名覆盖；请先 unregister 或复用同一实例"
            )

    def register(
        self,
        tool_cls: type[BaseTool],
        tags: list[str] | None = None,
        *,
        prefix: str | None = None,
    ) -> None:
        instance = tool_cls()
        _assert_valid_schema(instance)
        self._apply_registration_gate(instance)
        self._apply_dep_gate(instance)
        applied = tags or [instance.category.value]
        instance.tags = list(applied)
        self._guard_collision(instance.name, instance)
        self._tools[instance.name] = instance
        if prefix is not None:
            self._prefix_members.setdefault(prefix, set()).add(instance.name)
        self._bump_generation()

    def register_instance(self, tool: BaseTool, *, prefix: str | None = None) -> None:
        _assert_valid_schema(tool)
        self._apply_registration_gate(tool)
        self._apply_dep_gate(tool)
        if not tool.tags:
            tool.tags = [tool.category.value]
        self._guard_collision(tool.name, tool)
        self._tools[tool.name] = tool
        if prefix is not None:
            self._prefix_members.setdefault(prefix, set()).add(tool.name)
        self._bump_generation()

    def unregister_prefix(self, prefix: str) -> None:
        # 功能：按 namespace 记账精确卸载工具（MCP 重连先清后注——替换式语义）
        # 不匹配子串前缀——互为前缀的 client 名互不误删
        members = self._prefix_members.pop(prefix, None)
        if not members:
            return
        for name in members:
            self._tools.pop(name, None)
        self._bump_generation()

    def filtered_copy(self, predicate: Any) -> ToolRegistry:
        """功能：按 predicate 复制工具视图（不改共享 registry，供单任务工具白名单）。"""
        clone = ToolRegistry()
        clone._tools = {name: tool for name, tool in self._tools.items() if predicate(tool)}
        clone._prefix_members = {
            prefix: {name for name in names if name in clone._tools}
            for prefix, names in self._prefix_members.items()
        }
        clone._prefix_members = {
            prefix: names for prefix, names in clone._prefix_members.items() if names
        }
        clone._generation = self._generation
        clone._registered_groups = set(self._registered_groups)
        return clone

    def get(self, name: str) -> BaseTool | None:
        tool = self._tools.get(name)
        if tool is None:
            tool = next((t for t in self._tools.values() if name in t.aliases), None)
        if tool is not None and not tool.enabled:
            return None
        return tool

    def _enabled_tools_ordered(self) -> list[BaseTool]:
        """功能：启用工具**两段式排序**（单一来源）——内建段（无 prefix 注册）
        按 name 升序在前；namespace 扩展段（MCP 等 prefix 注册）按 name 升序在后。
        缓存前缀稳定性要求：provider 前缀匹配自第 0 个 token 起，而工具定义排在
        最前且占头部约 98%，顺序一旦不稳定整条前缀即失效。
        -27固定 name 口径（此前为 dict 插入序）；
        再分两段：MCP 工具迟到注册/重连增删不得使内建段位移——内建前缀对
        provider 保持稳定（扩展段变化只影响其自身之后）。"""
        extension_names: set[str] = set()
        for members in self._prefix_members.values():
            extension_names.update(members)
        enabled = [t for t in self._tools.values() if t.enabled]
        builtin = sorted(
            (t for t in enabled if t.name not in extension_names), key=lambda t: t.name
        )
        extension = sorted((t for t in enabled if t.name in extension_names), key=lambda t: t.name)
        return builtin + extension

    def _schemas(
        self, generation: int, mode: str | None, allowed_key: tuple[str, ...] | None
    ) -> list[dict[str, Any]]:
        tools = self._enabled_tools_ordered()
        if allowed_key is not None:
            tools = [t for t in tools if t.name in allowed_key]
        if mode == "plan":
            tools = [t for t in tools if t.name in ToolRegistry.PLAN_MODE_ALLOWED]
        return [t.to_openai_schema() for t in tools]

    def list_schemas(
        self, mode: str | None = None, allowed_tools: list[str] | None = None
    ) -> list[dict[str, Any]]:
        allowed_key = tuple(sorted(allowed_tools)) if allowed_tools is not None else None
        cache_key = (self._generation, mode, allowed_key)
        cached = self._schemas_cache.get(cache_key)
        if cached is None:
            cached = self._schemas(self._generation, mode, allowed_key)
            self._schemas_cache[cache_key] = cached
        return cached

    def list_flat_schemas(self) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._enabled_tools_ordered()
        ]

    def list_tool_meta(
        self, mode: str | None = None, allowed_tools: list[str] | None = None
    ) -> list[dict[str, Any]]:
        # 功能：能力管理面元数据（version：enabled 过滤内含 gate 与缺依赖禁用，与 list_schemas 同口径）
        tools = self._enabled_tools_ordered()
        if allowed_tools is not None:
            tools = [t for t in tools if t.name in allowed_tools]
        if mode == "plan":
            tools = [t for t in tools if t.name in ToolRegistry.PLAN_MODE_ALLOWED]
        return [t.to_tool_meta() for t in tools]

    def check_feature_group_coverage(self) -> list[str]:
        # 功能：拼写漂移防线——配置 feature_flags 中无任何工具声明的组返回清单
        # 装配点据此启动警告（运营者关闭意图不被组名拼写漂移静默吞掉，fail-open 显性暴露）
        return [g for g in self._feature_gate.flags if g not in self._registered_groups]

    def list_tools(self) -> list[BaseTool]:
        return self._enabled_tools_ordered()

    def search(
        self,
        query: str,
        max_results: int = 5,
        category: str | None = None,
        capability: str | None = None,
    ) -> dict[str, Any]:
        # 功能：运行时动态工具发现——仅检索 MCP（INTEGRATION）工具
        from bandi.tools.tool_search import _keyword_search

        enabled = self.list_tools()
        tools = [t for t in enabled if t.category == ToolCategory.INTEGRATION]
        total_deferred = len(tools)
        if category is not None:
            tools = [t for t in tools if t.category.value == category]
        if capability is not None:
            tools = [t for t in tools if capability in (getattr(t, "capability_tags", []) or [])]
        if query.startswith("select:") and len(query) > 7:
            requested = [s.strip() for s in query.split(":", 1)[1].split(",") if s.strip()]
            loaded = {t.name for t in tools}
            matches = [n for n in requested if n in loaded]
        else:
            matches = _keyword_search(tools, query, max_results)
        return {
            "matches": matches,
            "query": query,
            "total_mcp_tools": total_deferred,
            "total_deferred_tools": total_deferred,
        }

    def scoped_role(self, role: str) -> ScopedToolRegistry:
        tags = _ROLE_TAGS.get(role, {"util"})
        return ScopedToolRegistry(self, tags)

    def scoped_tags(self, tags: list[str]) -> ScopedToolRegistry:
        return ScopedToolRegistry(self, set(tags))
