# 功能：轮内过程累积器（-3c）——实现 StreamHandler 协议的事件透传装饰器：
# 全部事件原样转发给内层 handler（帧链路零变化），同时按轮累积过程节点；
# on_done（轮末）把节点以 entry_type="process" 落一条到 SessionStore。
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from bandi.memory.session.store import SessionStore

logger = logging.getLogger(__name__)

_WRITE_TOOLS = {"write", "edit", "apply_patch", "file_write", "file_edit"}
_ARTIFACT_PATH_KEYS = ("path", "file_path", "filename")


def register_write_artifact(
    store: Any,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    *,
    work_item_id: str = "",
) -> None:
    """功能：写类工具成功后按输入路径登记产物（hash 尽力而为；失败不抛）。

    用户 chat 回合（ProcessRecorder）与员工执行（EmployeeProgressHandler）共用——
    右栏「产物」对两条链路的产物一视同仁可见。work_item_id 非空时写入关联列
    （员工回复下方的交付物卡片据此精确匹配）。
    """
    if store is None:
        return
    input_map = tool_input if isinstance(tool_input, dict) else {}
    raw_path = ""
    for key in _ARTIFACT_PATH_KEYS:
        value = input_map.get(key)
        if isinstance(value, str) and value.strip():
            raw_path = value.strip()
            break
    if not raw_path:
        return
    hash_value = ""
    try:
        import hashlib

        candidate = Path(raw_path).expanduser()
        if candidate.is_file():
            hash_value = hashlib.sha256(candidate.read_bytes()).hexdigest()
    except (OSError, ValueError):
        hash_value = ""  # 路径不可达（相对路径等）——登记元数据，hash 留空
    try:
        store.register_artifact(
            session_id,
            tool_name=tool_name,
            path=raw_path,
            hash_value=hash_value,
            work_item_id=work_item_id,
        )
    except Exception:  # 登记失败不阻断工具结果转发
        logger.exception("artifact register failed: session=%s path=%s", session_id, raw_path)


# 功能：思考段起点的内部标记（单调钟秒）。落库前必须剥除——持久化只留 durationMs
# 与前端 settleThought 的口径一致（前端剥 startedAt，后端剥 _startedMono）。
_STARTED_MONO = "_startedMono"

# 功能：工具名 → 完成态结论短语（折叠态单行摘要的首段，与详情分开）。
_TOOL_DONE_LABELS: dict[str, str] = {
    "bash": "执行了命令",
    "shell": "执行了命令",
    "python_exec": "运行了代码",
    "read": "已读取文件",
    "file_read": "已读取文件",
    "write": "已写入文件",
    "file_write": "已写入文件",
    "edit": "修改了文件",
    "multi_edit": "修改了文件",
    "apply_patch": "修改了文件",
    "grep": "已搜索代码",
    "glob": "已搜索代码",
    "list_dir": "已查看目录",
    "web_search": "已搜索网页",
    "web_fetch": "已访问网页",
    "knowledge_search": "已检索知识库",
    "skill_discover": "已检索技能",
    "skill_invoke": "已调用技能",
}

# 功能：结论摘要上限——折叠态是一行，超出截断由前端省略号处理。
_SUMMARY_MAX = 160


def _accepts_kwarg(func: Any, name: str) -> bool:
    """功能：被装饰的 handler 是否接受该关键字（旧 handler 不接则不下发）。"""
    try:
        import inspect

        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def tool_result_summary(
    tool_name: str,
    success: bool,
    output_preview: str,
    tool_input: dict[str, Any] | None = None,
    done_label: str = "",
) -> str:
    """功能：工具完成后的一行结论——直接是输出首行，无输出时给完成态短语。

    参考实现里工具行右侧就是这次调用的产出本身（成功时是输出首行，无输出时是
    「已完成」），不拼「已搜索代码 · 」这类动作前缀：动作名已经在行首，重复一遍
    只会把摘要推远。失败同样只写输出内容，标红交给渲染侧。
    """
    first_line = ""
    for raw in (output_preview or "").splitlines():
        text = raw.strip()
        if text:
            first_line = text
            break
    if first_line:
        return first_line[:_SUMMARY_MAX]
    if not success:
        return "执行失败"
    return (done_label or _TOOL_DONE_LABELS.get((tool_name or "").lower()) or "已完成")[
        :_SUMMARY_MAX
    ]


def _apply_child_progress(parent: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
    # 功能：子代理过程节点累积到 task 节点 children（与主列表节点同构，单层）
    children: list[dict[str, Any]] = parent.setdefault("children", [])

    def _new_id() -> str:
        return f"{parent['id']}:sa:{len(children) + 1}"

    if kind == "thinking":
        last = children[-1] if children else None
        if last and last["kind"] == "thought" and last["status"] == "running":
            last["text"] += str(payload.get("text") or "")
        else:
            children.append(
                {
                    "id": _new_id(),
                    "kind": "thought",
                    "text": str(payload.get("text") or ""),
                    "status": "running",
                    _STARTED_MONO: time.monotonic(),
                }
            )
    elif kind == "tool_start":
        children.append(
            {
                "id": _new_id(),
                "kind": "tool",
                "toolName": str(payload.get("tool_name") or "?"),
                "input": payload.get("tool_input") or {},
                "status": "running",
                "toolCallId": payload.get("tool_call_id") or None,
            }
        )
    elif kind == "tool_result":
        tool_call_id = str(payload.get("tool_call_id") or "")
        tool_name = str(payload.get("tool_name") or "")
        for node in reversed(children):
            if node["kind"] != "tool" or node["status"] != "running":
                continue
            if tool_call_id and node.get("toolCallId") == tool_call_id:
                node["status"] = "success" if payload.get("success", True) else "error"
                node["detail"] = str(payload.get("output_preview") or "")
                node["durationMs"] = int(payload.get("duration_ms") or 0)
                break
            if not tool_call_id and node.get("toolName") == tool_name:
                node["status"] = "success" if payload.get("success", True) else "error"
                node["detail"] = str(payload.get("output_preview") or "")
                node["durationMs"] = int(payload.get("duration_ms") or 0)
                break
    elif kind == "tool_progress":
        for node in reversed(children):
            if node["kind"] == "tool" and node["status"] == "running":
                node["progress"] = {
                    "type": str(payload.get("progress_type") or ""),
                    "data": payload.get("data") or {},
                }
                break
    elif kind == "skill":
        skills = [str(s) for s in (payload.get("skills") or [])]
        if skills:
            children.append(
                {
                    "id": _new_id(),
                    "kind": "skill",
                    "skills": skills,
                    "count": 1,
                    "status": "success",
                }
            )
    elif kind == "note":
        children.append(
            {
                "id": _new_id(),
                "kind": "note",
                "noteType": str(payload.get("note_type") or ""),
                "status": "success",
            }
        )


def _converge_child_nodes(parent: dict[str, Any]) -> None:
    # 功能：轮末收敛 children 内运行中节点（工具→error / 思考→settle）
    for child in parent.get("children") or []:
        if child["kind"] == "tool" and child["status"] == "running":
            child["status"] = "error"
        elif child["kind"] == "thought" and child["status"] == "running":
            ProcessRecorder._settle_thought(child)


class ProcessRecorder:
    """功能：StreamHandler 过程累积装饰器（轮末持久化；转发一致性由契约钉死）。

    - thought：thinking_delta 增量累积（同段合并、工具后新开段，与前端语义一致）；
    - tool：tool_call_start 建 running 节点（携带 toolCallId），tool_result 回填
      tool_progress 附注；on_done 时仍 running 的收敛为 error（轮被打断的证据）；
    - skill：skill_injected 合并去重（「Skill 已激活」组节点）；
    - note：compaction/interrupt 注记 + memory_written（「记入记忆 xN」，相邻去重）；
    - 纯文本轮（无任何过程事件）不落条目——防空条目膨胀。
    """

    def __init__(self, inner: Any, store: SessionStore, session_id: str) -> None:
        self._inner = inner
        self._store = store
        self._session_id = session_id
        self._nodes: list[dict[str, Any]] = []
        # 功能：全局递增序号与步号——渲染侧据此还原真实执行次序并把过程分到具体推理步
        self._seq = 0
        self._step = 1

    # ── 节点累积 ────────────────────────────────────────────────

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _begin_step(self) -> int:
        """功能：当前推理步号——一段思考开启新的一步，其后执行都归入该步。

        一次「想 → 做」就是一个推理步：思考节点负责开步，工具节点跟随。
        这样渲染侧能把「执行完之后的再思考」识别成下一步，而不是并进同一步。
        """
        return self._step

    def _open_step(self) -> int:
        """功能：开启新推理步（思考到达时调用），返回推进后的步号。"""
        self._step += 1
        return self._step

    def _bind_session(self, session_id: str) -> None:
        # 功能：事件携带的真实会话 id 迟到绑定（新会话异常时构造侧尚为空）
        if session_id:
            self._session_id = session_id

    def _converge_running_thoughts(self) -> None:
        for node in self._nodes:
            if node["kind"] == "thought" and node["status"] == "running":
                self._settle_thought(node)

    @staticmethod
    def _settle_thought(node: dict[str, Any]) -> None:
        """功能：收敛思考节点并结算耗时。

         工具节点有 durationMs（来自 tool_result 帧），思考节点**没有**
        —— 前端只在浏览器内存里算（`settleThought`），从未回传后端 ⇒ 重载会话后
        纯文本轮完全无耗时、混合轮只剩工具耗时（「已完结 · —」）。此处按
        opencode `ReasoningPart.time.{start,end}` 口径在**落库侧**补上。
        """
        node["status"] = "success"
        started = node.pop(_STARTED_MONO, None)
        if isinstance(started, (int, float)):
            node["durationMs"] = max(0, int((time.monotonic() - started) * 1000))

    def _last_running_tool(self, tool_name: str, tool_call_id: str) -> dict[str, Any] | None:
        for node in reversed(self._nodes):
            if node["kind"] != "tool" or node["status"] != "running":
                continue
            if tool_call_id and node.get("toolCallId") == tool_call_id:
                return node
            if not tool_call_id and node.get("toolName") == tool_name:
                return node
        return None

    # ── StreamHandler 协议（透传 + 累积）────────────────────────

    async def on_content_delta(self, session_id: str, delta: str) -> None:
        await self._inner.on_content_delta(session_id, delta)

    async def on_thinking(self, session_id: str, delta: str) -> None:
        self._bind_session(session_id)
        last = self._nodes[-1] if self._nodes else None
        if last and last["kind"] == "thought" and last["status"] == "running":
            last["text"] += delta
        else:
            self._nodes.append(
                {
                    "id": f"pr:thought:{len(self._nodes) + 1}",
                    "kind": "thought",
                    "text": delta,
                    "status": "running",
                    "seq": self._next_seq(),
                    # 新一段思考开启新步：它既是上一批执行的结论，也是下一批执行的依据
                    "step": self._open_step(),
                    # 起点用单调钟：墙钟跳变（NTP/时区）不会算出负耗时
                    _STARTED_MONO: time.monotonic(),
                }
            )
        await self._inner.on_thinking(session_id, delta)

    async def on_tool_call_start(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool_call_id: str = "",
        present: dict[str, Any] | None = None,
    ) -> None:
        self._bind_session(session_id)
        self._converge_running_thoughts()
        node: dict[str, Any] = {
            "id": f"pr:tool:{len(self._nodes) + 1}",
            "kind": "tool",
            "toolName": tool_name,
            "input": tool_input or {},
            "status": "running",
            "toolCallId": tool_call_id or None,
            "seq": self._next_seq(),
            "step": self._begin_step(),
        }
        # 工具自报的渲染意图（卡片形态 + 完成态动作短语）；未声明时前端按工具名回落
        if present:
            if present.get("call"):
                node["presentCall"] = str(present["call"])
            if present.get("done_label"):
                node["doneLabel"] = str(present["done_label"])
        self._nodes.append(node)
        # 继续向下转发渲染意图：WS/SSE 帧要靠它让前端选卡片
        forward: dict[str, Any] = {}
        if present and _accepts_kwarg(self._inner.on_tool_call_start, "present"):
            forward["present"] = present
        await self._inner.on_tool_call_start(
            session_id, tool_name, tool_input, tool_call_id, **forward
        )

    async def on_tool_progress(
        self, session_id: str, tool_name: str, progress_type: str, data: dict[str, Any]
    ) -> None:
        node = self._last_running_tool(tool_name, "")
        if node is not None:
            node["progress"] = {"type": progress_type, "data": data}
        await self._inner.on_tool_progress(session_id, tool_name, progress_type, data)

    async def on_tool_result(
        self,
        session_id: str,
        tool_name: str,
        success: bool,
        output_preview: str,
        duration_ms: int,
        tool_call_id: str = "",
        citations: list[dict[str, Any]] | None = None,
    ) -> None:
        node = self._last_running_tool(tool_name, tool_call_id)
        if node is not None:
            node["status"] = "success" if success else "error"
            node["detail"] = output_preview
            node["durationMs"] = duration_ms
            node["summary"] = tool_result_summary(
                tool_name,
                success,
                output_preview,
                node.get("input"),
                done_label=str(node.get("doneLabel") or ""),
            )
            if citations:
                node["citations"] = citations
        if success and tool_name in _WRITE_TOOLS:
            self._register_artifact(session_id, tool_name, node)
        await self._inner.on_tool_result(
            session_id,
            tool_name,
            success,
            output_preview,
            duration_ms,
            tool_call_id,
            citations=citations,
        )

    def _register_artifact(
        self, session_id: str, tool_name: str, node: dict[str, Any] | None
    ) -> None:
        """功能：产物登记（公共实现委托——员工执行与用户回合同口径）。"""
        if node is None:
            return
        register_write_artifact(self._store, session_id, tool_name, node.get("input") or {})

    async def on_skill_injected(self, session_id: str, skills: list[str]) -> None:
        self._bind_session(session_id)
        last = self._nodes[-1] if self._nodes else None
        if last and last["kind"] == "skill":
            for skill in skills:
                if skill not in last["skills"]:
                    last["skills"].append(skill)
            last["count"] += 1
        else:
            self._nodes.append(
                {
                    "id": f"pr:skill:{len(self._nodes) + 1}",
                    "kind": "skill",
                    "skills": list(skills),
                    "count": 1,
                    "status": "success",
                    "seq": self._next_seq(),
                    "step": self._begin_step(),
                }
            )
        await self._inner.on_skill_injected(session_id, skills)

    async def on_note(self, session_id: str, note_type: str) -> None:
        self._bind_session(session_id)
        self._nodes.append(
            {
                "id": f"pr:note:{len(self._nodes) + 1}",
                "kind": "note",
                "noteType": note_type,
                "status": "success",
            }
        )
        await self._inner.on_note(session_id, note_type)

    async def on_compaction_start(self, session_id: str) -> None:
        self._bind_session(session_id)
        self._nodes.append(
            {
                "id": f"pr:note:{len(self._nodes) + 1}",
                "kind": "note",
                "noteType": "compaction",
                "status": "running",
            }
        )
        forward = getattr(self._inner, "on_compaction_start", None)
        if callable(forward):
            await forward(session_id)

    async def on_compaction(self, session_id: str, detail: dict[str, Any]) -> None:
        self._bind_session(session_id)
        running = next(
            (
                node
                for node in reversed(self._nodes)
                if node.get("kind") == "note"
                and node.get("noteType") == "compaction"
                and node.get("status") == "running"
            ),
            None,
        )
        if running is not None:
            running["status"] = "success"
            running["compaction"] = dict(detail)
        else:
            self._nodes.append(
                {
                    "id": f"pr:note:{len(self._nodes) + 1}",
                    "kind": "note",
                    "noteType": "compaction",
                    "compaction": dict(detail),
                    "status": "success",
                }
            )
        forward = getattr(self._inner, "on_compaction", None)
        if callable(forward):
            await forward(session_id, detail)
        else:
            await self._inner.on_note(session_id, "compaction")

    async def on_memory_written(self, session_id: str, channels: list[str]) -> None:
        self._bind_session(session_id)
        last = self._nodes[-1] if self._nodes else None
        if last and last["kind"] == "note" and last["noteType"] == "memory_written":
            # 同一轮多次写入合并成一行，通道明细累加——落库要留 channels，
            # 否则会话重载后前端只剩总条数、无法按通道分档
            merged = [*(last.get("channels") or []), *channels]
            last["channels"] = merged
            last["count"] = len(merged)
        else:
            self._nodes.append(
                {
                    "id": f"pr:note:{len(self._nodes) + 1}",
                    "kind": "note",
                    "noteType": "memory_written",
                    "count": len(channels),
                    "channels": list(channels),
                    "status": "success",
                }
            )
        await self._inner.on_memory_written(session_id, channels)

    async def on_task_ledger(self, session_id: str, items: list[dict[str, Any]]) -> None:
        await self._inner.on_task_ledger(session_id, items)

    async def on_subagent_progress(
        self, subagent: dict[str, Any], kind: str, payload: dict[str, Any]
    ) -> None:
        """功能：子代理嵌套过程——按 parent_tool_call_id 挂到 task 节点 children。"""
        self._bind_session(str(subagent.get("parent_session_id") or ""))
        parent = None
        parent_id = str(subagent.get("parent_tool_call_id") or "")
        for node in reversed(self._nodes):
            if node["kind"] != "tool":
                continue
            if parent_id and node.get("toolCallId") == parent_id:
                parent = node
                break
        if parent is not None:
            _apply_child_progress(parent, kind, payload)
        inner = getattr(self._inner, "on_subagent_progress", None)
        if inner is not None:
            await inner(subagent, kind, payload)

    async def on_error(self, session_id: str, error: str) -> None:
        await self._inner.on_error(session_id, error)

    async def persist_partial(self, session_id: str, turn_index: int = 0) -> None:
        """功能：仅落库当前累积过程（异常/中断收尾用，不转发 done 帧）。"""
        if not self._nodes:
            return
        for node in self._nodes:
            if node["kind"] == "tool" and node["status"] == "running":
                node["status"] = "error"
            elif node["kind"] == "thought" and node["status"] == "running":
                self._settle_thought(node)
            if node["kind"] == "tool" and node.get("children"):
                _converge_child_nodes(node)
        try:
            # 落库以实参为准（run_agent_turn 对新会话才生成真实 id）
            self._store.append_process_entry(
                session_id or self._session_id, turn_index, self._nodes
            )
        except Exception:  # 持久化失败不阻断轮收尾（帧链路优先）
            logger.exception("process entry persist failed: session=%s", session_id)
        self._nodes = []

    async def on_done(
        self,
        session_id: str,
        terminal_reason: str,
        total_tokens: int,
        turn_index: int,
        duration_ms: float = 0.0,
        steps: int = 0,
    ) -> None:
        await self.persist_partial(session_id, turn_index)
        inner_done = getattr(self._inner, "on_done", None)
        if inner_done is not None:
            await inner_done(
                session_id, terminal_reason, total_tokens, turn_index, duration_ms, steps
            )

    def __getattr__(self, name: str) -> Any:
        # 功能：协议外方法（如 _SSEStreamHandler.on_interaction_frame / put）透传内层
        return getattr(self._inner, name)
