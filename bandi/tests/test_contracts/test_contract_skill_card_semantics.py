# 功能：契约——SkillCard 剩余语义字段 + serializer 落盘闭环。
# 钉行为不钉实现：新增语义字段默认值恒定可继承 / 中文透传 / semver 格式校验 /
# 破坏性字段值拒绝（extra strict、非法 version）/
# skill_id 路径安全、load 双层校验（JSON 语法 + pydantic 构造即 validate_graph 闭环）
# 非 JSON / 脱图 JSON 拒绝。
# 落地前置（skill 资产持久化），save_card 写 ~/.bandi/skills/<skill_id>.card.json
# load_card 用作恢复/转发唯一入口——族级覆盖两条路径（构造族 + load 族）。

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from bandi.core.exceptions import BandiError
from bandi.skills.card import SkillCard, SkillGraphEdge, SkillGraphNode
from bandi.skills.serializer import SkillCardLoadError, SkillCardSaveError, load_card, save_card


def _full_card() -> dict[str, Any]:
    return {
        "skill_id": "sk_order_flow",
        "name": "订单处理 SOP",
        "version": "1.2.0",
        "business_domain": "电商",
        "description": "按流程处理用户订单。",
        "trigger_intents": ["处理订单", "发货"],
        "user_utterance_examples": ["帮我看看这单怎么处理", "这个订单需要退款"],
        "goal": ["完成订单状态流转"],
        "required_info": ["order_id", "用户意向"],
        "slot_filling_policy": {"order_id": {"required": True, "source": "user"}},
        "response_rules": ["结论先行", "确认后再执行"],
        "interruption_policy": {"cancel": "ask_user", "restart": "confirm"},
        "start_node_id": "check_order",
        "terminal_node_ids": ["done"],
        "nodes": [
            SkillGraphNode(
                node_id="check_order",
                name="校验订单",
                type="collect_info",
                instruction="读取 order_id 校验订单状态",
                expected_user_info=["order_id"],
                allowed_actions=["query_order"],
                knowledge_scope={"domain": "ecommerce"},
                retry_policy={"max_retries": 2},
                metadata={"owner": "support"},
            ),
            SkillGraphNode(node_id="done", name="完成", type="notify", instruction="告知结果"),
        ],
        "edges": [
            SkillGraphEdge(
                source_node_id="check_order",
                next_node_id="done",
                condition="订单有效",
                label="valid",
            )
        ],
    }


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestSemanticFieldsDefaults:
    def test_new_fields_default_constant_when_omitted(self) -> None:
        """契约：新增语义字段全部缺省时原最小图仍可构造
        默认值恒定可继承（不报错、不回退到 None）。"""
        card = SkillCard(
            skill_id="sk_min",
            name="最小图",
            version="1.0.0",
            start_node_id="n1",
            terminal_node_ids=["n1"],
            nodes=[SkillGraphNode(node_id="n1", name="单节点")],
        )
        assert card.business_domain is None
        assert card.user_utterance_examples == []
        assert card.goal == []
        assert card.required_info == []
        assert card.slot_filling_policy == {}
        assert card.response_rules == []
        assert card.interruption_policy == {}
        node = card.nodes[0]
        assert node.expected_user_info == []
        assert node.allowed_actions == []
        assert node.knowledge_scope == {}
        assert node.retry_policy == {}
        assert node.metadata == {}
        assert card.edges == []

    def test_semantic_fields_passthrough_unchanged(self) -> None:
        """契约（真实数据形态）：全部剩余语义字段填入中文值时
        构造成功且逐字透传——无静默裁剪/改写。"""
        card = SkillCard(**_full_card())
        assert card.business_domain == "电商"
        assert card.user_utterance_examples == ["帮我看看这单怎么处理", "这个订单需要退款"]
        assert card.goal == ["完成订单状态流转"]
        assert card.required_info == ["order_id", "用户意向"]
        assert card.slot_filling_policy == {"order_id": {"required": True, "source": "user"}}
        assert card.response_rules == ["结论先行", "确认后再执行"]
        assert card.interruption_policy == {"cancel": "ask_user", "restart": "confirm"}
        node = card.nodes[0]
        assert node.expected_user_info == ["order_id"]
        assert node.allowed_actions == ["query_order"]
        assert node.knowledge_scope == {"domain": "ecommerce"}
        assert node.retry_policy == {"max_retries": 2}
        assert node.metadata == {"owner": "support"}
        assert card.edges[0].label == "valid"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestVersionSemantics:
    def test_rejects_non_semver_version(self) -> None:
        """契约：version 非 `x.y.z` 三段数值格式 → 构造失败
        （版本语义 = 可比较三段式，拒绝 free-form 字符串）。"""
        for bad in ("v1.0", "1", "1.0", "1.0.0-beta", "a.b.c", ""):
            raw = _full_card()
            raw["version"] = bad
            with pytest.raises(ValidationError):
                SkillCard(**raw)

    def test_accepts_semver_versions(self) -> None:
        """契约：合法三段 semver（含位数差异）→ 构造成功。"""
        for good in ("0.0.1", "1.0.0", "2.10.100"):
            raw = _full_card()
            raw["version"] = good
            card = SkillCard(**raw)
            assert card.version == good

    def test_default_version_still_valid(self) -> None:
        """契约：默认 version 仍为 '1.0.0' 且通过语义校验。"""
        raw = _full_card()
        raw.pop("version")
        card = SkillCard(**raw)
        assert card.version == "1.0.0"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestSerializerRoundTrip:
    def test_save_then_load_roundtrip_exact(self, tmp_path: Path) -> None:
        """契约（端到端）：save_card 落盘真实文件 → load_card 读回
        ——最终产物=写入可读回且字段逐字一致（含中文语义字段/图）。"""
        card = SkillCard(**_full_card())
        saved = save_card(card, tmp_path)
        assert saved.is_file()
        assert saved.name == "sk_order_flow.card.json"
        loaded = load_card(saved)
        assert loaded.model_dump() == card.model_dump()
        assert loaded.nodes[0].instruction == "读取 order_id 校验订单状态"
        assert loaded.interruption_policy == {"cancel": "ask_user", "restart": "confirm"}

    def test_save_writes_then_load_without_extras(self, tmp_path: Path) -> None:
        """契约（端到端）：落盘文件内容是合法 JSON（可独立解析）
        且不含 extra 键——待读方（蒸馏器）无需特殊处理。"""
        card = SkillCard(**_full_card())
        saved = save_card(card, tmp_path)
        text = saved.read_text(encoding="utf-8")
        data = json.loads(text)
        assert data["skill_id"] == "sk_order_flow"
        assert "start_node_id" in data
        assert set(data.keys()) == set(card.model_dump().keys())

    def test_load_rejects_non_json(self, tmp_path: Path) -> None:
        """契约（破坏者）：损坏/非 JSON 文件 → load_card 明确拒绝
        抛出 SkillCardLoadError（不抛出裸 JSONDecodeError，调用方可分类处理）。"""
        bad = tmp_path / "broken.card.json"
        bad.write_text("{not-a-json", encoding="utf-8")
        with pytest.raises(SkillCardLoadError):
            load_card(bad)

    def test_load_rejects_graph_invalid_json(self, tmp_path: Path) -> None:
        """契约（破坏者）：JSON 语法合法但违反图闭环（死路/不可达）
        → load_card 拒绝——load 是恢复/转发唯一入口，必须二次走 validate_graph
        闭环校验（在恢复路径生效），不让坏图回流运行。"""
        bad_json = {
            "skill_id": "sk_broken",
            "name": "坏图",
            "version": "1.0.0",
            "start_node_id": "a",
            "terminal_node_ids": ["c"],
            "nodes": [
                SkillGraphNode(node_id="a", name="入口").model_dump(),
                SkillGraphNode(node_id="b", name="死路").model_dump(),
                SkillGraphNode(node_id="c", name="出口").model_dump(),
            ],
            "edges": [SkillGraphEdge(source_node_id="a", next_node_id="b").model_dump()],
        }
        f = tmp_path / "bad-graph.card.json"
        f.write_text(json.dumps(bad_json), encoding="utf-8")
        with pytest.raises(SkillCardLoadError):
            load_card(f)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestSerializerSafety:
    def test_skill_id_path_traversal_rejected(self, tmp_path: Path) -> None:
        """契约（破坏者）：skill_id 带路径穿越段（../、反斜杠、绝对路径）
        → save_card 拒绝且不产生任何文件——溢出的卡片不得写出到库目录之外。"""
        for evil in ("..", "../evil", "a\\b", "/abs/path", "x;rm", "="):
            card = SkillCard(
                skill_id=evil,
                name="evil",
                version="1.0.0",
                start_node_id="n1",
                terminal_node_ids=["n1"],
                nodes=[SkillGraphNode(node_id="n1", name="n")],
            )
            with pytest.raises(SkillCardSaveError):
                save_card(card, tmp_path)
        leftovers = list(tmp_path.iterdir())
        assert leftovers == []

    def test_save_creates_dir_and_leaves_no_temp_files(self, tmp_path: Path) -> None:
        """契约：目标目录不存在时自动创建；写入完成后目录内
        只有最终 .card.json，无 .tmp/.partial 残留（原子写）。"""
        nested = tmp_path / "cards"
        card = SkillCard(**_full_card())
        saved = save_card(card, nested)
        assert saved.parent == nested
        entries = [p.name for p in nested.iterdir()]
        assert entries == ["sk_order_flow.card.json"]

    def test_save_overwrites_atomically(self, tmp_path: Path) -> None:
        """契约（幂等覆盖）：同一 skill_id 重复 save → 覆盖为最新"""
        card_a = SkillCard(**_full_card())
        save_card(card_a, tmp_path)
        card_b = SkillCard(
            skill_id="sk_order_flow",
            name="订单处理 SOP v2",
            version="1.3.0",
            start_node_id="n1",
            terminal_node_ids=["n1"],
            nodes=[SkillGraphNode(node_id="n1", name="单节点")],
        )
        save_card(card_b, tmp_path)
        entries = [p.name for p in tmp_path.iterdir()]
        assert entries == ["sk_order_flow.card.json"]
        loaded = load_card(tmp_path / "sk_order_flow.card.json")
        assert loaded.version == "1.3.0"

    def test_load_missing_file_rejected(self, tmp_path: Path) -> None:
        """契约：load_card 指向不存在文件 → SkillCardLoadError。"""
        with pytest.raises(SkillCardLoadError):
            load_card(tmp_path / "absent.card.json")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestErrorFamily:
    def test_save_unserializable_field_maps_to_save_error(self, tmp_path: Path) -> None:
        """契约（破坏者）：卡片字段含不可 JSON 序列化值（如 Path/datetime）
        → save_card 抛 SkillCardSaveError（不抛裸 TypeError，调用方可分类统一处理）。"""
        card = SkillCard(
            skill_id="sk_evil",
            name="坏值",
            version="1.0.0",
            start_node_id="n1",
            terminal_node_ids=["n1"],
            nodes=[SkillGraphNode(node_id="n1", name="n")],
            slot_filling_policy={"deadline": datetime(2026, 1, 1)},
        )
        with pytest.raises(SkillCardSaveError):
            save_card(card, tmp_path)

    def test_save_lone_surrogate_maps_to_save_error_zero_residue(self, tmp_path: Path) -> None:
        """契约（破坏者）：str 字段含孤立代理项（lone surrogate U+）
        → save_card 抛 SkillCardSaveError（不抛裸 UnicodeEncodeError）且 tmp 零残留
        ——写盘步编码失败同样归类于 SaveError 族。"""
        card = SkillCard(
            skill_id="sk_surr",
            name="代理",
            version="1.0.0",
            start_node_id="n1",
            terminal_node_ids=["n1"],
            nodes=[SkillGraphNode(node_id="n1", name="n")],
            description="x\ud800y",
        )
        with pytest.raises(SkillCardSaveError):
            save_card(card, tmp_path)
        leftovers = [p.name for p in tmp_path.iterdir()]
        assert leftovers == []

    def test_errors_derive_from_bandi_error(self) -> None:
        """契约：SkillCardSaveError/LoadError 必须继承 BandiError
        ——调用方按 `except BandiError` 统一归类 Bandi 异常族。"""
        assert issubclass(SkillCardSaveError, BandiError)
        assert issubclass(SkillCardLoadError, BandiError)
