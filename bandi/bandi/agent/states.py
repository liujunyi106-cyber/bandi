# 功能：AgentLoop 状态处理（on_* 钩子 + CoT 记录 + 置信度应用 + LLM 调用）
import asyncio
import hashlib
import json
import logging
import re
from typing import Any

from bandi.agent.base import CoTStep, CoTStepType, ExecutionContext, ParseOutcome, TerminalReason
from bandi.agent.deps import get_query_deps
from bandi.agent.verifier import VerifierPacket
from bandi.core.events import Event, EventType
from bandi.core.message import (
    LLMResponse,
    Message,
    MessageRole,
    TokenUsage,
    ToolCall,
    ToolResult,
    message_to_dict,
    repair_tool_arguments,
)
from bandi.llm.confidence import detect_confidence
from bandi.llm.streaming import StreamEventType, StreamingBuffer
from bandi.tools.registry import ToolRegistry
from bandi.utils.token_counter import TokenCounter

logger = logging.getLogger(__name__)

_MAX_CLARIFICATION_ROUNDS = 2
_TRUNCATION_MAX_TOKENS_MULTIPLIER = 4
_TRUNCATION_MAX_TOKENS_CAP = 32768
_TRUNCATION_EXHAUSTED_NOTE = "\n\n[注意] 输出多次因长度限制被截断，上述内容可能不完整。"
# "max_tokens"；消费端（恢复循环/耗尽标记）必须两形态等价，否则后两者截断静默失效
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})
_PROMPT_TOO_LONG_MESSAGE = "上下文长度超出模型窗口且压缩后无法收敛，请开新会话或减小输入规模。"


def _response_model_name(ctx: Any) -> str:
    """（批 3）：记账/事件用的模型名——实际调用模型（provider_name）优先
    回退 provider 首选名（降级后按实际模型记账，成本口径不偏）。"""
    return str(getattr(ctx, "provider_name", "") or "") or str(
        getattr(getattr(ctx, "provider", None), "model_name", "") or ""
    )


# 首轮豁免启发式：无既往历史时，仅当回复为『短确认式』（长度未超输入规模
# 且低于绝对上限）才跳过幻觉 ；超限视为可能含实质事实断言，仍须送检
_HALLUCINATION_CONFIRM_MAX_CHARS = 60
_HALLUCINATION_REPLY_INPUT_FACTOR = 2.0
# knowledge_search/read 检索召回后回答，但 judge 证据链只拼 USER/ASSISTANT
# （TOOL 消息被滤）→ 无据可对照 → score=0.8 误判拦截正确回答。补最近 TOOL
# 成功输出作证据（条数与单条长度双上限，防工具结果冲垮 judge 输入）。
_HALLUCINATION_TOOL_EVIDENCE_MSGS = 3
_HALLUCINATION_TOOL_EVIDENCE_MAX_CHARS = 600
# 先给模型基于证据的校对机会（拦截不再直接拒绝输出）；达上限仍 blocked 才终态
# 拦截。重写内容会再次过 judge（校对不可绕过），校对指令含 judge 指出的待核对片段。
_HALLUCINATION_MAX_REWRITES = 2
_HALLUCINATION_REWRITE_QUOTE_CHARS = 400


def _is_confirmation_reply(content: str, raw_input: str | None) -> bool:
    """功能：判定回复是否『短确认式』（无实质事实断言）——首轮幻觉豁免依据。
    长度超绝对上限、或远超输入规模（明显新增内容）→ 视为可能含实质事实断言
    不可豁免。短回复默认视为对输入的无新增确认。"""
    if len(content) > _HALLUCINATION_CONFIRM_MAX_CHARS:
        return False
    if raw_input and len(content) > len(raw_input) * _HALLUCINATION_REPLY_INPUT_FACTOR:
        return False
    return True


# 纯功能确认词元（长词优先贪心匹配）——判定回复是否『无实质事实断言的确认性
# 收尾』（收到/好的/可以…+语气词）。含具体实体/事实陈述的短句（如"代号是天马"）
# 不在词表内 → 不判纯确认（幻觉 7 护栏）。
_CONFIRMATION_TOKENS: tuple[str, ...] = (
    "收到即可",
    "好的收到",
    "已经收到",
    "已收到",
    "知道了",
    "没问题",
    "收到",
    "好的",
    "好嘞",
    "行的",
    "明白",
    "了解",
    "可以",
    "完成",
    "搞定",
    "多谢",
    "谢谢",
    "好的呢",
    "嗯嗯",
    "嗯",
    "好",
    "行",
    "对",
    "是",
    "即可",  # 语气收尾（"收到即可"拆解后残词）
    "了",  # 语气收尾（"好了/收到了"）
    "ok",
    "yes",
    "no",
)


def _is_pure_confirmation(content: str) -> bool:
    """功能：判定回复是否『纯功能确认』（全串由确认词元+标点构成，无实质事实
    断言）——多轮短确认豁免依据（r2 ：有既往轮时"回复收到"类被弱 judge
    判 0.9+ 幻觉误拦，6c470d1 只豁免首轮）。幻觉概念针对『陈述了与事实不符的
    内容』——纯确认未陈述任何事实，无幻觉可判。"""
    import re

    s = re.sub(r"[\s，。,.！!？?；;：:、~～\"'“”‘’\-—()（）\[\]]+", "", str(content).lower())
    if not s or len(content) > _HALLUCINATION_CONFIRM_MAX_CHARS * 2:
        return False
    while s:
        for tok in _CONFIRMATION_TOKENS:
            if s.startswith(tok):
                s = s[len(tok) :]
                break
        else:
            return False
    return True


_XML_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|apos);")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_text(text: str) -> str:
    # 功能：清洗控制字符与孤立代理项（3.5）——NUL/控制符剔除；
    # 孤立代理项无法 UTF-8 编码，一律替换为 ? 保证出网安全
    text = _CONTROL_CHARS_RE.sub("", text)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "replace").decode("utf-8")
    return text


def _escape_plain_segment(segment: str) -> str:
    # 功能：无既有实体的文本段转义（& < > 三替换）
    return segment.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_escape_text(text: str) -> str:
    """功能：XML 文本转义——已有实体（&amp;/&lt;/&gt;/&quot;/&apos;）保留
    裸 & < > 转义。3.5：单遍实现替换旧哨兵方案（\\x00\\x01\\x02\\x03x 与
    真实输入冲突会误还原成 &amp; 致数据失真），并清洗控制字符/孤立代理项。"""
    text = _sanitize_text(text)
    if "&" not in text:
        return _escape_plain_segment(text)
    out: list[str] = []
    pos = 0
    for m in _XML_ENTITY_RE.finditer(text):
        out.append(_escape_plain_segment(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_escape_plain_segment(text[pos:]))
    return "".join(out)


def _xml_escape_attr(value: str) -> str:
    return _xml_escape_text(value).replace('"', "&quot;").replace("'", "&apos;")


async def on_init(
    ctx: ExecutionContext,
) -> None:  # 功能：INIT 处理器——重置上下文、空消息时直接终止、安全护栏前置检查
    # turn_index 为会话累计轮数——全新会话（无恢复消息）归 0
    # 会话累计语义，避免恢复后 item_id 碰撞/事件计数重置）
    if not ctx.messages:
        ctx.turn_index = 0
    ctx.current_response = None
    ctx.current_content = None
    ctx.current_tool_calls = None
    ctx.tool_results = []
    ctx.terminal_reason = None
    # 恢复重跑（checkpoint 恢复带回旧审批文案）若不清理，`reply = final_message or
    # current_content` 会沿用旧文案（真实产出被无视且不落库）；新请求边界一并清除
    # （恢复重放路径不经 on_init，续跑语义不受影响）。
    ctx.final_message = None
    # 调度（retry 链 reflecting→thinking 永不经 init），故在此清除上一 artifact
    # 持续计数、pass 才清零，停滞封顶不受影响。
    ctx.verifier_state.rework_count = 0
    ctx.verifier_state.rework_feedback = ""
    # 让新请求直接跳过校对机会
    ctx.meta.pop("_hallucination_rewrite_count", None)
    ctx.meta.pop("_abort_summary_used", None)
    # 只读循环计数（新方向重新计数）。恢复重放路径不经 on_init，记录不受影响。
    ctx.meta.pop("dispatched_tool_call_ids", None)
    ctx.meta.pop("_readonly_loop_sig", None)
    ctx.meta.pop("_readonly_loop_streak", None)
    ctx.meta.pop("_loop_results_hash", None)
    ctx.meta.pop("_loop_results_hashes", None)
    ctx.tool_sequence_hashes.clear()
    if not ctx.messages:
        ctx.terminal_reason = TerminalReason.COMPLETED
        return

    # + UserPromptSubmit（每次新请求）；additionalContext 注入 raw_input 前缀
    # （感知层/请求组装在其后消费，注入对模型可见）
    _hook_runner = ctx.meta.get("hook_runner")
    if _hook_runner is not None and ctx.raw_input:
        from bandi.tools.hooks import HOOK_SESSION_START, HOOK_USER_PROMPT_SUBMIT

        _hook_session = str(ctx.meta.get("session_id") or "")
        if ctx.turn_index == 0:
            await _hook_runner.run_event(
                HOOK_SESSION_START, {"session_id": _hook_session, "source": "init"}
            )
        _hook_outcome = await _hook_runner.run_event(
            HOOK_USER_PROMPT_SUBMIT,
            {"prompt": ctx.raw_input[:4000], "session_id": _hook_session},
        )
        if _hook_outcome.additional_context:
            ctx.raw_input = f"{_hook_outcome.additional_context}\n\n{ctx.raw_input}"

    _refresh_projection_if_changed(ctx)

    # 安全护栏：prompt_guard + pii(input) 在 INIT 状态执行
    guard = getattr(ctx, "prompt_guard", None)
    if guard and ctx.raw_input:
        result = guard.scan(ctx.raw_input)
        if result.blocked:
            ctx.terminal_reason = TerminalReason.SAFETY_BLOCKED
            ctx.final_message = f"检测到注入攻击: {result.matched_patterns}"
            await ctx.bus.emit(
                Event(
                    type=EventType.SAFETY_GUARD_TRIGGERED,
                    data={
                        "module": "prompt_guard",
                        "matched": result.matched_patterns,
                    },
                )
            )
            return

    pii = getattr(ctx, "pii_detector", None)
    if pii and ctx.raw_input:
        pii_result = pii.detect(ctx.raw_input)
        if pii_result.has_pii:
            await ctx.bus.emit(
                Event(
                    type=EventType.PII_DETECTED,
                    data={
                        "phase": "input",
                        "types": pii_result.pii_types,
                    },
                )
            )


_MAX_TOOL_ARGS_PREVIEW = 200
_MAX_TOOL_OUTPUT_PREVIEW = 500
# 124L-5：写类工具名单——无 registry 元数据时的循环检测写类回落（名单内按非只读
# 处置；MCP 等动态写类工具经 registry is_read_only 元数据覆盖本名单）
_LOOP_STATE_CHANGING_TOOLS = frozenset(
    {"bash", "write", "edit", "apply_patch", "git_commit", "python_exec"}
)

# registry 真实注册名且声明 is_read_only（契约 test_recovery_readonly_names_... 钉住）。
# -3：清单单一来源收敛到 ToolRegistry.READONLY_TOOL_NAMES（与 plan 白名单同源）。
RECOVERY_SAFE_READONLY_TOOLS: frozenset[str] = frozenset(ToolRegistry.READONLY_TOOL_NAMES)

# 模式、可能再次触发审批）——恢复分区必须优先于 registry 元数据判 not-replay。
RECOVERY_UNSAFE_READONLY_TOOLS: frozenset[str] = frozenset({"enter_plan_mode", "exit_plan_mode"})


def readonly_tool_kind(name: str, registry: Any) -> bool | None:
    """功能：工具只读三态判定（循环检测与 恢复分区共用单一来源）——
    True=registry 声明只读；False=非只读或恢复不安全（切模式类）；None=未知
    （无 registry / 未注册，调用方按显式集合回落，fail-closed 由调用方定）。"""
    if name in RECOVERY_UNSAFE_READONLY_TOOLS:
        return False
    if registry is not None:
        getter = getattr(registry, "get", None)
        try:
            tool = getter(name) if callable(getter) else None
        except Exception:  # noqa: BLE001 — 桩/包装 registry 异常按未知处理（fail-closed）
            tool = None
        if tool is not None:
            return bool(getattr(tool, "is_read_only", False))
    return None


def _loop_tool_class(name: str, registry: Any) -> str:
    """功能：循环检测工具分类——readonly（确定只读）/ write（确定非只读
    含写名单回落）/ unknown（无元数据且不在任一显式集合，按只读路径保守检测）。"""
    kind = readonly_tool_kind(name, registry)
    if kind is True:
        return "readonly"
    if kind is False:
        return "write"
    if name in _LOOP_STATE_CHANGING_TOOLS:
        return "write"
    if name in RECOVERY_SAFE_READONLY_TOOLS:
        return "readonly"
    return "unknown"


def _prompt_hash(provider_messages: list[dict]) -> str:
    # 功能：七-轮指纹——整轮消息哈希，供同轮重放识别
    try:
        return hashlib.sha256(
            json.dumps(provider_messages, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:16]
    except (TypeError, ValueError):
        return ""


def _static_prefix_hash(provider_messages: list[dict]) -> str:
    """功能（10.10）：System Prompt 静态前缀指纹——首条 system 消息（assembler
    静态段，跨轮字节稳定）的 SHA-256 前 16 位。跨轮比对可发现前缀漂移
    （服务端前缀缓存未命中的观测信号）；无 system 消息返回空串哨兵。"""
    for message in provider_messages:
        if message.get("role") != "system":
            continue
        content = str(message.get("content") or "")
        if content:
            return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return ""
    return ""


def _record_cot(
    ctx: ExecutionContext,
    type_: CoTStepType,
    *,
    thought: str = "",
    observation: str = "",
    tool_name: str = "",
    meta: dict | None = None,
) -> CoTStep:  # 功能：追加一条 CoT 推理轨迹步骤（step 自动递增，n 由 ANSWER 回填）
    step = CoTStep(
        step=len(ctx.cot_trace) + 1,
        type=type_,
        thought=thought,
        observation=observation,
        tool_name=tool_name,
        meta=meta or {},
    )
    ctx.cot_trace.append(step)
    return step


def _state_block_for(ctx: ExecutionContext) -> str:
    # 功能：常驻任务状态块取值——台账 + 上一轮快照 + 本轮原话；关闭/异常返回空串
    try:
        meta = getattr(ctx, "meta", None)
        if not isinstance(meta, dict) or str(meta.get("state_block") or "") == "off":
            return ""
        from bandi.memory.context_projection import collect_state_block

        manager = meta.get("memory_manager")
        raw_input = str(getattr(ctx, "raw_input", "") or "")
        hint_source = raw_input
        first_user = next(
            (
                message
                for message in getattr(ctx, "messages", None) or []
                if str(
                    getattr(getattr(message, "role", None), "value", getattr(message, "role", None))
                )
                == "user"
                and str(getattr(message, "content", "") or "").strip()
            ),
            None,
        )
        original_goal = str(getattr(first_user, "content", "") or "").strip()
        if "【本卡任务如下】" in original_goal:
            original_goal = original_goal.split("【本卡任务如下】", 1)[1].strip()
        if "任务描述：" in original_goal:
            original_goal = original_goal.split("任务描述：", 1)[1].strip()
        last_user = next(
            (
                message
                for message in reversed(getattr(ctx, "messages", None) or [])
                if str(
                    getattr(getattr(message, "role", None), "value", getattr(message, "role", None))
                )
                == "user"
            ),
            None,
        )
        # 状态块紧邻本轮 user 原文——短输入不重复占 token，长输入保留以免裁剪后丢失
        if (
            last_user is not None
            and str(getattr(last_user, "content", "") or "").strip() == raw_input.strip()
            and len(raw_input) <= 200
        ):
            raw_input = ""
        return collect_state_block(
            ledger=meta.get("task_ledger"),
            working=getattr(manager, "working", None) if manager is not None else None,
            session_id=str(meta.get("session_id") or getattr(ctx, "session_id", "") or ""),
            turn_index=int(getattr(ctx, "turn_index", 0) or 0),
            raw_input=raw_input,
            hint_source=hint_source,
            original_goal=original_goal,
        )
    except Exception:  # noqa: BLE001 — 状态块属增强信息，取值失败不得阻断请求
        return ""


def _build_provider_messages(ctx: ExecutionContext) -> list[dict]:
    """功能：LLM 请求 provider dict 组装统一入口（3.4）——孤儿/stub 防御
    （guard_messages）+ 折叠视图（fold-view）+ dict 组装三段对全部请求重建路径
    一致生效（主路径/截断恢复/响应式压缩），不再是 on_thinking 单点防线。"""
    from bandi.agent.tool_message_guard import guard_messages

    ctx.messages = guard_messages(
        ctx.messages,
        extra_pending_ids={tc.id for tc in (ctx.current_tool_calls or []) if tc.id},
    )
    fold_chars = getattr(ctx.config, "fold_tool_view_chars", 0) or 0
    if fold_chars > 0:
        from bandi.agent.request_view import build_request_view

        executor = getattr(ctx, "tool_executor", None)
        fold_persist = getattr(executor, "persist_folded_output", None) if executor else None
        view_messages = build_request_view(
            ctx.messages,
            fold_max_tool_chars=fold_chars,
            fold_persist=fold_persist,
        )
    else:
        view_messages = ctx.messages
    state_block = _state_block_for(ctx)
    if state_block:
        from bandi.agent.request_view import inject_state_block

        view_messages = inject_state_block(view_messages, state_block)
    provider_messages = [message_to_dict(m, protocol=ctx.provider.protocol) for m in view_messages]
    # 降级状态（工具面过滤不做：layer 是状态层非工具名，工具级降级由 executor
    # 连续失败熔断承担）
    degraded = ctx.meta.get("degraded_layers")
    if degraded:
        insert_at = len(provider_messages)
        for i, m in enumerate(provider_messages):
            if m.get("role") != "system":
                insert_at = i
                break
        provider_messages.insert(
            insert_at,
            {
                "role": "system",
                "content": f"[降级提示] 以下执行层近期处于降级状态：{', '.join(degraded)}。",
            },
        )
    _track_prompt_prefix(ctx, provider_messages)
    _record_context_usage(ctx, provider_messages)
    return provider_messages


def _record_context_usage(ctx: ExecutionContext, provider_messages: list[dict]) -> None:
    """功能：把本次请求的上下文构成落一份快照（前端「上下文用量」面板数据源）。

    统计对象就是即将发出的这份请求（provider dict + active_tools）；同一次请求被
    重复组装（流式失败转非流式）时按签名跳过，避免重复计数与重复写库。写库属展示面，
    失败不得阻断主链。
    """
    meta = getattr(ctx, "meta", None)
    if not isinstance(meta, dict):
        return
    store = meta.get("session_store")
    session_id = getattr(ctx, "session_id", "") or str(meta.get("session_id") or "")
    if store is None or not session_id:
        return
    tools = list(getattr(ctx, "active_tools", None) or ())
    signature = (
        len(provider_messages),
        sum(len(str(message.get("content") or "")) for message in provider_messages),
        len(tools),
    )
    if meta.get("_context_usage_sig") == signature:
        return
    meta["_context_usage_sig"] = signature
    registry = getattr(getattr(ctx, "tool_executor", None), "registry", None)
    mcp_names = (
        {tool.name for tool in registry.list_tools() if getattr(tool, "mcp_remote", False)}
        if registry is not None
        else set()
    )
    from bandi.agent.context_usage import build_snapshot
    from bandi.core.config import DEFAULT_CONTEXT_WINDOW

    window = int(getattr(ctx.config, "context_window", 0) or 0) or DEFAULT_CONTEXT_WINDOW
    snapshot = build_snapshot(
        messages=provider_messages,
        tools=tools,
        mcp_tool_names=mcp_names,
        model=str(getattr(ctx.provider, "model_name", "") or ""),
        context_window=window,
    )
    try:
        store.save_context_usage(session_id, snapshot)
    except Exception:
        logger.debug("context usage snapshot failed for %s", session_id, exc_info=True)


_PREFIX_FP_META = "_prompt_prefix_fp"
_PREFIX_TEXT_META = "_prompt_prefix_text"
_PREFIX_CHANGE_META = "_prompt_prefix_change"
# 会话级前缀基线：ctx 每轮新建，指纹必须挂在进程内按会话保存才能跨轮比较
_PREFIX_STATE: dict[str, tuple[str, str]] = {}
_PREFIX_STATE_MAX = 128


def _first_changed_section(previous: str, current: str) -> str:
    # 功能：定位首个差异所属的 `## 段`——把「缓存掉了」变成「哪一段变了」
    # （差异可能落在段标题前的空行上，故先从差异点向后看，再回退到前面最近的段）
    previous_lines = previous.splitlines()
    current_lines = current.splitlines()
    limit = min(len(previous_lines), len(current_lines))
    first_diff = next(
        (index for index in range(limit) if previous_lines[index] != current_lines[index]),
        limit,
    )
    for line in current_lines[first_diff : first_diff + 3]:
        if line.startswith("## "):
            return line[3:].strip()
    for line in reversed(current_lines[:first_diff]):
        if line.startswith("## "):
            return line[3:].strip()
    return "(无段)"


def _track_prompt_prefix(ctx: ExecutionContext, provider_messages: list[dict]) -> None:
    # 功能：前缀指纹——比对同一会话相邻两次请求的 system 区是否字节一致，
    # 不一致则记录变化段（前缀一变，其后全部 prompt cache 失效）
    import hashlib

    meta = getattr(ctx, "meta", None)
    if not isinstance(meta, dict):
        return
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in provider_messages
        if message.get("role") == "system"
    )
    fingerprint = hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:16]
    session_key = str(meta.get("session_id") or getattr(ctx, "session_id", "") or "")
    previous = _PREFIX_STATE.get(session_key) if session_key else None
    if previous and previous[0] != fingerprint:
        detail = {
            "prev_fingerprint": previous[0],
            "fingerprint": fingerprint,
            "prev_chars": len(previous[1]),
            "chars": len(system_text),
            "changed_section": _first_changed_section(previous[1], system_text),
        }
        meta[_PREFIX_CHANGE_META] = detail
        logger.info("prompt_prefix_changed %s", detail)
    if session_key:
        if len(_PREFIX_STATE) >= _PREFIX_STATE_MAX:
            _PREFIX_STATE.clear()
        _PREFIX_STATE[session_key] = (fingerprint, system_text)
    meta[_PREFIX_FP_META] = fingerprint
    meta[_PREFIX_TEXT_META] = system_text


def _prune_before_request(ctx: ExecutionContext) -> None:
    """功能：每次 LLM 请求前的零 LLM 免费 prune（审计 2.11，cc-haha 每请求 microcompact
    同构）——旧工具输出超保护预算即占位化，未触发压缩的低占用会话同样受益；
    异常不阻断主链（prune 是优化非正确性前提）。"""
    try:
        from bandi.memory.compression_pipeline import prune_before_request

        pruned, trimmed = prune_before_request(ctx.messages)
        if trimmed > 0:
            ctx.messages = pruned
            ctx.meta["free_prune_trimmed"] = int(ctx.meta.get("free_prune_trimmed", 0)) + trimmed
    except Exception as e:  # noqa: BLE001
        logger.warning("request_path_prune_failed: %s", e)


def _exceeds_high_risk_cap(
    output: str, *, max_lines: int = 2000, max_bytes: int = 50 * 1024
) -> bool:
    """功能：判定工具输出是否超「行 + 字节」双上限（utils/truncate.ts：
    行数默认 2000、字节默认 50KB，先命中者为准）。"""
    if max(len(output.splitlines()), 0) > max_lines:
        return True
    return len(output.encode("utf-8", errors="ignore")) > max_bytes


def _cap_high_risk_output(output: str, *, tool_name: str, max_lines: int, max_bytes: int) -> str:
    """功能：按行/字节上限裁剪高风险工具输出，**并给模型可行动的取回出路**。

     旧实现按 2000 *字符* 硬砍且只留一句「[高风险工具输出已截断]」
    模型既不知道被砍了多少、也不知道怎么取回 ⇒ 只能基于残缺内容作答（读 md 答错题目）。
    出路统一为「重新调用」：高风险名单为命令/写类工具（bash/python_exec 等），无
    offset 语义（原 resumable 分支与读类名单交集恒为空，已删）；读类工具
    自带 offset/limit 分段。 <path>」的取回思路。
    保留「不切半行」语义（pi truncate.ts 头注：never partial lines）。
    """
    lines = output.splitlines()
    kept = lines[:max_lines]
    body = "\n".join(kept)
    if len(body.encode("utf-8", errors="ignore")) > max_bytes:
        # 逐行累加到字节帽（保留完整行，不切半行）
        acc: list[str] = []
        size = 0
        for line in kept:
            line_size = len((line + "\n").encode("utf-8", errors="ignore"))
            if size + line_size > max_bytes:
                break
            acc.append(line)
            size += line_size
        kept = acc
    total = len(lines)
    shown = len(kept)
    outlet = f"如需完整输出，请用更精确的参数重新调用 {tool_name}（缩小范围/分段获取）。"
    return "\n".join(kept) + f"\n\n[输出已截断：显示第 1-{shown} 行，共 {total} 行。{outlet}]"


def _append_tool_results_to_messages(ctx: ExecutionContext) -> None:
    """将工具结果 append 进 ctx.messages（按 tool_call_id 幂等，防重复 append）。

    在 on_executing 工具执行后立即调用——reflecting 直接终止（VERIFIED）时工具
    结果仍必须进入对话上下文（快照/恢复/落库完整性）。
    """
    # 同族防线缺一员——与 executor 层统一截断互补，避免大输出直灌父上下文。
    #
    # ① `read` 移出名单——它的产出是「用户指定的一段文本」，属模型主动索取的信息
    # 与 `bash` 的意外巨量输出性质不同；且 `read` 自带 offset/limit 分段能力
    # 防护应交给「分段读」而不是硬砍（glob/grep/list_dir 同样可能返回大量文本
    # 都不在名单里 ⇒ 这条线本身就划错了）。
    # DEFAULT_MAX_LINES=2000 / DEFAULT_MAX_BYTES=50KB）。原来 2000 *字符*
    _high_risk_tools = {
        "bash",
        "web_search",
        "web_fetch",
        "python_exec",
        "subagent_spawn",
    }
    _max_high_risk_lines = 2000
    _max_high_risk_bytes = 50 * 1024
    calls = [tc for tc in (ctx.current_tool_calls or []) if tc is not None]
    results = [r for r in (ctx.tool_results or []) if r is not None]
    if not calls:
        if results:
            logger.warning("%d extra tool result(s) dropped (no pending calls)", len(results))
        return
    if len(calls) != len(results):
        logger.warning(
            "tool call count mismatch: %d calls vs %d results",
            len(calls),
            len(results),
        )
    # 记录 id → 所属轮次，命中历史轮时 warning；同轮幂等（on_executing 后
    # on_thinking 二次回填）保持静默防噪音。
    existing_turn_by_id: dict[str, int] = {}
    for m in ctx.messages:
        if m.role != MessageRole.TOOL or not m.tool_call_id:
            continue
        existing_turn_by_id.setdefault(str(m.tool_call_id), int(getattr(m, "turn_index", 0) or 0))
    existing_ids = set(existing_turn_by_id)
    # （历史桩数据）参与位置兜底；重复非空 id 结果丢弃并告警——不得错挂到别的调用。
    by_id: dict[str, ToolResult] = {}
    unkeyed: list[ToolResult] = []
    duplicate_keyed: list[ToolResult] = []
    for result in results:
        rid = str(getattr(result, "tool_call_id", "") or "")
        if not rid:
            unkeyed.append(result)
        elif rid not in by_id:
            by_id[rid] = result
        else:
            duplicate_keyed.append(result)
    paired: list[tuple[Any, ToolResult | None]] = []
    for tc in calls:
        result = by_id.pop(str(tc.id), None)
        if result is None and unkeyed:
            result = unkeyed.pop(0)
        paired.append((tc, result))
    if duplicate_keyed:
        logger.warning(
            "%d duplicate tool result(s) with non-empty id dropped (no cross-pairing)",
            len(duplicate_keyed),
        )
    n_extra = len(unkeyed) + len(by_id)
    if n_extra > 0:
        logger.warning("%d extra tool result(s) dropped (unpaired with calls)", n_extra)
    for tc, result in paired:
        # 调用本函数，已回填的（含 stub）不得重复 append（同 id 双 TOOL 消息）
        if tc.id in existing_ids:
            prior_turn = existing_turn_by_id.get(str(tc.id))
            if prior_turn is not None and prior_turn < ctx.turn_index - 1:
                # 相邻轮差 1 属主链 on_executing→on_thinking 二次回填幂等，不告警
                logger.warning(
                    "tool result for id=%s (tool=%s) dropped: id already used in history turn %s",
                    tc.id,
                    tc.name,
                    prior_turn,
                )
            continue
        if result is None:
            from bandi.agent.tool_message_guard import INTERRUPTED_TOOL_RESULT

            ctx.messages.append(
                Message(
                    role=MessageRole.TOOL,
                    content=INTERRUPTED_TOOL_RESULT,
                    tool_call_id=tc.id,
                    name=tc.name,
                    turn_index=ctx.turn_index,
                    meta={"tool_error": True},
                )
            )
            existing_ids.add(tc.id)
            continue
        output = str(result.output) if result.output is not None else ""
        # 只回空白 <tool_output>，模型看不到原因只能重复调用直到 LOOP_DETECTED。
        error_text = str(getattr(result, "error", None) or "").strip()
        if error_text and not getattr(result, "success", True):
            output = (
                f"{output}\n[tool error: {error_text}]" if output else f"[tool error: {error_text}]"
            )
        from bandi.memory.compression_pipeline import strip_images_from_text

        output, stripped_images = strip_images_from_text(output)
        if stripped_images:
            ctx.meta["images_stripped"] = int(ctx.meta.get("images_stripped", 0)) + stripped_images
        if not output.strip():
            output = "(empty output)"
        if tc.name.lower() in _high_risk_tools and _exceeds_high_risk_cap(output):
            output = _cap_high_risk_output(
                output,
                tool_name=tc.name,
                max_lines=_max_high_risk_lines,
                max_bytes=_max_high_risk_bytes,
            )
        pii = getattr(ctx, "pii_detector", None)
        if pii:
            try:
                output = pii.sanitize(output)
            except Exception as e:  # noqa: BLE001
                logger.warning("pii_detector.sanitize failed during thinking: %s", e)
                output = "[tool output withheld: PII sanitization failed]"
        sanitizer = getattr(ctx, "output_sanitizer", None)
        if sanitizer:
            try:
                output = sanitizer.sanitize(output)
            except Exception as e:  # noqa: BLE001
                logger.warning("output_sanitizer failed during thinking: %s", e)
                output = "[tool output withheld: output sanitization failed]"
        # 工具输出/工具名必须 XML 转义——原样拼接时，输出含
        # `</tool_output>` 或属性引号可伪造包裹结构（模型视角注入面）
        wrapped = (
            f'<tool_output tool="{_xml_escape_attr(tc.name)}">\n'
            f"{_xml_escape_text(output)}\n</tool_output>"
        )
        ctx.messages.append(
            Message(
                role=MessageRole.TOOL,
                content=wrapped,
                tool_call_id=tc.id,
                name=tc.name,
                turn_index=ctx.turn_index,
                # B5b 8.6a：失败标记随消息落库——AuditReporter 工具失败率分子
                meta={"tool_error": True} if not getattr(result, "success", True) else {},
            )
        )
        existing_ids.add(tc.id)


async def on_thinking(
    ctx: ExecutionContext,
) -> None:  # 功能：THINKING 处理器——追加工具结果、压缩检测、调用 LLM、累计 tokens
    if ctx.terminal_reason is not None:
        return
    ctx.turn_index += 1
    # （含候选降级 / 响应式压缩重试 / 截断恢复重试）经 _do_llm_call_inner 自增
    # 供 LLM_RESPONSE 成本拆解区分「一次成」与「重试成」。
    ctx.meta["llm_attempt"] = 0
    # 用户消息归属当前轮——perception/初始化 append 时轮次尚未递增（默认 0）
    # 不回填则 session_memory_boundary 把当前轮 raw 误判为已覆盖而整体裁剪
    # （keep 空 + anchor 重注入，压缩永远无法收敛）
    raw = getattr(ctx, "raw_input", None)
    if raw:
        for m in reversed(ctx.messages):
            if m.role == MessageRole.USER and m.content == raw and m.turn_index != ctx.turn_index:
                m.turn_index = ctx.turn_index
                break
    ctx.clarification_injected = False
    # 显示过期根因而非当前轮真正问题
    ctx.meta.pop("last_llm_error", None)
    # 同一 tick 完成，下一轮 thinking 入口清除，防同一标记跨轮重复触发
    ctx.meta.pop("_hallucination_rewrite_pending", None)
    # reasoning-only 重试闸门是轮级：上一轮重试过，下一轮必须重新获得机会
    ctx.meta.pop("reasoning_only_retried", None)

    _append_tool_results_to_messages(ctx)
    ctx.tool_results = []

    _prune_before_request(ctx)

    if ctx.needs_compression():
        return

    # 预算检查在 LLM 调用前进行，避免超预算后仍执行请求
    budget = ctx.config.token_budget_limit
    if budget > 0 and ctx.consumed_tokens >= budget:
        ctx.terminal_reason = TerminalReason.TOKEN_BUDGET_EXHAUSTED
        return

    await ctx.bus.emit(Event(type=EventType.TURN_STARTED, data={"turn_index": ctx.turn_index}))
    await ctx.bus.emit(
        Event(
            type=EventType.LLM_REQUEST,
            data={
                "provider": ctx.provider.model_name,
                "session_id": ctx.meta.get("session_id", ""),
                "turn": ctx.turn_index,
            },
        )
    )

    # 三条重建路径共用，防单点防线绕过
    provider_messages = _build_provider_messages(ctx)

    provider_messages = await _inject_prefetch(ctx, provider_messages, ctx.meta.get("bandi_config"))

    response = await get_query_deps(ctx).call_model(ctx, provider_messages)

    _max_truncation_recoveries = 2
    _recovery_attempts = 0
    while (
        response.finish_reason in _TRUNCATED_FINISH_REASONS
        and _recovery_attempts < _max_truncation_recoveries
    ):
        _recovery_attempts += 1
        logger.info(
            "Output truncated (finish_reason=length), recovery attempt %d", _recovery_attempts
        )
        # stub（配对出网防 API 400），调用记录不再静默丢弃
        _trunc_reasoning = (response.reasoning or "").strip()
        ctx.messages.append(
            Message(
                role=MessageRole.ASSISTANT,
                content=response.content or "",
                tool_calls=response.tool_calls or None,
                # 功能：thinking 模式带工具调用时 reasoning_content 必须回传（防 API 400）
                meta=({"reasoning_content": _trunc_reasoning} if _trunc_reasoning else {}),
            )
        )
        ctx.messages.append(
            Message(
                role=MessageRole.USER,
                content="[Continue] 你的回答被截断了，请继续输出。直接从上一次中断处继续，不要重复已经写过的内容。",
            )
        )
        if response.usage:
            # 每次截断响应立即累计 token（含恢复调用），不得只记最终一次
            _tokens = response.usage.prompt_tokens + response.usage.completion_tokens
            ctx.total_tokens += _tokens
            ctx.consumed_tokens += _tokens
        _base_max = int(
            getattr(getattr(ctx.meta.get("bandi_config"), "llm", None), "max_output_tokens", 0) or 0
        )
        if _base_max > 0:
            ctx.meta["truncation_max_tokens"] = min(
                _base_max * _TRUNCATION_MAX_TOKENS_MULTIPLIER, _TRUNCATION_MAX_TOKENS_CAP
            )
        provider_messages = _build_provider_messages(ctx)
        response = await get_query_deps(ctx).call_model(ctx, provider_messages)
    if response.finish_reason in _TRUNCATED_FINISH_REASONS:
        ctx.meta["truncation_exhausted"] = True
        logger.warning("truncation recovery exhausted (turn=%s)", ctx.turn_index)
        if (response.content or "").strip():
            response.content = f"{response.content}{_TRUNCATION_EXHAUSTED_NOTE}"
    ctx.current_response = response
    ctx.current_tool_calls = response.tool_calls if response.tool_calls else None
    ctx.current_content = response.content

    # 记账先于失败/空响应早退——一次真实调用（含返回空的失败响应）产生的 usage
    # 必须计入预算与成本台账（LLM_RESPONSE 事件是计费唯一来源）
    if response.usage:
        tokens = response.usage.prompt_tokens + response.usage.completion_tokens
        ctx.total_tokens += tokens
        ctx.consumed_tokens += tokens
        # 为按模型校准系数积累数据（观测失败不影响主流程）
        estimated = 0
        system_tokens = 0
        history_tokens = 0
        memory_tokens = 0
        tool_schema_tokens = 0
        system_sections: dict[str, int] = {}
        try:
            from bandi.utils.token_counter import TokenCounter

            estimated = sum(
                TokenCounter.count(str(m.get("content") or "")) for m in provider_messages
            )
            system_tokens = sum(
                TokenCounter.count(str(m.get("content") or ""))
                for m in provider_messages
                if m.get("role") == "system"
            )
            history_tokens = estimated - system_tokens
            # 工具面 schema 经 tools 参数旁路发送，不入 messages——单独记账
            tool_schema_tokens = (
                TokenCounter.count(json.dumps(ctx.active_tools, ensure_ascii=False, default=str))
                if ctx.active_tools
                else 0
            )
            # 请求真实开销 = messages + 工具 schema（对齐 cc-haha `calculateContextBudget` 口径）。
            # 只算 messages 会让 estimated 低估约一半（实测 8950 vs reported 19830，工具占 55%），
            # 并把这个偏差喂给校准观测对（record_observation）→ 校准系数被推到 2.2 倍补偿。
            estimated += tool_schema_tokens
            # system 消息承载，此值把「记忆占用」从 system 总量中拆出可观测）
            memory_tokens = memory_section_tokens(provider_messages)
            system_sections = _system_sections(provider_messages)
            TokenCounter.record_observation(
                model=ctx.provider.model_name,
                estimated=estimated,
                actual=response.usage.prompt_tokens,
            )
        except Exception as e:  # noqa: BLE001 — 观测失败不阻断调用链
            logger.debug("token observation skipped: %s", e)
        # provider 字段必须是真实 provider 名（config.provider 或
        # api_protocol）——存模型名会让成本台账 provider 维度失真
        provider_obj = ctx.provider
        provider_cfg = getattr(provider_obj, "config", None)
        real_provider = getattr(provider_cfg, "provider", "") or getattr(
            provider_obj, "api_protocol", ""
        )
        await ctx.bus.emit(
            Event(
                type=EventType.LLM_RESPONSE,
                data={
                    "provider": real_provider or provider_obj.model_name,
                    "model": _response_model_name(ctx),
                    "session_id": ctx.meta.get("session_id", ""),
                    "turn": ctx.turn_index,
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "cache_read_tokens": int(
                            getattr(response.usage, "cache_read_tokens", 0) or 0
                        ),
                        "cache_creation_tokens": int(
                            getattr(response.usage, "cache_creation_tokens", 0) or 0
                        ),
                        "input_tokens_total": int(
                            getattr(response.usage, "input_tokens_total", 0) or 0
                        ),
                    },
                    "trace_id": ctx.meta.get("session_id", ""),
                    # 成本按角色归属（多 Agent 协同）；单 agent CLI 下为空串
                    "agent_role": str(ctx.meta.get("agent_role", "") or ""),
                    "attempt_id": int(ctx.meta.get("llm_attempt", 0) or 0),
                    "prompt_hash": _prompt_hash(provider_messages),
                    # 10.10：静态前缀稳定性指纹（跨轮对比发现缓存前缀漂移）
                    "static_prefix_hash": _static_prefix_hash(provider_messages),
                    "system_tokens": system_tokens,
                    "system_sections": system_sections,
                    "history_tokens": history_tokens,
                    "memory_tokens": memory_tokens,
                    "tool_schema_tokens": tool_schema_tokens,
                    "input_tokens_estimated": estimated,
                    "input_tokens_reported": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens,
                    # 前缀缓存命中回填（Anthropic/OpenAI/Gemini 适配器解析）
                    "cache_tokens": int(getattr(response.usage, "cache_read_tokens", 0) or 0),
                    # ——台账命中率分母必须用归一总量（Anthropic 的 input_tokens 不含
                    # 已缓存部分，直接作分母会 >>100%）；顶层键名与 cost_tracker 消费端对齐
                    "cache_creation_tokens": int(
                        getattr(response.usage, "cache_creation_tokens", 0) or 0
                    ),
                    "input_tokens_total": int(
                        getattr(response.usage, "input_tokens_total", 0) or 0
                    ),
                    "reason": "",
                },
            )
        )

        if budget > 0 and ctx.consumed_tokens >= budget:
            ctx.messages.append(Message(role=MessageRole.ASSISTANT, content=response.content or ""))
            ctx.terminal_reason = TerminalReason.TOKEN_BUDGET_EXHAUSTED
            ctx.final_message = "Token 预算已耗尽，当前轮回答已保存。请降低预算或切换模型后继续。"
            return

    # 例外：content_filter/refusal 等安全语义 finish 必须放行给 on_parsing
    # 走 SAFETY_BLOCKED（不得被 MODEL_ERROR 抢占）
    safety_finish = response.finish_reason == "content_filter" or getattr(
        response, "refusal", False
    )
    # 也按失败语义处置（工具轮 tool_calls 完整时保留，内容半截不影响工具执行）
    if response.finish_reason == "incomplete" and not response.tool_calls:
        ctx.terminal_reason = TerminalReason.MODEL_ERROR
        ctx.final_message = "模型流式响应未完整结束（incomplete），请重试或继续对话。"
        return
    if (
        not (response.content or "").strip()
        and not response.tool_calls
        and not safety_finish
        and not ctx.final_message
    ):
        ctx.terminal_reason = TerminalReason.MODEL_ERROR
        reasoning_text = (response.reasoning or "").strip()
        if reasoning_text:
            # 最终答案放进 reasoning 通道、content 为空（usage.completion_tokens>0）——
            # 如实区分「仅思维链」与「真空回复」，并给可行动指引；不得报 unknown 掩盖。
            # 不把 reasoning 直接当正文提升：思维链不是答案，长度与语义都不可控。
            ctx.final_message = (
                "模型仅返回思维链、正文为空"
                f"（reasoning {len(reasoning_text)} 字符；答案疑似被上游放进 reasoning 通道——"
                "可直接重试，或在输入框切换思考档位后重发）"
            )
        else:
            ctx.final_message = (
                "LLM returned empty response "
                f"(last_llm_error: {ctx.meta.get('last_llm_error', 'unknown')})"
            )
        return
    _record_cot(ctx, CoTStepType.THOUGHT, thought=response.reasoning or "")

    _apply_confidence(ctx, response)


def _apply_confidence(
    ctx: ExecutionContext, response: LLMResponse
) -> None:  # 功能：检测 LLM 输出置信度，低置信度时澄清/转人工
    response.confidence = detect_confidence(response.content or "")

    if response.confidence >= 0.6:
        return

    if response.confidence < 0.3 or ctx.clarification_count >= _MAX_CLARIFICATION_ROUNDS:
        ctx.terminal_reason = TerminalReason.ESCALATED
        ctx.final_message = f"模型对回答置信度较低（{response.confidence:.1f}），建议人工确认。"
        ctx.current_tool_calls = None
        return

    ctx.clarification_injected = True
    ctx.clarification_count += 1
    clarify_msg = Message(
        role=MessageRole.SYSTEM,
        content=(
            f"[置信度提示] 你上一轮回答的置信度为 {response.confidence:.1f}，"
            f"请重新审视你的推理过程，提供更确定的答案。"
        ),
    )
    ctx.messages.append(clarify_msg)
    # messages，只进内存列表的话中断恢复后该消息丢失
    session_store = ctx.meta.get("session_store")
    session_id = ctx.meta.get("session_id", "")
    if session_store is not None and session_id:
        session_store.append_message(session_id, clarify_msg)
    ctx.current_response = None
    ctx.current_content = None
    ctx.current_tool_calls = None


_PREFETCH_FINGERPRINT_CHARS = 80


def _filter_prefetch_payload(
    payload: dict[str, Any],
    existing_system_texts: list[str],
    injected_keys: list[str] | None = None,
) -> dict[str, Any]:
    """
    记忆行，第二轮 prefetch 剔除——先按 memory key（injected_keys 命中
    `_focused_memory_items`）整条剔除已注入记忆，再按行前 80 字符指纹做跨截断去重
    （memory_source 截断 200 字符，指纹取前缀可跨截断匹配）。全部行重复的节整体删除；
    `_` 开头的隐藏元数据（_focused_memory_items）不参与行级指纹，随 payload 透传。"""
    existing_blob = "\n".join(existing_system_texts)

    # ① key 级去重：逐条记忆按 memory key 剔除已注入项，并重建 focused_memory 文本
    items: list[dict[str, str]] = list(payload.get("_focused_memory_items") or [])
    if items and injected_keys:
        injected = set(injected_keys)
        # 纯 key 兼容匹配（去掉 category 前缀后与 prefetch key 一致）
        injected_plain = {k.split(":", 1)[1] for k in injected_keys if ":" in k}
        keep = [it for it in items if it["key"] not in injected and it["key"] not in injected_plain]
        if len(keep) != len(items):
            payload = dict(payload)
            payload["_focused_memory_items"] = keep
            joined = "\n".join(it["value"] for it in keep)
            if joined:
                payload["focused_memory"] = joined
            else:
                payload.pop("focused_memory", None)
                payload.pop("_focused_memory_items", None)

    # ② 行级指纹去重（兼容旧签名行为；隐藏元数据直接透传不参与 splitlines）
    filtered: dict[str, Any] = {}
    for name, content in payload.items():
        if name.startswith("_"):
            filtered[name] = content
            continue
        kept_lines = [
            line
            for line in content.splitlines()
            if not (line.strip() and line.strip()[:_PREFETCH_FINGERPRINT_CHARS] in existing_blob)
        ]
        if kept_lines:
            filtered[name] = "\n".join(kept_lines)
    return filtered


async def _inject_prefetch(
    ctx: ExecutionContext,
    provider_messages: list[dict],
    bandi_config: object,
) -> list[dict]:
    """第二轮（turn_index>=2）起消费并行预取记忆，注入临时 provider_messages（不写 ctx.messages）。"""
    cfg = getattr(bandi_config, "prefetch", None)

    if cfg is None or not cfg.enabled:
        return provider_messages
    if ctx.turn_index < 2:
        return provider_messages
    handle = ctx.meta.get("prefetch_handle")
    if handle is None:
        return provider_messages
    from bandi.perception.prefetch import render_prefetch_block

    payload = await handle.consume(timeout=cfg.timeout_secs)
    # 防同 key 但 value 编辑漂移（前 80 字符变化）导致跨会话/跨轮重复注入
    system_texts = [
        str(m.get("content") or "") for m in provider_messages if m.get("role") == "system"
    ]
    injected_keys: list[str] = []
    session_id = ctx.meta.get("session_id", "")
    if session_id:
        from bandi.context.sources.memory_source import SEEN_BY_SESSION

        seen = SEEN_BY_SESSION.get(session_id)
        if seen:
            injected_keys = list(seen.keys())
    payload = _filter_prefetch_payload(payload, system_texts, injected_keys)
    block = render_prefetch_block(payload)
    if not block:
        return provider_messages
    await ctx.bus.emit(
        Event(
            type=EventType.PREFETCH_HITS,
            data={"turn_index": ctx.turn_index, "keys": list(payload.keys())},
        )
    )
    insert_at = 0
    for i, m in enumerate(provider_messages):
        if m.get("role") != "system":
            insert_at = i
            break
    else:
        insert_at = len(provider_messages)
    return [
        *provider_messages[:insert_at],
        {"role": "system", "content": block},
        *provider_messages[insert_at:],
    ]


# 记忆类注入段落的渲染标题（注入侧单源：perception/pipeline.py 的
# 「## 上下文参考」= memory_source 分节；perception/prefetch.py 的
# system_tokens 中单独拆出，避免 memory_tokens 恒 0 不可观测。
_MEMORY_SECTION_HEADERS = (
    "## 上下文参考",
    "## Runtime Prefetch",
    "## 全局记忆",
    "## 项目记忆",
    "## 会话记忆",
    "## 员工记忆",
    "## 用户偏好",
)


def _system_sections(messages: list[dict]) -> dict[str, int]:
    """功能：把 system 消息按 Markdown 二级标题分段计 token——定位固定开销落在哪一段。

    system 是一条拼接后的长文本，按 `## ` 切段能直接回答「这 5000 token 花在哪」：
    首轮比稳态多的部分会以某段的差值出现。标题前的内容记为 `(前言)`。
    """
    from bandi.utils.token_counter import TokenCounter

    merged = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    if not merged:
        return {}
    parts = re.split(r"(?m)^## ", merged)
    out: dict[str, int] = {}
    head = parts[0].strip()
    if head:
        out["(前言)"] = TokenCounter.count(head)
    for chunk in parts[1:]:
        name, _, body = chunk.partition("\n")
        label = f"## {name.strip()}"[:60]
        out[label] = out.get(label, 0) + TokenCounter.count(body)
    return out


def memory_section_tokens(messages: list[dict]) -> int:
    """功能：统计 system 消息中记忆类注入段落的 token 占用（无命中返回 0）。

    段落边界取「下一个二级标题」或字符串结尾——与注入侧分节渲染格式对齐。"""
    total = 0
    for m in messages:
        if m.get("role") != "system":
            continue
        text = str(m.get("content") or "")
        for header in _MEMORY_SECTION_HEADERS:
            start = text.find(header)
            if start < 0:
                continue
            segment = text[start:]
            nxt = segment.find("\n## ", 1)
            if nxt > 0:
                segment = segment[:nxt]
            total += TokenCounter.count(segment)
    return total


def compute_prompt_breakdown(
    messages: list[dict], tools_schema: list[dict] | None = None
) -> dict[str, int]:
    """
    tools 定义占用的 tokens 单独记账，审计不再只看到 messages 而看不到 schema
    开销；tools 缺省为 0（不误判无工具场景）。"""
    out = TokenCounter.breakdown(messages)
    if tools_schema:
        out["tools"] = TokenCounter.count(json.dumps(tools_schema, ensure_ascii=False, default=str))
    return out


async def _do_llm_call(
    ctx: ExecutionContext,
    provider_messages: list[dict],
    use_fallback: bool,
    registry: object,
    bandi_config: object,
    *,
    allow_reactive_compact: bool = True,
    allow_stream: bool = True,
) -> LLMResponse:
    # check_budget() 超限即拒绝
    tracker = ctx.meta.get("cost_tracker")
    if tracker is not None and hasattr(tracker, "check_budget") and not tracker.check_budget():
        ctx.terminal_reason = TerminalReason.TOKEN_BUDGET_EXHAUSTED
        ctx.final_message = "当日 Token 预算已耗尽，请调高 budget_limit 或明天再试。"
        return LLMResponse(content=ctx.final_message)
    tracer = ctx.meta.get("tracer")
    try:
        ctx.meta["last_prompt_breakdown"] = compute_prompt_breakdown(
            provider_messages, getattr(ctx, "active_tools", None)
        )
    except Exception:  # noqa: BLE001
        pass

    async def invoke() -> LLMResponse:
        if allow_reactive_compact:
            return await _llm_call_with_reactive_compact(
                ctx, provider_messages, use_fallback, registry, bandi_config
            )
        return await _do_llm_call_inner(
            ctx,
            provider_messages,
            use_fallback,
            registry,
            bandi_config,
            allow_stream=allow_stream,
        )

    if tracer is not None:
        with tracer.span(
            "llm_call",
            attributes={"provider": ctx.provider.model_name, "model": ctx.provider.model_name},
        ):
            return await invoke()
    return await invoke()


async def _llm_call_with_reactive_compact(
    ctx: ExecutionContext,
    provider_messages: list[dict],
    use_fallback: bool,
    registry: object,
    bandi_config: object,
) -> LLMResponse:
    # 响应式压缩重试；非超长异常原样冒泡走既有 recovery 链。
    from bandi.llm.strategy import is_prompt_too_long_error

    try:
        return await _do_llm_call_inner(
            ctx, provider_messages, use_fallback, registry, bandi_config
        )
    except Exception as e:
        if not is_prompt_too_long_error(e):
            raise
        logger.warning("prompt too long -> reactive compaction retry (turn=%s)", ctx.turn_index)
        result = await _reactive_compact_retry(ctx, max_attempts=2)
        if result is None:
            # 重试耗尽已置 PROMPT_TOO_LONG——回填空响应对齐 TOKEN_BUDGET_EXHAUSTED
            # 先例，让 on_thinking 正常消费、由 terminal_reason 驱动 transitions 终止
            return LLMResponse(content=ctx.final_message or "")
        return result


async def _do_llm_call_inner(
    ctx: ExecutionContext,
    provider_messages: list[dict],
    use_fallback: bool,
    registry: object,
    bandi_config: object,
    *,
    allow_stream: bool = True,
) -> LLMResponse:
    # 截断恢复）自增一次，1=一次成、≥2=重试后成。
    ctx.meta["llm_attempt"] = int(ctx.meta.get("llm_attempt", 0) or 0) + 1
    # 禁用回落链——配错模型 id / API Key 无效 / 余额不足 / 网关不可用等情形必须**报错**
    # 成功回复，用户无从察觉自己选的模型根本没被使用）。strict 标记由 agent_runner 在
    # 应用 provider_config 时写入 ctx.meta。
    if str(ctx.meta.get("strict_model") or "").strip():
        use_fallback = False
    max_tokens_override = ctx.meta.pop("truncation_max_tokens", None)
    override_kwargs = {"max_tokens": max_tokens_override} if max_tokens_override is not None else {}
    preferred_model = str(getattr(ctx.provider, "model_name", "") or "")
    if use_fallback:
        from bandi.llm.capabilities.bootstrap import bootstrap_capabilities
        from bandi.llm.capabilities.registry import get_capability_registry
        from bandi.llm.model_catalog import ModelCatalog
        from bandi.llm.strategy import CapabilityFallback, is_prompt_too_long_error

        capability_registry = get_capability_registry()
        if not capability_registry.all():
            bootstrap_capabilities(ModelCatalog(), registry=capability_registry)
        fallback = CapabilityFallback(capability_registry, bandi_config)

        # 非流式 fallback 遮蔽。修复：stream_to_ui 时按候选池顺序尝试流式消费
        # 全部候选失败降级下方非流式回退链（兜底行为不变）；超长错误穿透止损。
        # 能力探测：fallback 缺候选接口（测试短路 stub）时跳过流式分支
        candidates_fn = getattr(fallback, "ordered_candidates", None)
        if allow_stream and getattr(ctx.config, "stream_to_ui", False) and callable(candidates_fn):
            stats = getattr(fallback, "_stats", None)
            for model in candidates_fn(preferred_model, provider_messages):
                try:
                    stream_provider = registry.get_provider(model, bandi_config)
                except Exception:  # noqa: BLE001
                    continue
                if not getattr(stream_provider, "supports_streaming", False):
                    continue
                try:
                    ctx.provider_name = model
                    response = await asyncio.wait_for(
                        _stream_thinking(
                            ctx, provider_messages, provider=stream_provider, **override_kwargs
                        ),
                        timeout=_stream_total_timeout(bandi_config),
                    )
                    # 正常 return 一个 finish_reason="incomplete" 的响应。原代码只对
                    # `Exception` 降级，故此处直接 `return response`（1191）
                    # **fallback 候选降级与非流式回退链被整段绕过**，随后在
                    # on_response（855）被判成硬错误 → MODEL_ERROR 终结。
                    # 契约：incomplete 且无内容 = 一次**失败尝试**，须继续降级。
                    if response.finish_reason in ("incomplete", "error") and not (
                        (response.content or "").strip() or (response.reasoning or "").strip()
                    ):
                        if response.finish_reason == "error":
                            # ERROR 事件已写终态；候选回退前清空，避免 on_parsing 早退。
                            ctx.terminal_reason = None
                            ctx.final_message = ""
                        logger.warning(
                            "stream attempt %s (no content) on %s, falling back to non-stream: %s",
                            response.finish_reason,
                            model,
                            response.error_message or "no error message",
                        )
                        if stats is not None:
                            stats.record_attempt(
                                "chat", fallback._provider_name(model), success=False
                            )
                        continue
                    if stats is not None:
                        # reliability 排序，只记失败会稀释健康 provider 排名
                        stats.record_attempt("chat", fallback._provider_name(model), success=True)
                    return await _guard_reasoning_only(
                        ctx,
                        response,
                        provider_messages,
                        allow_stream,
                        registry=registry,
                        bandi_config=bandi_config,
                        **override_kwargs,
                    )
                except Exception as e:
                    if is_prompt_too_long_error(e):
                        raise
                    # 长期隐身的直接原因）：流式候选失败是空回复的前兆信号
                    logger.warning(
                        "stream attempt failed on %s, will fall back to non-stream: %s",
                        model,
                        e,
                    )
                    if stats is not None:
                        stats.record_attempt("chat", fallback._provider_name(model), success=False)
                    continue
            # 流式全败 → 落入非流式回退链
        try:
            response, used_model = await fallback.complete_with_fallback(
                preferred_model=preferred_model,
                messages=provider_messages,
                registry=registry,
                config=bandi_config,
                tools=ctx.active_tools,
                **override_kwargs,
            )
            ctx.provider_name = used_model
            # 空回复第二形态修复：非流式结果补发流帧（否则 UI 零帧 = 空回复表象）
            if allow_stream:
                await _emit_aggregated_response_as_deltas(ctx, response)
            return await _guard_reasoning_only(
                ctx,
                response,
                provider_messages,
                allow_stream,
                registry=registry,
                bandi_config=bandi_config,
                **override_kwargs,
            )
        except Exception as e:
            # 继续烧配置链/直调（止损语义在生产路径生效）
            if is_prompt_too_long_error(e):
                raise
            logger.warning("Capability fallback exhausted, trying config chain: %s", e)
            ctx.meta["last_llm_error"] = f"{type(e).__name__}: {e}"
            # 异常对象另存：except 块的 `e` 在块结束即被 Python 删除，
            # 下游还要从它提取上游状态码（503 场景正走此路径）
            ctx.meta["last_llm_error_obj"] = e
        # 不启用模型候选链：配置/应用什么模型就用什么，失败按显式错误透出
        from bandi.core.exceptions import LLMUnavailableError, upstream_status

        _err = LLMUnavailableError(
            "LLM call failed (candidates disabled): "
            f"last_llm_error={ctx.meta.get('last_llm_error', 'unknown')}"
        )
        _status = upstream_status(ctx.meta.get("last_llm_error_obj"))
        if _status is not None:
            _err.status_code = _status
        raise _err
    if (
        allow_stream
        and ctx.config.stream_to_ui
        and getattr(ctx.provider, "supports_streaming", False)
    ):
        # 流式调用整体超时：provider 流可能挂起（网络卡住时 async for 永久等待），
        # 无超时则单卡卡死直到调度层超时。预算可配（llm.stream_timeout）——
        # 高思考档模型单次生成可达数分钟，240s 缺省会掐断长推理。
        stream_budget = _stream_total_timeout(bandi_config)
        try:
            response = await asyncio.wait_for(
                _stream_thinking(ctx, provider_messages, **override_kwargs),
                timeout=stream_budget,
            )
        except TimeoutError:
            logger.warning("LLM stream hung > %ss, aborting turn", stream_budget)
            ctx.terminal_reason = TerminalReason.MODEL_ERROR
            ctx.final_message = f"模型响应超时（流式调用超过 {int(stream_budget)} 秒未完成）"
            return LLMResponse(content="", reasoning="")
        return await _guard_reasoning_only(
            ctx,
            response,
            provider_messages,
            allow_stream,
            registry=registry,
            bandi_config=bandi_config,
            **override_kwargs,
        )
    # 非流式直调（use_fallback=False / provider 不支持流式）：stream_to_ui 时同样
    # 补发流帧——否则该路径天然零 delta，UI 表现同空回复第二形态
    response = await ctx.provider.complete(
        provider_messages, tools=ctx.active_tools, **override_kwargs
    )
    if allow_stream:
        await _emit_aggregated_response_as_deltas(ctx, response)
    return await _guard_reasoning_only(
        ctx,
        response,
        provider_messages,
        allow_stream,
        registry=registry,
        bandi_config=bandi_config,
        **override_kwargs,
    )


def _safety_finish(response: LLMResponse) -> bool:
    """功能：安全语义终态（内容过滤/拒答）不得被当成 reasoning-only 重试。"""
    return response.finish_reason == "content_filter" or bool(getattr(response, "refusal", False))


def _is_reasoning_only(response: LLMResponse, *, safety_finish: bool) -> bool:
    """功能：判定「模型只走 reasoning 通道、正文为空」。

    思考型模型偶发把答案整体写进 reasoning、content 留空。这是**可自愈**的输出
    形态问题（换模型重问一次就能拿到答案），不是真的模型故障——真机 L1 74 条里
    有 5 条栽在这里，等于白白作废一次交互，而系统只提示用户「可直接重试」。
    """
    if safety_finish:
        return False
    return not (response.content or "").strip() and bool((response.reasoning or "").strip())


async def _guard_reasoning_only(
    ctx: ExecutionContext,
    response: LLMResponse,
    provider_messages: list[dict],
    allow_stream: bool,
    registry: object = None,
    bandi_config: object = None,
    **override_kwargs,
) -> LLMResponse:
    """功能：LLM 响应返回前的统一闸门——reasoning-only 则换模型重问一次。

    必须挂在**每个**返回 LLMResponse 的路径上（流式候选成功、非流式
    complete_with_fallback、直调回退），不是只挂一处：主路径走的是前两者，
    只补直调那一处等于死代码——真机实测 reasoning-only 一条都没被重试过。
    """
    if not _is_reasoning_only(response, safety_finish=_safety_finish(response)):
        return response
    retried = await _retry_reasoning_only(
        ctx,
        provider_messages,
        allow_stream,
        registry=registry,
        bandi_config=bandi_config,
        **override_kwargs,
    )
    return retried if retried is not None else response


async def _emit_reasoning_only_retry(ctx: ExecutionContext, outcome: str, detail: str) -> None:
    """功能：把 reasoning-only 自愈结果落成事件。

    不能只写日志：应用日志在生产部署里常被丢弃，而 §六 过程类指标要靠 events
    自动统计「重试挽回率」。没有这条事件就既无法观测也无法纳入回归。
    """
    from bandi.core.events import Event, EventType

    if ctx.bus is None:
        return
    try:
        await ctx.bus.emit(
            Event(
                type=EventType.LLM_REQUEST,
                data={
                    "session_id": ctx.meta.get("session_id", ""),
                    "turn": ctx.turn_index,
                    "provider": ctx.provider.model_name,
                    "trigger": "reasoning_only_retry",
                    "outcome": outcome,
                    "detail": detail[:200],
                },
            )
        )
    except Exception as exc:  # noqa: BLE001 — 埋点失败不得影响主链路
        logger.warning("emit reasoning-only retry event failed: %s", exc)


async def _retry_reasoning_only(
    ctx: ExecutionContext,
    provider_messages: list[dict],
    allow_stream: bool,
    registry: object = None,
    bandi_config: object = None,
    **override_kwargs,
) -> LLMResponse | None:
    """功能：reasoning-only 时换模型重问一次，失败返回 None。

    不把 reasoning 直接当正文提升——思维链不是答案，长度与语义都不可控。
    换模型而非重发同一请求：同模型同 prompt 大概率复现同样的通道偏好，换模型
    才是真正的重采样。**只试一次**：连续两次说明是 provider 侧系统性行为，再试
    只是烧钱。候选必须**排除当前模型**（链首常等于当前模型），且 reasoning-only
    属「成功响应」——用内部信号让 FallbackEngine 继续换下一候选。
    """
    if ctx.meta.get("reasoning_only_retried"):
        logger.debug("reasoning-only retry skipped: already attempted this turn")
        return None
    ctx.meta["reasoning_only_retried"] = True
    from bandi.llm.strategy import FallbackEngine, ProviderChainExhaustedError

    # 链来源取 bandi_config（覆盖后主配置）——ctx.config 是 AgentConfig（无 llm
    # 字段），从它取链恒空 ⇒ 换模型重试退化为同模型重发（真机实证：日志恒
    # "fallback <当前模型>" 且随 bearer 空 key 连败）。
    llm_cfg = getattr(bandi_config, "llm", None) or getattr(
        getattr(ctx, "config", None), "llm", None
    )
    timeout = getattr(llm_cfg, "timeout", 60) or 60

    current = ctx.provider.model_name
    # reasoning-only 重试 = 同模型保底重发一次
    # （早退只会把 reasoning-only 原样退回给用户，白白作废一次交互）
    candidates = [current]

    async def _call_with_provider(msgs: list[dict], cfg: dict) -> LLMResponse:
        # 链项是模型名：经 registry 按目录归属解析新 provider（换模型才有效——
        # 不存在的 clone_for_model 曾致重试退化为同模型重发，same_shape 高企）；
        # 解析失败回落当前 provider 重发一次（保底不炸）
        model = str(cfg.get("model") or cfg.get("provider") or "")
        provider = ctx.provider
        resolver = (
            getattr(registry, "get_provider_for_model", None) if registry is not None else None
        )
        # 保底重发（候选=当前模型）直用现实例——经 resolver 再解析会被激活档案
        # 洗回端点/凭证（跨 provider 判定落到 per-provider env，无 env 即 Bearer
        # 空 key 连败）；仅换模型候选才走 resolver 按目录归属新建
        if (
            model != ctx.provider.model_name
            and callable(resolver)
            and model
            and bandi_config is not None
        ):
            try:
                provider = resolver(model, bandi_config)
            except Exception as exc:  # noqa: BLE001 — 解析失败回落原 provider
                logger.warning(
                    "reasoning-only retry: resolve %s failed, keep current provider: %s",
                    model,
                    exc,
                )
        ctx.provider_name = getattr(provider, "model_name", ctx.provider.model_name)
        response = await asyncio.wait_for(
            provider.complete(msgs, tools=ctx.active_tools, **override_kwargs),
            timeout=timeout,
        )
        if _is_reasoning_only(response, safety_finish=_safety_finish(response)):
            # reasoning-only 不是异常，FallbackEngine 会当成功返回——抛内部信号
            # 才会继续遍历下一候选（_classify 对未知异常默认 fallback）
            raise _ReasoningOnlyError()
        return response

    try:
        response = await FallbackEngine(candidates).call_with_fallback(
            provider_messages,
            {"provider": current},
            _call_with_provider,
        )
    except ProviderChainExhaustedError as exc:
        last = exc.args[0] if exc.args else None
        if isinstance(last, _ReasoningOnlyError):
            await _emit_reasoning_only_retry(
                ctx, "same_shape", "retry also produced reasoning-only"
            )
        else:
            await _emit_reasoning_only_retry(ctx, "failed", f"{type(last).__name__}: {last}")
        return None
    except Exception as exc:  # noqa: BLE001 — 重试是兜底层，任何异常都不得拖垮主链路
        await _emit_reasoning_only_retry(ctx, "failed", f"{type(exc).__name__}: {exc}")
        return None
    await _emit_reasoning_only_retry(
        ctx, "recovered", f"finish_reason={getattr(response, 'finish_reason', '')}"
    )
    if allow_stream:
        await _emit_aggregated_response_as_deltas(ctx, response)
    return response


class _ReasoningOnlyError(Exception):
    # 功能：reasoning-only 内部信号——驱动 FallbackEngine 换下一候选
    ...


# 手机号前缀 / 邮箱局部 / 长数字（保守：悬空部分不与正则匹配，等待完整模式）
_HANGING_PII_PATTERNS = (
    re.compile(r"1[3-9]\d{0,9}$"),
    re.compile(r"[\w.+-]{1,30}@$"),
    re.compile(r"\d{4,20}$"),
)


def _hanging_pii_suffix(text: str) -> str:
    """功能：取文本尾部悬空 PII 前缀（最长匹配）——用于跨 delta 合并过滤。"""
    for pat in _HANGING_PII_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return ""


async def _emit_aggregated_response_as_deltas(ctx: ExecutionContext, response: LLMResponse) -> None:
    """功能：非流式回退成功后把聚合 reasoning/content 补发为流帧。

    空回复第二形态：流式候选全败 → 静默降级非流式
    回退链 ⇒ 聚合 content 只落库（DB 有「2」）、从不产生 content_delta ⇒
    UI 表现为空回复（WS 零帧、verdict_pass、completion_tokens=136）。
    修复：stream_to_ui 场景下补发与 `_stream_thinking` 同形的
    thinking_delta/content_delta 事件——下游 recorder/WS/前端零改动。
    已知残余：若流式候选已吐出部分 delta 后抛异常、再走非流式补发，极端情形
    下内容可能重复（生产 provider.stream 中途失败转 ERROR 事件不抛，触发面极窄）。
    """
    if not getattr(ctx.config, "stream_to_ui", False):
        return
    text = getattr(response, "content", "") or ""
    reasoning = getattr(response, "reasoning", "") or ""
    pii = getattr(ctx, "pii_detector", None)
    sanitizer = getattr(ctx, "output_sanitizer", None)
    try:
        if pii is not None:
            reasoning = pii.sanitize(reasoning)
            text = pii.sanitize(text)
        if sanitizer is not None:
            reasoning = sanitizer.sanitize(reasoning)
            text = sanitizer.sanitize(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("non-stream delta filter failed: %s", e)
    if reasoning:
        await ctx.bus.emit(
            Event(
                type=EventType.LLM_STREAM_EVENT,
                data={"type": "thinking_delta", "content": reasoning},
            )
        )
    if text:
        await ctx.bus.emit(
            Event(
                type=EventType.LLM_STREAM_EVENT,
                data={"type": "content_delta", "content": text},
            )
        )


async def _stream_thinking(
    ctx: ExecutionContext,
    provider_messages: list[dict],
    provider: Any | None = None,
    max_tokens: int | None = None,
) -> (
    LLMResponse
):  # 功能：流式 THINKING——逐块累积 content + tool_calls delta，通过 EventBus 推送中间事件；
    # provider 可注入（CapabilityFallback 候选池流式降级分支），缺省用 ctx.provider 保持直调路径
    # 的双实现漂移）；bus 发射/abort 检查/参数修复链保留在本函数
    sp = provider if provider is not None else ctx.provider
    buffer = StreamingBuffer()
    usage: TokenUsage | None = None
    finish_reason = "stop"
    seen_done = False
    _hanging_pii = ""
    stream_kwargs = {"max_tokens": max_tokens} if max_tokens is not None else {}

    async for event in sp.stream(provider_messages, tools=ctx.active_tools, **stream_kwargs):
        _abort = ctx.meta.get("abort_controller")
        if _abort and _abort.is_aborted()[0]:
            ctx.terminal_reason = TerminalReason.USER_INTERRUPT
            ctx.final_message = _abort.is_aborted()[1] or "已被用户中断。"
            return LLMResponse(content=buffer.text, reasoning=buffer.thinking)
        if event.type == StreamEventType.CONTENT_DELTA and event.content_delta:
            delta = event.content_delta
            combined = _hanging_pii + delta
            try:
                pii = getattr(ctx, "pii_detector", None)
                if pii:
                    combined = pii.sanitize(combined)
                sanitizer = getattr(ctx, "output_sanitizer", None)
                if sanitizer:
                    combined = sanitizer.sanitize(combined)
            except Exception as e:  # noqa: BLE001
                logger.warning("stream delta filter failed, delta dropped: %s", e)
                combined = ""
            # 从合并过滤结果切回当前 delta 部分（悬空前缀原样保留时按长度切分；
            # 整段丢弃会把本次 delta 的合法尾部文本一并吞掉）
            if combined.startswith(_hanging_pii):
                delta = combined[len(_hanging_pii) :]
            else:
                delta = combined
            _hanging_pii = _hanging_pii_suffix(delta)
            if delta:
                buffer.append_text(delta)
                await ctx.bus.emit(
                    Event(
                        type=EventType.LLM_STREAM_EVENT,
                        data={
                            "type": "content_delta",
                            "content": delta,
                        },
                    )
                )
        elif event.type == StreamEventType.THINKING_DELTA and event.thinking_delta:
            buffer.append_thinking(event.thinking_delta)
            await ctx.bus.emit(
                Event(
                    type=EventType.LLM_STREAM_EVENT,
                    data={
                        "type": "thinking_delta",
                        "content": event.thinking_delta,
                    },
                )
            )
        elif event.type == StreamEventType.TOOL_CALL_START and event.tool_call_id:
            buffer.start_tool_call(event.tool_call_id, event.tool_name or "")
            await ctx.bus.emit(
                Event(
                    type=EventType.LLM_STREAM_EVENT,
                    data={
                        "type": "tool_call_start",
                        "tool_call_id": event.tool_call_id,
                    },
                )
            )
        elif event.type == StreamEventType.TOOL_CALL_DELTA and event.tool_call_id:
            # Buffer auto-create 兼容 DELTA 先于 START 的乱序
            buffer.append_tool_call(event.tool_call_id, event.arguments_delta or "")
            await ctx.bus.emit(
                Event(
                    type=EventType.LLM_STREAM_EVENT,
                    data={
                        "type": "tool_call_delta",
                        "tool_call_id": event.tool_call_id,
                    },
                )
            )
        elif event.type == StreamEventType.DONE:
            seen_done = True
            usage = event.usage
            finish_reason = event.finish_reason or "stop"
            await ctx.bus.emit(
                Event(
                    type=EventType.LLM_STREAM_EVENT,
                    data={
                        "type": "done",
                        "finish_reason": finish_reason,
                    },
                )
            )
        elif event.type == StreamEventType.ERROR:
            await ctx.bus.emit(
                Event(
                    type=EventType.LLM_ERROR,
                    data={
                        "session_id": ctx.meta.get("session_id", ""),
                        "turn": ctx.turn_index,
                        "error": event.error_message,
                    },
                )
            )
            ctx.terminal_reason = TerminalReason.MODEL_ERROR
            ctx.final_message = (
                event.error_message or "流式错误（回复可能不完整），请重试或继续对话。"
            )
            response = LLMResponse(
                content="",
                tool_calls=[],
                usage=None,
                finish_reason="error",
                refusal=False,
            )
            response.error_message = event.error_message  # type: ignore[attr-defined]
            return response

    tool_calls: list[ToolCall] = []
    for acc in buffer.get_completed_tool_calls():
        try:
            # 修复链，仍失败转可行动错误（禁止空参执行写工具）
            args = json.loads(acc.get("arguments", ""))
        except (json.JSONDecodeError, TypeError):
            # 损坏 JSON 走共享修复链（尾逗号/截断/围栏），仍失败则返回
            # 可行动的错误文本而非空 dict（空 dict 会导致写工具按默认参数执行，危险）
            args = repair_tool_arguments(acc.get("arguments", ""))
            if args is None:
                args = {
                    "_parse_error": (
                        "流式参数 JSON 解析失败，请重新生成合法的 JSON arguments，"
                        "确保引号正确转义且括号闭合。"
                    )
                }
                logger.warning(
                    "JSON parse failed for tool %s arguments, returning actionable error",
                    acc.get("name", "?"),
                )
        tool_calls.append(ToolCall(id=acc["id"], name=acc.get("name", ""), arguments=args))

    if not seen_done:
        # provider 未发 DONE 即流自然关闭——半截断线，不得伪装正常 stop
        logger.warning("stream ended without DONE event — treating as incomplete")
        finish_reason = "incomplete"

    # 记账与 LLM_RESPONSE 事件统一由 on_thinking 落（usage 随 response 透传）——
    # 本层不得重复累计，否则 token 双计、成本台账翻倍、预算闸门双速耗尽

    return LLMResponse(
        content=buffer.text,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=finish_reason,
        refusal=False,
        reasoning=buffer.thinking,
    )


def _is_blank_reply(text: str) -> bool:
    # 功能：判断回复是否为空/纯空白；零宽空格（U+200B/C/D）与 BOM 属视觉空白
    # 非空格 whitespace，须先剔除再判 —— 防模型/通道输出空白串被当作有效完成
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(ch, "")
    return not text.strip()


async def on_parsing(
    ctx: ExecutionContext,
) -> (
    None
):  # 功能：PARSING 处理器——检查安全/Token 预算/解析工具调用/组装 assistant 消息（含输出安全护栏）
    if ctx.terminal_reason is not None:
        return

    if ctx.current_response is None:
        if ctx.needs_compression():
            return
        ctx.terminal_reason = TerminalReason.MODEL_ERROR
        return

    if ctx.current_response.refusal or ctx.current_response.finish_reason == "content_filter":
        # 同时清空 current_content，防「final_message 固定文案」与
        # 「current_content 模型原话」两处来源在最终回复/落库判定中竞争。
        ctx.terminal_reason = TerminalReason.SAFETY_BLOCKED
        ctx.final_message = "模型拒绝回答"
        ctx.current_content = None
        ctx.current_tool_calls = None
        return

    tool_calls = ctx.current_tool_calls
    content = ctx.current_content or ""

    # 内容安全护栏在写入消息之前执行，防止不安全内容进入上下文
    try:
        content_filter = getattr(ctx, "content_filter", None)
        if content_filter and content:
            filter_result = content_filter.check(content)
            if filter_result.blocked:
                await ctx.bus.emit(
                    Event(
                        type=EventType.SAFETY_GUARD_TRIGGERED,
                        data={
                            "module": "content_filter",
                            "categories": filter_result.categories,
                        },
                    )
                )
                ctx.terminal_reason = TerminalReason.SAFETY_BLOCKED
                ctx.final_message = "内容被安全护栏拦截"
                ctx.current_content = None
                ctx.current_tool_calls = None
                return
    except Exception as e:  # noqa: BLE001
        logger.warning("content_filter.check failed: %s", e)

    try:
        pii = getattr(ctx, "pii_detector", None)
        if pii and content:
            pii_result = pii.detect(content)
            if pii_result.has_pii:
                content = pii.sanitize(content)
                await ctx.bus.emit(
                    Event(
                        type=EventType.PII_DETECTED,
                        data={
                            "phase": "output",
                            "types": pii_result.pii_types,
                        },
                    )
                )
    except Exception as e:  # noqa: BLE001
        logger.warning("pii_detector failed in on_parsing: %s", e)
        ctx.terminal_reason = TerminalReason.SAFETY_BLOCKED
        ctx.final_message = "输出无法通过隐私安全处理，已拦截"
        ctx.current_content = None
        ctx.current_tool_calls = None
        return

    try:
        sanitizer = getattr(ctx, "output_sanitizer", None)
        if sanitizer and content:
            content = sanitizer.sanitize(content)
    except Exception as e:  # noqa: BLE001
        logger.warning("output_sanitizer failed in on_parsing: %s", e)
        ctx.terminal_reason = TerminalReason.SAFETY_BLOCKED
        ctx.final_message = "输出无法通过安全净化，已拦截"
        ctx.current_content = None
        ctx.current_tool_calls = None
        return

    try:
        hc = getattr(ctx, "hallucination_checker", None)
        if hc and content and ctx.raw_input and not tool_calls:
            # 工具轮拦截会直接掐断工具执行（误伤查证路径）。
            # 输入时，跨轮事实（上轮定义、本轮引用）会被误判为幻觉（score=1.0
            dialogue = [
                m
                for m in ctx.messages[-6:]
                if m.role in (MessageRole.USER, MessageRole.ASSISTANT) and (m.content or "").strip()
            ]
            # 本轮输入（末条 USER，内容含 raw_input）不属于既往证据链——
            # score=1.0/0.9 连拦两轮致三场景全灭）。
            if dialogue and (
                dialogue[-1].role == MessageRole.USER
                and ctx.raw_input
                and ctx.raw_input in (dialogue[-1].content or "")
            ):
                dialogue = dialogue[:-1]
            history = "\n".join(f"{m.role.value}: {(m.content or '')[:300]}" for m in dialogue)
            # 文件读取、多跳计算），judge 看不到工具结果 = 无法对照「模型引用
            # 拦截）。补最近 TOOL 输出入证据链——judge 仍可凭证据发现曲解/夸大
            # 不豁免 judge 本身（契约幻觉 5 护栏）。
            tool_evidence = [
                (m.content or "")[:_HALLUCINATION_TOOL_EVIDENCE_MAX_CHARS]
                for m in reversed(ctx.messages[-_HALLUCINATION_TOOL_EVIDENCE_MSGS:])
                if m.role == MessageRole.TOOL and (m.content or "").strip()
            ]
            evidence_parts = [history]
            if tool_evidence:
                evidence_parts.append(
                    "[最近工具输出（模型回答可引用的事实来源）]\n" + "\n".join(tool_evidence)
                )
            if not history and _is_confirmation_reply(content, ctx.raw_input):
                # 首轮无既往对话、且回复为短确认式（无实质事实断言）——无跨轮
                # test_hallucination_blocks_and_records）。
                logger.debug("hallucination check skipped: no history and confirmation reply")
            elif _is_pure_confirmation(content):
                # （无实质事实断言）→ 同样豁免——幻觉针对『陈述与事实不符』
                # 纯确认未陈述事实。含实体断言的短回复（"代号是天马"）不在此列
                # 照常送检（幻觉 3/7 契约护栏）。
                logger.debug("hallucination check skipped: pure confirmation (no factual claim)")
            else:
                judge_context = "\n".join(evidence_parts) + f"\n[本轮用户输入] {ctx.raw_input}"
                report = await hc.check(text=content, context=judge_context)
                if report.blocked:
                    rewrite_used = int(ctx.meta.get("_hallucination_rewrite_count", 0) or 0)
                    if rewrite_used < _HALLUCINATION_MAX_REWRITES:
                        # 的待核对片段回喂模型，要求基于证据校对重写；重写内容会
                        # 再次经过本检查（校对不可绕过），达上限仍 blocked 才拦截
                        ctx.meta["_hallucination_rewrite_count"] = rewrite_used + 1
                        l2 = getattr(report, "l2", None)
                        spans = list(getattr(l2, "hallucination_spans", None) or [])
                        span_text = (
                            "；".join(str(s) for s in spans[:5])[:300] or "（judge 未给出具体片段）"
                        )
                        await ctx.bus.emit(
                            Event(
                                type=EventType.SAFETY_GUARD_TRIGGERED,
                                data={
                                    "module": "hallucination_checker",
                                    "score": report.overall_score,
                                    "action": "rewrite",
                                },
                            )
                        )
                        ctx.messages.append(
                            Message(
                                role=MessageRole.SYSTEM,
                                content=(
                                    "[事实校对] 你上一轮回复存在与证据不符的可能陈述"
                                    f"（judge 置信度 {report.overall_score:.2f}）。"
                                    f"回复原文（截断）：{content[:_HALLUCINATION_REWRITE_QUOTE_CHARS]}\n"
                                    f"需核对片段：{span_text}\n"
                                    "请基于对话与工具证据校对后重写完整答复：只保留证据中可验证的"
                                    "数据与事实；无法验证的内容明确标注「未查证」；不得原样重复上一轮。"
                                ),
                                turn_index=ctx.turn_index,
                            )
                        )
                        ctx.current_response = None
                        ctx.current_content = None
                        ctx.current_tool_calls = None
                        ctx.meta["_hallucination_rewrite_pending"] = True
                        return
                    await ctx.bus.emit(
                        Event(
                            type=EventType.SAFETY_GUARD_TRIGGERED,
                            data={
                                "module": "hallucination_checker",
                                "score": report.overall_score,
                                "action": "blocked",
                            },
                        )
                    )
                    ctx.terminal_reason = TerminalReason.SAFETY_BLOCKED
                    ctx.final_message = "输出存在幻觉风险，已拦截"
                    ctx.current_tool_calls = None
                    bt = getattr(ctx, "badcase_tracker", None)
                    if bt:
                        await bt.record_async(ctx, "safety_violation")
                    return
    except Exception as e:  # noqa: BLE001
        logger.warning("hallucination check failed in on_parsing: %s", e)

    # 功能：thinking 模式带工具调用时 reasoning_content 必须回传（防 API 400）
    _assistant_reasoning = (ctx.current_response.reasoning or "").strip()
    assistant_msg = Message(
        role=MessageRole.ASSISTANT,
        content=content,
        tool_calls=tool_calls,
        turn_index=ctx.turn_index,
        meta=({"reasoning_content": _assistant_reasoning} if _assistant_reasoning else {}),
    )
    ctx.messages.append(assistant_msg)

    if not tool_calls:
        # 与进入 parsing 前的 None 未置值区分）
        ctx.parse_outcome = ParseOutcome.NONE
        if _is_blank_reply(content):
            ctx.terminal_reason = TerminalReason.NO_REPLY
            ctx.final_message = "模型返回空回复，无法确认任务完成"
            return
        answer_step = _record_cot(ctx, CoTStepType.ANSWER, thought=content)
        answer_step.n = len(ctx.cot_trace)
        ctx.terminal_reason = TerminalReason.COMPLETED
        # 正常完成的正文必须落在 final_message——agent_runner 的 reply 取
        # `final_message or current_content`，非流式路径 current_content 为空，
        # 不赋值会让用户收到空白（消息已落库，API 却回空）。
        ctx.final_message = content
        return

    # 参数可能不完整，显式标 PARTIAL 供观测与后续策略化；放行 executing 的
    # 策略与 PARSED 一致（provider 已尽力解析，executor 校验参数）。
    ctx.parse_outcome = (
        ParseOutcome.PARTIAL
        if ctx.current_response.finish_reason in _TRUNCATED_FINISH_REASONS
        else ParseOutcome.PARSED
    )


def agent_runtime_snapshot(ctx: ExecutionContext) -> dict[str, Any]:
    # ctx_inspect 类自查工具经 ToolPermissionContext.extra 读取
    from bandi.core.config import DEFAULT_CONTEXT_WINDOW

    compress = getattr(ctx, "compress", None)
    pipeline = getattr(compress, "pipeline", None)
    meta = getattr(ctx, "meta", {}) or {}
    # 哨兵 0（未显式配置且未经 factory 解析）不得把 0 当窗口报给运行时快照
    # （ctx_inspect 会把它当真实窗口展示）——回落安全默认值。
    window = getattr(getattr(ctx, "config", None), "context_window", 0) or DEFAULT_CONTEXT_WINDOW
    return {
        "session_id": meta.get("session_id", ""),
        "mode": meta.get("mode", ""),
        "turn_index": getattr(ctx, "turn_index", 0),
        "consumed_tokens": getattr(ctx, "consumed_tokens", 0),
        "total_tokens": getattr(ctx, "total_tokens", 0),
        "context_window": window,
        "compression_threshold": getattr(
            getattr(ctx, "config", None), "compression_threshold", 0.0
        ),
        "token_budget_limit": getattr(getattr(ctx, "config", None), "token_budget_limit", -1),
        "message_count": len(getattr(ctx, "messages", []) or []),
        "active_tool_count": len(getattr(ctx, "active_tools", None) or []),
        "compress_token_history": list(pipeline.token_history()) if pipeline else [],
    }


async def on_executing(
    ctx: ExecutionContext,
) -> None:  # 功能：EXECUTING 处理器——批量执行工具调用、检测重复调用循环
    if ctx.terminal_reason is not None:
        return
    if not ctx.current_tool_calls or not ctx.tool_executor:
        ctx.terminal_reason = TerminalReason.TOOL_ERROR
        return

    # 无 setter 时静默跳过）
    snap_setter = getattr(ctx.tool_executor, "set_runtime_snapshot", None)
    if callable(snap_setter):
        snap_setter(agent_runtime_snapshot(ctx))
    # 统一记录，保证快照先于任何工具执行；否则执行中崩溃时快照缺 id 会重放副作用。
    # 写工具据此在写前备份）
    ctx.meta.setdefault("session_id", ctx.session_id or "")
    ctx.meta["turn_index"] = ctx.turn_index

    meta_binder = getattr(ctx.tool_executor, "bind_session_meta", None)
    if callable(meta_binder):
        meta_binder(ctx.meta)

    tracer = ctx.meta.get("tracer")
    if tracer is not None:
        tool_names = ",".join(tc.name for tc in ctx.current_tool_calls[:5])
        with tracer.span("tool_exec", attributes={"tools": tool_names}):
            results = await ctx.tool_executor.execute(ctx.current_tool_calls)
    else:
        results = await ctx.tool_executor.execute(ctx.current_tool_calls)
    ctx.tool_results = results
    _append_tool_results_to_messages(ctx)

    # 转向消息按 FIFO 追加为 USER 消息（append-only 不改写已发上下文）；
    from bandi.agent.steer import get_default_registry

    steer_registry = ctx.meta.get("steer_registry") or get_default_registry()
    if steer_registry is not None:
        session_id = ctx.session_id or ctx.meta.get("session_id")
        for note in steer_registry.drain(session_id):
            ctx.messages.append(Message(role=MessageRole.USER, content=note))

    ledger = ctx.meta.get("task_ledger")
    if ledger is not None and any(tc.name == "todo_write" for tc in ctx.current_tool_calls):
        await ledger.persist()

    for tc in ctx.current_tool_calls:
        args_preview = json.dumps(tc.arguments, ensure_ascii=False)[:_MAX_TOOL_ARGS_PREVIEW]
        _record_cot(ctx, CoTStepType.TOOL_CALL, tool_name=tc.name, thought=args_preview)
    for r in results:
        raw = str(r.output) if r.output is not None else ""
        obs = raw[:_MAX_TOOL_OUTPUT_PREVIEW]
        _record_cot(
            ctx,
            CoTStepType.ERROR if not r.success else CoTStepType.TOOL_RESULT,
            tool_name=r.tool_name,
            observation=obs,
            meta={"duration_ms": r.duration_ms},
        )

    aborting = [r for r in results if r.should_abort]
    if aborting:
        logger.warning("tool execution aborted: %s", [r.tool_name for r in aborting])
        # 撞暂停工具时先要求它停止调用并总结现状（不直接拒绝输出）；机会用过仍
        # 硬撞才终止（防死循环兜底）。注入后不设终态 → observing→thinking 再给
        # 模型一次输出机会。
        if not ctx.meta.get("_abort_summary_used"):
            ctx.meta["_abort_summary_used"] = True
            names = "、".join(sorted({r.tool_name for r in aborting}))
            ctx.messages.append(
                Message(
                    role=MessageRole.SYSTEM,
                    content=(
                        f"工具 {names} 因连续失败已被暂停且触发硬性终止阈值。"
                        "请立即停止调用工具，并用一段话总结：已完成的部分、当前状态、"
                        "失败原因与建议的下一步（不要调用任何工具）。"
                    ),
                    turn_index=ctx.turn_index,
                )
            )
            return
        ctx.terminal_reason = TerminalReason.TOOL_ERROR
        # ToolResult.error 可空——空时给兜底文案，避免 final_message None
        ctx.final_message = aborting[0].error or "工具执行被中止（连续失败或超限）"
        return

    # 终止（终态文案化——问题卡 payload 走独立审批/交互帧通道，不借道 final_message；
    awaiting = next((r for r in results if (r.error or "").startswith("APPROVAL_REQUIRED:")), None)
    if awaiting:
        ctx.terminal_reason = TerminalReason.AWAITING_APPROVAL
        # 审批锚点由执行链直接落 meta（不再让调度侧反查 pending 猜是哪一次调用）
        ctx.meta["approval_pending_tool_call_id"] = str(awaiting.tool_call_id or "")
        ctx.meta["approval_pending_tool_name"] = str(awaiting.tool_name or "")
        ctx.final_message = f"等待人工决策：请审批工具 `{awaiting.tool_name}` 后继续。"
        return

    mode_manager = ctx.meta.get("mode_manager")
    if mode_manager is not None and ctx.meta.get("mode") != mode_manager.mode:
        ctx.meta["mode"] = mode_manager.mode
        registry = getattr(ctx.tool_executor, "registry", None)
        if registry is not None:
            allowed = mode_manager.filter_tools([t.name for t in registry.list_tools()])
            from bandi.tools.registry import with_always_available
            from bandi.tools.selection import select_relevant_schemas as _select_schemas

            mcp_names = {
                tool.name for tool in registry.list_tools() if getattr(tool, "mcp_remote", False)
            }
            tools_config = getattr(ctx.meta.get("bandi_config"), "tools", None)
            ctx.active_tools = _select_schemas(
                registry.list_schemas(allowed_tools=with_always_available(allowed)),
                getattr(ctx, "raw_input", "") or "",
                mcp_tool_names=mcp_names,
                mcp_lazy=bool(getattr(tools_config, "mcp_lazy_exposure", True)),
                mcp_max_tools=int(getattr(tools_config, "mcp_max_exposed_tools", 5) or 0),
            )
        # executor 侧 mode 快照同步（权限检查/中间件消费 executor._mode——
        # 不同步会让运行中切换模式后写工具被旧模式拒绝/放行）
        executor = ctx.tool_executor
        if executor is not None and hasattr(executor, "_mode"):
            executor._mode = mode_manager.mode

    tool_keys = [
        (tc.name, json.dumps(tc.arguments, sort_keys=True)) for tc in ctx.current_tool_calls
    ]
    fingerprint = hashlib.sha256(json.dumps(tool_keys, sort_keys=True).encode()).hexdigest()
    ctx.tool_sequence_hashes.append(fingerprint)
    # 拿不到元数据时回落显式集合/写名单；51 个原名单外副作用工具不再按「只读」语义
    # 处置——明确写类走 124L-5（同指纹窗口），未知走只读路径但文案区分（不误标）。
    registry = getattr(ctx.tool_executor, "registry", None)
    tool_classes = [_loop_tool_class(tc.name, registry) for tc in ctx.current_tool_calls]
    state_changing = any(cls == "write" for cls in tool_classes)
    # 结果签名与调用指纹同窗比对：相邻轮有变化只说明本步有进展，窗口内仍含
    # 旧变化（如 r1,r2,r1,r1）时不得判循环；仅当窗口内结果签名全同才视为
    results_hash = hashlib.sha256(
        json.dumps(
            [
                (
                    str(result.tool_name),
                    bool(result.success),
                    hashlib.sha256(str(result.output or "").encode()).hexdigest(),
                )
                for result in (ctx.tool_results or [])
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    window = ctx.loop_detection_window
    results_window = [str(h) for h in (ctx.meta.get("_loop_results_hashes") or [])]
    if not results_window:
        # 兼容旧恢复上下文/既有契约：仅有最近一次结果签名时作为窗口起点
        legacy_prev = ctx.meta.get("_loop_results_hash")
        if legacy_prev is not None:
            results_window.append(str(legacy_prev))
    results_window.append(results_hash)
    results_window = results_window[-window:]
    ctx.meta["_loop_results_hashes"] = results_window
    ctx.meta["_loop_results_hash"] = results_hash
    if len(ctx.tool_sequence_hashes) >= window:
        recent = ctx.tool_sequence_hashes[-window:]
        # 如 grep/search/read 的重复调用是合法行为）——全 hash 判定会误伤
        if state_changing and len(set(recent)) == 1 and len(set(results_window)) == 1:
            ctx.terminal_reason = TerminalReason.LOOP_DETECTED
            ctx.final_message = "检测到工具调用循环"
    # （无进展）时判循环；输出变化 / 批次变化 / 掺写类工具即清零（不误伤合法重复
    # 124L-5 的只读豁免只保留给「有变化」的重复）。
    if not state_changing:
        current_sig = (fingerprint, results_hash)
        streak = int(ctx.meta.get("_readonly_loop_streak", 0) or 0)
        streak = streak + 1 if ctx.meta.get("_readonly_loop_sig") == current_sig else 1
        ctx.meta["_readonly_loop_sig"] = current_sig
        ctx.meta["_readonly_loop_streak"] = streak
        if streak == max(1, ctx.loop_detection_window - 1):
            # 观测上不可区分，此处无法改写终止语义，但必须让运维看得到near-miss
            logger.warning(
                "只读工具疑似空转（第 %d 轮同调用同结果，窗口 %d）：%s",
                streak,
                ctx.loop_detection_window,
                [tc.name for tc in ctx.current_tool_calls],
            )
        if streak >= ctx.loop_detection_window:
            ctx.terminal_reason = TerminalReason.LOOP_DETECTED
            # 元数据且不在显式集合）保守仍检测，但使用中性文案避免误导
            label = "只读工具" if all(cls == "readonly" for cls in tool_classes) else "工具"
            ctx.final_message = f"检测到{label}重复调用循环（无进展）"
    else:
        ctx.meta.pop("_readonly_loop_sig", None)
        ctx.meta.pop("_readonly_loop_streak", None)

    _mark_retrieval_stall(ctx, tool_classes, results_window, window)


_RETRIEVAL_STALL_TOOLS: frozenset[str] = frozenset(
    {
        "memory_search",
        "memory_read",
        "ontology_query",
        "web_search",
        "web_fetch",
        "web_scraper",
        "grep",
        "glob",
        "read",
        "list_dir",
        "pdf_structure",
        "pdf_reader",
        "read_document",
        "word_reader",
        "excel_reader",
        "data_aggregate",
        "data_merge",
        "tool_search",
    }
)

# 功能：单次流式 LLM 调用整体超时（provider 挂起保护；正常响应远低于此值）
_STREAM_TOTAL_TIMEOUT_SECONDS = 240.0


def _stream_total_timeout(bandi_config: object) -> float:
    # 功能：流式调用整体超时预算——`llm.stream_timeout` 可配；
    # 缺省/空话/零/负/非数值一律回落防挂起常量（禁止垃圾值造出零超时）
    try:
        llm = getattr(bandi_config, "llm", None)
        value = getattr(llm, "stream_timeout", None)
        if value is not None:
            budget = float(value)
            if budget > 0:
                return budget
    except (TypeError, ValueError):
        pass
    return _STREAM_TOTAL_TIMEOUT_SECONDS


RETRIEVAL_STALL_TEXT = (
    "[系统提示] 检索类调用已连续 {rounds} 轮没有返回任何新结果，换关键词也不会有更多线索。\n"
    "请停止继续检索，直接基于已有信息回复用户；确实没有找到就如实说明没有找到。"
)

TURN_BUDGET_TEXT = (
    "[系统提示] 本轮仅剩 {remaining} 次工具调用机会。请立即停止新增工具调用，"
    "用已获得的信息直接写出最终回复正文（交付物结论、文件路径、下一步）；"
    "继续调用工具将导致本轮没有正文产出。"
)


def _mark_retrieval_stall(
    ctx: ExecutionContext,
    tool_classes: list[str],
    results_window: list[str],
    window: int,
) -> None:
    """功能：标记「检索空转」——连续多轮结果无变化时叫停检索，逼模型给结论。

    与上面的同指纹判据互补：那里要求**调用参数与结果同时相同**，模型换个关键词
    就能绕过；这里只看**结果是否变化**——检索工具换词重查若拿不到新东西，就
    属于空转。命中只置标志，由主循环注入提示后让模型自己收尾，不直接终止，
    避免把「本来就只该查一次」的正常单次检索误杀。
    """
    names = [tc.name for tc in (ctx.current_tool_calls or [])]
    if not names or not all(_is_retrieval_tool(n) for n in names):
        ctx.meta.pop("_retrieval_stall_rounds", None)
        return
    if len(results_window) < min(3, window):
        ctx.meta.pop("_retrieval_stall_rounds", None)
        return
    recent = results_window[-min(3, window) :]
    if len(set(recent)) == 1:
        rounds = int(ctx.meta.get("_retrieval_stall_rounds", 0) or 0) + 1
        ctx.meta["_retrieval_stall_rounds"] = rounds
        if rounds >= 1:
            logger.warning("检索空转：连续 %d 轮结果无变化（%s）", rounds, [n for n in names])
    else:
        ctx.meta.pop("_retrieval_stall_rounds", None)


def _is_retrieval_tool(name: str) -> bool:
    """功能：判定工具是否为纯查询类——结果只取决于输入，不推进状态。"""
    return name in _RETRIEVAL_STALL_TOOLS


async def on_observing(
    ctx: ExecutionContext,
) -> None:
    # 功能：OBSERVING 处理器（填充）——从工具结果提炼观察摘要进 meta
    # 供 REFLECTING 反思与 lesson 提炼消费
    if ctx.terminal_reason is not None:
        return
    results = ctx.tool_results or []
    ctx.meta["observing_digest"] = {
        "tool_count": len(results),
        "failed_tools": [r.tool_name for r in results if not r.success],
    }


async def on_reflecting(
    ctx: ExecutionContext,
) -> None:  # 功能：REFLECTING 处理器——LLM-as-Judge VER 验证
    # 工具错误（TOOL_ERROR）/循环检测（LOOP_DETECTED）等已设置的
    # 终止原因不得被 VER 闸门覆盖——否则错误终止被放行且 final_message 丢失。
    # COMPLETED 除外：on_parsing 纯文本分支提前设 COMPLETED，若在此早退则
    if ctx.terminal_reason is not None and ctx.terminal_reason != TerminalReason.COMPLETED:
        return
    verifier = ctx.safety.verifier
    if verifier is None:
        ctx.terminal_reason = TerminalReason.VERIFIED
        return

    # 任务描述取最近一条 USER 消息——messages[0] 在多轮/注入 system 后
    # 可能是 SYSTEM 提示，会把系统提示当任务描述送给 Verifier 判定
    task_desc = ctx.raw_input
    # 员工链路 raw_input 携带「工作合同 + 分界 + 任务」结构——任务描述取「【本卡任务如下】」
    # 分隔标记后的真实任务段；无标记时回退按合同提示词块双向切割
    _task_marker = "【本卡任务如下】"
    _contract_marker = "## Company Work Item Contract"

    def _strip_contract_text(text: str) -> str:
        if _task_marker in text:
            text = text.split(_task_marker, 1)[1].strip()
        elif _contract_marker in text:
            head, _, tail = text.partition(_contract_marker)
            # 合同可能前置（头部空）或后置——两向取非空侧；尾部仅当不含指引正文时才可信
            candidate = head.strip()
            if candidate:
                text = candidate
            else:
                tail_body = tail.strip()
                text = "" if "You are executing one projected work item" in tail_body else tail_body
        # 任务段形如「{标题}\n\n任务描述：{正文}」——取描述正文（避免标题重复进摘要/技能名）
        if "任务描述：" in text:
            text = text.split("任务描述：", 1)[1].strip()
        return text.strip()

    task_desc = _strip_contract_text(task_desc)
    if not task_desc:
        for m in reversed(ctx.messages):
            if m.role == MessageRole.USER:
                task_desc = _strip_contract_text(m.content or "")
                break

    if not verifier.verification_required(ctx):
        ctx.terminal_reason = TerminalReason.VERIFIED
        # 纯文本轮（无工具证据）不跑 judge——judge 是每轮一次 LLM 调用
        # 员工数×轮数放大后是真实成本，而纯文本轮无工具证据可核。
        return

    output = ctx.current_content or ""

    tr_list = ctx.tool_results or []
    tool_results_str = "; ".join(
        f"{r.tool_name}: {'ok' if r.success else 'fail'} ({r.error or 'ok'})" for r in tr_list
    )
    if not tool_results_str:
        # 从最近 TOOL 消息恢复结果摘要，保证 judge 输入包含工具证据
        try:
            from bandi.agent.verifier import _VERIFY_TRUNCATE_CHARS, _recent_tool_messages

            tool_results_str = "; ".join(
                (m.content or "")[:_VERIFY_TRUNCATE_CHARS] for m in _recent_tool_messages(ctx)
            )
        except Exception:  # noqa: BLE001 — 摘要恢复失败不阻断验证
            tool_results_str = ""

    user_texts = [
        _strip_contract_text(str(message.content or ""))
        for message in ctx.messages
        if message.role == MessageRole.USER and str(message.content or "").strip()
    ]
    packet = VerifierPacket(
        task_description=task_desc,
        output=output,
        prior_rework_count=ctx.verifier_state.rework_count,
        prior_rework_feedback=ctx.verifier_state.rework_feedback,
        original_goal=user_texts[0] if user_texts else task_desc,
        user_messages="\n".join(
            f"[{index}] {text}" for index, text in enumerate(user_texts, start=1)
        )[:4000],
    )
    if tool_results_str:
        packet._tool_results_str = tool_results_str  # type: ignore[attr-defined]

    verdict = await verifier.evaluate(packet)
    verdict = verifier.apply_stagnation_cap(
        {
            "last_verdict_outcome": "rework" if ctx.verifier_state.rework_count > 0 else "",
            "rework_count": ctx.verifier_state.rework_count,
        },
        verdict,
    )

    await ctx.bus.emit(
        Event(
            type=EventType.VERDICT_ISSUED,
            data={"action": verdict.action, "reason": verdict.reason},
        )
    )

    if verdict.action == "pass":
        ctx.terminal_reason = TerminalReason.VERIFIED
        # should_retry()（transitions reflecting 分支先于 verdict_pass()）仍
        # True，已通过验证却重回 thinking 多耗一轮空迭代。与 feedback 一并清零
        ctx.verifier_state.rework_count = 0
        ctx.verifier_state.rework_feedback = ""
    else:
        # 无 hierarchy（个人链）best-effort 静默跳过
        _record_lesson(ctx, task_desc or "", verdict)
        if verdict.action == "escalate":
            ctx.terminal_reason = TerminalReason.ESCALATED
            ctx.final_message = verdict.reason
        elif verdict.action == "fail":
            ctx.terminal_reason = TerminalReason.ESCALATED
            ctx.final_message = verdict.reason or "验证闸门未通过（fail-closed），建议人工确认。"
        elif verdict.action == "rework":
            ctx.verifier_state.rework_count += 1
            ctx.verifier_state.rework_feedback = verdict.reason
            ctx.current_response = None
            ctx.current_content = None
            ctx.current_tool_calls = None
            ctx.tool_results = None
            feedback_msg = Message(
                role=MessageRole.SYSTEM,
                content=(
                    f"[反思反馈] 上一轮执行需要返工。原因：{verdict.reason}\n"
                    "请根据反馈调整方案后重新执行；如上一轮工具结果有误或缺失，"
                    "可重新调用相应工具，而非仅改写文字回答。"
                ),
            )
            ctx.messages.append(feedback_msg)
            await ctx.bus.emit(
                Event(
                    type=EventType.REWORK_REQUESTED,
                    data={
                        "reason": verdict.reason,
                        "rework_count": ctx.verifier_state.rework_count,
                    },
                )
            )


_LESSON_MAX_CHARS = 200
_COMPRESSION_FALLBACK_RISK_THRESHOLD = 3


def _record_lesson(ctx: ExecutionContext, task_desc: str, verdict: Any) -> None:
    # 功能：反思 lesson 沉淀——rework/escalate/fail 时写 global scope
    # 幂等键 (session_id, turn_index) 防同轮双写；一切失败路径静默降级。
    # 所有项目/员工，任务细节即跨员工信息面）；单条 ≤200 字防写满 4000 字上限。
    hierarchy = ctx.meta.get("hierarchy_manager")
    if hierarchy is None:
        return
    written: set = ctx.meta.setdefault("lesson_written_turns", set())
    key = (str(ctx.meta.get("session_id") or ""), ctx.turn_index)
    if key in written:
        return
    digest = ctx.meta.get("observing_digest") or {}
    failed_tools = ",".join(digest.get("failed_tools") or []) or "无"
    # 过滤，多团队共库不混合；无团队（个人链）无标签=通用
    team = str(ctx.meta.get("team_id") or "").strip()
    tag = f"[教训|team={team}]" if team else "[教训]"
    lesson = (
        f"{tag} 失败：{verdict.action}｜工具：{failed_tools[:60]}｜"
        f"原因：{str(verdict.reason)[:100]}"
    )[:_LESSON_MAX_CHARS]
    try:
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        hierarchy.save_memory(SCOPE_GLOBAL, lesson, allow_shared=True)
        written.add(key)
    except Exception as e:  # noqa: BLE001 — lesson 落库失败不影响判定流程
        logger.warning("lesson_save_failed: %s", e)
    # 物理隔离、不进 visible/每轮上下文；检索/memory_read 按需读；一切失败静默
    project_id = str(ctx.meta.get("project_id") or "").strip()
    manager = ctx.meta.get("memory_manager")
    if project_id and manager is not None:
        try:
            detail = (
                "# 失败教训详情\n\n"
                f"## 任务\n{(task_desc or '').strip()[:500]}\n\n"
                f"## 判定\n{verdict.action}：{str(verdict.reason)[:400]}\n\n"
                f"## 失败工具\n{failed_tools}\n"
            )
            manager.save_entry(
                title=f"教训详情：{verdict.action}（{failed_tools[:40]}）",
                content=detail[:1500],
            )
        except Exception as e:  # noqa: BLE001 — 详情落盘失败不影响判定流程
            logger.warning("lesson_detail_save_failed: %s", e)


async def on_compressing(
    ctx: ExecutionContext,
) -> (
    None
):  # 功能：COMPRESSING 处理器——CompressionPipeline 三层压缩（Micro → Progressive → Truncate）
    # 终态早退：与 on_thinking/parsing/executing/observing/reflecting 同口径——
    # 预算耗尽/异常终止后不得再启动压缩（避免多烧一次压缩 LLM 摘要调用）
    if ctx.terminal_reason is not None:
        return
    _hook_runner = ctx.meta.get("hook_runner")
    if _hook_runner is not None:
        from bandi.tools.hooks import HOOK_PRE_COMPACT

        await _hook_runner.run_event(
            HOOK_PRE_COMPACT, {"session_id": str(ctx.meta.get("session_id") or "")}
        )
    # 缩减口径 = live（含 SYSTEM）前后对比：压缩会把对话内容搬进 SYSTEM 摘要消息
    # 排除 SYSTEM 的 compressible 口径会使「摘要膨胀/搬运」误判为已缩减
    before_live = ctx.live_token_count()

    from bandi.memory.compression_pipeline import CompressionPipeline

    ratio = (
        ctx.compressible_token_count() / ctx.effective_context_window
        if ctx.effective_context_window > 0
        else 1.0
    )
    maintenance_mode = ratio < ctx.config.compression_threshold
    # autocompact（pipeline 跨轮复用与 provider.complete 注入在生产实现内）
    outcome = (
        await get_query_deps(ctx).microcompact(ctx)
        if maintenance_mode
        else await get_query_deps(ctx).autocompact(ctx)
    )
    stats = outcome.stats
    # 4.9：压缩统计落 ctx.meta——轮末 _persist_memory 据此计算摘要动态 importance
    ctx.meta["last_compression_stats"] = stats
    success = outcome.success
    ctx.compress.compression_attempts += 1
    if success:
        ctx.compress.compression_successes += 1
        ctx.compress.consecutive_fallbacks = 0
        ctx.compress.compress_failed_count = 0
        ctx.compress.compress_ok = True
        ctx.messages = outcome.messages
        # 不重置则同一 turn 内状态机每轮迭代 needs_compression 恒 True
        # 会反复触发维护压缩（boundary 标记累积 + 无谓重复执行）
        if maintenance_mode:
            import time

            ctx.compress.turns_since_maintenance = 0
            ctx.compress.last_active_ts = time.time()
        if stats.stage != "none":
            logger.info(
                "compression: stage=%s input=%d->%d chars input=%d->%d msgs "
                "micro_dedup=%d trimmed=%d merged=%d prog_chunks=%d",
                stats.stage,
                stats.input_chars,
                stats.output_chars,
                stats.input_messages,
                stats.output_messages,
                stats.micro_deduplicated,
                stats.micro_trimmed,
                stats.micro_merged,
                stats.progressive_chunks,
            )
            # B5b 8.3：压缩事件——stage/前后规模随事件流出网，trace 可复盘
            await ctx.bus.emit(
                Event(
                    type=EventType.COMPRESSION_COMPLETED,
                    data={
                        "stage": stats.stage,
                        "success": True,
                        "input_messages": stats.input_messages,
                        "output_messages": stats.output_messages,
                        "input_chars": stats.input_chars,
                        "output_chars": stats.output_chars,
                        "progressive_chunks": stats.progressive_chunks,
                        "micro_trimmed": stats.micro_trimmed,
                    },
                )
            )
            _inject_continuity_anchor(ctx)
            if _hook_runner is not None:
                from bandi.tools.hooks import HOOK_POST_COMPACT

                await _hook_runner.run_event(
                    HOOK_POST_COMPACT, {"success": True, "stage": stats.stage}
                )
            _insert_context_projection(ctx)
            _inject_post_compact_context(ctx)
            _persist_compaction_boundary(ctx, success=True, stage=stats.stage)
        ctx.total_tokens = sum(ctx.provider.count_tokens(m.content or "") for m in ctx.messages)
    else:
        # 可达，状态机可跳出压缩循环）；compress_ok=True 仅为下游处置确认
        # 不代表压缩成功。连续 fallback 达阈值发 loss_risk 告警（监控可观测）
        ctx.compress.compress_failed_count += 1
        ctx.compress.consecutive_fallbacks += 1
        ctx.messages = CompressionPipeline.truncate(ctx.messages)
        ctx.compress.compress_ok = True
        # 注入锚点——续作连续性不得只靠成功压缩路径（成功分支 :1253 注入，此处补齐）
        _inject_continuity_anchor(ctx)
        if _hook_runner is not None:
            from bandi.tools.hooks import HOOK_POST_COMPACT

            await _hook_runner.run_event(
                HOOK_POST_COMPACT, {"success": False, "stage": getattr(stats, "stage", "")}
            )
        _insert_context_projection(ctx)
        _persist_compaction_boundary(
            ctx, success=False, stage=str(getattr(stats, "stage", "") or "")
        )
        # B5b 8.3：压缩失败（走截断兜底）同样发事件（success=False 可复盘）
        await ctx.bus.emit(
            Event(
                type=EventType.COMPRESSION_COMPLETED,
                data={
                    "stage": stats.stage,
                    "success": False,
                    "input_messages": stats.input_messages,
                    "output_messages": stats.output_messages,
                },
            )
        )
        if ctx.compress.consecutive_fallbacks >= _COMPRESSION_FALLBACK_RISK_THRESHOLD:
            logger.warning(
                "compression_loss_risk: consecutive_fallbacks=%d failed_count=%d "
                "stage=%s——连续压缩失败已截断兜底，存在上下文信息丢失风险",
                ctx.compress.consecutive_fallbacks,
                ctx.compress.compress_failed_count,
                getattr(stats, "stage", ""),
            )
        ctx.total_tokens = sum(ctx.provider.count_tokens(m.content or "") for m in ctx.messages)
        # 不可收敛（对话压到最小仍超预算）显式失败，杜绝假 MAX_TURNS 静默空回复。
        # 判定用可压缩对话口径——SYSTEM（工具 schema/静态提示）不可压，其占比高
        if (
            ctx.compressible_token_count()
            >= ctx.effective_context_window * ctx.config.compression_threshold
        ):
            ctx.terminal_reason = TerminalReason.PROMPT_TOO_LONG
            ctx.final_message = _PROMPT_TOO_LONG_MESSAGE
    # 放不进模型窗口，继续压缩/循环只会空转导致假 MAX_TURNS 空回复。与失败分支的
    # 单条巨大 raw 压缩后全变 SYSTEM，compressible 归零致旧判据失守），失败分支管
    # 「压无可压仍超阈」。request_token_count 为 LLM 请求前真实性口径（messages +
    # 工具 schema；工具 JSON Schema 经 tools 参数旁路发送、不进 messages，仅用 live
    # 口径会漏判超窗）。窗口取有效值（扣除输出预留）。
    if ctx.request_token_count() >= ctx.effective_context_window:
        ctx.terminal_reason = TerminalReason.PROMPT_TOO_LONG
        ctx.final_message = _PROMPT_TOO_LONG_MESSAGE
    # 转移层据此在「未收敛但有成效」时回 thinking 续压再评估（巨大单条首轮 micro
    # 无效/渐进部分收敛即被旧转移 terminated → 兜底假 MAX_TURNS 空回复）；无成效或
    # reason 已设（PROMPT_TOO_LONG）则直终。
    ctx.compress.last_shrunk = ctx.live_token_count() < before_live
    logger.info(
        "compression post-state: compressible=%d needs=%s failed=%s live=%d window=%d shrunk=%s "
        "input_msgs=%s",
        ctx.compressible_token_count(),
        ctx.needs_compression(),
        ctx.compress_failed(),
        ctx.live_token_count(),
        ctx.config.context_window,
        ctx.compress.last_shrunk,
        [(m.role.value, len(m.content or "")) for m in ctx.messages],
    )


async def _reactive_compact_retry(
    ctx: ExecutionContext,
    max_attempts: int = 2,
) -> LLMResponse | None:
    # + _inject_continuity_anchor 锚点；压缩无实际缩减时提前终止防无效循环
    from bandi.llm.strategy import is_prompt_too_long_error

    for _attempt in range(max_attempts):
        count_before = len(ctx.messages)
        # pipeline 复用与 provider.complete 注入在生产实现内）
        outcome = await get_query_deps(ctx).autocompact(ctx)
        if (
            not outcome.success
            or outcome.stats.stage == "none"
            or len(outcome.messages) >= count_before
        ):
            break  # 无法压缩或无实际缩减——继续重试纯属浪费 LLM 摘要成本
        ctx.messages = outcome.messages
        if outcome.stats.stage != "none":
            logger.info(
                "reactive compaction: stage=%s msgs %d->%d",
                outcome.stats.stage,
                count_before,
                len(ctx.messages),
            )
            _inject_continuity_anchor(ctx)
        ctx.total_tokens = sum(ctx.provider.count_tokens(m.content or "") for m in ctx.messages)
        rebuilt = _build_provider_messages(ctx)
        try:
            previous = ctx.meta.get("_reactive_retry_active")
            ctx.meta["_reactive_retry_active"] = True
            try:
                return await get_query_deps(ctx).call_model(ctx, rebuilt)
            finally:
                if previous is None:
                    ctx.meta.pop("_reactive_retry_active", None)
                else:
                    ctx.meta["_reactive_retry_active"] = previous
        except Exception as e:
            if not is_prompt_too_long_error(e):
                raise
            continue  # 压缩后仍超长 → 进入下一次压缩重试
    ctx.terminal_reason = TerminalReason.PROMPT_TOO_LONG
    ctx.final_message = "上下文过长且压缩后仍超出模型窗口，请开新会话或减小输入规模。"
    return None


def _inject_continuity_anchor(ctx: ExecutionContext) -> None:
    # 无 raw_input 则注入 synthetic Continue；输入仍在保留区时不注入。
    # 压缩按「最新 raw 是否仍在消息内」评估，raw 被裁后重新注入新锚点（刷新）
    # continuity_injected 仅作显式抑制开关（测试/紧急兜底），注入器自身不再置位。
    from bandi.core.message import Message, MessageRole
    from bandi.memory.compaction import CONTINUE_PROMPT

    if ctx.compress.continuity_injected:
        return
    raw = ctx.raw_input
    if raw:
        if any(m.role == MessageRole.USER and m.content == raw for m in ctx.messages):
            return
    elif any(m.role == MessageRole.USER and bool(m.meta.get("synthetic")) for m in ctx.messages):
        return
    content = raw or CONTINUE_PROMPT
    if raw and len(content) > _ANCHOR_RAW_MAX_CHARS + _ANCHOR_RAW_TAIL_CHARS:
        # 原文整体回注会抵消压缩；只留头（任务语义/约束）+ 尾（最终要求），中段
        # 已进压缩摘要。头/尾双保避免「长中文指令只截前缀把最后要求砍掉」。
        head = content[:_ANCHOR_RAW_MAX_CHARS]
        tail = content[-_ANCHOR_RAW_TAIL_CHARS:]
        content = f"{head}\n...[原始输入中段省略 {len(content) - _ANCHOR_RAW_MAX_CHARS - _ANCHOR_RAW_TAIL_CHARS} 字符]...\n{tail}"
    anchor = Message(
        role=MessageRole.USER,
        content=content,
        meta={"synthetic": bool(not raw)},
    )
    ctx.messages.append(anchor)


_ANCHOR_RAW_MAX_CHARS = 600  # 连续性锚点头段上限（任务语义集中在前缀）
_ANCHOR_RAW_TAIL_CHARS = 200  # 连续锚点尾段上限（最后的要求/约束不能丢）

POST_COMPACT_CONTEXT_HEADER = "[压缩后上下文]"
_POST_COMPACT_CONTEXT_MAX_CHARS = 10000
_POST_COMPACT_TOOLS_CAP = 3000
_POST_COMPACT_PLAN_CAP = 2000


def _post_compact_clip(text: str, cap: int) -> str:
    # 功能：压缩后附件单节裁剪（超限补省略标记）
    value = str(text or "").strip()
    if cap <= 0 or len(value) <= cap:
        return value
    return value[: max(cap - 16, 0)] + "\n...[已截断]"


def _post_compact_context_text(ctx: ExecutionContext) -> str:
    # 功能：压缩后回注——已用技能 + 工具面 + 当前计划（不依赖摘要记忆）
    from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX  # noqa: F401

    parts: list[str] = []

    tool_lines: list[str] = []
    used = 0
    for schema in ctx.active_tools or []:
        if not isinstance(schema, dict):
            continue
        name = str(schema.get("name") or "").strip()
        if not name:
            continue
        description = _post_compact_clip(str(schema.get("description") or ""), 200)
        line = f"- {name}: {description}" if description else f"- {name}"
        if used + len(line) > _POST_COMPACT_TOOLS_CAP:
            break
        tool_lines.append(line)
        used += len(line)
    if tool_lines:
        parts.append("### 工具面\n" + "\n".join(tool_lines))

    plan_text = ""
    ledger = ctx.meta.get("task_ledger")
    items = getattr(ledger, "items", None)
    if items:
        try:
            from bandi.tools.task_ledger import render_todos

            plan_text = str(render_todos(items) or "")
        except Exception:  # noqa: BLE001 — 计划渲染失败不阻断压缩
            plan_text = ""
    if not plan_text and str(ctx.meta.get("mode") or "") == "plan":
        try:
            from pathlib import Path

            session_id = str(ctx.meta.get("session_id") or getattr(ctx, "session_id", "") or "")
            plan_path = Path.home() / ".bandi" / "plans" / f"plan-{session_id[:8]}.md"
            if plan_path.is_file():
                plan_text = plan_path.read_text(encoding="utf-8")
        except OSError:
            plan_text = ""
    if plan_text.strip():
        parts.append("### 当前计划\n" + _post_compact_clip(plan_text, _POST_COMPACT_PLAN_CAP))

    if not parts:
        return ""
    body = "\n\n".join(parts)
    return f"{POST_COMPACT_CONTEXT_HEADER}\n\n{_post_compact_clip(body, _POST_COMPACT_CONTEXT_MAX_CHARS)}"


def _inject_post_compact_context(ctx: ExecutionContext) -> None:
    # 功能：把压缩后上下文附件放到摘要之后；重复压缩先删旧附件防叠加
    text = _post_compact_context_text(ctx)
    if not text:
        return
    from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX

    ctx.messages = [
        m
        for m in ctx.messages
        if not (
            m.role == MessageRole.SYSTEM
            and (m.content or "").startswith(POST_COMPACT_CONTEXT_HEADER)
        )
    ]
    insert_at = 0
    for index, message in enumerate(ctx.messages):
        if message.role == MessageRole.SYSTEM and (message.content or "").startswith(
            SUMMARY_MESSAGE_PREFIX
        ):
            insert_at = index + 1
    ctx.messages.insert(insert_at, Message(role=MessageRole.SYSTEM, content=text))


def _projection_tool_lines(
    ctx: ExecutionContext, *, keyword: str = "", limit: int = 4
) -> list[str]:
    # 功能：最近工具消息输出首行（剥 `<tool_output>` XML 包裹；keyword 按工具名筛选）
    out: list[str] = []
    for message in reversed(ctx.messages):
        role = getattr(message.role, "value", message.role)
        if str(role) != "tool":
            continue
        tool_name = str(getattr(message, "name", "") or "")
        if keyword and keyword not in tool_name:
            continue
        content = str(getattr(message, "content", "") or "").strip()
        if not content:
            continue
        body = next(
            (
                line.strip()
                for line in content.splitlines()
                if line.strip()
                and not line.strip().startswith("<tool_output")
                and line.strip() != "</tool_output>"
            ),
            "",
        )
        if not body:
            continue
        out.append(body[:200])
        if len(out) >= limit:
            break
    return list(reversed(out))


def _projection_sources_for(ctx: ExecutionContext) -> Any:
    # 功能：从 ctx.meta 组装投影数据源（缺失源自动跳过——投影按可用性降级）
    from bandi.memory.context_projection import ProjectionSources

    def _frames() -> Any:
        session_id = str(ctx.meta.get("session_id") or "")
        if not session_id:
            return None
        from bandi.skills.frame_store import load_frames
        from bandi.skills.progress import frame_scope_for_session

        return load_frames(frame_scope_for_session(session_id))

    def _tasks() -> list[dict[str, Any]]:
        ledger = ctx.meta.get("task_ledger")
        items = getattr(ledger, "items", None)
        return list(items) if isinstance(items, list) else []

    def _memories() -> list[str]:
        manager = ctx.meta.get("memory_manager")
        session_id = str(ctx.meta.get("session_id") or "")
        if manager is None or not session_id:
            return []
        text = str(manager.load_memory("session", session_id=session_id) or "")
        return [line.strip() for line in text.splitlines() if line.strip()][:3]

    def _steer_note() -> str | None:
        note = str(ctx.meta.get("_steer_note") or "").strip()
        return note or None

    return ProjectionSources(
        tool_results=lambda: _projection_tool_lines(ctx),
        frames=_frames,
        tasks=_tasks,
        memories=_memories,
        steer_note=_steer_note,
        knowledge=lambda: _projection_tool_lines(ctx, keyword="knowledge", limit=3),
    )


def _refresh_projection_if_changed(ctx: ExecutionContext) -> None:
    # 功能：变更刷新——已有投影段且帧/任务指纹变化时重建替换；无投影段不生成
    # （落点 A：压缩后才有投影）；源全空时清除过期投影段
    from bandi.memory.context_projection import (
        build_context_projection,
        projection_fingerprint,
        projection_message_index,
        upsert_projection_message,
    )

    if projection_message_index(ctx.messages) < 0:
        return
    sources = _projection_sources_for(ctx)
    fingerprint = projection_fingerprint(sources)
    if fingerprint == ctx.meta.get("projection_fingerprint"):
        return
    text = build_context_projection(sources)
    if text:
        upsert_projection_message(ctx.messages, text)
    else:
        stale = projection_message_index(ctx.messages)
        if stale >= 0:
            ctx.messages.pop(stale)
    ctx.meta["projection_fingerprint"] = fingerprint


def _insert_context_projection(ctx: ExecutionContext) -> None:
    # 功能：压缩后插入投影段（成功/截断兜底两路径统一出口）
    from bandi.memory.context_projection import (
        build_context_projection,
        projection_fingerprint,
        upsert_projection_message,
    )

    sources = _projection_sources_for(ctx)
    text = build_context_projection(sources)
    if text:
        upsert_projection_message(ctx.messages, text)
        ctx.meta["projection_fingerprint"] = projection_fingerprint(sources)


def _persist_compaction_boundary(ctx: ExecutionContext, *, success: bool, stage: str = "") -> None:
    """压缩结果落库——summary/boundary + 物理 preserved segment。

    此前压缩只改 ctx.messages 内存态，重启 get_branch 读回全量旧消息 →
    每轮重压（烧钱+数秒）、摘要白做。落库时把 active slice 重挂到 boundary
    之后：`get_branch()` 默认只回切片（含真实 entry id），原始条目仍在
    boundary 之前可审计；`active_messages` 快照保留为旧数据恢复兜底。
    """
    from bandi.core.message import message_to_snapshot

    session_store = ctx.meta.get("session_store")
    session_id = str(ctx.meta.get("session_id") or "")
    if session_store is None or not session_id:
        return
    from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX

    summary_text = ""
    for m in ctx.messages:
        if m.role == MessageRole.SYSTEM and (m.content or "").startswith(SUMMARY_MESSAGE_PREFIX):
            summary_text = m.content or ""
            break
    # 摘要由 boundary entry 承载；SYSTEM 消息（身份 prompt/技能指令/投影）
    # 每轮由感知链重新组装，不得随 preserved segment 持久化——否则恢复后
    # 会与新一轮的 system prompt 重复。
    preserved = [m for m in ctx.messages if m.role != MessageRole.SYSTEM]
    post_context = next(
        (
            m.content or ""
            for m in ctx.messages
            if m.role == MessageRole.SYSTEM
            and (m.content or "").startswith(POST_COMPACT_CONTEXT_HEADER)
        ),
        "",
    )
    try:
        session_store.append_summary_entry(
            session_id,
            int(ctx.turn_index),
            summary_text,
            metadata={
                "success": success,
                "stage": stage,
                "message_count": len(preserved),
                # 恢复/回放失败时的恢复快照（不含 SYSTEM 消息）
                "active_messages": [message_to_snapshot(m) for m in preserved],
                # 压缩后技能/工具/计划回注（重启恢复用）
                "post_compact_context": post_context,
            },
        )
        session_store.append_preserved_segment(session_id, preserved)
    except Exception as e:  # noqa: BLE001 — 落库失败不阻断压缩主链
        logger.warning("compaction boundary persist failed: %s", e)
