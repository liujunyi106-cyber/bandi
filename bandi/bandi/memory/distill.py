# 功能：草稿提炼器——提名时对情节提炼一次（LLM 优先、规则降级），产出结构化记忆
#
# 触发点 = 提名时：只对通过晋升门槛的情节调用，绝不每轮对话调用。
# 产出形态：item.content = 提炼后的可读断言句；metadata["structured"] = {kind,key,...}；
# evidence = 情节原句留底。提炼失败（断 LLM/非法输出）→ 规则降级，kind/key 恒存在。
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bandi.memory.episodic import Episode
from bandi.memory.structured import STRUCTURED_KINDS, build_structured, normalize_key
from bandi.utils.llm_json import extract_llm_json

logger = logging.getLogger(__name__)

_WRAPPER_SEP = "→ 回答:"
_CONTENT_MAX_CHARS = 240
_INPUT_MAX_CHARS = 1500
_DISTILL_BUDGET_SEC = 30.0
_DISTILL_MAX_TOKENS = 512

# 功能：提炼提示词——固定 JSON 契约（kind/key/content/importance），
# 参考 StaffDeck memory_extractor_prompt 的稳定键与不存流水纪律。
DISTILL_PROMPT = """你是长期记忆提炼助手。从下面一段对话经历摘要中提炼一条**稳定的长期记忆**。

严格遵守：
- 只保留稳定信息：用户身份/称呼、稳定偏好、长期背景、对服务方式的稳定要求、项目约定与决策。
- 不保存：一次性业务流水、临时诉求、工具执行结果、任务完成汇报（已生成/已完成/预览路径/功能清单）、助手回复原文堆砌。
- 同一信息更新时 key 保持不变（更新覆盖，不新增重复条目）。
- key 用稳定 snake_case 英文（如 report_format / preferred_name / contact）。
- content 写可直接使用的中文断言句，不得复制对话包装（不含「用户:」「回答:」字样）。
- importance 取 0~1：身份/称呼 ≥0.9，稳定偏好 0.75~0.9，弱事实 0.5~0.7。
- **这段经历没有可长期复用的信息时，只输出 {"kind": "discard"}**——宁可丢弃，不要勉强概括。
- 只输出 JSON 对象，不要 Markdown、解释或代码围栏。

输出格式（二选一）：
{"kind": "profile|preference|fact|episode", "key": "stable_snake_case_key", "content": "断言句", "importance": 0.8}
{"kind": "discard"}
"""

# 功能：丢弃结论的 kind——调用方据此跳过提名，不写任何记忆。
_DISCARD_KIND = "discard"

_MARKDOWN_NOISE_RE = re.compile(r"[*#>`]+")


@dataclass(frozen=True)
class DistilledMemory:
    """功能：一次提炼的产物（structured 三件套 + 可选值/三元组字段 + 来源）。"""

    kind: str
    key: str
    content: str
    importance: float = 0.0
    value: str = ""
    subject: str = ""
    predicate: str = ""
    object_value: str = ""
    distilled_by: str = "rule"

    def to_structured(self) -> dict[str, str]:
        """功能：渲染 metadata["structured"] 落库形态（空字段不入库）。"""
        data = {"kind": self.kind, "key": self.key}
        if self.value:
            data["value"] = self.value
        if self.subject:
            data["subject"] = self.subject
        if self.predicate:
            data["predicate"] = self.predicate
        if self.object_value:
            data["object"] = self.object_value
        return data


def strip_wrapper(summary: str) -> tuple[str, str]:
    """功能：剥离「用户: X → 回答: Y」对话包装——返回 (用户侧, 回答侧)。

    无包装时用户侧为空串、回答侧为全文（原句即内容）。
    """
    body = str(summary or "").strip()
    if _WRAPPER_SEP in body:
        user_side, _, answer_side = body.partition(_WRAPPER_SEP)
        return user_side.removeprefix("用户:").strip(), answer_side.strip()
    return "", body


def _clip_content(text: str) -> str:
    """功能：内容清理——去 Markdown 装饰符、折叠空白、截断上限。"""
    body = _MARKDOWN_NOISE_RE.sub("", str(text or ""))
    body = re.sub(r"\s+", " ", body).strip()
    return body[:_CONTENT_MAX_CHARS]


def is_discard(memory: DistilledMemory) -> bool:
    """功能：提炼结论是否为「不值得记」——调用方据此跳过提名。"""
    return memory.kind == _DISCARD_KIND or (
        memory.kind == "episode" and memory.key == "unprocessed" and memory.distilled_by != "rule"
    )


def _discard(*, distilled_by: str) -> DistilledMemory:
    """功能：构造丢弃结论（不携带任何记忆内容）。"""
    return DistilledMemory(kind=_DISCARD_KIND, key="", content="", distilled_by=distilled_by)


def rule_distill(summary: str, *, importance: float = 0.0) -> DistilledMemory:
    """功能：规则降级提炼——优先用户侧；用户侧无内容时只收「带结论的回答侧」。

    绝不无条件搬运「→ 回答:」之后的助手回复：执行汇报与功能清单写进长期记忆后，
    会在后续召回里冒充「项目做过什么」（真机实证：审批卡出现「做好了老板！待办
    列表 App 已生成：预览路径…」）。回答侧只有在自身不是噪声、且明确带结论信号
    （结论/决定/约定/教训/原因/下一步）时才被采用——用户随口一问、助手查出关键
    事实的场景仍然留得下来。
    """
    from bandi.memory.ingest_review import MEMORY_VALUE_SIGNALS, is_noise_memory

    user_side, answer_side = strip_wrapper(summary)
    wrapped = _WRAPPER_SEP in str(summary or "")
    source = (user_side if wrapped else answer_side).strip()
    if not source or is_noise_memory(source) is not None:
        answer = answer_side.strip() if wrapped else ""
        usable_answer = bool(answer) and (
            is_noise_memory(answer) is None
            and any(signal in answer for signal in MEMORY_VALUE_SIGNALS)
        )
        if not usable_answer:
            return _discard(distilled_by="rule")
        source = answer
    content = _clip_content(source)
    if not content:
        return _discard(distilled_by="rule")
    structured = build_structured(source) or {}
    return DistilledMemory(
        kind=str(structured.get("kind") or "episode"),
        key=str(structured.get("key") or "unprocessed"),
        content=content,
        importance=float(importance or 0.0),
        value=str(structured.get("value") or ""),
        subject=str(structured.get("subject") or ""),
        predicate=str(structured.get("predicate") or ""),
        object_value=str(structured.get("object") or ""),
        distilled_by="rule",
    )


def _from_llm_data(data: object, *, importance: float) -> DistilledMemory | None:
    """功能：LLM JSON → DistilledMemory——discard 直通；kind/key/content 任一缺失即非法（None 降级）。"""
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "").strip().lower()
    if kind == _DISCARD_KIND:
        return _discard(distilled_by="llm")
    if kind not in STRUCTURED_KINDS:
        return None
    key = normalize_key(data.get("key"))
    if not key:
        return None
    content = str(data.get("content") or "").strip()
    if not content:
        return None
    try:
        llm_importance = float(data.get("importance") or 0.0)
    except (TypeError, ValueError):
        llm_importance = 0.0
    return DistilledMemory(
        kind=kind,
        key=key,
        content=_clip_content(content) or content[:_CONTENT_MAX_CHARS],
        importance=min(1.0, max(0.0, llm_importance or importance)),
        distilled_by="llm",
    )


async def distill_episode_async(episode: Episode, provider: Any) -> DistilledMemory:
    """功能：一次异步提炼——LLM 优先、任何失败降级规则（永不抛出）。"""
    summary = str(getattr(episode, "summary", "") or "")
    importance = float(getattr(episode, "importance", 0.0) or 0.0)
    try:
        from bandi.llm.accounted import accounted_complete

        response = await accounted_complete(
            provider,
            [
                {"role": "system", "content": DISTILL_PROMPT},
                {"role": "user", "content": summary[:_INPUT_MAX_CHARS]},
            ],
            label="memory_distill",
            session_id="memory_distill",
            agent_id="memory_distill",
            max_tokens=_DISTILL_MAX_TOKENS,
            budget_sec=_DISTILL_BUDGET_SEC,
        )
        memory = _from_llm_data(extract_llm_json(response.content or ""), importance=importance)
        if memory is not None:
            return memory
        logger.debug("memory distill payload invalid, rule fallback")
    except Exception as e:  # noqa: BLE001 — 提炼失败必须降级不阻断提名
        logger.warning("memory distill LLM failed: %s", e)
    return rule_distill(summary, importance=importance)


def _build_default_provider() -> Any:
    """功能：构建默认 provider（每次同步提炼独立构建）。

    不做进程级缓存：provider 内部的 HTTP 客户端绑定创建时的事件循环，
    而同步门面每次 asyncio.run 都会新建/关闭 loop——跨调用复用同一
    provider 会在第二次调用报「Event loop is closed」。构建本身无网络
    调用（仅读配置装配），按次数开销可忽略。
    """
    try:
        from bandi.agent.factory import build_provider
        from bandi.core.config import BandiConfig

        return build_provider(BandiConfig.load())
    except Exception as e:  # noqa: BLE001 — 构建失败保持 None（走规则降级）
        logger.warning("memory distill provider build failed: %s", e)
        return None


def distill_episode(episode: Episode, *, provider: Any | None = None) -> DistilledMemory:
    """功能：同步提炼门面——默认 provider 独立构建；Mock/无凭证/事件循环占用即降级。

    线程要求：调用线程不得持有运行中的事件循环（巩固在 asyncio.to_thread 线程池，
    CLI 在主线程——均满足）；不满足时降级规则提炼不抛出。
    """
    summary = str(getattr(episode, "summary", "") or "")
    importance = float(getattr(episode, "importance", 0.0) or 0.0)
    if provider is None:
        provider = _build_default_provider()
    if provider is None:
        return rule_distill(summary, importance=importance)
    from bandi.llm.mock import MockProvider

    if isinstance(provider, MockProvider):
        # 无凭证/测试环境：MockProvider 不产生真实提炼，直接规则降级（不假调用）
        return rule_distill(summary, importance=importance)
    try:
        return asyncio.run(distill_episode_async(episode, provider))
    except RuntimeError as e:
        logger.warning("memory distill skipped (event loop in thread): %s", e)
    except Exception as e:  # noqa: BLE001 — 门面兜底：任何异常不得穿透提名链
        logger.warning("memory distill failed: %s", e)
    return rule_distill(summary, importance=importance)


def distill_for_episode(
    episode: Episode, distiller: Callable[[Episode], DistilledMemory] | None = None
) -> DistilledMemory:
    """功能：提名路径统一入口——注入的 distiller 优先；异常/非法返回降级规则提炼。"""
    if distiller is None:
        return distill_episode(episode)
    try:
        result = distiller(episode)
        if isinstance(result, DistilledMemory):
            return result
        logger.warning("distiller returned invalid result, rule fallback")
    except Exception as e:  # noqa: BLE001 — 提炼失败不得丢失提名
        logger.warning("distiller failed, rule fallback: %s", e)
    return rule_distill(
        str(getattr(episode, "summary", "") or ""),
        importance=float(getattr(episode, "importance", 0.0) or 0.0),
    )


__all__ = [
    "DISTILL_PROMPT",
    "DistilledMemory",
    "distill_episode",
    "distill_episode_async",
    "distill_for_episode",
    "is_discard",
    "rule_distill",
    "strip_wrapper",
]
