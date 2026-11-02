# 功能：线性/条件二分工作流引擎（简化实现，非并行 DAG——并行 fan-out 校验期拒绝 fail-closed）——沿边执行、条件分支、模板解析、LLM/工具节点、环检测、暂停检查点
from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bandi.llm.base import BaseProvider
from bandi.tools.workflow.store import ExecutionStatus

_TEMPLATE_RE = re.compile(r"\$\{([^}]+)\}")

_COND_BRANCH_TRUE = {"true", "1", "yes", "on"}
_COND_BRANCH_FALSE = {"false", "0", "no", "off"}

_NODE_KINDS: dict[str, set[str]] = {
    "start": {"start", "startnode"},
    "end": {"end", "endnode"},
    "condition": {"condition", "conditionnode", "branch"},
    "transform": {"transform", "transformnode"},
    "llm": {"llmcall", "llm_call", "llmc", "llmnode", "llmcallnode"},
    "tool": {"tool", "toolnode", "toolcall", "toolcallnode"},
}

_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "neq": lambda a, b: a != b,
    "gt": lambda a, b: bool(a > b),
    "ge": lambda a, b: bool(a >= b),
    "lt": lambda a, b: bool(a < b),
    "le": lambda a, b: bool(a <= b),
    "in": lambda a, b: a in (b or []),
    "notin": lambda a, b: a not in (b or []),
    "contains": lambda a, b: (b in a) if isinstance(a, (str, list, dict, tuple)) else False,
    "startswith": lambda a, b: isinstance(a, str) and a.startswith(str(b)),
    "endswith": lambda a, b: isinstance(a, str) and a.endswith(str(b)),
    "isnull": lambda a, b: a is None,
    "isnotnull": lambda a, b: a is not None,
}


def _coerce_pair(left: Any, right: Any) -> tuple[Any, Any]:
    # 功能：数值比较前做宽松类型归一——模板替换产生字符串数字，需与字面量数值对齐
    try:
        numeric_left = float(left)
        numeric_right = float(right)
    except (TypeError, ValueError):
        return left, right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return numeric_left, numeric_right
    return left, right


class NodeExecutionError(RuntimeError):
    pass


@dataclass
class WorkflowState:
    # 功能：一次执行的运行态——输入/输出/变量/历史/错误；暂停时可整体序列化为 checkpoint
    inputs: dict[str, Any]
    variables: dict[str, Any]
    outputs: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    resume_node: str | None = None

    def build_context(self) -> _Context:
        return _Context(inputs=self.inputs, variables=self.variables, outputs=self.outputs)


@dataclass
class EngineResult:
    # 功能：工作流单次执行结果——状态/最终产物/逐节点历史/耗时/暂停检查点
    status: str
    outputs: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    execution_history: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    checkpoint: dict[str, Any] | None = None


class _Context:
    # 功能：模板求值上下文——inputs/variables/outputs 三根命名空间 + 裸 key 回退
    def __init__(self, inputs: dict, variables: dict, outputs: dict) -> None:
        self._inputs = inputs
        self._variables = variables
        self._outputs = outputs

    def resolve(self, expr: str) -> Any:
        if expr.startswith("inputs."):
            return _get_path(self._inputs, expr[len("inputs.") :])
        if expr.startswith("variables."):
            return _get_path(self._variables, expr[len("variables.") :])
        if expr.startswith("outputs."):
            rest = expr[len("outputs.") :]
            if "." in rest:
                node, _, tail = rest.partition(".")
                out = self._outputs.get(node)
                return _get_path(out, tail) if isinstance(out, dict) else None
            return self._outputs.get(rest)
        if expr in self._variables:
            return self._variables[expr]
        return _get_path(self._inputs, expr)


def _get_path(root: Any, path: str) -> Any:
    value = root
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _canonical(kind: str) -> str:
    normalized = kind.strip().lower().replace("_", "")
    for name, aliases in _NODE_KINDS.items():
        if normalized in aliases:
            return name
    raise NodeExecutionError(f"未知节点类型: {kind}")


def _branch(edge: dict[str, Any]) -> str:
    handle = str(edge.get("sourceHandle") or "").strip().lower()
    if handle in _COND_BRANCH_TRUE:
        return "true"
    if handle in _COND_BRANCH_FALSE:
        return "false"
    return ""


class WorkflowEngine:
    # 功能：DAG 工作流执行器——不依赖任何外部事实，节点作曲面数据流组合

    async def execute(
        self,
        document: dict[str, Any],
        *,
        inputs: dict[str, Any] | None = None,
        provider: BaseProvider | None = None,
        tool_runner: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
        resume_state: dict[str, Any] | None = None,
        should_cancel: Callable[[], Awaitable[bool]] | None = None,
        should_pause: Callable[[], Awaitable[bool]] | None = None,
    ) -> EngineResult:
        started = time.perf_counter()
        try:
            doc = self._normalize(document)
            nodes: dict[str, dict[str, Any]] = doc["nodes"]
            by_source: dict[str, list[dict[str, Any]]] = doc["by_source"]
            state = self._seed_state(resume_state, inputs or {}, nodes)
        except NodeExecutionError as exc:
            return EngineResult(
                status=ExecutionStatus.FAILED.value,
                errors=[str(exc)],
                duration_ms=self._elapsed(started),
            )
        try:
            start = self._resolve_start(nodes, doc["start_node"], by_source)
        except NodeExecutionError as exc:
            return self._fail(state, started, [str(exc)])
        # 在节点执行前拒绝（禁止 fan-out 静默丢分支后仍报 completed）
        static_errors = self._static_checks(nodes, by_source)
        if static_errors:
            return self._fail(state, started, static_errors)

        if state.resume_node is not None:
            current = state.resume_node
        else:
            current = start
        seen: set[str] = set()
        while True:
            if should_cancel is not None and await should_cancel():
                return EngineResult(
                    status=ExecutionStatus.CANCELLED.value,
                    outputs=dict(state.outputs),
                    errors=list(state.errors),
                    execution_history=list(state.history),
                    duration_ms=self._elapsed(started),
                )
            if should_pause is not None and await should_pause():
                return self._paused(result=state, started=started, resume_node=current)
            if current in seen:
                return self._fail(state, started, [f"检测到 DAG 环（节点 {current} 被重复执行）"])
            seen.add(current)
            node = nodes.get(current)
            if node is None:
                return self._fail(state, started, [f"未知节点 {current}"])
            outs = by_source.get(current, [])
            if node["type"] == "end":
                state.history.append(_record(current, None, None, time.perf_counter()))
                break
            node_started = time.perf_counter()
            try:
                output, branch = await self._handle(node, state, provider, tool_runner)
            except NodeExecutionError as exc:
                return self._fail(
                    state, started, [str(exc)], current, exc, node_started=node_started
                )
            nxt = self._select_next(outs, node["type"], branch)
            state.history.append(_record(current, output, nxt, node_started))
            if output is not None:
                state.outputs[current] = output
            if nxt is None:
                break
            current = nxt
        return EngineResult(
            status=ExecutionStatus.COMPLETED.value,
            outputs=dict(state.outputs),
            errors=list(state.errors),
            execution_history=list(state.history),
            duration_ms=self._elapsed(started),
        )

    def validate(self, document: dict[str, Any]) -> int:
        # 功能：公开校验入口——DAG 规范化 + start 解析 + 静态结构校验
        # （fan-out/悬空边/环）全通过即视为合法，返回节点数
        doc = self._normalize(document)
        self._resolve_start(doc["nodes"], doc["start_node"], doc["by_source"])
        errors = self._static_checks(doc["nodes"], doc["by_source"])
        if errors:
            raise NodeExecutionError("; ".join(errors))
        return len(doc["nodes"])

    def _static_checks(
        self, nodes: dict[str, dict[str, Any]], by_source: dict[str, list[dict[str, Any]]]
    ) -> list[str]:
        """功能：DAG 静态结构校验——非 condition 节点多出边（fan-out 静默丢分支）
        悬空边端点、有向环。返回错误列表（空 = 通过）。"""
        errors: list[str] = []
        for source, edges in by_source.items():
            if source not in nodes:
                errors.append(f"边引用不存在的源节点: {source}")
                continue
            for e in edges:
                target = e.get("target")
                if target is not None and target not in nodes:
                    errors.append(f"边 {e.get('id') or e} 引用不存在的目标节点: {target}")
            if len(edges) > 1 and nodes[source]["type"] != "condition":
                errors.append(
                    f"节点 {source} 有多条出边（{len(edges)}）——当前引擎仅支持"
                    "线性/条件二分执行；并行 fan-out 请用 ConditionNode 分支表达"
                )
        white, gray, black = 0, 1, 2
        color: dict[str, int] = {nid: white for nid in nodes}

        def dfs(nid: str) -> str | None:
            color[nid] = gray
            for e in by_source.get(nid, []):
                nxt = e.get("target")
                if nxt not in color:
                    continue
                if color[nxt] == gray:
                    return f"检测到 DAG 环（节点 {nid} → {nxt} 参与循环）"
                if color[nxt] == white:
                    inner = dfs(nxt)
                    if inner is not None:
                        return inner
            color[nid] = black
            return None

        for nid in nodes:
            if color[nid] == white:
                cycle = dfs(nid)
                if cycle is not None:
                    errors.append(cycle)
                    break
        return errors

    def _normalize(self, document: dict[str, Any]) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {}
        for raw in document.get("nodes") or []:
            node_id = str(raw.get("id") or "")
            if not node_id:
                raise NodeExecutionError("节点缺少 id")
            nodes[node_id] = {
                "id": node_id,
                "type": _canonical(str(raw.get("type") or "")),
                "name": raw.get("name") or node_id,
                "config": raw.get("config") or {},
            }
        edges = list(document.get("edges") or [])
        by_source: dict[str, list[dict[str, Any]]] = {}
        for e in edges:
            by_source.setdefault(str(e.get("source") or ""), []).append(e)
        return {
            "nodes": nodes,
            "by_source": by_source,
            "start_node": (document.get("start_node") or ""),
            "end_node": (document.get("end_node") or ""),
        }

    def _seed_state(self, resume_state, inputs, nodes) -> WorkflowState:
        if resume_state:
            return WorkflowState(
                inputs=dict(inputs),
                variables=dict(resume_state.get("variables") or {}),
                outputs=dict(resume_state.get("outputs") or {}),
                history=list(resume_state.get("history") or []),
                resume_node=resume_state.get("resume_node"),
            )
        if not nodes:
            raise NodeExecutionError("工作流 data 为空——无任何节点")
        return WorkflowState(inputs=dict(inputs), variables={})

    def _resolve_start(self, nodes, start_given, by_source) -> str:
        if start_given:
            if start_given not in nodes:
                raise NodeExecutionError(f"start_node {start_given} 不在节点集合中")
            return start_given
        targets = {e["target"] for edges in by_source.values() for e in edges}
        sources = [nid for nid in nodes if nid not in targets]
        if len(sources) == 1:
            return sources[0]
        raise NodeExecutionError(
            "无法确定 start_node：入度为 0 的节点必须唯一（或显式声明 start_node）"
        )

    async def _handle(self, node, state, provider, tool_runner=None):
        node_type = node["type"]
        config = node["config"]
        ctx = state.build_context()
        if node_type == "start":
            return None, None
        if node_type == "condition":
            condition = config.get("condition")
            if not condition:
                raise NodeExecutionError(f"条件节点 {node['id']} 缺少 config.condition")
            return None, self._eval_condition(condition, ctx)
        if node_type == "transform":
            template = config.get("template")
            output = self._resolve_template(template, ctx)
            output_var = config.get("output_var")
            if output_var:
                state.variables[output_var] = output
            return output, None
        if node_type == "llm":
            if provider is None:
                raise NodeExecutionError(f"LLM 节点 {node['id']} 未注入 provider——无法调用模型")
            prompt = self._resolve_template(str(config.get("prompt") or ""), ctx)
            response = await provider.complete([{"role": "user", "content": prompt}])
            output = response.content
            output_var = config.get("output_var")
            if output_var:
                state.variables[output_var] = output
            return output, None
        if node_type == "tool":
            tool_name = str(config.get("tool_name") or "").strip()
            if not tool_name:
                raise NodeExecutionError(f"工具节点 {node['id']} 缺少 config.tool_name")
            if tool_runner is None:
                raise NodeExecutionError(f"工具节点 {node['id']} 未注入 tool_runner——无法调用工具")
            params = self._resolve_template(config.get("params") or {}, ctx)
            try:
                output = await tool_runner(tool_name, params)
            except NodeExecutionError:
                raise
            except Exception as exc:
                raise NodeExecutionError(f"工具 {tool_name} 调用失败: {exc}") from exc
            output_var = config.get("output_var")
            if output_var:
                state.variables[output_var] = output
            return output, None
        raise NodeExecutionError(f"不支持执行的节点类型: {node_type}")

    def _select_next(self, outs, node_type, branch) -> str | None:
        if node_type == "condition":
            want = "true" if branch else "false"
            for e in outs:
                if _branch(e) == want:
                    target = e.get("target")
                    return target if target else None
            return None
        for e in outs:
            if not str(e.get("sourceHandle") or "").strip():
                target = e.get("target")
                return target if target else None
        if outs:
            target = outs[0].get("target")
            return target if target else None
        return None

    def _eval_condition(self, condition, ctx) -> bool:
        operator = str(condition.get("operator") or "eq").lower().replace("_", "")
        if operator in ("and", "or", "not"):
            results = [
                self._eval_condition(sub, ctx) for sub in (condition.get("conditions") or [])
            ]
            if operator == "and":
                return all(results)
            if operator == "or":
                return any(results)
            return (not results[0]) if results else False
        left = self._resolve_template(str(condition.get("left") or ""), ctx)
        if operator in ("isnull", "isnotnull"):
            fn = _OPERATORS.get(operator)
            return bool(fn(left, None))
        right = self._resolve_template(condition.get("right"), ctx)
        if operator in ("gt", "ge", "lt", "le", "eq", "ne"):
            left, right = _coerce_pair(left, right)
        fn = _OPERATORS.get(operator)
        if fn is None:
            raise NodeExecutionError(f"未知条件操作符: {operator}")
        try:
            return bool(fn(left, right))
        except TypeError:
            return False

    def _resolve_template(self, value, ctx) -> Any:
        if isinstance(value, str):
            single = _TEMPLATE_RE.fullmatch(value)
            if single is not None:
                resolved = ctx.resolve(single.group(1).strip())
                return resolved if resolved is not None else value
            return _TEMPLATE_RE.sub(self._substitute(ctx), value)
        if isinstance(value, dict):
            return {key: self._resolve_template(v, ctx) for key, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_template(v, ctx) for v in value]
        return value

    def _substitute(self, ctx):
        def replace(match: re.Match) -> str:
            expr = match.group(1).strip()
            resolved = ctx.resolve(expr)
            if resolved is None:
                return match.group(0)
            if isinstance(resolved, str):
                return resolved
            return json.dumps(resolved, ensure_ascii=False)

        return replace

    def _paused(self, *, result, started, resume_node) -> EngineResult:
        checkpoint = {
            "resume_node": resume_node,
            "outputs": dict(result.outputs),
            "variables": dict(result.variables),
            "history": list(result.history),
        }
        return EngineResult(
            status=ExecutionStatus.PAUSED.value,
            outputs=dict(result.outputs),
            errors=list(result.errors),
            execution_history=list(result.history),
            duration_ms=self._elapsed(started),
            checkpoint=checkpoint,
        )

    def _fail(
        self,
        state,
        started,
        errors,
        node_id=None,
        exc=None,
        node_started: float | None = None,
    ) -> EngineResult:
        if exc is not None:
            node_clock = node_started if node_started is not None else started
            state.history.append(
                {
                    "node_id": node_id,
                    "status": "failed",
                    "output": None,
                    "error": str(exc),
                    "duration_ms": self._elapsed(node_clock),
                    "next_node": None,
                }
            )
        return EngineResult(
            status=ExecutionStatus.FAILED.value,
            outputs=dict(state.outputs),
            errors=list(state.errors) + errors,
            execution_history=list(state.history),
            duration_ms=self._elapsed(started),
        )

    def _elapsed(self, started: float) -> int:
        return int(round((time.perf_counter() - started) * 1000))


def _record(node_id, output, next_node, node_started: float) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "status": "completed",
        "output": output,
        "error": None,
        "duration_ms": int(round((time.perf_counter() - node_started) * 1000)),  # 节点耗时
        "next_node": next_node,
    }
