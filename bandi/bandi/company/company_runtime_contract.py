from __future__ import annotations

from typing import Any, Literal

from bandi.company.turn_mode import TurnMode, infer_turn_mode

ContractAudience = Literal["native", "external"]


class _WorkItemLike:
    """将 dict 包装为 infer_turn_mode 所需的 WorkItemLike 协议对象。"""

    __slots__ = ("_d",)

    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    @property
    def metadata(self) -> dict[str, Any]:
        return self._d.get("metadata") or {}

    @property
    def turn_type(self) -> str:
        return self._d.get("turn_type") or "execute"

    @property
    def phase(self) -> str | None:
        return self._d.get("phase")


_COMPANY_WORK_ITEM_GUIDELINES = (
    "## Company Work Item Contract\n"
    "- You are executing one projected work item inside company mode, not the whole project alone.\n"
    "- Stay inside the current work-item boundary and use upstream handoffs, annotations, "
    "and inbox context before re-solving prior work.\n"
    "- Prefer asynchronous collaboration tools for cross-role clarification. "
    "Use meetings only for genuine cross-role decisions or conflicts.\n"
    "- Keep downstream handoffs crisp: preserve decisions, risks, open questions, "
    "artifacts, and verification status when they matter.\n"
    "- Team collaboration MUST flow through the collaboration tools and inbox context. "
    "Plain assistant prose does not count as inter-member coordination.\n"
    "- External executors such as Codex / OpenCode / Claude Code are temporary workers "
    "you may call; they are not organization members and do not replace your role ownership.\n"
    "- Respect the work-item ownership and artifact contracts in task metadata. "
    "If you cannot satisfy them, surface that explicitly instead of silently widening scope.\n"
    "- 自称只使用当前角色的中文岗位名：禁止输出 role_id、英文译名或「名字 + id」的组合。"
)

_CHAT_WORK_ITEM_GUIDELINES = (
    "## Company Chat Turn: Conversation\n"
    "- 本轮是问候/闲聊/简单确认，不是任务派发，也不是执行汇报。\n"
    "- 直接自然回应用户，保持简短；不要输出「任务 #N 已完成」「交付文件」「关键结论」"
    "「链路推进」「成员任务」「产物清单」等任务模板。\n"
    "- 除非用户明确要求执行动作，否则不要调用工具，不要创建或派发工作项。\n"
    "- 自称只使用角色中文名，禁止输出 role_id、英文译名或「名字 + id」的组合。"
)

_MULTI_TEAM_ORG_GUIDELINES = (
    "## Organization Runtime Contract\n"
    "- You are one seat inside company mode; work inside your assigned WorkItem.\n"
    "- The runtime has prepared your assignment, mailbox snapshot, board context, "
    "and workspace roots for this turn.\n"
    "- The WorkItem is the collaboration source of truth. Use WorkItem IDs for "
    "collaboration; never use runtime Task IDs as WorkItem IDs.\n"
    "- `workspace_root` and `comms_workspace_root` are guaranteed. "
    "`output_root` may be blank; if needed, choose a suitable subfolder "
    "under `workspace_root` and communicate it in handoffs or delegation briefs.\n"
    "- Kanban state is advanced by the runtime from completion reports and review verdicts. "
    "Do not manually flip board states.\n"
    "- There is no need to poll work items for progress: the runtime watches state "
    "changes and re-activates whichever roles need to act once delegated or dependent "
    "work completes. So after you have confirmed that your delegation succeeded or "
    "that your review verdict advanced the work, end your current run if nothing else "
    "requires your own work — the system monitors on your behalf.\n"
    "- Use mailbox tools only for coordination, questions, blockers, or handoffs.\n"
    "- Cross-team collaboration is request-based; only direct managers delegate executable work.\n"
    "- If you are the root final decider, only your finished turn is the authoritative "
    "owner-facing result."
)

_MANAGER_RUNTIME_CONTRACT = (
    "## Manager Runtime Contract\n"
    "- You have direct reports or an allowed delegation surface; "
    "prefer delegation and monitoring before local execution.\n"
    "- Use `spawn_thread_task` only to CREATE child WorkItems for direct reports.\n"
    "- 串行链路：多卡按执行顺序一次派发时传 `tasks` + `sequence: true`——后一张卡"
    "等前一张完成才放行；并行任务不传 sequence。**默认必须串行**（除非用户明确要求并行）。\n"
    "- 派发覆盖：按 `allowed_delegate_role_ids` 把有任务的下游角色全部写进同一次"
    "`tasks` 调用（按执行顺序），不得漏派、不得让下游再互相派（链条由顶层一次成型）。\n"
    "- 提问顺序（硬性）：本卡需要用户确认口径时，必须在本轮最开始调用"
    "`request_user_input` 并等待答复；用户答复前不得产出最终计划、不得派发子卡、"
    "不得按默认锚点落定。\n"
    "- 派发简报边界（硬性）：每张子卡只写该成员自己的交付物与验收标准，"
    "禁止把整条链或多个成员的交付物写进同一张子卡的 task_prompt；"
    "上游结论通过依赖放行自动进入下游上下文，不需要越界代做。\n"
    "- 交付规范写入每张子卡：统一证据口径（来源/日期/关键假设）、"
    "文件命名、输出目录，保证全员一致。\n"
    "- Use `modify_work_item` to revise an existing child WorkItem when the owner "
    "is still right but the brief, deliverables, acceptance criteria, or dependencies changed.\n"
    "- Use `delete_work_item` to cancel/hide an obsolete or wrong child WorkItem "
    "so it no longer blocks the parent board.\n"
    "- Use `manager_board_read` only to READ child-board state. For your current board, "
    "omit `parent_work_item_id` or use the current WorkItem ID; never use the runtime Task ID.\n"
    "- Do not send executable orders outside your direct reports; coordinate with peers "
    "or other teams by request."
)

_PLAN_WORK_ITEM_GUIDELINES = (
    "## Company Work Item Turn: Plan / Discussion\n"
    "- 本轮是方案讨论轮：调研与澄清优先——先理解用户真实诉求、补齐关键口径，"
    "与用户把方案讨论清楚；用户确认前不产出最终交付物、不套用执行分工格式。\n"
    "- 参数槽位缺失时（如任务模板里的【目标市场】【品类】仍为占位符）：先用多问卡"
    "（request_user_input，带选项）向用户澄清关键口径；仅当用户明确表示「按默认/"
    "反推路径走」时才采用默认锚点——不得静默猜测。\n"
    "- 直接回应用户当前问题：该给结论就给结论（可列表/对比表按内容需要），"
    "该追问就追问；把回复写进正文，不能只放在工具调用里。\n"
    "- 不要输出 Timeline/时间线区块；不产出「分工与执行链」——执行分工在用户"
    "确认方案后的派发轮产出。\n"
    "- Prefer read-only exploration when the codebase slice is broad.\n"
    "- Avoid implementation-level edits unless the task explicitly assigns execution "
    "to this work item.\n"
    "- During intake or initial dispatch, do NOT use `ask_peer_and_wait` on a role "
    "that does not already have an active work package. Create or delegate the work "
    "first, then use `send_dm` for non-blocking coordination.\n"
    "- If you need another role's view before delegation exists, send a non-blocking "
    "message or include the question in the delegated brief; do not park the whole "
    "project startup."
)

_DISPATCH_WORK_ITEM_GUIDELINES = (
    "## Company Work Item Turn: Dispatch\n"
    "- 用户已确认口径与方案：本轮产出可执行的计划并派发；不再重复询问已确认的口径。\n"
    "- Investigate first, then convert findings into a concrete work-item plan "
    "with sequencing, assumptions, and validation targets.\n"
    "- 计划交付格式（分级，中文，按任务性质选择）：\n"
    "  · 多步骤 / 多角色协作 / 需要派发的任务 → 正文必须包含 `## 分工与执行链` 小节，"
    "用 Markdown 表格呈现（表头固定 `| 顺序 | 交付物 | 负责人 | 状态 |`，按执行顺序逐行）；"
    "并包含 `## 锁定目标与交付物` 小节（逐条分点：已锁定口径 + 最终交付物清单）。\n"
    "  · 单一询问 / 研究型任务（如「找 3–5 款同类商品比较售价规格卖点」）→ 直接给出"
    "研究结论或答案本身（可列表/对比表按内容需要），**不要**套用「分工与执行链」与"
    "「锁定目标与交付物」小节。\n"
    "  · 调整既有计划 → 更新执行链表格与锁定项（保持既有格式），并说明改了什么。\n"
    "- 不要输出 Timeline/时间线区块；把计划写进回复正文，不能只放在工具调用里。\n"
    "- 派发子任务时：在正文用「成员任务」块说明每个被派发成员的指派对象、任务编号与"
    "完整任务说明（一句话标题 + 可展开细节）——串行链路写下一个执行成员，"
    "并行派发逐个成员各写一段（并行任务同样需要成员卡）。\n"
    "- 「成员任务」块一律用 Markdown 纯文本（标题行 + 分点列表），**禁止使用 HTML 标签**"
    "（如 `<details>`/`<summary>`/`<b>`——会被原样显示）。\n"
    "- Prefer read-only exploration when the codebase slice is broad.\n"
    "- Avoid implementation-level edits unless the task explicitly assigns execution "
    "to this work item.\n"
    "- During intake or initial dispatch, do NOT use `ask_peer_and_wait` on a role "
    "that does not already have an active work package. Create or delegate the work "
    "first, then use `send_dm` for non-blocking coordination.\n"
    "- If you need another role's view before delegation exists, send a non-blocking "
    "message or include the question in the delegated brief; do not park the whole "
    "project startup."
)

_EXECUTE_WORK_ITEM_GUIDELINES = (
    "## Company Work Item Turn: Execute\n"
    "- Prefer direct execution for the assigned slice once the approach is clear.\n"
    "- 完成汇报格式（面向老板，回复卡可直接对表）：「任务 #N 已完成」"
    "首行 → 「交付文件：<相对路径>」→ 关键结论（≤3 条，带数据）→ 链路推进（下一环解锁/"
    "派发情况）→ 产物清单。派发下游时在正文「成员任务」块写清下一环负责人与任务简报"
    "（标题 + 目标 + 输入/上游文件 + 输出文件路径 + 验收标准）。\n"
    "- 接力必须真的写出 `@角色名`：下一环成员只凭正文里的 @ 才会收到交接卡，"
    "只写「已派发给 X」「交给 X 处理」不会产生交接，链路会在你这里断掉。\n"
    "- Use read-only exploration for context reduction when it reduces noise.\n"
    "- If work-item swarm tools are available, use the shared microtask board to "
    "break the assigned slice into tactical work items before spawning burst workers.\n"
    "- Before handing off, leave evidence that a reviewer can verify quickly: "
    "changed areas, artifact pointers, and any remaining risks.\n"
    "- Treat write scope as constrained by the ownership contract. Do not edit "
    "outside that scope unless the task metadata explicitly expands it.\n"
    '- Your completion bar is higher than "it works on my turn": leave a handoff '
    "that satisfies summary, artifact index, decisions, risks, open questions, "
    "and verification status."
)

_REVIEW_WORK_ITEM_GUIDELINES = (
    "## Company Work Item Turn: Review\n\n"
    "- 评审轮硬性约束：**不要调用 request_user_input / 向用户提问**；直接读取产出、"
    "自行判定并输出 verdict（无经理意见时按质量通过或驳回）。\n"
    "You are reviewing a subordinate's deliverable. The runtime applies your verdict "
    "mechanically \u2014 approve sends the work to done, reject sends it back to the worker "
    "with your summary + blocking_issues as rework feedback. The runtime does NOT "
    "second-guess the shape or content of your verdict; you are responsible for the call.\n\n"
    "### How to judge\n"
    "- You have read access to the workspace. Use your tools (file_read, bash, git_*, "
    "web_search, etc.) to verify the worker's claims against the actual current state. "
    "Don't trust the handoff blindly and don't reject blindly either.\n"
    "- Current workspace evidence is the truth. Previous review notes and old memory "
    "are leads to re-check, not facts.\n"
    "- Reject only for gaps that still exist now. If an earlier finding was fixed, "
    "don't repeat it.\n"
    "- Compare the deliverable against the original brief and acceptance criteria in "
    "plain English: did the assignee produce the requested output, or did they only "
    "provide analysis/planning? If the brief asked for an artifact and the worker "
    "shipped a plan, reject.\n\n"
    "### Verdict (suggested JSON shape)\n"
    "End your turn with one JSON object on its own line:\n\n"
    '  Approve: `{"review_verdict":"approve","summary":"<concrete reason it meets the bar>"}`\n'
    '  Reject:  `{"review_verdict":"reject","summary":"<overall reason>","blocking_issues":["<specific change needed>"],"followups":["<non-blocking improvement>"]}`\n\n'
    "If you cannot be parsed into approve or reject, the runtime will spawn one more "
    "review attempt and ask you again; after that it will escalate to a human. So "
    "please emit a clear label.\n\n"
    "You are trusted to choose the level of detail. A short summary on a clear approve "
    "is fine. For rejections, name specific files / tests / artifacts in `blocking_issues` "
    "so the worker can act."
)

_AGGREGATE_WORK_ITEM_GUIDELINES = (
    "## Company Work Item Turn: Aggregate / Deliver\n"
    "- Synthesize upstream outputs into a decision-ready summary instead of repeating "
    "every intermediate detail.\n"
    "- Preserve artifact pointers, unresolved risks, and owner-facing next actions.\n"
    "- Keep the final surface area small enough that the next work item can act without "
    "replaying the whole run.\n"
    "- Aggregation does not erase accountability: preserve which role produced which "
    "artifact or review conclusion when it matters for follow-up."
)

_COMPANY_REPORT_GENERATION_HEADER = (
    "## Work Item Turn: Report Generation\n\n"
    "You have just finished executing the assigned work. This turn is dedicated to "
    "writing a self-contained handoff report for your reviewer. Do NOT do any new "
    "execution work in this turn \u2014 your execution is already done. Do NOT delegate, "
    "do not message peers, do not run the original task again.\n\n"
    "Use your own session context plus the runtime-injected execute-turn summary/evidence "
    "below. If your session memory is unavailable but the injected execute-turn summary, "
    "output, artifacts, or verification evidence are present, use those injected facts "
    "as the handoff source.\n\n"
    "### Suggested report shape (JSON, not strictly required)\n"
    "End your turn with EXACTLY one JSON object on its own line:\n"
    "```\n"
    "{\n"
    '  "summary": "<2-3 sentence overall outcome>",\n'
    '  "deliverables": [\n'
    '    {"name": "<deliverable name>", "path": "<path or pointer>", "status": "complete" | "partial" | "blocked"}\n'
    "  ],\n"
    '  "acceptance_status": [\n'
    '    {"criterion": "<original acceptance criterion>", "met": true | false, "evidence": "<file path / command / proof>"}\n'
    "  ],\n"
    '  "risks": ["<known risk or caveat>"],\n'
    '  "next_actions": ["<what reviewer or downstream should do next>"]\n'
    "}\n"
    "```\n\n"
    "If a structured shape doesn't fit your situation, write a clear narrative report "
    "instead \u2014 the runtime will pass your prose to the reviewer as-is. Do NOT make up "
    "content to fill the schema; leave fields out or fall back to narrative.\n\n"
    "### Why this matters\n"
    "The reviewer will receive this report PLUS the original brief and will independently "
    "verify your claims with their own tools (file_read, bash, etc.). Be honest about "
    "partial work and open issues \u2014 silent gaps will be caught by the reviewer and "
    "counted against this delivery."
)

_REVIEW_PENDING_HEADER = (
    "## Review Requirement\n"
    "- One or more of your direct reports has submitted a completed work item for your "
    "review. You MUST clear the review queue before dispatching new children, monitoring, "
    "or executing local work.\n"
    "- For each pending item: compare the deliverable against the original acceptance "
    "criteria and non_overlap_guard. Inspect artifacts, completion reports, and any "
    "cross-team coordination notes.\n"
    "- Also compare the result against the original work item brief in plain English: "
    "did the assignee produce the requested output, or did they only provide analysis/"
    "planning/search notes? If the brief asked for actual production, reject planning-"
    "only submissions and request concrete rework.\n"
    "- Treat current files and command results as the truth. Previous review findings "
    "are only leads; verify them against the latest workspace before repeating them.\n\n"
    "### Verdict (suggested JSON shape, one per item)\n"
    '  Approve: `{"review_verdict":"approve","summary":"<concrete reason>"}`\n'
    '  Reject:  `{"review_verdict":"reject","summary":"<reason>","blocking_issues":["<specific fix>"],"followups":["<nice-to-have>"]}`\n\n'
    "The runtime applies the verdict mechanically. For rejections, name specific files / "
    "tests / artifacts in `blocking_issues` so the worker can act on your feedback.\n\n"
    "- If a review depends on information you lack (e.g., evidence from another team), "
    "send a targeted `send_dm` or `ask_peer_and_wait` message rather than approving "
    "blindly.\n"
    "- Do NOT approve simply to unblock the pipeline; reject with specific, actionable "
    "feedback if acceptance criteria are not met."
)

_REVIEW_EXECUTE_HEADER = (
    "## Kanban Review Turn\n\n"
    "This turn is a dedicated review of one child work item. Do not dispatch new work, "
    "do not rewrite scope, and do not message peers unless your review depends on "
    "information you cannot get yourself.\n\n"
    "### Inputs you have\n"
    "- The original brief (target description) below.\n"
    "- The worker's handoff report below.\n"
    "- Your own session memory of any prior review on this item.\n"
    "- Read access to the workspace via your tools (file_read, bash, git_*, web_search, etc.).\n\n"
    "### How to judge\n"
    "- Use your tools to verify the worker's claims directly against the workspace. "
    "Don't trust the handoff blindly and don't reject blindly either. Current workspace "
    "evidence is the truth.\n"
    "- Treat the original brief as the contract. Approve only if the submitted result "
    "actually satisfies the requested production output; if the worker shipped only a "
    "plan or concept memo when the brief required an artifact or implementation, reject "
    "and request rework.\n"
    "- Before rejecting, re-check the cited paths from the latest report; do not reuse "
    "stale line numbers or already-fixed findings.\n\n"
    "### Verdict (suggested JSON shape)\n"
    "End your turn with one JSON object on its own line:\n\n"
    '  Approve: `{"review_verdict":"approve","summary":"<concrete reason it meets the bar>"}`\n'
    '  Reject:  `{"review_verdict":"reject","summary":"<overall reason>","blocking_issues":["<specific change needed>"],"followups":["<non-blocking improvement>"]}`\n\n'
    "The runtime applies your verdict mechanically \u2014 approve moves the child to done, "
    "reject sends it back to the worker with your summary + blocking_issues as rework "
    "feedback. The runtime does NOT second-guess the shape or content of your verdict. "
    "You are responsible for the call.\n\n"
    "If your output cannot be parsed into approve or reject, the runtime will give you "
    "one more review attempt with a parse-failure hint, and after that it will escalate "
    "to a human reviewer. So please emit a clear label."
)

_EXTERNAL_TOOL_WORDING_REPLACEMENTS: list[tuple[str, str]] = [
    (
        "Prefer read-only `agent_spawn(profile='explore')` exploration when the codebase slice is broad.",
        "Prefer read-only exploration using your external agent's own search, inspection, "
        "or context-isolation capabilities when the workspace slice is broad.",
    ),
    (
        "Use `agent_spawn(profile='explore')` for read-only exploration when it reduces context noise.",
        "Use your external agent's own search, inspection, or context-isolation capabilities "
        "for read-only exploration when it reduces context noise.",
    ),
    (
        "You have read access to the workspace. Use your tools (file_read, bash, git_*, web_search, etc.) to verify the worker's claims against the actual current state. Don't trust the handoff blindly and don't reject blindly either.",
        "You have read access to the workspace. Use your external agent's available "
        "inspection, search, shell, version-control, browser, and verification capabilities "
        "to verify the worker's claims against the actual current state. Don't trust the "
        "handoff blindly and don't reject blindly either.",
    ),
    (
        "The reviewer will receive this report PLUS the original brief and will independently verify your claims with their own tools (file_read, bash, etc.). Be honest about partial work and open issues \u2014 silent gaps will be caught by the reviewer and counted against this delivery.",
        "The reviewer will receive this report PLUS the original brief and will "
        "independently verify your claims with their own workspace inspection and "
        "verification capabilities. Be honest about partial work and open issues \u2014 "
        "silent gaps will be caught by the reviewer and counted against this delivery.",
    ),
    (
        "- Read access to the workspace via your tools (file_read, bash, git_*, web_search, etc.).",
        "- Read access to the workspace via your external agent's available inspection, "
        "search, shell, version-control, browser, and verification capabilities.",
    ),
]


def _format_pending_review_items(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        work_item_id = str(entry.get("work_item_id") or "").strip()
        if not work_item_id:
            continue
        title = str(entry.get("title") or "").strip() or "(untitled)"
        role_id = str(entry.get("role_id") or "").strip() or "unknown"
        phase = str(entry.get("phase") or "").strip() or "awaiting_manager_review"
        deliverable_summary = str(entry.get("deliverable_summary") or "").strip()
        line = f"- work_item_id=`{work_item_id}` role=`{role_id}` title=`{title}` phase=`{phase}`"
        if deliverable_summary:
            preview = deliverable_summary.replace("\n", " ")[:200]
            line += f" deliverable_preview=`{preview}`"
        lines.append(line)
    return "\n".join(lines)


def _review_pending_block(work_item: dict[str, Any]) -> str:
    metadata = work_item.get("metadata") or {}
    runtime_model = str(metadata.get("runtime_model") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    pending_items = list(metadata.get("pending_review_items") or [])
    rendered = _format_pending_review_items(pending_items)
    if not rendered:
        return _REVIEW_PENDING_HEADER
    return f"{_REVIEW_PENDING_HEADER}\n\n### Pending Review Queue\n{rendered}"


def _review_execute_block(work_item: dict[str, Any]) -> str:
    metadata = work_item.get("metadata") or {}
    runtime_model = str(metadata.get("runtime_model") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    if infer_turn_mode(_WorkItemLike(work_item)) not in {TurnMode.REVIEW, TurnMode.REWORK}:
        return ""
    explicit_review = bool(
        metadata.get("review_execution_work_item")
        or metadata.get("review_task")
        or str(metadata.get("review_target_work_item_id") or "").strip()
    )
    if not explicit_review:
        return ""
    target_id = str(metadata.get("review_target_work_item_id") or "").strip()
    title = str(metadata.get("review_target_title") or "").strip()
    worker_role = str(metadata.get("review_target_worker_role_id") or "").strip()
    completion_report = str(metadata.get("review_completion_report") or "").strip()
    review_evidence = dict(metadata.get("review_evidence") or {})
    prompt_contract = dict(metadata.get("prompt_contract") or {})
    target_contract = dict(
        metadata.get("review_target_prompt_contract")
        or prompt_contract.get("target_contract")
        or {}
    )
    lines = [
        _REVIEW_EXECUTE_HEADER,
        "",
        "### Target Child Work Item",
        f"- work_item_id=`{target_id}`",
    ]
    if title:
        lines.append(f"- title=`{title}`")
    if worker_role:
        lines.append(f"- worker_role=`{worker_role}`")
    if target_contract:
        lines.append("")
        lines.append("### Target Contract")
        lines.append(str(target_contract))
    if completion_report:
        lines.append("")
        lines.append("### Completion Report")
        lines.append(completion_report[:2000])
    if bool(metadata.get("review_parse_failure_hint")):
        lines.extend(
            [
                "",
                "### Parse Failure Hint",
                "Your previous output could not be parsed into an approve/reject verdict. "
                "End this turn with exactly one JSON object on its own line, e.g. "
                '`{"review_verdict":"approve","summary":"..."}` or '
                '`{"review_verdict":"reject","summary":"...","blocking_issues":["..."]}`.',
            ]
        )
    artifact_manifest = [
        dict(item)
        for item in list(review_evidence.get("artifact_manifest") or [])
        if isinstance(item, dict)
    ]
    changed_areas = [
        str(item).strip()
        for item in list(review_evidence.get("changed_areas") or [])
        if str(item).strip()
    ]
    verification_results = dict(review_evidence.get("verification_results") or {})
    verification_status = dict(verification_results.get("status") or {})
    verification_checks = [
        dict(item)
        for item in list(verification_results.get("checks") or [])
        if isinstance(item, dict)
    ]
    key_commands = [
        str(item).strip()
        for item in list(review_evidence.get("key_commands") or [])
        if str(item).strip()
    ]
    output_paths = [
        str(item).strip()
        for item in list(review_evidence.get("output_paths") or [])
        if str(item).strip()
    ]
    open_risks = [
        str(item).strip()
        for item in list(review_evidence.get("open_risks") or [])
        if str(item).strip()
    ]
    if artifact_manifest:
        lines.append("")
        lines.append("### Artifact Manifest")
        for item in artifact_manifest[:10]:
            label = str(item.get("label") or item.get("kind") or "artifact").strip()
            value = str(item.get("value") or "").strip()
            lines.append(f"- {label}: `{value}`" if value else f"- {label}")
    if changed_areas:
        lines.append("")
        lines.append("### Changed Areas")
        lines.extend(f"- `{item}`" for item in changed_areas[:10])
    if verification_status or verification_checks:
        lines.append("")
        lines.append("### Verification")
        if verification_status:
            label = str(verification_status.get("label") or "").strip()
            summary = str(verification_status.get("summary") or "").strip()
            if label:
                lines.append(f"- status=`{label}`")
            if summary:
                lines.append(f"- summary=`{summary[:300]}`")
        for item in verification_checks[:6]:
            command = str(item.get("command") or "").strip()
            status = str(item.get("status") or "").strip()
            vsummary = str(item.get("summary") or "").strip()[:220]
            rendered = f"- `{command}` -> `{status}`" if command else f"- `{status}`"
            if vsummary:
                rendered += f" :: {vsummary}"
            lines.append(rendered)
    if key_commands:
        lines.append("")
        lines.append("### Key Commands")
        lines.extend(f"- `{item}`" for item in key_commands[:8])
    if output_paths:
        lines.append("")
        lines.append("### Output Paths")
        lines.extend(f"- `{item}`" for item in output_paths[:10])
    if open_risks:
        lines.append("")
        lines.append("### Open Risks")
        lines.extend(f"- {item}" for item in open_risks[:8])
    return "\n".join(lines)


def _render_report_source_evidence(work_item: dict[str, Any]) -> str:
    metadata = work_item.get("metadata") or {}
    evidence = dict(metadata.get("report_source_evidence") or {})
    summary = str(metadata.get("report_source_summary") or "").strip()
    result_content = str(metadata.get("report_source_result_content") or "").strip()
    lines: list[str] = []
    if summary:
        lines.append("### Last Execute-Turn Summary")
        lines.append(summary[:2000])
    if result_content and result_content != summary:
        lines.append("")
        lines.append("### Last Execute-Turn Output")
        lines.append(result_content[:2000])
    artifact_manifest = [
        dict(item)
        for item in list(evidence.get("artifact_manifest") or [])
        if isinstance(item, dict)
    ]
    changed_areas = [
        str(item).strip() for item in list(evidence.get("changed_areas") or []) if str(item).strip()
    ]
    verification_results = dict(evidence.get("verification_results") or {})
    verification_status = dict(verification_results.get("status") or {})
    verification_checks = [
        dict(item)
        for item in list(verification_results.get("checks") or [])
        if isinstance(item, dict)
    ]
    key_commands = [
        str(item).strip() for item in list(evidence.get("key_commands") or []) if str(item).strip()
    ]
    output_paths = [
        str(item).strip() for item in list(evidence.get("output_paths") or []) if str(item).strip()
    ]
    open_risks = [
        str(item).strip() for item in list(evidence.get("open_risks") or []) if str(item).strip()
    ]
    if artifact_manifest:
        lines.append("")
        lines.append("### Known Artifacts From Execute Turn")
        for item in artifact_manifest[:10]:
            label = str(item.get("label") or item.get("kind") or "artifact").strip()
            value = str(item.get("value") or "").strip()
            lines.append(f"- {label}: `{value}`" if value else f"- {label}")
    if changed_areas:
        lines.append("")
        lines.append("### Changed Areas")
        lines.extend(f"- `{item}`" for item in changed_areas[:10])
    if verification_status or verification_checks:
        lines.append("")
        lines.append("### Verification Evidence From Execute Turn")
        if verification_status:
            label = str(verification_status.get("label") or "").strip()
            vs = str(verification_status.get("summary") or "").strip()
            if label:
                lines.append(f"- status=`{label}`")
            if vs:
                lines.append(f"- summary=`{vs[:300]}`")
        for item in verification_checks[:6]:
            command = str(item.get("command") or "").strip()
            status = str(item.get("status") or "").strip()
            vsummary = str(item.get("summary") or "").strip()[:220]
            rendered = f"- `{command}` -> `{status}`" if command else f"- `{status}`"
            if vsummary:
                rendered += f" :: {vsummary}"
            lines.append(rendered)
    if key_commands:
        lines.append("")
        lines.append("### Key Commands")
        lines.extend(f"- `{item}`" for item in key_commands[:8])
    if output_paths:
        lines.append("")
        lines.append("### Output Paths")
        lines.extend(f"- `{item}`" for item in output_paths[:10])
    if open_risks:
        lines.append("")
        lines.append("### Open Risks")
        lines.extend(f"- {item}" for item in open_risks[:8])
    if not lines:
        return ""
    return "\n".join(lines).strip()


def _report_execute_block(work_item: dict[str, Any]) -> str:
    metadata = work_item.get("metadata") or {}
    runtime_model = str(metadata.get("runtime_model") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    if infer_turn_mode(_WorkItemLike(work_item)) != TurnMode.REPORT:
        return ""
    is_report_turn = (
        metadata.get("report_execution_work_item")
        or str(metadata.get("report_target_work_item_id") or "").strip()
    )
    if not is_report_turn:
        return ""
    target_contract = dict(
        metadata.get("report_target_prompt_contract")
        or metadata.get("prompt_contract", {}).get("target_contract", {})
        or {}
    )
    source_evidence = _render_report_source_evidence(work_item)
    parts = [_COMPANY_REPORT_GENERATION_HEADER]
    if target_contract:
        parts.append("### Work Item Contract To Report Against\n" + str(target_contract))
    if source_evidence:
        parts.append(source_evidence)
    return "\n\n".join(part for part in parts if part)


def _peer_wait_context_block(work_item: dict[str, Any]) -> str:
    """功能：peer wait 唤醒上下文——回复注入 / 超时续行指引（唤醒重跑轮可见）。"""
    metadata = work_item.get("metadata") or {}
    runtime_model = str(metadata.get("runtime_model") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    latest_reply = str(metadata.get("latest_peer_reply") or "").strip()
    timeout_action = str(metadata.get("peer_timeout_action") or "").strip()
    if not latest_reply and not timeout_action:
        return ""
    lines = ["## Peer Reply Context"]
    if latest_reply:
        lines.append(f"- A peer has replied to your question: {latest_reply[:1500]}")
        lines.append("- Continue your work using this reply; do not re-ask the same question.")
    if timeout_action:
        lines.append(
            "- A previous peer wait timed out without a reply. Continue with the information "
            "you already have; do not park on the same question again."
        )
    return "\n".join(lines)


def _multi_team_manager_capable(work_item: dict[str, Any]) -> bool:
    metadata = work_item.get("metadata") or {}
    for key in ("direct_report_seat_ids", "allowed_delegate_role_ids", "direct_report_role_ids"):
        items = [str(item).strip() for item in list(metadata.get(key) or []) if str(item).strip()]
        if items:
            return True
    if str(metadata.get("managed_team_id") or "").strip():
        return True
    topology = dict(metadata.get("runtime_topology") or {})
    seats = [dict(item) for item in list(topology.get("seats") or []) if isinstance(item, dict)]
    current_seat_id = str(
        metadata.get("delegation_seat_id") or metadata.get("seat_id") or ""
    ).strip()
    current_role_id = str(
        work_item.get("assignee_role") or metadata.get("work_item_role_id") or ""
    ).strip()
    current_seat: dict[str, Any] = {}
    for seat in seats:
        if current_seat_id and str(seat.get("seat_id") or "").strip() == current_seat_id:
            current_seat = seat
            break
    if not current_seat and current_role_id:
        for seat in seats:
            if str(seat.get("role_id") or "").strip() == current_role_id:
                current_seat = seat
                break
    if not current_seat:
        return False
    for key in ("direct_report_seat_ids", "allowed_delegate_role_ids", "direct_report_role_ids"):
        items = [
            str(item).strip() for item in list(current_seat.get(key) or []) if str(item).strip()
        ]
        if items:
            return True
    return bool(str(current_seat.get("managed_team_id") or "").strip())


def _dispatch_requirement_block(work_item: dict[str, Any]) -> str:
    metadata = work_item.get("metadata") or {}
    runtime_model = str(metadata.get("runtime_model") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    if not _multi_team_manager_capable(work_item):
        return ""
    turn_type = str(work_item.get("turn_type") or "").strip().lower()
    lines: list[str] = []
    # 功能：派发强制要求只在派发语义轮（intake/dispatch = 确认后、需要执行前）——
    # 其余轮次的管理者不因该契约被迫先派发（与讨论/澄清人设冲突）。
    if turn_type in {"intake", "dispatch"}:
        lines.append(
            """## Dispatch Planning Contract
- This turn is currently in `dispatch_required`.
- Scope first: preserve the upstream goal, requested deliverable form, required paths, constraints, and hard dependencies.
- Delegate outcome-based child WorkItems; planning or checklist work must not replace requested production work.
- Separate hard blockers from startable preparation, and split phases only when they have distinct outputs, blockers, or handoff points.
- If production cannot happen in this environment, dispatch or escalate the blocker instead of substituting a plan.
- After that, do exactly one of the following:
  1. Use `spawn_thread_task` to create downstream child work for at least one direct report.
  2. If local execution is truly required because no downstream seat is a fit, include exactly one line in your final response:
     `NO_DELEGATION_JUSTIFICATION: <specific reason>`
- If you execute locally without delegation or the justification line, the runtime will reject the turn and ask you to dispatch first.
- If this is a follow-up over an existing board, inspect it with `manager_board_read` and use `modify_work_item` / `delete_work_item` for wrong existing items before creating additional work."""
        )
    snapshot = dict(work_item.get("context_snapshot") or {})
    followup_text = str(snapshot.get("user_supplied_input") or "").strip()
    is_final_decider_followup = bool(metadata.get("followup_routed_to_final_decider")) or bool(
        followup_text
    )
    if is_final_decider_followup:
        if followup_text:
            lines.append(
                "\n".join(
                    [
                        "## User Follow-up Board Reconciliation",
                        f"User follow-up: {followup_text[:800]}",
                    ]
                )
            )
        else:
            lines.append("## User Follow-up Board Reconciliation")
        lines.append(
            "\n".join(
                [
                    "- You are resuming this same role session with a fresh owner directive; rely on the session history already available to you.",
                    "- Decide the next step from the current state and available collaboration tools: answer directly, close review when appropriate, inspect or revise the board, delegate more work, or take another supported runtime action.",
                    "- When changing an existing board, inspect it with `manager_board_read` and prefer `modify_work_item` / `delete_work_item` for stale or wrong child work before adding more work.",
                    "- If you create replacement child work, also resolve obsolete siblings so old work does not keep running or blocking completion.",
                    "- Keep your final response focused on what you decided or changed.",
                ]
            )
        )
    mutation_action = str(metadata.get("manager_mutation_action") or "").strip()
    mutation_reason = str(metadata.get("manager_mutation_reason") or "").strip()
    mutation_user_input = str(
        metadata.get("latest_user_directive") or metadata.get("manager_mutation_user_input") or ""
    ).strip()
    if mutation_action == "modify" or mutation_user_input:
        mutation_lines = ["## Upstream Work Item Mutation Reconciliation"]
        if mutation_user_input:
            mutation_lines.append(f"Latest upstream user instruction: {mutation_user_input[:800]}")
        if mutation_reason:
            mutation_lines.append(f"Manager mutation reason: {mutation_reason[:500]}")
        mutation_lines.extend(
            [
                "- Your current WorkItem must follow this latest upstream instruction. Before creating replacement child work, inspect your existing child board with `manager_board_read`.",
                "- For each existing child, decide whether it still supports the revised parent brief. Use `modify_work_item` for a child that should continue under the new scope.",
                "- Use `delete_work_item` for stale children that still describe the old direction, especially suspended/running children left over from before Stop.",
                "- If you delegate a replacement child, also resolve the obsolete child so old work does not keep running or remain visible on the board.",
            ]
        )
        lines.append("\n".join(mutation_lines))
    guard_violation = str(metadata.get("manager_dispatch_guard_violation") or "").strip()
    if guard_violation:
        lines.append(
            "\n".join(
                [
                    "## Dispatch Guard Rejection",
                    f"Your previous turn was rejected by the runtime dispatch guard: {guard_violation}.",
                    "This turn MUST either create/modify/delete child WorkItems or end with exactly "
                    "one `NO_DELEGATION_JUSTIFICATION: <specific reason>` line.",
                ]
            )
        )
    return "\n\n".join(lines)


def is_dispatch_ready(work_item: dict[str, Any]) -> bool:
    """功能：派发语义轮判定（确定方案后、需要执行前）——「分工与执行链 + 锁定
    目标与交付物」只在这样的轮次产出与校验：
    dispatch 轮；有派发面的 intake 轮；或槽位口径已确认的 plan 续跑轮。"""
    metadata = work_item.get("metadata") or {}
    turn_type = str(work_item.get("turn_type") or "").strip().lower()
    if turn_type == "dispatch":
        return True
    if turn_type == "intake":
        for key in (
            "allowed_delegate_role_ids",
            "direct_report_role_ids",
            "direct_report_seat_ids",
        ):
            items = [str(x).strip() for x in list(metadata.get(key) or []) if str(x).strip()]
            if items:
                return True
        return bool(str(metadata.get("managed_team_id") or "").strip())
    if turn_type == "plan":
        return bool(metadata.get("slot_answered"))
    return False


def _normalize_contract_audience(audience: str | None) -> ContractAudience:
    return "external" if str(audience or "").strip().lower() == "external" else "native"


def _contract_text_for_audience(text: str, audience: ContractAudience) -> str:
    if audience != "external":
        return text
    rendered = text
    for old, new in _EXTERNAL_TOOL_WORDING_REPLACEMENTS:
        rendered = rendered.replace(old, new)
    return rendered


_SMALLTALK_TITLES = frozenset(
    {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "hello",
        "hi",
        "hey",
        "在吗",
        "在不在",
        "谢谢",
        "辛苦了",
        "好的",
        "好",
        "ok",
        "收到",
    }
)


def _is_smalltalk_work_item(work_item: dict[str, Any]) -> bool:
    # 功能：标题/描述整句为问候时按闲聊契约处理（不依赖调用方是否正确标记 chat_kind）。
    metadata = work_item.get("metadata") or {}
    if str(metadata.get("chat_kind") or "") == "chat":
        return True
    text = str(work_item.get("title") or work_item.get("description") or "").strip()
    compact = text.strip(" \t\r\n，。！？!?、,.~～")
    return bool(compact) and compact.casefold() in _SMALLTALK_TITLES


def build_company_work_item_contract(
    work_item: dict[str, Any],
    *,
    audience: ContractAudience = "native",
) -> str:
    """按 work_item 的 turn_type 拼装公司运行时合约提示词（完整版）。

    param work_item: 工作项 dict，至少含 ``turn_type``、``title``、``metadata``
    param audience: ``"native"`` 或 ``"external"``，影响工具措辞
    return: 拼接后的提示词片段
    """
    resolved_audience = _normalize_contract_audience(audience)
    metadata = work_item.get("metadata") or {}
    runtime_model = str(metadata.get("runtime_model") or "").strip()

    # 问候/闲聊回合优先于任何执行/组织契约——不得套用任务汇报模板
    if _is_smalltalk_work_item(work_item):
        return _contract_text_for_audience(_CHAT_WORK_ITEM_GUIDELINES.strip(), resolved_audience)

    if runtime_model == "multi_team_org":
        report_execute_block = _report_execute_block(work_item)
        if report_execute_block:
            return _contract_text_for_audience(report_execute_block, resolved_audience)
        review_execute_block = _review_execute_block(work_item)
        if review_execute_block:
            return _contract_text_for_audience(review_execute_block, resolved_audience)
        parts = [_MULTI_TEAM_ORG_GUIDELINES.strip()]
        if _multi_team_manager_capable(work_item):
            parts.append(_MANAGER_RUNTIME_CONTRACT.strip())
        review_block = _review_pending_block(work_item)
        if review_block:
            parts.append(review_block)
        dispatch_block = _dispatch_requirement_block(work_item)
        if dispatch_block:
            parts.append(dispatch_block)
        peer_block = _peer_wait_context_block(work_item)
        if peer_block:
            parts.append(peer_block)
        return _contract_text_for_audience("\n\n".join(parts), resolved_audience)

    turn_type = str(work_item.get("turn_type") or "execute").strip().lower() or "execute"
    title = str(work_item.get("title") or "projected work item").strip()

    header = [
        _COMPANY_WORK_ITEM_GUIDELINES.strip(),
        f"Current work item: `{title}`",
        f"Work item turn type: `{turn_type}`",
    ]
    orchestration = str(metadata.get("work_item_orchestration_profile") or "").strip()
    if orchestration:
        header.append(f"Orchestration profile: `{orchestration}`")
    verification_required = bool(metadata.get("work_item_verification_required", False))
    header.append(
        "Work item verification requirement: "
        + (
            "required before completion."
            if verification_required
            else "not automatically required for this work item."
        )
    )
    if str(metadata.get("chat_kind") or "") == "chat":
        header.append(_CHAT_WORK_ITEM_GUIDELINES.strip())
    elif is_dispatch_ready({"turn_type": turn_type, "metadata": metadata}):
        header.append(_DISPATCH_WORK_ITEM_GUIDELINES.strip())
    elif turn_type in {"plan", "intake"}:
        header.append(_PLAN_WORK_ITEM_GUIDELINES.strip())
    elif turn_type == "review":
        header.append(_REVIEW_WORK_ITEM_GUIDELINES.strip())
    elif turn_type in {"aggregate", "deliver"}:
        header.append(_AGGREGATE_WORK_ITEM_GUIDELINES.strip())
    else:
        header.append(_EXECUTE_WORK_ITEM_GUIDELINES.strip())
    return _contract_text_for_audience(
        "\n\n".join(part for part in header if part), resolved_audience
    )


def build_external_company_work_item_contract(work_item: dict[str, Any]) -> str:
    """Return the company/runtime contract with external-agent tool wording."""
    return build_company_work_item_contract(work_item, audience="external")
