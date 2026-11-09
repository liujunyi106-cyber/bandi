# 功能：摘要实体保真提取校验（文件路径/错误串/函数调用/数值——本地正则，无 RAG）
"""摘要保真：从源文本提取关键实体并校验 LLM 摘要是否保留。

设计约束：纯本地方则 + 集合比对，不调 LLM、不建索引。
宁误报不漏斗——校验只触发告警/重试提示，不阻断主链，故提取可激进（召回优先）
通过去重 + 并存比控制噪声。"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

FIDELITY_RETENTION_THRESHOLD = 0.5

# 数值降权阈值：数字本就难全保留（token 数/端口/PID/批大小等真实必然略去）
FIDELITY_NUMBER_THRESHOLD = 0.7

# 保真重试提示（追加到摘要请求 user 内容，回填缺失实体引导 LLM 补全）
FIDELITY_RETRY_HINT_PREFIX = (
    "下方摘要遗漏了以下关键实体，请在保持既有结构中补全（若无法在原文核实则标注 (none)）："
)

MAX_FIDELITY_RETRIES = 2

_FILE_EXT = (
    r"\.(?:py|js|ts|jsx|tsx|json|yaml|yml|toml|ini|cfg|md|rst|txt|sql|log|c|h|"
    r"cpp|hpp|java|go|rs|sh|ps1|bat|xml|html|css|scss|vue|svelte|lock|env|"
    r"gitignore|dockerfile|config|ini|conf|csv|tsv|pdf|zip|tar|gz|whl)"
)
# 文件路径：可选目录（含 / \ . - 或盘符）+ 文件名 + 扩展名，可带 :行号
_FILEPATH_RE = re.compile(
    rf"(?<!\w)((?:[A-Za-z]:[\\/]|[\w.\-/\\]{{0,120}})?[\w.\-]+{_FILE_EXT}(?::\d+)?|"
    rf"[\w.\-/\\]{{2,45}}\.(?:yml|yaml|toml|ini|cfg|env)(?::\d+)?)(?!\w)",
    re.IGNORECASE,
)
# 错误串：异常名( '内容' ) 或 `Error`/`Failure`/`Exception` 后缀令牌
_ERROR_RE = re.compile(
    r"(?<!\w)((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Failure)"
    r"\([^)]{0,220}\))|((?<!\w)[A-Za-z_]\w*(?:Error|Exception|Failure)\b)(?![\w(])",
    re.IGNORECASE,
)
# 函数/方法调用：标识符与左括号紧邻（无空格）——散文括号（note (below)/see (above)）
_CALL_RE = re.compile(r"(?<!\w)((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)\(")
_CALL_KEYWORDS = {
    "if",
    "for",
    "while",
    "with",
    "def",
    "class",
    "return",
    "not",
    "and",
    "or",
    "in",
    "is",
    "lambda",
    "assert",
    "del",
    "raise",
    "except",
    "print",
    "import",
    "switch",
    "case",
    "match",
}
# 数值：整数/小数/带单位（排除纯单数字噪声——如日期、序号计数大段）
_NUMBER_RE = re.compile(
    r"(?<!\w)(\d{1,6}(?:\.\d{1,6})?(?:%|us|ms|s|mb|kb|gb|px|pt|vh|vw|rem|em|dpi)?)(?!\w)"
)
_NUMBER_STOPWORDS = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"}
# 端口/参数/批大小等裸数字仍保留。
_NUMBER_ORDINAL_PREFIX = "第"
_NUMBER_MEASURE_SUFFIXES = (
    "个",
    "次",
    "条",
    "位",
    "张",
    "年",
    "月",
    "日",
    "点",
    "号",
    "章",
    "节",
    "步",
    "项",
    "轮",
    "遍",
    "份",
    "台",
    "篇",
    "段",
)

# 保留至少一个同类标记——防止 LLM 摘要把「进行中/阻塞/失败」改写为「已完成/成功」
# （续作脚手架会据此误判任务已收尾）。`(none)` 表示无阻塞，不算活跃未完成标记。
_STATUS_NEGATIVE_KEYWORDS = (
    "blocked",
    "pending",
    "失败",
    "未完成",
    "阻塞",
    "待办",
    "进行中",
    "等待",
)

# 而摘要一个都不保留时，续作脚手架无法恢复「用户要做什么」——即使实体/状态保真
# 都通过，意图仍丢失。只收动作动词不收状态词（阻塞/等待等由 status_degraded 托管）
# 防与状态维度重叠误报。
_INTENT_ACTION_VERBS = (
    "修复",
    "实现",
    "排查",
    "优化",
    "重构",
    "添加",
    "删除",
    "修改",
    "支持",
    "集成",
    "解决",
    "调整",
    "升级",
    "迁移",
    "编写",
    "验证",
    "测试",
    "设计",
    "改进",
    "分析",
    "定位",
    "搭建",
    "配置",
    "部署",
    "fix",
    "implement",
    "investigate",
    "optimize",
    "refactor",
    "resolve",
    "upgrade",
    "migrate",
    "verify",
    "integrate",
    "deploy",
    "configure",
)

# 区分「已完成了什么」，判 completed_degraded（防只保实体不保进度）。
_COMPLETED_MARKERS = (
    "已完成",
    "完成",
    "已实现",
    "已修复",
    "已合并",
    "已提交",
    "已落地",
    "已解决",
    "done",
    "merged",
    "closed",
    "resolved",
    "committed",
)

_NEXT_ACTION_MARKERS = (
    "下一步",
    "接下来",
    "后续",
    "随后",
    "继续",
    "to-do",
    "todo",
    "next",
)

# 续作会违反约束而系统无感——独立维度判定 + 重试引导。
_CONSTRAINT_MARKERS = (
    "不要",
    "必须",
    "请勿",
    "禁止",
    "务必",
    "不得",
    "只能用",
    "仅支持",
    "don't",
    "do not",
    "must ",
    "never ",
    "avoid ",
)


def _extract_intent_verbs(text: str) -> list[str]:
    """功能：提取文本中的意图动作动词（去重保序；无动词返回空表）。"""
    out: list[str] = []
    for v in _INTENT_ACTION_VERBS:
        if v in (text or ""):
            out.append(v)
    return out


def _active_negative_markers(text: str) -> list[str]:
    """功能：提取未完成/阻塞状态标记（排除 `(none)` 无阻塞语境，防误报）。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        if not line or "(none)" in line:
            continue
        low = line.lower()
        for kw in _STATUS_NEGATIVE_KEYWORDS:
            if kw in low:
                out.append(kw)
                break
    return out


def _extract_markers(text: str, keywords: tuple[str, ...]) -> list[str]:
    """功能：逐行提取指定标记词集（排除 `(none)` 无内容语境）——完成态/下一步
    字段级保真的共用提取器。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        if not line or "(none)" in line:
            continue
        low = line.lower()
        for kw in keywords:
            if kw in low:
                out.append(kw)
                break
    return out


@dataclass
class FidelityEntity:
    """一个被提取的关键实体。"""

    kind: str  # filepath | error | call | number
    value: str


@dataclass
class FidelityReport:
    """一次源↔摘要保真对比的结果。"""

    source_entities: list[FidelityEntity] = field(default_factory=list)
    missing: list[FidelityEntity] = field(default_factory=list)
    missing_ratio: float = 0.0
    status_degraded: bool = False
    # （续作无法恢复「用户要做什么」）
    intent_degraded: bool = False
    completed_degraded: bool = False
    next_action_degraded: bool = False
    constraint_degraded: bool = False
    output_truncated: bool = False
    # 去重后的实体指纹（供摘要侧比对，含规范化数值）——不参与对外契约
    _source_keys: set[str] = field(default_factory=set, repr=False)
    _summary_keys: set[str] = field(default_factory=set, repr=False)

    @property
    def hard_failed(self) -> bool:
        """保真实体硬失败（审计 2.17）：filepath/error 类关键实体有缺失 → True
        调用方须回填原文附件兜底。"""
        return any(e.kind in ("filepath", "error") for e in self.missing)

    @property
    def degraded(self) -> bool:
        """保真是否不足——分层判定：任一关键类（filepath/error/call）缺失率超严格
        阈值、数值类缺失率超宽松阈值，或语义状态被改写（审查项2；数字降权避免
        噪声绑架 .11 回归），或用户意图动作动词全丢（审查项3）。状态/意图保真
        独立于结构化实体（纯状态/意图源无实体也须判定）。"""
        for kind in ("filepath", "error", "call"):
            if self._kind_ratio_above(kind, FIDELITY_RETENTION_THRESHOLD):
                return True
        if self._kind_ratio_above("number", FIDELITY_NUMBER_THRESHOLD):
            return True
        return (
            self.status_degraded
            or self.intent_degraded
            or self.completed_degraded
            or self.next_action_degraded
            or self.constraint_degraded
            or self.output_truncated
        )

    def _kind_ratio_above(self, kind: str, threshold: float) -> bool:
        """功能：某类实体的缺失率是否超阈值（无该类实体时不算缺失）。"""
        total = sum(1 for e in self.source_entities if e.kind == kind)
        if total == 0:
            return False
        miss = sum(1 for e in self.missing if e.kind == kind)
        return miss / total > threshold


def _normalize_kind_value(kind: str, value: str) -> str:
    """功能：实体归一化键——忽略大小写差、相对/绝对前缀、行号尾巴，数值按值。"""
    if kind in ("filepath", "call"):
        v = value.strip("/").strip("\\")
        return v.lower()
    return value.lower()


def _is_measure_word_context(text: str, start: int, end: int) -> bool:
    """功能：数字是否处于中文量词/序数语境（第 N 个/共 N 条）——叙述噪声不算实体。"""
    before = text[max(0, start - 2) : start].rstrip()
    if before.endswith(_NUMBER_ORDINAL_PREFIX):
        return True
    after = text[end : end + 2].lstrip()
    return bool(after) and after[0] in _NUMBER_MEASURE_SUFFIXES


def extract_fidelity_entities(text: str) -> list[FidelityEntity]:
    """功能：从文本提取关键实体（文件路径/错误串/函数调用/数值），去重保序。"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[FidelityEntity] = []
    for m in _FILEPATH_RE.finditer(text):
        v = m.group(0)
        k = _normalize_kind_value("filepath", v)
        if k not in seen:
            seen.add(k)
            out.append(FidelityEntity(kind="filepath", value=v))
    for m in _ERROR_RE.finditer(text):
        v = next(g for g in m.groups() if g) or m.group(0)
        k = _normalize_kind_value("error", v)
        if k not in seen:
            seen.add(k)
            out.append(FidelityEntity(kind="error", value=v))
    for m in _CALL_RE.finditer(text):
        name = m.group(1).split(".")[-1]
        if name in _CALL_KEYWORDS or name == "self":
            continue
        k = _normalize_kind_value("call", name)
        if k not in seen:
            seen.add(k)
            out.append(FidelityEntity(kind="call", value=name + "()"))
    for m in _NUMBER_RE.finditer(text):
        v = m.group(0)
        if v in _NUMBER_STOPWORDS:
            continue
        if _is_measure_word_context(text, m.start(), m.end()):
            continue
        k = _normalize_kind_value("number", v)
        if k not in seen:
            seen.add(k)
            out.append(FidelityEntity(kind="number", value=v))
    return out


def check_fidelity(source_text: str, summary_text: str) -> FidelityReport:
    """功能：对比源文本与摘要的实体保留情况，返回保真报告。"""
    source = extract_fidelity_entities(source_text)
    summary = extract_fidelity_entities(summary_text or "")
    source_keys = {_normalize_kind_value(e.kind, e.value) for e in source}
    summary_keys = {
        _normalize_kind_value(e.kind, e.value)
        for e in summary
        if e.kind in ("filepath", "error", "call") or e.kind == "number"
    }
    missing = [e for e in source if _normalize_kind_value(e.kind, e.value) not in summary_keys]
    ratio = len(missing) / len(source) if source else 0.0
    source_neg = _active_negative_markers(source_text)
    summary_neg = _active_negative_markers(summary_text)
    status_degraded = bool(source_neg) and not summary_neg
    source_verbs = _extract_intent_verbs(source_text)
    summary_verbs = _extract_intent_verbs(summary_text)
    intent_degraded = bool(source_verbs) and not summary_verbs
    source_done = _extract_markers(source_text, _COMPLETED_MARKERS)
    summary_done = _extract_markers(summary_text, _COMPLETED_MARKERS)
    completed_degraded = bool(source_done) and not summary_done
    source_next = _extract_markers(source_text, _NEXT_ACTION_MARKERS)
    summary_next = _extract_markers(summary_text, _NEXT_ACTION_MARKERS)
    next_action_degraded = bool(source_next) and not summary_next
    source_constraints = _extract_markers(source_text, _CONSTRAINT_MARKERS)
    summary_constraints = _extract_markers(summary_text, _CONSTRAINT_MARKERS)
    constraint_degraded = bool(source_constraints) and not summary_constraints
    report = FidelityReport(
        source_entities=source,
        missing=missing,
        missing_ratio=round(ratio, 4),
        status_degraded=status_degraded,
        intent_degraded=intent_degraded,
        completed_degraded=completed_degraded,
        next_action_degraded=next_action_degraded,
        constraint_degraded=constraint_degraded,
    )
    report._source_keys = source_keys
    report._summary_keys = summary_keys
    return report


def build_fidelity_retry_hint(report: FidelityReport, max_hint: int = 8) -> str:
    """功能：生成重试提示——列出摘要遗漏的缺失实体（去重），引导 LLM 补全；
    语义状态缺失（审查项2）时额外提示保留未完成/阻塞标记。"""
    missing_values = [e.value for e in report.missing]
    seen: set[str] = set()
    unique: list[str] = []
    for v in missing_values:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    parts: list[str] = []
    if unique:
        body = "\n".join(f"- {v}" for v in unique[:max_hint])
        parts.append(f"{FIDELITY_RETRY_HINT_PREFIX}\n{body}")
    if report.status_degraded:
        parts.append(
            "摘要遗漏了源中的未完成/阻塞状态（如 Blocked/pending/失败/待办），"
            "请如实保留这些未完成项，勿改写成已完成/成功。"
        )
    if report.intent_degraded:
        parts.append(
            "摘要丢失了源中的任务动作（如 修复/实现/排查/优化 等意图动词），"
            "请保留用户要执行的动作，否则后续无法恢复任务意图。"
        )
    if report.completed_degraded:
        parts.append(
            "摘要遗漏了源中的已完成事项（如 已完成/完成/done/已修复 等标记），"
            "请如实保留已完成的进度。"
        )
    if report.next_action_degraded:
        parts.append(
            "摘要遗漏了源中的「下一步/接下来/继续」等后续动作，请保留后续动作序列，否则无法续作。"
        )
    if report.constraint_degraded:
        parts.append(
            "摘要丢失了源的显式约束/偏好（如 不要/必须/请勿/只能 等），"
            "请在新摘要中如实保留这些约束，勿省略或改写。"
        )
    if report.output_truncated:
        parts.append(
            "上一次摘要因输出长度上限被截断（不完整检查点），"
            "请更简洁地输出完整结构，确保各节齐全、末尾不残缺。"
        )
    return "\n\n".join(parts)


def _resp_truncated(resp: Any) -> bool:
    """功能：LLM 响应是否因输出上限被截断（finish_reason=length / Anthropic·Google
    的 max_tokens 形态，审计 2.3；审查修复统一两系枚举）。"""
    return str(getattr(resp, "finish_reason", "") or "").lower() in {"length", "max_tokens"}


def _candidate_preferred(new_report: FidelityReport, best_report: FidelityReport) -> bool:
    """功能：多候选择优——未截断 > 非硬失败 > 缺失率低；
    旧实现只看 missing_ratio，会把已知被 provider 截断的候选选为最终产物。"""
    if new_report.output_truncated != best_report.output_truncated:
        return not new_report.output_truncated
    if new_report.hard_failed != best_report.hard_failed:
        return not new_report.hard_failed
    return new_report.missing_ratio <= best_report.missing_ratio


async def summarize_with_fidelity(
    llm_complete: Callable[[list[dict]], Awaitable[Any]],
    prompt: list[dict],
    source_text: str,
    *,
    postprocess: Callable[[str], str] | None = None,
) -> tuple[str, FidelityReport, int, int]:
    """功能：调用摘要 LLM + 实体保真校验；遗漏超阈值重试（最多 MAX_FIDELITY_RETRIES=2 次
    审计 2.17）。每次响应检查 finish_reason=length（不完整检查点，审计 2.3）并触发重试。

    返回 (final_summary, report, retries, failures)：
      - final_summary：最终采用摘要（重试成功用最后摘要；仍不足取遗漏较少者）
      - retries：实际重试次数（0..MAX_FIDELITY_RETRIES）
      - failures：保真不足且重试用尽的次数（0 或 1）
    产物总被接受（有内容优于空摘要）——保真缺失只告警不阻断主链；
    filepath/error 硬失败额外 ERROR 标记（调用方回填原文附件兜底）。
    postprocess：可选正文后处理（在保真校验前应用——如剥离 <analysis> 草稿段）。
    """
    best_text: str | None = None
    best_report: FidelityReport | None = None
    current_prompt = prompt
    for attempt in range(MAX_FIDELITY_RETRIES + 1):
        resp = await llm_complete(current_prompt)
        text = _content_of(resp)
        if postprocess is not None:
            # ①：格式后处理在保真校验前（校验针对最终产物——草稿实体不算保留）
            text = postprocess(text)
        report = check_fidelity(source_text, text)
        if _resp_truncated(resp):
            report.output_truncated = True
        if best_report is None or _candidate_preferred(report, best_report):
            best_text, best_report = text, report
        if not report.degraded:
            return text, report, attempt, 0
        if attempt >= MAX_FIDELITY_RETRIES:
            break
        hint = build_fidelity_retry_hint(report)
        current_prompt = prompt + [{"role": "user", "content": hint}] if hint else prompt

    assert best_text is not None and best_report is not None
    if best_report.hard_failed:
        logger.error(
            "compression_fidelity_hard_fail: filepath/error 缺失未修复 key_entities=%d missing=%d",
            sum(1 for e in best_report.missing if e.kind in ("filepath", "error")),
            len(best_report.source_entities),
        )
    logger.warning(
        "compression_fidelity_risk: missing_ratio=%.2f missing=%d entities=%d",
        best_report.missing_ratio,
        len(best_report.missing),
        len(best_report.source_entities),
    )
    return best_text, best_report, MAX_FIDELITY_RETRIES, 1


def _content_of(resp: Any) -> str:
    """功能：从 LLM 响应取 content，兼容 None/缺字段。"""
    if resp is None:
        return ""
    return getattr(resp, "content", "") or ""
