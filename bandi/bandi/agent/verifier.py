# 功能：Verifier — VER 验证闸门（LLM-as-Judge）

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bandi.agent.base import ExecutionContext

if TYPE_CHECKING:
    from bandi.llm.base import BaseProvider

logger = logging.getLogger(__name__)

_VERIFY_TRUNCATE_CHARS = 2000
_VERIFY_TOOL_WINDOW = 8


def _recent_tool_messages(ctx: ExecutionContext) -> list[Any]:
    """取消息尾部窗口内的 TOOL 消息——工具证据的持久化来源（瞬时列表清空后仍可判定）。"""
    from bandi.core.message import MessageRole

    return [
        m
        for m in getattr(ctx, "messages", [])[-_VERIFY_TOOL_WINDOW:]
        if getattr(m, "role", None) == MessageRole.TOOL
    ]


VER_JUDGE_SYSTEM_PROMPT = """你是 Bandi 数字员工组织的验证裁判。判断 Agent 是否完成了用户任务。

判定标准（从高到低优先级）：
1. 安全风险：输出是否包含不安全/恶意/越权内容？→ escalate
2. 原始目标：最终输出是否仍服务用户最初请求（不是只完成最近一轮子任务）？
3. 任务完成：输出是否达成了当前用户要求的结果？→ pass
4. 工具证据：工具选择和参数是否合理？涉及文件/命令/代码改动时，是否给出可核对的路径、命令或测试结果？不合理 → rework

输出格式：先用不超过 100 字给出简要分析（对照判定标准逐条核对），随后单独一行输出最终判定 JSON。

few-shot 示例：
示例1：任务=编写排序函数，输出实现了要求的排序且处理了边界输入。
分析：实现了要求的功能，边界输入已覆盖，未见安全风险。
{"action": "pass", "reason": "功能完成且无安全风险"}

示例2：任务=修复空输入崩溃的报错，输出改了日志文案但未处理空输入。
分析：用户报告的崩溃场景未被修复，改动与问题无关。
{"action": "rework", "reason": "空输入场景未处理"}

示例3：输出包含删除用户数据目录的命令且未获得授权。
分析：存在不可逆的高危操作，需人工确认。
{"action": "escalate", "reason": "高危操作需人工确认"}

最终判定 JSON 只含 action 与 reason 两个字段：action 取 "pass"（任务完成，无安全问题）、"rework"（任务未完成或可改进，通知 Agent 重试）或 "escalate"（存在安全风险或无法解决，需要人工介入）；reason 为中文理由（20 字以内）。"""

VER_JUDGE_USER_TEMPLATE = """原始目标：{original_goal}
所有用户消息（保真锚点）：{user_messages}
当前用户任务：{task}
Agent 响应：{output}
工具调用结果：{tool_results}

请判定。若最终输出只完成了当前子任务、却偏离原始目标，必须 rework/escalate，不能 pass。"""


@dataclass
class VerifierPacket:
    task_description: str
    output: str
    prior_rework_count: int = 0
    prior_rework_feedback: str = ""
    original_goal: str = ""
    user_messages: str = ""


@dataclass
class Verdict:
    action: str
    reason: str
    blocker_fingerprint: str = ""
    analysis: str = ""


@dataclass
class VerifierPolicy:
    # stagnation_threshold（verifier.py `_parse_verdict` 判定 rework_count 达阈值
    # 即 escalate），max_revisions 全仓零读零配置，保留只会误导。
    enabled: bool = True
    min_todos_for_verification: int = 3
    fail_open: bool = False
    stagnation_threshold: int = 3
    require_on_code_edits: bool = True


class Verifier:
    ALLOWED_ACTIONS = ("pass", "rework", "escalate", "fail")

    def __init__(
        self,
        policy: VerifierPolicy | None = None,
        provider: BaseProvider | None = None,
    ) -> None:
        self.policy = policy or VerifierPolicy()
        self._provider = provider

    async def evaluate(self, packet: VerifierPacket) -> Verdict:
        if self._provider is None:
            if self.policy.fail_open:
                return Verdict(action="pass", reason="Verifier: no LLM provider, default pass")
            return Verdict(
                action="fail", reason="Verifier: no LLM provider, verification failed (fail-closed)"
            )

        return await self._call_judge(packet)

    async def _call_judge(self, packet: VerifierPacket) -> Verdict:
        tool_results_str = ""
        if hasattr(packet, "_tool_results_str"):
            tool_results_str = packet._tool_results_str

        user_prompt = VER_JUDGE_USER_TEMPLATE.format(
            original_goal=(packet.original_goal or packet.task_description)[
                :_VERIFY_TRUNCATE_CHARS
            ],
            user_messages=(packet.user_messages or "无")[:_VERIFY_TRUNCATE_CHARS],
            task=packet.task_description[:_VERIFY_TRUNCATE_CHARS],
            output=packet.output[:_VERIFY_TRUNCATE_CHARS],
            tool_results=tool_results_str or "无",
        )
        try:
            from bandi.llm.accounted import COMPANY_JUDGE_BUDGET_SEC, accounted_complete

            resp = await accounted_complete(
                self._provider,
                messages=[
                    {"role": "system", "content": VER_JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                label="verifier",
                session_id="verifier",
                budget_sec=COMPANY_JUDGE_BUDGET_SEC,
                temperature=0.0,
            )
            return self._parse_verdict(resp.content or "", fail_open=self.policy.fail_open)
        except TimeoutError as e:
            # 功能：裁判基础设施超时按 best-effort 降级放行——主回复已产出，
            # 不能因旁路检查超时把整轮判失败并让卡永久停在 running。
            from bandi.utils.errors import error_text

            logger.warning("Verifier judge timeout; deliver output: %s", error_text(e))
            return Verdict(action="pass", reason=f"judge unavailable (timeout): {error_text(e)}")
        except Exception as e:  # noqa: BLE001
            from bandi.utils.errors import error_text

            logger.warning("Verifier judge call failed: %s", error_text(e))
            if self.policy.fail_open:
                return Verdict(action="pass", reason=f"judge error: {error_text(e)}")
            return Verdict(action="fail", reason=f"judge error (fail-closed): {error_text(e)}")

    @staticmethod
    def _parse_verdict(text: str, fail_open: bool = False) -> Verdict:
        text = text.strip()
        analysis = ""
        data: dict[str, Any] | None = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 含 action 字段的 JSON 对象（分析文本可能含散落花括号，取尾防误截）
            import re

            candidates = re.findall(r"\{[^{}]*\}", text, re.DOTALL)
            for candidate in reversed(candidates):
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and "action" in parsed:
                    data = parsed
                    analysis = text[: max(text.find(candidate), 0)].strip()
                    break
            if data is None and "```" in text:
                m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(1))
                    except json.JSONDecodeError:
                        data = {}
        if data is None:
            data = {}
        if not analysis:
            analysis = ""
        action = data.get("action") or ("pass" if fail_open else "fail")
        reason = data.get("reason", "")
        if action not in Verifier.ALLOWED_ACTIONS:
            # fail-closed 下 LLM 输出格式错误/非法 action 按 fail 处理
            # （与 judge 异常路径 fail-closed 一致）；fail_open 才默认 pass
            action = "pass" if fail_open else "fail"
        return Verdict(action=action, reason=reason, analysis=analysis[:500])

    def verification_required(self, ctx: ExecutionContext) -> bool:
        if not self.policy.enabled:
            return False
        live = list(ctx.tool_results or [])
        # 工具结果已由 on_executing 物化为 TOOL 消息、on_thinking 清空瞬时列表——
        # 最终回答轮 REFLECTING 时 ctx.tool_results 为空，只看列表会漏掉工具轮
        # 后的综合回答（VER 闸门被空转绕过）。补消息窗口证据（最近窗口内
        # 的 TOOL 消息），窗口外滑出的历史工具轮不误触发。
        recent = _recent_tool_messages(ctx)
        if not live and not recent:
            return False
        if self.policy.require_on_code_edits:
            names = [tr.tool_name for tr in live] + [m.name or "" for m in recent]
            if any(n in ("bash", "write", "edit", "apply_patch") for n in names):
                return True
        return len(live) + len(recent) >= self.policy.min_todos_for_verification

    def apply_stagnation_cap(self, task_meta: dict[str, Any] | None, verdict: Verdict) -> Verdict:
        if not task_meta:
            return verdict
        last_outcome = task_meta.get("last_verdict_outcome")
        rework_count = task_meta.get("rework_count", 0)
        # 仅当本次仍判 rework 时才升级——pass 是任务已修好的信号
        # 不得被停滞封顶无条件覆盖（先花钱 judge 判通过再丢弃）
        if (
            last_outcome == "rework"
            and rework_count >= self.policy.stagnation_threshold
            and verdict.action == "rework"
        ):
            return Verdict(
                action="escalate",
                reason=f"超过停滞阈值({self.policy.stagnation_threshold})，强制升级",
            )
        return verdict
