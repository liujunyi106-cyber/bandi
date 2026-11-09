# 功能：收官（收口轮批 2）——`bandi skills distill` CLI 生产入口测试。
# 走真实命令链（app 顶层挂载）+ MockProvider 假蒸馏（patch factory.build_provider /
# build_tool_registry），断言最终产物：sk_ship_order.card.json 落盘且 load_card 逐字读回。
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bandi.cli.app import app
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider
from bandi.skills.serializer import load_card
from bandi.tools.base import BaseTool
from bandi.tools.registry import ToolRegistry

runner = CliRunner()


def _valid_payload() -> str:
    """功能：合法蒸馏响应（两节点闭环草稿卡）。"""
    return json.dumps(
        {
            "draft_skill": {
                "skill_id": "sk_ship_order",
                "name": "发货 SOP",
                "business_domain": "仓储物流",
                "description": "处理发货流程。",
                "goal": ["确认订单信息", "反馈单号"],
                "required_info": ["订单号"],
                "nodes": [
                    {
                        "node_id": "collect_order",
                        "type": "collect_info",
                        "name": "收集订单信息",
                        "instruction": "收集订单号。",
                        "expected_user_info": ["订单号"],
                        "allowed_actions": ["ask_user"],
                    },
                    {
                        "node_id": "reply_final_result",
                        "type": "response",
                        "name": "反馈最终结果",
                        "instruction": "给出明确最终回复。",
                        "expected_user_info": [],
                        "allowed_actions": ["answer_user", "handoff_human"],
                    },
                ],
                "edges": [
                    {
                        "source_node_id": "collect_order",
                        "next_node_id": "reply_final_result",
                        "priority": 0,
                    }
                ],
                "start_node_id": "collect_order",
                "terminal_node_ids": ["reply_final_result"],
            },
            "warnings": [],
        },
        ensure_ascii=False,
    )


class _StubTool(BaseTool):
    name = "create_shipping_order"
    description = "创建物流运单"

    async def execute(
        self, params: dict[str, Any], context: object | None = None
    ) -> str | dict[str, Any]:
        return {"ok": True}


@pytest.fixture()
def patched_factory(monkeypatch: pytest.MonkeyPatch) -> MockProvider:
    provider = MockProvider()
    provider.add_response(LLMResponse(content=_valid_payload()))
    provider.add_response(LLMResponse(content=json.dumps({"passed": True})))
    registry = ToolRegistry()
    registry.register_instance(_StubTool())
    monkeypatch.setattr("bandi.agent.factory.build_provider", lambda *args, **kwargs: provider)
    monkeypatch.setattr("bandi.agent.factory.build_tool_registry", lambda *args, **kwargs: registry)
    return provider


def test_distill_cli_from_source_file(tmp_path: Path, patched_factory: MockProvider) -> None:
    skills_dir = tmp_path / "skills"
    src = tmp_path / "ship_flow.md"
    src.write_text("收到发货指令后核对订单，调用运单接口，反馈单号。", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "skills",
            "distill",
            "--source-file",
            str(src),
            "--domain",
            "仓储物流",
            "--skills-dir",
            str(skills_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["skill_id"] == "sk_ship_order"
    assert payload["name"] == "发货 SOP"

    on_disk = load_card(skills_dir / "sk_ship_order.card.json")
    assert on_disk.skill_id == "sk_ship_order"
    assert on_disk.name == "发货 SOP"
    assert on_disk.start_node_id == "collect_order"


def test_distill_cli_from_inline_text(tmp_path: Path, patched_factory: MockProvider) -> None:
    skills_dir = tmp_path / "skills"

    result = runner.invoke(
        app,
        [
            "skills",
            "distill",
            "--text",
            "客户咨询退货时先确认订单号，再查询退货政策，最后给出处理方案。",
            "--title",
            "退货处理 SOP",
            "--skills-dir",
            str(skills_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (skills_dir / "sk_ship_order.card.json").is_file()


def test_distill_cli_missing_input_rejected(tmp_path: Path, patched_factory: MockProvider) -> None:
    result = runner.invoke(app, ["skills", "distill", "--skills-dir", str(tmp_path / "skills")])

    assert result.exit_code != 0


def test_distill_cli_missing_source_file_fails(
    tmp_path: Path, patched_factory: MockProvider
) -> None:
    result = runner.invoke(
        app,
        [
            "skills",
            "distill",
            "--source-file",
            str(tmp_path / "ghost.md"),
            "--skills-dir",
            str(tmp_path / "skills"),
        ],
    )

    assert result.exit_code == 1
    assert "无法读取" in json.loads(result.output)["error"]
