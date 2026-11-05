"""功能：工具面 Top-N 关键词裁剪（对齐 LeAgent `registry._select_relevant_tools`）。

背景（真机台账）：全量工具面 108 个 ≈ 32712 tokens，占单轮请求 80%——
「一句你好花 2 万 token」的主因。LeAgent 的做法是按用户输入关键词打分，
只注入最相关的 ≤35 个（核心工具永远保留）。

打分权重与 LeAgent 一致：工具名命中 3.0 / 描述命中 1.5 / 中文意图词命中 2.0。
中文查询与英文工具名之间存在词面断层（「查竞品」≠ `web_search`），
故补一张**中文意图词表**（`_INTENT_HINTS`，对齐 LeAgent 的 `search_hint` 设计），
命中该表的意图词即给对应工具组加权。

安全约束：裁剪只在**超量**时触发（≤ max_tools 一律原样返回，行为零变化）；
被裁掉的工具仍可通过 `tool_search`（ToolSearchTool，仅检索 MCP）与显式白名单找回；
核心工具（读写/终端/交互/任务台账/技能）永不裁剪——避免「模型连基本工具都没有」。
"""

from __future__ import annotations

import re
from typing import Any

# 功能：全量工具面超过此数才触发裁剪（超量才裁，≤此数原样返回）。
MAX_TOOLS = 35

# 功能：查询分词成本护栏——输入截断（意图通常在前段）+ 词元数上限。
_QUERY_MAX_CHARS = 200
_MAX_QUERY_TERMS = 80

# 功能：永不裁剪的核心工具名（读写/终端/交互/任务台账/技能检索/团队组建/记忆族）。
# 记忆族（search/read/assert）入核心集的理由：真机实测「订单服务依赖支付服务」
# 这类**关系记忆**请求不含检索意图词，memory_assert 被裁掉后模型只剩 ontology_query
# 可调——连查 7 次空结果、三元组始终写不进来。
CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "ask_user",
        "request_user_input",
        "todo_write",
        "todo_read",
        "task_ledger",
        "read",
        "read_document",
        "write",
        "edit",
        "apply_patch",
        "bash",
        "python_exec",
        "skill_invoke",
        "skill_discover",
        "tool_search",
        "team_create",
        "team_list",
        "memory_search",
        "memory_read",
        "memory_assert",
    }
)

# 功能：中文意图词 → 工具名关键词（对齐 LeAgent `search_hint`：把中文语义接到英文工具上）。
_INTENT_HINTS: dict[str, tuple[str, ...]] = {
    # 检索/联网
    "搜索": ("web_search", "web", "search", "knowledge_search"),
    "查找": ("search", "grep", "glob", "find"),
    "查一下": ("search", "web", "knowledge"),
    "查下": ("search", "web", "knowledge"),
    "查查": ("search", "web", "knowledge"),
    "查询": ("search", "query", "knowledge"),
    "资料": ("knowledge_search", "web_search", "search"),
    "竞品": ("web_search", "search", "knowledge_search"),
    "新闻": ("web_search", "search"),
    "天气": ("web_search", "web_fetch"),
    "网页": ("web_fetch", "browser", "web_scraper"),
    "链接": ("web_fetch", "web_scraper", "download"),
    "下载": ("download", "web_image_download", "fetch"),
    # 文件/文档
    "文件": ("file", "read", "write", "folder", "list_dir"),
    "文档": ("document", "read_document", "word", "pdf", "markdown"),
    "表格": ("excel", "csv", "data", "sheet"),
    "数据": ("data", "csv", "excel", "aggregate"),
    "代码": ("bash", "python_exec", "edit", "apply_patch", "grep"),
    "写一个": ("write", "edit", "document", "gen"),
    "生成": ("gen", "document", "slides", "excel", "chart", "image"),
    "图片": ("image", "media", "ocr", "gen_image"),
    "图表": ("chart", "diagram", "svg"),
    "幻灯片": ("slides", "gen_slides", "canvas"),
    "报告": ("document", "report", "markdown", "gen"),
    # 任务/协作
    "任务": ("task", "todo", "ledger", "work"),
    "计划": ("plan", "todo", "task", "enter_plan_mode"),
    "提醒": ("cron", "reminder", "schedule"),
    "定时": ("cron", "schedule", "task"),
    "群聊": ("collab", "dm", "thread", "broadcast", "meeting"),
    "员工": ("employee", "collab", "agent", "team"),
    "同事": ("collab", "dm", "team", "agent"),
    "审批": ("approval", "review", "gate"),
    # 记忆/知识
    "记住": ("memory", "memorize", "remember"),
    "记忆": ("memory", "recall", "knowledge"),
    "知识": ("knowledge", "ontology", "entity"),
    "实体": ("entity", "knowledge", "ontology"),
    # 数据源
    "数据库": ("db_", "sql", "sqlite", "datasource", "query"),
    "抓取": ("scraper", "web_scraper", "fetch", "download"),
    "浏览器": ("browser", "playwright", "web"),
}

# 功能：查询分词——英文/数字词 + 中文连续段；中文长段补 2-gram 提升召回。
_WORD_RE = re.compile(r"[0-9A-Za-z_]+|[\u4e00-\u9fff]+")

# 功能：寒暄问法——这类轮次直接作答、不调工具，故只保留最小工具面。
# 命中任一即走精简分支（真机台账：闲聊轮仍送 25 个工具 schema ≈ 5524 tokens，
# 占单轮 input 的 61%，而 history 仅 16 tokens——对话内容几乎不占预算）。
_CHITCHAT_PHRASES = (
    "你是谁",
    "你叫什么",
    "介绍一下你",
    "自我介绍",
)

# 功能：能力询问——问的是产品**实际**能做什么，与寒暄不是一类。
# 混进闲聊精简会让模型只能看见最小工具面，进而如实但错误地回答
# 「暂不支持写文件/数据处理/图表生成」——对用户谎报能力（rubric
# capability-honesty-1 直接判负）。这类轮次必须送完整工具面。
_CAPABILITY_PHRASES = (
    "你能做什么",
    "能帮我做什么",
    "你会什么",
    "能干什么",
    "有什么功能",
    "有什么能力",
    "你都能做什么",
    "会做什么",
    "支持什么",
    "有什么功能",
)

# 功能：闲聊轮保底工具——对话本身不需要任何工具；仅留一个按需检索入口，
# 模型若判断需要可现场扩面，其余全部不送 schema。
_CHITCHAT_TOOL_NAMES: frozenset[str] = frozenset({"tool_search"})

# 功能：长度护栏——超长输入必含具体诉求，不按闲聊处理。
_CHITCHAT_MAX_CHARS = 40

# 功能：业务信号词——出现即视为有实质诉求，不走闲聊精简分支。
# 只收无歧义的动作动词；「帮我」这类泛化请求词不入表——它是「能帮我做什么」的子串，
# 收了会把能力询问误判成业务轮（真机实测门槛第一版就栽在这）。
_BUSINESS_SIGNALS = (
    "报错",
    "错误",
    "失败",
    "修复",
    "异常",
    "问题",
    "需要",
    "无法",
    "不能",
    "生成",
    "创建",
    "删除",
    "修改",
    "统计",
    "分析",
    "方案",
    "bug",
    "error",
)

_PLEASANTRY_WORDS = (
    "你好",
    "您好",
    "hi",
    "hello",
    "嗨",
    "在吗",
    "早上好",
    "晚上好",
    "谢谢",
    "辛苦",
)

_MARKS = "，。！？!?；;、,.…~～ \t\r\n"


def is_capability_query(query: str) -> bool:
    """功能：判定是否在问「产品实际能做什么」——与寒暄分开对待。

    能力询问必须送完整工具面：模型只有看得见，才谈得上如实列举。送精简面时
    它会（正确地）报告自己看见的那几个工具，从而对用户谎报产品能力。
    """
    text = str(getattr(query, "content", None) or query or "").strip().lower()
    return any(phrase in text for phrase in _CAPABILITY_PHRASES)


def is_chitchat_query(query: str) -> bool:
    """功能：判定输入是否为寒暄/能力询问——是则本轮不注入完整工具面。

    判据（同时满足，避免误杀业务句）：
    - 命中能力询问/身份问法词表；**或**整句极短且只由寒暄词构成
    - 长度不超 `_CHITCHAT_MAX_CHARS`（超长输入必含具体诉求）
    - 不含任何动作意图词（`_INTENT_HINTS`），也不含业务信号词
    """
    text = str(getattr(query, "content", None) or query or "").strip().lower()
    if not text or len(text) > _CHITCHAT_MAX_CHARS:
        return False
    condensed = text.strip(_MARKS)
    if not condensed:
        return False
    if any(key in text for key in _INTENT_HINTS):
        return False
    if any(w in text for w in _BUSINESS_SIGNALS):
        return False
    if any(phrase in text for phrase in _CHITCHAT_PHRASES):
        return True
    return len(condensed) <= 12 and any(w in text for w in _PLEASANTRY_WORDS)


def query_terms(query: str) -> list[str]:
    """功能：查询 → 小写词元列表（英文按词、中文按段 + 滑动 2-gram + 意图词原文命中）。

    滑动（步长 1）而非固定步长：固定步长会跨过真实词——「查一下竞品」按 i+=2 切出
    「查一/下竞」，恰好丢掉「竞品」；滑动窗口保证任意相邻二字都在词元里。
    意图词表再对原文做一次子串命中，覆盖「查一下」这类 3 字词（2-gram 覆盖不到）。

    成本护栏：输入截断 + 词元去重 + 数量上限——超长输入（粘贴大段文本）不截断时
    会生成与输入同量级的 2-gram，再乘工具数（真机实证：3000 字 x 111 工具把轮次
    拖入超时；意图表达通常在前段，截断不损失检索质量）。
    """
    text = str(query or "").lower()[:_QUERY_MAX_CHARS]
    terms: list[str] = []
    for word in _WORD_RE.findall(text):
        terms.append(word)
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", word):
            terms.extend(word[i : i + 2] for i in range(len(word) - 1))
    terms.extend(key for key in _INTENT_HINTS if key in text)
    return [t for t in dict.fromkeys(terms) if t][:_MAX_QUERY_TERMS]


def _schema_name(schema: dict[str, Any]) -> str:
    fn = schema.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name", "") or "")
    return str(schema.get("name", "") or "")


def schema_name(schema: dict[str, Any]) -> str:
    """功能：schema → 工具名（兼容嵌套 function 与扁平两种形态）。"""
    return _schema_name(schema)


def _schema_description(schema: dict[str, Any]) -> str:
    fn = schema.get("function")
    if isinstance(fn, dict):
        return str(fn.get("description", "") or "")
    return str(schema.get("description", "") or "")


_MCP_QUERY_HINTS = (
    "mcp",
    "model context protocol",
    "外部工具",
    "外部服务",
    "外部系统",
)


def _mcp_explicit(query: str, mcp_names: set[str]) -> bool:
    # 功能：判断用户是否显式要求 MCP 能力（避免普通对话携带 MCP schema）
    text = (query or "").lower()
    if "select:" in text:
        return True
    if any(hint in text for hint in _MCP_QUERY_HINTS):
        return True
    return any(name.lower() in text for name in mcp_names if len(name) >= 4)


def _pick_mcp_schemas(
    schemas: list[dict[str, Any]],
    query: str,
    max_tools: int,
    mcp_names: set[str],
) -> list[dict[str, Any]]:
    # 功能：按 MCP 意图/关键词挑选有限 schema；普通对话返回空
    if max_tools <= 0 or not schemas:
        return []
    terms = query_terms(query)
    explicit = _mcp_explicit(query, mcp_names)
    ranked: list[tuple[float, int, str, dict[str, Any]]] = []
    for schema in schemas:
        name = _schema_name(schema)
        score = score_schema(name, _schema_description(schema), terms)
        exact = 0 if name.lower() in (query or "").lower() else 1
        ranked.append((-score, exact, name, schema))
    ranked.sort(key=lambda item: (item[1], item[0], item[2]))
    if not explicit:
        ranked = [item for item in ranked if item[0] < 0]
    return [item[3] for item in ranked[:max_tools]]


def score_schema(name: str, description: str, terms: list[str]) -> float:
    """功能：LeAgent 同权重打分——工具名 3.0 / 描述 1.5 / 中文意图词 2.0。"""
    low_name = name.lower()
    low_desc = description.lower()
    score = 0.0
    for term in terms:
        if term in low_name:
            score += 3.0
        if term and term in low_desc:
            score += 1.5
        hints = _INTENT_HINTS.get(term)
        if hints:
            for hint in hints:
                if hint in low_name:
                    score += 2.0
                    break
    return score


def select_relevant_schemas(
    schemas: list[dict[str, Any]],
    query: str,
    *,
    max_tools: int = MAX_TOOLS,
    core_names: frozenset[str] = CORE_TOOL_NAMES,
    mcp_tool_names: set[str] | frozenset[str] | None = None,
    mcp_lazy: bool = False,
    mcp_max_tools: int = 5,
) -> list[dict[str, Any]]:
    """功能：按查询裁剪工具面（超量才裁；核心工具保底；闲聊轮只留检索入口）。

    - `len(schemas) <= max_tools` → 原样返回（小工具面零行为变化）；
    - **能力询问 → 原样返回全量**（问的是产品实际能做什么，裁剪会让模型只能
      看见挑剩的工具，进而如实却错误地回答「暂不支持写文件/图表/团队」——
      对用户谎报能力，rubric capability-honesty-1 直接判负）；
    - **纯闲聊轮 → 只留 `_CHITCHAT_TOOL_NAMES`**（真机台账：这类轮次工具
      schema 占 input 的 61%、对话内容仅占 1%——模型直接作答，不需要工具面）；
    - **MCP 懒暴露**：普通对话不注入 MCP schema；只有显式 MCP 意图、工具名
      命中或关键词得分命中时，才最多注入 `mcp_max_tools` 个；
    - 核心工具全部保留（若核心数已超上限，上限自动放宽到核心数——不与安全约束打架）；
    - 其余按得分降序取余量，**得分相同按名字升序**（字节稳定的前提，避免前缀缓存被打穿）。
    """
    mcp_names = set(mcp_tool_names or ())
    picked_mcp: list[dict[str, Any]] = []
    if mcp_lazy and mcp_names:
        mcp_schemas = [s for s in schemas if _schema_name(s) in mcp_names]
        schemas = [s for s in schemas if _schema_name(s) not in mcp_names]
        if mcp_schemas:
            core_count = len([s for s in schemas if _schema_name(s) in core_names])
            mcp_budget = min(mcp_max_tools, max(0, max_tools - core_count))
            picked_mcp = _pick_mcp_schemas(mcp_schemas, query, mcp_budget, mcp_names)
    # 能力询问先于闲聊判定：它问的是产品实际能力，裁剪即失真
    if is_capability_query(query):
        result = schemas
    elif is_chitchat_query(query):
        minimal = [s for s in schemas if _schema_name(s) in _CHITCHAT_TOOL_NAMES]
        result = minimal or schemas[:1]
    elif len(schemas) <= max(0, max_tools - len(picked_mcp)):
        result = schemas
    else:
        core: list[dict[str, Any]] = []
        rest: list[tuple[float, str, dict[str, Any]]] = []
        for schema in schemas:
            name = _schema_name(schema)
            if name in core_names:
                core.append(schema)
                continue
            rest.append(
                (-score_schema(name, _schema_description(schema), query_terms(query)), name, schema)
            )
        rest.sort(key=lambda item: (item[0], item[1]))
        budget = max(max(0, max_tools - len(picked_mcp)), len(core))
        picks = [schema for _, _, schema in rest[: max(0, budget - len(core))]]
        result = core + picks
    if picked_mcp:
        result = [*result, *picked_mcp]
    if result is not schemas:
        result = sorted(result, key=_schema_name)
    return result
