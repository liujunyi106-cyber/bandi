# 功能：契约——SkillCard 图模型（SkillGraphNode/Edge/Card + validate_graph 闭环校验）。
# 钉行为不钉实现：合法图可构建且字段透传 / 中文与默认值保留 / 破坏性输入拒绝
# （空节点、重复 node_id、start/terminal/边引用缺失、存在从 start 不可达的节点）/
# 生产路径族：SkillCard(**dict) 构造即校验（pydantic model_validator after）；
# validate_graph 承诺「每个节点必须从 start 经 edges 可达」的闭环不变式。

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from bandi.skills.card import SkillCard, SkillGraphEdge, SkillGraphNode


def _linear_card() -> dict[str, Any]:
    return {
        "skill_id": "sk_deploy",
        "name": "部署 SOP",
        "description": "按步骤完成上线。",
        "trigger_intents": ["我要上线", "发布"],
        "start_node_id": "check_preflight",
        "terminal_node_ids": ["confirm_done"],
        "nodes": [
            SkillGraphNode(
                node_id="check_preflight",
                name="预检",
                type="collect_info",
                instruction="检查 verify 是否六项全过",
            ),
            SkillGraphNode(
                node_id="confirm_done", name="完成", type="notify", instruction="告知用户上线结果"
            ),
        ],
        "edges": [
            SkillGraphEdge(
                source_node_id="check_preflight",
                next_node_id="confirm_done",
                condition="预检通过",
            )
        ],
    }


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestSkillCardBuild:
    def test_valid_linear_graph_builds_with_field_pass_through(self) -> None:
        """契约：由 SkillGraphNode/Edge 组成的线性图可成功构造
        图字段与语义字段原样透传（最终产物 = 输入值，无静默改写）。"""
        card = SkillCard(**_linear_card())
        assert card.skill_id == "sk_deploy"
        assert card.name == "部署 SOP"
        assert card.start_node_id == "check_preflight"
        assert [n.node_id for n in card.nodes] == ["check_preflight", "confirm_done"]
        assert [(e.source_node_id, e.next_node_id) for e in card.edges] == [
            ("check_preflight", "confirm_done")
        ]
        assert card.trigger_intents == ["我要上线", "发布"]

    def test_chinese_values_passthrough_unchanged(self) -> None:
        """契约（真实数据形态）：节点名/指令/边条件/描述为中文时
        逐字透传不丢不截（长文本也不例外）。"""
        card = SkillCard(**_linear_card())
        node = card.nodes[0]
        assert node.name == "预检"
        assert node.instruction == "检查 verify 是否六项全过"
        assert card.edges[0].condition == "预检通过"
        long_name = "上线部署" * 40
        long_card = SkillCard(
            skill_id="sk_long",
            name=long_name,
            start_node_id="n1",
            terminal_node_ids=["n1"],
            nodes=[SkillGraphNode(node_id="n1", name="只有起点")],
        )
        assert long_card.name == long_name

    def test_version_and_intents_defaults(self) -> None:
        """契约：version 默认 '1.0.0'，trigger_intents/terminal 默认
        恒定可继承——缺省不报错且补默认语义。"""
        card = SkillCard(
            skill_id="sk_min",
            name="最小图",
            start_node_id="n1",
            terminal_node_ids=["n1"],
            nodes=[SkillGraphNode(node_id="n1", name="单节点")],
        )
        assert card.version == "1.0.0"
        assert card.trigger_intents == []
        assert card.terminal_node_ids == ["n1"]


# ---------------------------------------------------------------------------
# 破坏性输入拒绝（pydantic 校验触发点 = 构造即校验）
# ---------------------------------------------------------------------------


class TestSkillCardValidation:
    def test_empty_nodes_rejected(self) -> None:
        """契约：图没有任何节点 → 构造失败
        （校验点 = 构造期间即抛 ValidationError）。"""
        with pytest.raises(ValidationError):
            SkillCard(skill_id="sk", name="空图", start_node_id="n1", terminal_node_ids=["n1"])

    def test_duplicate_node_ids_rejected(self) -> None:
        """契约：node_id 重复 → 构造失败，不做静默去重。"""
        raw = _linear_card()
        raw["nodes"] = [
            SkillGraphNode(node_id="dup", name="甲"),
            SkillGraphNode(node_id="dup", name="乙"),
        ]
        raw["edges"] = []
        raw["start_node_id"] = "dup"
        raw["terminal_node_ids"] = ["dup"]
        with pytest.raises(ValidationError):
            SkillCard(**raw)

    def test_start_node_references_missing_node_rejected(self) -> None:
        """契约：start_node_id 不引用任何节点 → 构造失败。"""
        raw = _linear_card()
        raw["start_node_id"] = "ghost"
        with pytest.raises(ValidationError):
            SkillCard(**raw)

    def test_empty_terminal_ids_rejected(self) -> None:
        """契约：terminal_node_ids 为空列表 → 构造失败（死路图）."""
        raw = _linear_card()
        raw["terminal_node_ids"] = []
        with pytest.raises(ValidationError):
            SkillCard(**raw)

    def test_terminal_references_missing_node_rejected(self) -> None:
        """契约：terminal_node_ids 引用不存在的节点 → 构造失败。"""
        raw = _linear_card()
        raw["terminal_node_ids"] = ["missing_end"]
        with pytest.raises(ValidationError):
            SkillCard(**raw)

    def test_edge_references_missing_nodes_rejected(self) -> None:
        """契约：边两端任一引用缺失节点 → 构造失败。"""
        cases: list[tuple[str, str]] = [
            ("ghost", "confirm_done"),
            ("check_preflight", "ghost"),
        ]
        for source, nxt in cases:
            raw = _linear_card()
            raw["edges"] = [SkillGraphEdge(source_node_id=source, next_node_id=nxt)]
            with pytest.raises(ValidationError):
                SkillCard(**raw)

    def test_unreachable_node_rejected(self) -> None:
        """契约：存在从 start 经 edges
        **不可达**的节点 → 构造失败。绕过防护：仅把孤立节点加入 terminal 或挂出边
        都不行——图必须整体从 start 连通。"""
        raw = _linear_card()
        raw["nodes"] = [
            SkillGraphNode(node_id="check_preflight", name="预检"),
            SkillGraphNode(node_id="confirm_done", name="完成"),
            SkillGraphNode(node_id="orphan", name="走不到的旁支"),
        ]
        raw["edges"] = [
            SkillGraphEdge(
                source_node_id="check_preflight",
                next_node_id="confirm_done",
                condition="预检通过",
            )
        ]
        raw["terminal_node_ids"] = ["confirm_done", "orphan"]
        with pytest.raises(ValidationError):
            SkillCard(**raw)

    def test_extra_fields_rejected(self) -> None:
        """契约（破坏者）：构造时夹带非规范字段（如注入类字典键）
        → 严格拒绝，不静默吞掉。"""
        raw = _linear_card()
        raw["hacked_extra"] = {"drop_table": True}
        with pytest.raises(ValidationError):
            SkillCard(**raw)


# ---------------------------------------------------------------------------
# P6b 校验幂等：图校验后 dump→重建往返稳定，重复校验不改变结果
# ---------------------------------------------------------------------------


class TestValidateGraphIdempotent:
    def test_dump_reconstruct_roundtrip_stable(self) -> None:
        """契约（幂等）：合法图校验通过后，model_dump() → 重新构造
        → 再次校验仍通过且字段逐字一致——validate_graph 不产生状态/不回退图。"""
        first = SkillCard(**_linear_card())
        dumped = first.model_dump()
        second = SkillCard(**dumped)
        assert second.model_dump() == dumped
        assert second.name == first.name
        assert second.nodes[1].node_id == "confirm_done"


# ---------------------------------------------------------------------------
# 本不变式为 Bandi 闭环超集加固。
# ---------------------------------------------------------------------------


class TestDeadEndClosedLoop:
    def test_non_terminal_dead_end_branch_rejected(self) -> None:
        """契约（修复核心）：从 start 可达但其子树整体走不到任何
        terminal 的非 terminal 节点（死路 k 无出边）→ 构造失败——运行时走到
        该分支将没有 next，闭环要求任何可达分支都能到达出口。"""
        raw = {
            "skill_id": "sk_dead",
            "name": "死路分支",
            "start_node_id": "a",
            "terminal_node_ids": ["c"],
            "nodes": [
                SkillGraphNode(node_id="a", name="入口"),
                SkillGraphNode(node_id="v", name="中间"),
                SkillGraphNode(node_id="k", name="死路"),
                SkillGraphNode(node_id="c", name="出口"),
            ],
            "edges": [
                SkillGraphEdge(source_node_id="a", next_node_id="v"),
                SkillGraphEdge(source_node_id="v", next_node_id="k"),
                SkillGraphEdge(source_node_id="a", next_node_id="c"),
            ],
        }
        with pytest.raises(ValidationError):
            SkillCard(**raw)

    def test_non_exiting_cycle_rejected(self) -> None:
        """契约（纯循环无出口）：a↔b 循环内节点都无法到达 terminal
        c（出口走 s→c 旁路，不进循环）→ 构造失败——跑起来永远困在循环里
        只有反向可达（强闭环）才能拦下；正向「从 start 可达」查不到它。"""
        raw = {
            "skill_id": "sk_cycle",
            "name": "无出口循环",
            "start_node_id": "s",
            "terminal_node_ids": ["c"],
            "nodes": [
                SkillGraphNode(node_id="s", name="入口"),
                SkillGraphNode(node_id="a", name="循环甲"),
                SkillGraphNode(node_id="b", name="循环乙"),
                SkillGraphNode(node_id="c", name="出口"),
            ],
            "edges": [
                SkillGraphEdge(source_node_id="s", next_node_id="a"),
                SkillGraphEdge(source_node_id="a", next_node_id="b"),
                SkillGraphEdge(source_node_id="b", next_node_id="a"),
                SkillGraphEdge(source_node_id="s", next_node_id="c"),
            ],
        }
        with pytest.raises(ValidationError):
            SkillCard(**raw)

    def test_retry_loop_with_exit_accepted(self) -> None:
        """契约（合法重试环）：a↔b 重试循环带 a→c 出口 → 构造成功
        ——证明强闭环不误伤有界重试语义（循环能从旁路退出到 terminal）。"""
        raw = {
            "skill_id": "sk_retry",
            "name": "带出口重试环",
            "start_node_id": "a",
            "terminal_node_ids": ["c"],
            "nodes": [
                SkillGraphNode(node_id="a", name="预检"),
                SkillGraphNode(node_id="b", name="重试"),
                SkillGraphNode(node_id="c", name="出口"),
            ],
            "edges": [
                SkillGraphEdge(source_node_id="a", next_node_id="b"),
                SkillGraphEdge(source_node_id="b", next_node_id="a"),
                SkillGraphEdge(source_node_id="a", next_node_id="c"),
            ],
        }
        card = SkillCard(**raw)
        assert [n.node_id for n in card.nodes] == ["a", "b", "c"]

    def test_terminal_without_outgoing_accepted(self) -> None:
        """契约：terminal 节点允许无出边（天然终点），单节点
        start 即 terminal 的最小图同样合法——不变式只约束非 terminal 节点。"""
        raw = {
            "skill_id": "sk_end",
            "name": "终点无出边",
            "start_node_id": "a",
            "terminal_node_ids": ["b"],
            "nodes": [
                SkillGraphNode(node_id="a", name="入口"),
                SkillGraphNode(node_id="b", name="出口"),
            ],
            "edges": [SkillGraphEdge(source_node_id="a", next_node_id="b")],
        }
        card = SkillCard(**raw)
        assert card.terminal_node_ids == ["b"]
        minimal = SkillCard(
            skill_id="sk_min",
            name="单节点",
            start_node_id="n1",
            terminal_node_ids=["n1"],
            nodes=[SkillGraphNode(node_id="n1", name="只有起点")],
        )
        assert minimal.nodes[0].node_id == "n1"
