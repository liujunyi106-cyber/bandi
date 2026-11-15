from __future__ import annotations

from bandi.company.company_runtime_contract import (
    build_company_work_item_contract,
    build_external_company_work_item_contract,
)


def _work_item(turn_type: str = "execute") -> dict:
    return {
        "turn_type": turn_type,
        "title": "季度数据整理" if turn_type == "execute" else "review delivery",
        "metadata": {},
    }


class TestExternalContractWiring:
    """契约：外部执行（ExternalSeatExecutor）使用的 external 措辞合约。"""

    def test_external_wording_replaces_native_tool_enum(self):
        """external 合约不得残留 native 工具枚举措辞；native 合约保持原样。"""
        item = _work_item("review")
        external_text = build_external_company_work_item_contract(item)
        native_text = build_company_work_item_contract(item, audience="native")
        assert "external agent" in external_text
        assert "(file_read, bash, git_*, web_search, etc.)" not in external_text
        assert native_text == build_company_work_item_contract(item)
