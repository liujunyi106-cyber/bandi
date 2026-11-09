# 功能：SOP 技能卡图模型（SkillGraphNode/Edge/Card + validate_graph 闭环校验）。
from __future__ import annotations

import re
from collections import Counter, deque
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class SkillGraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str  # 功能：节点唯一标识（图内引用键）
    name: str  # 功能：节点展示名
    type: str = "collect_info"  # 功能：节点类型指令（collect_info/notify 等）
    instruction: str = ""  # 功能：该节点的执行指令文本
    optional: bool = False  # 功能：是否可跳过
    condition: str | None = None  # 功能：进入/跳过该节点的条件表达式
    expected_user_info: list[str] = Field(default_factory=list)  # 功能：需向用户收集的信息项
    allowed_actions: list[str] = Field(default_factory=list)  # 功能：本节点可用工具白名单
    knowledge_scope: dict[str, Any] = Field(default_factory=dict)  # 功能：节点知识检索范围配置
    retry_policy: dict[str, Any] = Field(default_factory=dict)  # 功能：失败重试策略配置
    metadata: dict[str, Any] = Field(default_factory=dict)  # 功能：扩展元数据（自定义键）


class SkillGraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_node_id: str  # 功能：边起点节点标识
    next_node_id: str  # 功能：边终点节点标识
    condition: str | None = None  # 功能：该边生效条件（空=无条件转移）
    priority: int = 0  # 功能：多候选出边时的选择优先级
    label: str | None = None  # 功能：转移语义标签（如 valid/fallback）


class SkillCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str  # 功能：技能唯一标识（文件名基，仅字母数字/下划线/连字符）
    name: str  # 功能：技能展示名
    version: str = "1.0.0"  # 功能：三段式 semver（构造时校验）
    business_domain: str | None = None  # 功能：业务领域（空=通用技能）
    description: str = ""  # 功能：技能用途描述
    enabled: bool = True  # 功能：是否启用（禁用后不进入技能目录与调用面）
    trigger_intents: list[str] = Field(default_factory=list)  # 功能：触发意图关键词
    user_utterance_examples: list[str] = Field(
        default_factory=list
    )  # 功能：用户话术示例（蒸馏/评测用）
    goal: list[str] = Field(default_factory=list)  # 功能：技能要实现的目标
    required_info: list[str] = Field(default_factory=list)  # 功能：执行前必须收集的信息项
    slot_filling_policy: dict[str, Any] = Field(default_factory=dict)  # 功能：槽位填充策略配置
    response_rules: list[str] = Field(default_factory=list)  # 功能：回复遵循规则
    interruption_policy: dict[str, str] = Field(default_factory=dict)  # 功能：中断场景处理策略
    nodes: list[SkillGraphNode] = Field(default_factory=list)  # 功能：技能流程节点
    edges: list[SkillGraphEdge] = Field(default_factory=list)  # 功能：流程转移边
    start_node_id: str  # 功能：起始节点标识
    terminal_node_ids: list[str] = Field(default_factory=list)  # 功能：终止节点标识集（可多个出口）

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _SEMVER_RE.fullmatch(value):
            raise ValueError(f"version 必须是三段式 semver（如 1.0.0），得到: {value!r}")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> SkillCard:
        if not self.nodes:
            raise ValueError("技能图至少需要 1 个节点")
        node_ids = [node.node_id for node in self.nodes]
        duplicates = sorted(nid for nid, count in Counter(node_ids).items() if count > 1)
        if duplicates:
            raise ValueError(f"node_id 必须唯一: {', '.join(duplicates)}")
        node_id_set = set(node_ids)
        if self.start_node_id not in node_id_set:
            raise ValueError(f"start_node_id 必须引用存在的节点: {self.start_node_id}")
        if not self.terminal_node_ids:
            raise ValueError("terminal_node_ids 至少包含 1 个节点")
        missing_terminal = [nid for nid in self.terminal_node_ids if nid not in node_id_set]
        if missing_terminal:
            raise ValueError(f"terminal_node_ids 引用了缺失节点: {', '.join(missing_terminal)}")
        outgoing: dict[str, list[str]] = {nid: [] for nid in node_ids}
        incoming: dict[str, list[str]] = {nid: [] for nid in node_ids}
        for edge in self.edges:
            if edge.source_node_id not in node_id_set:
                raise ValueError(f"边 source 引用缺失节点: {edge.source_node_id}")
            if edge.next_node_id not in node_id_set:
                raise ValueError(f"边 next 引用缺失节点: {edge.next_node_id}")
            outgoing[edge.source_node_id].append(edge.next_node_id)
            incoming[edge.next_node_id].append(edge.source_node_id)
        unreachable = sorted(node_id_set - self._reachable_from({self.start_node_id}, outgoing))
        if unreachable:
            raise ValueError(f"存在从 start 不可达的节点: {', '.join(unreachable)}")
        terminal_set = set(self.terminal_node_ids)
        no_exit = sorted(
            nid
            for nid in node_ids
            if nid not in terminal_set and nid not in self._reachable_from(terminal_set, incoming)
        )
        if no_exit:
            raise ValueError(f"存在无法到达 terminal 的非终端节点: {', '.join(no_exit)}")
        return self

    @staticmethod
    def _reachable_from(starts: set[str], adjacency: dict[str, list[str]]) -> set[str]:
        """功能：BFS 求从 starts 出发沿邻接表可达的节点集（含 starts 自身）。
        边方向由调用方决定：outgoing（正向，从 start 找全节点）/ incoming
        （反向，从 terminal 沿前驱回溯找能到达出口的节点）。"""
        seen: set[str] = set(starts)
        queue = deque(starts)
        while queue:
            current = queue.popleft()
            for target in adjacency.get(current, []):
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
        return seen
