# 功能：入库价值判断——纯规则三条件评分（长期价值/可结构化/影响个性化）
from __future__ import annotations

import re
from dataclasses import dataclass

from bandi.memory.extraction import extract_memory_fields

_VALUE_WORDS = ("教训", "结论", "偏好", "决定", "约定", "配置", "规范", "流程", "事实")
# 功能：问题/异常信号词——含业务问题的寒暄句不得被纯寒暄短路误杀
_ISSUE_WORDS = (
    "报错",
    "错误",
    "失败",
    "修复",
    "断了",
    "断开",
    "异常",
    "问题",
    "需要",
    "无法",
    "不能",
    "崩溃",
    "超时",
    "丢失",
    "bug",
    "error",
    "fail",
)
_PLEASANTRY_WORDS = (
    "你好",
    "您好",
    "谢谢",
    "感谢",
    "再见",
    "拜拜",
    "辛苦",
    "hello",
    "hi",
    "thanks",
    "thank you",
    "好的",
    "收到",
)
# 功能：纯确认/回执——没有可复用事实，不得进入情节或长期记忆。
_ACK_ONLY_RE = re.compile(
    r"^(?:ok|okay|收到|已收到|好的|好|嗯+|明白|了解|知道了|已记录|谢谢|感谢|辛苦了?)$",
    re.IGNORECASE,
)
# 功能：纯问候/短确认——用户只打招呼或要求回 OK，不构成可复用经历。
_USER_GREETING_RE = re.compile(
    r"^(?:你好|您好|嗨|哈喽|在吗|在不在|早上好|下午好|晚上好|hello|hi|hey|"
    r"只回复\s*ok|只回\s*ok|回复\s*ok|ok)$",
    re.IGNORECASE,
)
_EMOTION_WORDS = (
    "哈哈",
    "呵呵",
    "嘿嘿",
    "嘻嘻",
    "呜呜",
    "哎呀",
    "天哪",
    "太棒了",
    "厉害",
    "棒极了",
)

_LONG_TEXT_CHARS = 60
_PLEASANTRY_MAX_CHARS = 12
_MARKS = "，。！？!?；;、,.…~～ \t\r\n"

# 功能：可从现状推导的内容（cc-haha「What NOT to save」第2–5 类移植）——读代码、
# `git log`/`git blame` 即可得出，不是经历事实。命中任一即不进经历层。
_DERIVABLE_PATTERNS = re.compile(
    "|".join(
        (
            r"git\s+(?:log|blame|diff)\b",  # 版本历史
            r"\bcommit\s+[0-9a-f]{7,}",  # 提交哈希
            r"代码\s*(?:架构|模式|规范|约定)",  # 代码模式/架构
            r"修复步骤|调试配方|解决办法是改",  # 调试配方
            r"重构了|已合并|合并到\s",  # 已落地的改动记录
            r"文件\s*(?:路径|结构|目录结构)",  # 文件结构
        )
    ),
    re.IGNORECASE,
)

# 功能：任务完成汇报——「已生成/已创建/已补上」+ 产物路径或功能清单，顺序固定。
# 这是一次执行的收尾话术：路径随产物移动、清单随版本变化，既不构成用户偏好也不
# 构成可复用结论；存下来只会在召回时冒充「项目做过什么」。
_EXECUTION_REPORT_RE = re.compile(
    r"(?:已生成|已创建|已补上|已实现|已写好|已保存|已添加|已完成|已上线|做好了)"
    r"[\s\S]{0,160}?"
    r"(?:预览路径|访问路径|保存到|写入到|存到|双击打开|浏览器访问|刷新不丢失"
    r"|功能\s*[:：]|当前功能|路径\s*[:：])"
)

# 功能：连通性探针/占位输入——不承载任何事实，不构成经历。
_TRIVIAL_PROBE_RE = re.compile(
    r"^(?:ping|pong|test|testing|测试|在吗|在不在|1|\.|。。)$",
    re.IGNORECASE,
)

# 功能：验收/测试指令——要求模型逐字回复、真机验证等，不是可复用事实。
_TEST_INSTRUCTION_RE = re.compile(
    r"(?:real[-\s]?machine\s+verification|reply\s+with\s+exactly|test\s+message"
    r"|真机验证|验证消息|测试指令|请只回复\s*[\w_]+)",
    re.IGNORECASE,
)


def is_execution_report(text: str) -> bool:
    """功能：判断文本是否为「任务完成汇报」——完成词后接产物路径/功能清单。"""
    return bool(_EXECUTION_REPORT_RE.search(str(text or "")))


_PERSONAL_TYPES = {"preference", "procedural", "process_conclusion"}
_CONDITION_NAMES = ("long_term_value", "structurable", "personalized")
_WEIGHTS = (0.4, 0.3, 0.3)
# 功能：回复里的「有产出」信号——判定一轮回复是否留下可跨轮复用的东西。
# 单一来源：情节写入（agent_runner）与降级提炼（distill）同用这份，禁止各自维护。
MEMORY_VALUE_SIGNALS = ("结论", "决定", "约定", "教训", "原因", "下一步", "已修复", "已确认")
# 功能：显式触达词——用户明确要求记住的内容直接入库（触点触发语义），不看条件数。
_EXPLICIT_REMEMBER_WORDS = (
    "记住",
    "记下",
    "帮我记",
    "别忘了",
    "别忘",
    "我的偏好",
    "以后都",
    "下次都",
    "请始终",
    "永远用",
)


@dataclass
class IngestVerdict:
    """功能：入库价值判断结果（decision = accept/stage/reject）。"""

    decision: str
    reason: str
    score: float


def _has_business_signal(text: str) -> bool:
    """功能：文本是否含价值或问题信号（纯寒暄判定据此放行）。"""
    lowered = text.lower()
    return any(word in text for word in _VALUE_WORDS) or any(
        word in lowered for word in _ISSUE_WORDS
    )


def _low_value_reason(text: str) -> str | None:
    """功能：低价值短路判定（空/单字/纯寒暄/纯情绪 → reject）。"""
    body = text.strip()
    condensed = body.strip(_MARKS)
    if not condensed:
        return "empty"
    if len(condensed) <= 1:
        return "single_char"
    if _ACK_ONLY_RE.match(condensed):
        return "ack_only"
    lowered = body.lower()
    if (
        len(condensed) <= _PLEASANTRY_MAX_CHARS
        and any(w in lowered for w in _PLEASANTRY_WORDS)
        and not _has_business_signal(body)
    ):
        return "pleasantry"
    residual = body
    for mark in _EMOTION_WORDS:
        residual = residual.replace(mark, "")
    if not residual.strip(_MARKS):
        return "emotion"
    return None


def has_explicit_remember(text: str) -> bool:
    # 功能：文本是否含显式记住触达词（写入门槛与准入判定共用同一词表）。
    return any(word in str(text or "") for word in _EXPLICIT_REMEMBER_WORDS)


_USER_FACT_MARKERS = (
    # 功能：用户陈述事实/偏好的标志词——第一人称陈述与偏好/约定类表述
    "我叫",
    "我的",
    "我喜欢",
    "我不",
    "我讨厌",
    "我偏好",
    "我习惯",
    "我不吃",
    "以后",
    "一律",
    "都给我",
    "别再",
    "不要再",
    "记住",
    "记一下",
    "代号",
    "端口",
    "暗号",
    "座右铭",
    "称呼",
    "偏好",
    "习惯",
    "忌口",
)
# 功能：自我指涉与能力清单标志——出现在回复开头即判定为「介绍/寒暄」类，非事实内容。
_SELF_INTRO_MARKERS = (
    "我是",
    "我叫",
    "我可以",
    "我能",
    "我能帮",
    "我可以帮",
    "有什么可以",
    "有什么需要",
    "老板好",
    "您好",
)


_CONCLUSION_SIGNALS = (
    # 功能：回复里出现即视为「有可记事实产出」的信号词——用于区分
    # 「结论型回复」与「自我介绍/能力清单型回复」
    "结论",
    "确认",
    "已完成",
    "发现",
    "原因",
    "结果",
    "决定",
    "约定",
    "教训",
    "建议",
    "下一步",
)


def is_self_intro_reply(reply: str) -> bool:
    """功能：判断助手回复是否为自我介绍/能力清单——这类内容没有可记事实。

    BC-004：一句「你好呀，你能帮我做什么？」的回复（241 字能力清单）被当成
    task_state 写入长期记忆并被召回一次。判据看正文是否以自我指涉或能力
    清单开头，且不含结论类信号。
    """
    body = (reply or "").strip()
    if not body:
        return False
    if any(signal in body for signal in _CONCLUSION_SIGNALS):
        return False
    head = body[:80]
    return any(marker in head for marker in _SELF_INTRO_MARKERS)


# 功能：任务契约模板（公司派单拼在 raw_input 最前）——不是用户陈述，不参与可记判定。
_TASK_CONTRACT_MARKERS = ("任务契约", "输出要求", "验收标准", "## 任务", "请按以下")
# 功能：用户陈述的长度上限——超长文本里的编号/数字来自模板，不代表可记事实。
_USER_STATEMENT_MAX_CHARS = 200


def is_rememberable_user_input(text: str, reply: str = "") -> bool:
    """功能：判断用户输入本身是否值得作为长期记忆直接落库。

    用户已陈述事实/偏好（含偏好/约定表述）时为真——此时记用户原话。
    **含数字不再单独放行**：公司派单把任务契约模板拼在 raw_input 最前，模板里的
    编号/数量会让「含数字」恒真，于是每一轮员工执行都被写成长期记忆。
    """
    body = (text or "").strip()
    if not body:
        return False
    if is_self_intro_reply(reply):
        return False
    if any(marker in body for marker in _USER_FACT_MARKERS):
        return True
    if len(body) > _USER_STATEMENT_MAX_CHARS or any(
        marker in body for marker in _TASK_CONTRACT_MARKERS
    ):
        return False
    if any(char.isdigit() for char in body):
        return True
    return False


_ARTIFACT_REF = re.compile(
    r"`[^`\n]*[/\\][^`\n]*\.[A-Za-z0-9]{1,6}`"
    r"|[A-Za-z]:[/\\][^\s，。；、|]{2,}"
    r"|`[^`\n]*[/\\][^`\n]*`"
)
_RESULT_CLAIM = re.compile(
    r"(?:共|合计|总计|共计|一共有|去重后|统计结果|不重复)\s*[:：]?\s*\d+"
    r"|分别是\s*[:：]"
    r"|(?:共|合计|总计)\s*[一二三四五六七八九十百零两]+\s*[个人名条]"
    r"|(?:第一行|第[一二三四五六七八九十\d]+行|表头|字段|内容)\s*(?:是|为)\s*[:：]?\s*\S"
)


def is_conversational_prose(text: str) -> str | None:
    """功能：判定候选是否只是助手的话术而非事实陈述，返回拒绝理由或 None。

    助手结论回落进长期记忆时（用户只给指令、事实由助手查出），必须挡住下面
    几类「读起来像内容、实际不是事实」的输出。L1 真机实测 72 条曾把 19 条
    助手原话整段入库——拒绝话术、目录清单、执行计划、能力清单、故障说明。

    这些不是「价值低」，是**根本不是事实**：入库后会在后续召回里冒充用户的
    偏好与历史事实。
    """
    t = str(text or "").strip()
    if not t:
        return "empty"

    # ① 权限/模式拒绝——陈述的是「做不到」，不是「是什么」
    for marker in (
        "计划模式",
        "只允许只读",
        "无法写入",
        "无法直接",
        "暂不支持",
        "不支持该",
        "没有权限",
        "权限不足",
    ):
        if marker in t:
            return f"permission_refusal:{marker}"

    # ② 上游/环境故障说明——描述的是一次调用失败，不是持久事实
    for marker in (
        "暂时不可用",
        "无法获取",
        "无法访问",
        "沙箱限制",
        "调用失败",
        "请求失败",
    ):
        if marker in t:
            return f"service_failure:{marker}"

    # ③ 目录/文件清单——枚举的是当下状态，下次就变
    for marker in (
        "目录下只有",
        "当前存在的",
        "目录当前只有",
        "文件情况如下",
        "根目录（",
        "主要内容",
        "主要文件夹",
        "帮您整理了一下",
        "都包含了什么",
        "有哪些字段",
    ):
        if marker in t:
            return f"inventory:{marker}"

    # ④ 执行计划/方案——尚未执行，是打算不是事实
    for marker in ("计划如下", "执行步骤", "重构方案", "步骤列表", "## 计划", "修复计划"):
        if marker in t:
            return f"plan:{marker}"

    # ⑤ 能力自我介绍——BC-004 已拦部分问法，这里兜住 markdown 分档式复述
    for marker in ("我能帮你做什么", "我能做的", "我可以帮你做", "目前我的能力"):
        if marker in t:
            return f"capability_listing:{marker}"

    # ⑥ 任务应答正文——引用了工具产物路径 **且** 给出计数/名单结论。
    # 这类文本"有具体事实"故前五类都拦不住，但它是**一次查询的结果快照**：
    # staff.csv 一换内容，"去重后共 5 人"立刻过时，却已固化进记忆库被反复召回。
    # 更糟的是把当时的幻觉一并存下（实测存过"陈敏"，真实是"陈静"）。
    # 需两个信号同时出现才拒——单独出现任一个仍可能是合法事实
    # （「老板偏好 fixtures/ 下的数据」含路径但无结论）。
    has_artifact_ref = bool(_ARTIFACT_REF.search(t))
    has_result_claim = bool(_RESULT_CLAIM.search(t))
    if has_artifact_ref and has_result_claim:
        return "task_answer_snapshot"

    return None


# 功能：失败回合的回答特征——这些文本是链路错误/兜底文案，不是可复用的事实。
_FAILED_ANSWER_PATTERNS = re.compile(
    r"(?:all LLM candidates failed"
    r"|\bLLMUnavailableError\b"
    r"|Stream connection failed"
    r"|Provider request failed"
    r"|流中断"
    r"|会话已中断"
    r"|本轮回复未生成)",
    re.IGNORECASE,
)

# 功能：审批等待轮的典型文案——过程噪音，不是可复用的经历或事实。
_APPROVAL_WAIT_PATTERNS = re.compile(
    r"(?:等待[^。；\n]{0,24}审批"
    r"|需[^。；\n]{0,12}审批"
    r"|待[^。；\n]{0,12}审批"
    r"|审批[^。；\n]{0,12}工具"
    r"|工具审批"
    r"|人工审批"
    r"|等待人工决策"
    r"|等待人工确认"
    r"|requires?\s+user\s+approval"
    r"|approval\s+required"
    r"|waiting\s+for\s+approval"
    r"|pending\s+approval)",
    re.IGNORECASE,
)


def is_approval_wait_text(text: str) -> bool:
    """功能：文本是否为「等待工具审批/人工决策」的过程噪音（不构成可记忆经历）。"""
    return bool(_APPROVAL_WAIT_PATTERNS.search(text or ""))


def _looks_failed_answer(answer: str) -> bool:
    """功能：判断回答侧是否是失败/中断文案（详见负面清单说明）。"""
    return bool(_FAILED_ANSWER_PATTERNS.search(answer or ""))


def _episode_answer_side(text: str) -> str:
    """功能：取情节摘要的回答侧（无回答分隔符时返回全文）。"""
    body = str(text or "").strip()
    if "→ 回答:" not in body:
        return body
    return body.rsplit("→ 回答:", 1)[-1].strip()


def _episode_user_side(text: str) -> str:
    """功能：取情节摘要的用户侧（无回答分隔符时返回全文）。"""
    body = str(text or "").strip()
    if "→ 回答:" not in body:
        return body
    return body.split("→ 回答:", 1)[0].strip()


def _is_greeting_only(text: str) -> bool:
    """功能：判断输入是否只是问候/探针/要求回 OK（含用户/任务前缀、尾括号与首尾标点）。"""
    value = str(text or "").strip().strip(_MARKS).strip()
    value = re.sub(r"^(?:用户|任务|老板)[:：]\s*", "", value)
    value = re.sub(r"[（(][^）)]*[）)]\s*$", "", value).strip(_MARKS).strip()
    return bool(_USER_GREETING_RE.match(value) or _TRIVIAL_PROBE_RE.match(value))


def is_noise_memory(text: str) -> str | None:
    """功能：统一的「非记忆」噪声判定——审批等待/失败文案/回执/寒暄/自我介绍/可推导。

    返回拒绝理由；None 表示可进入晋升/长期记忆。供情节写入、巩固与维护晋升共用，
    防止无人值守的 episodic_maintenance 绕过轮末价值门。
    """
    body = str(text or "").strip()
    if not body:
        return "empty"
    # 用户显式说「记住/我的偏好/以后」时，短确认回复也是用户要求的内容。
    if has_explicit_remember(body):
        return None
    if _TEST_INSTRUCTION_RE.search(body):
        return "test_instruction"
    if "→ 回答:" in body and _is_greeting_only(_episode_user_side(body)):
        return "greeting"
    # 整串就是问候/探针（无对话包装时）——同样不构成记忆
    if _is_greeting_only(body):
        return "greeting"
    # 执行汇报（已生成/已补上 + 产物路径或功能清单）是收尾话术，不是可复用事实
    if is_execution_report(body):
        return "execution_report"
    if is_approval_wait_text(body):
        return "approval_wait"
    answer = _episode_answer_side(body)
    for candidate in {body, answer}:
        if _looks_failed_answer(candidate):
            return "failed_turn"
        low_value = _low_value_reason(candidate)
        if low_value is not None:
            return low_value
    if is_self_intro_reply(answer):
        return "self_intro"
    if _DERIVABLE_PATTERNS.search(answer):
        return "derivable"
    prose = is_conversational_prose(answer)
    if prose is not None:
        return prose
    return None


def review_ingest(
    candidate: str, *, memory_type: str, source: str = "agent_turn", user_input: str = ""
) -> IngestVerdict:
    """功能：三条件分档判断候选记忆是否值得入库（≥2 accept / 1 reject / 0 reject）。

    条件：长期价值（价值词/问题信号或 ≥60 字非寒暄）、可结构化（实体/类型/时间/进度）
    影响个性化（偏好/程序性/过程结论）。

    显式触达（`user_input` 或候选文本含「记住/我的偏好/以后都…」）→ 直接 accept。
    `source` 为调用方上下文保留参数。
    """
    text = str(candidate or "")
    # 显式触达不受话术闸门约束——用户说「记住」时他就是要记这段话
    explicit = any(word in f"{user_input}\n{text}" for word in _EXPLICIT_REMEMBER_WORDS)
    if not explicit:
        # 统一噪声闸门：执行汇报/失败文案/审批等待/可推导/寒暄——与情节层同一套判据
        noise = is_noise_memory(text)
        if noise is not None:
            return IngestVerdict("reject", f"noise:{noise}", 0.0)
    if not explicit and _is_greeting_only(user_input):
        return IngestVerdict("reject", "greeting", 0.0)
    if not explicit:
        prose_reason = is_conversational_prose(text)
        if prose_reason is not None:
            return IngestVerdict("reject", prose_reason, 0.0)
    low_value = _low_value_reason(text)
    if low_value is not None and not explicit:
        return IngestVerdict("reject", low_value, 0.0)
    fields = extract_memory_fields(text)
    lowered = text.lower()
    long_term_value = (
        any(word in text for word in _VALUE_WORDS)
        or any(word in lowered for word in _ISSUE_WORDS)
        or len(text.strip()) >= _LONG_TEXT_CHARS
    )
    structurable = (
        bool(fields.entities)
        or fields.temporal is not None
        or fields.progress is not None
        or str(memory_type) != "business_fact"
    )
    personalized = str(memory_type) in _PERSONAL_TYPES
    hits = (long_term_value, structurable, personalized)
    count = sum(hits)
    score = round(sum(weight for weight, hit in zip(_WEIGHTS, hits) if hit), 3)
    reason = "+".join(name for name, hit in zip(_CONDITION_NAMES, hits) if hit) or "no_signal"
    # 失败回合的回复是错误串/兜底文案——早于任何打分规则拒掉，否则 MODEL_ERROR 的
    # 报错全文会被当成高价值事实写进长期记忆（agent_runner 的 turn: 直写路径）。
    if is_approval_wait_text(text):
        return IngestVerdict("reject", "approval_wait", 0.0)
    if _looks_failed_answer(text):
        return IngestVerdict("reject", "failed_turn", 0.0)
    # 触点触发：用户明确说「记住/我的偏好是/以后都…」→ 直接入库（不管条件数）。
    # 用户输入侧也要查——「记住：…」这句话在用户消息里，候选文本（助手回复）通常不含该词。
    explicit_source = f"{user_input}\n{text}"
    if any(word in explicit_source for word in _EXPLICIT_REMEMBER_WORDS):
        return IngestVerdict("accept", f"explicit_remember+{reason}", max(score, 0.9))
    if count >= 2:
        return IngestVerdict("accept", reason, score)
    # 1 条件不再提请审批（历史行为：stage 让每轮普通对话都进审批中心——用户明确反对
    # 「不可能每次都直接提名」）。低置信内容直接丢弃；审批只保留给显式触达 /
    # 无人值守背景写入（MemoryWriteGate）/ 冲突裁决（check_conflict）三类真触点。
    if count == 1:
        return IngestVerdict("reject", f"low_confidence({reason})", score)
    return IngestVerdict("reject", "no_signal", score)


def review_episodic_ingest(candidate: str) -> IngestVerdict:
    """功能：经历层入库判断——拒「不该存的内容」+ 空/寒暄，业务句才记录。

    移植 cc-haha `WHAT_NOT_TO_SAVE_SECTION`（`src/memdir/memoryTypes.ts:183-195`）：
    那一版的负面清单是唯一**显式覆盖用户显式指令**的记忆门槛——用户说「保存这个」
    也不能把可从代码/git 推导的内容写成记忆，只留真正反直觉的部分。本移植把同一
    思路落到经历层，适配我们的四类噪音：

    1. 助手自我介绍/能力清单——可由提示词推导，不是经历事实
    2. 代码模式/架构/文件路径——读代码即可推导
    3. git 历史/谁改了什么——`git log` / `git blame` 是权威
    4. 调试配方/修复步骤——修复已在代码里，提交信息里有上下文
    5. 瞬时任务细节——当前对话上下文本身

    第 1 类同时堵住 recall_count 虚高：这类情节会被相似问句反复召回，把自己顶成
    高价值候选（实测自我介绍 recall_count 达8），进而生成噪音晋升审批。
    """
    text = str(candidate or "")
    body = text.strip()
    condensed = body.strip(_MARKS)
    if not condensed:
        return IngestVerdict("reject", "empty", 0.0)
    if len(condensed) <= 1:
        return IngestVerdict("reject", "single_char", 0.0)
    noise = is_noise_memory(body)
    if noise is not None:
        return IngestVerdict("reject", noise, 0.0)
    lowered = body.lower()
    if (
        len(condensed) <= _PLEASANTRY_MAX_CHARS
        and any(w in lowered for w in _PLEASANTRY_WORDS)
        and not _has_business_signal(body)
    ):
        return IngestVerdict("reject", "pleasantry", 0.0)
    # 负面清单：只看「回答侧」——用户问题里的代码路径/寒暄不代表情节无价值
    # （如「帮我看下 src/a.py 的报错」是真实排查经历）
    answer = body.rsplit("→ 回答:", 1)[-1] if "→ 回答:" in body else body
    if is_self_intro_reply(answer):
        return IngestVerdict("reject", "self_intro", 0.0)
    if _DERIVABLE_PATTERNS.search(answer):
        return IngestVerdict("reject", "derivable", 0.0)
    # 失败回合的「回答」是错误串——不可作为可复用经历（终态白名单是第一道防线，
    # 此处是防终态漏判的第二道：LLM 兜底文案/链路异常文案同样不该进长期召回）
    if is_approval_wait_text(answer):
        return IngestVerdict("reject", "approval_wait", 0.0)
    if _looks_failed_answer(answer):
        return IngestVerdict("reject", "failed_turn", 0.0)
    return IngestVerdict("accept", "episodic_record", 1.0)
