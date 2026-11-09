"""契约：交付格式分轮——plan 轮为方案讨论（轻）；派发轮才要求分工表。

- 背景：分工表格最初进入 plan 轮合约 + output_contract 必填键。
- 语义调整：plan 模式不应含「分工与执行链」——岗位人设（反问式澄清）
 与「先派发计划」的契约冲突；「分工与执行链 + 锁定目标与交付物」只在用户确认后、
 需要执行前的派发轮（intake/dispatch）产出并要求校验。"""

from __future__ import annotations


class TestPlanTurnIsDiscussion:
    def test_plan_contract_is_discussion_without_dispatch_format(self) -> None:
        from bandi.company.company_runtime_contract import build_company_work_item_contract

        item = {"turn_type": "plan", "title": "规划任务", "metadata": {}}
        text = build_company_work_item_contract(item)
        assert "方案讨论" in text, "plan 轮合约必须是讨论/澄清语义"
        assert "不产出「分工与执行链」" in text, "plan 轮必须显式声明不产出分工表"
        assert "| 顺序 | 交付物 | 负责人 | 状态 |" not in text, "plan 轮不得带分工表格规格"

    def test_plan_required_keys_are_empty(self) -> None:
        from bandi.company.output_contract import _infer_contract
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        item = create_work_item(title="t", turn_type=WorkItemTurnType.PLAN)
        contract = _infer_contract(item)
        assert contract.required_keys == (), "plan 轮（讨论）不因缺计划小节判失败"


class TestDispatchTurnRequiresPlanFormat:
    def test_intake_contract_mentions_dispatch_format(self) -> None:
        from bandi.company.company_runtime_contract import build_company_work_item_contract

        item = {
            "turn_type": "intake",
            "title": "接单派发",
            "metadata": {"allowed_delegate_role_ids": ["visual"]},
        }
        text = build_company_work_item_contract(item)
        assert "分工与执行链" in text and "锁定目标与交付物" in text
        assert "| 顺序 | 交付物 | 负责人 | 状态 |" in text, "表格列规格必须写明"
        assert "Timeline" in text and "不要输出" in text, "必须显式声明不再输出 Timeline"
        assert "回复正文" in text, "必须指引把计划放进最终回复而非仅工具载荷"
        assert "成员任务" in text and "下一个" in text, "串行成员任务块只写下一个执行成员"

    def test_dispatch_turn_same_requirements(self) -> None:
        from bandi.company.company_runtime_contract import build_company_work_item_contract

        item = {"turn_type": "dispatch", "title": "派发", "metadata": {}}
        text = build_company_work_item_contract(item)
        assert "分工与执行链" in text and "锁定目标与交付物" in text

    def test_dispatch_required_keys_are_chinese_sections(self) -> None:
        """契约校验键与合约文案同源：旧英文键（steps/timeline）不得命中。"""
        from bandi.company.output_contract import _infer_contract, validate_output
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        item = create_work_item(
            title="t",
            turn_type=WorkItemTurnType.INTAKE,
            metadata={"allowed_delegate_role_ids": ["visual"]},
        )
        contract = _infer_contract(item)
        assert contract.required_keys == ("分工与执行链", "锁定目标与交付物")

        ok = validate_output(
            "## 分工与执行链\n| 顺序 | 交付物 | 负责人 | 状态 |\n## 锁定目标与交付物\n- 市场",
            contract,
        )
        assert ok.valid, f"合规范文必须通过：{ok.missing_keys}"
        bad = validate_output("## Steps\n## Timeline", contract)
        assert not bad.valid and "分工与执行链" in bad.missing_keys, "旧英文格式必须判缺失"
