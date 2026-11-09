# 功能：路线 A「换大脑」契约——run_agent/provider_config 透传 + 员工 llm_config

from __future__ import annotations

import os
from typing import Any

import pytest

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.org_engine import OrgEmployee
from bandi.company.seat_executor import EngineSeatExecutor
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core.config import BandiConfig, LLMConfig

# 功能：真实 LLM 门控（同 tests/test_e2e/ 惯例）——无凭证时 build_provider 回退 MockProvider
# 真实 run_agent 全链会跑满 MAX_TURNS 而非正常终止，故无开闸即跳过。
_REAL_LLM_ENABLED = os.environ.get("BANDI_REAL_LLM_TESTS") == "1"


class _FakeOrgEngine:
    def __init__(self, employee: OrgEmployee | None = None) -> None:
        self._employee = employee

    def get_employee(self, employee_id: str) -> OrgEmployee | None:
        return self._employee


def _employee(llm_config: Any) -> OrgEmployee:
    return OrgEmployee(employee_id="e1", name="张三", role_id="r1", llm_config=llm_config)


def _patch_config_load(monkeypatch: pytest.MonkeyPatch, config: BandiConfig) -> None:
    monkeypatch.setattr("bandi.core.config.BandiConfig.load", classmethod(lambda cls: config))


def _patch_run_agent(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    async def fake_run_agent(**kwargs: Any) -> tuple[str, str | None, int, int]:
        captured.update(kwargs)
        return "ok", "COMPLETED", 10, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)


@pytest.mark.asyncio
async def test_run_agent_forwards_provider_config_to_run_agent_turn(monkeypatch):
    """factory.run_agent 签名必须暴露 provider_config 并透传 run_agent_turn。"""
    captured: dict[str, Any] = {}

    async def fake_run_agent_turn(**kwargs: Any) -> Any:
        captured.update(kwargs)

        class _R:
            reply = "ok"
            terminal_reason = "COMPLETED"
            total_tokens = 1
            turn_index = 1

        return _R()

    monkeypatch.setattr("bandi.core.agent_runner.run_agent_turn", fake_run_agent_turn)
    from bandi.agent.factory import run_agent

    await run_agent(
        user_input="hi",
        provider_config={"default_model": "claude-sonnet-5"},
    )
    assert captured.get("provider_config") == {"default_model": "claude-sonnet-5"}, (
        "run_agent 必须把 provider_config 透传给 run_agent_turn（请求级 model 生效入口）"
    )


@pytest.mark.asyncio
async def test_seat_executor_resolves_employee_llm_config(monkeypatch):
    """员工带 llm_config 时 seat 透传 provider_config（换大脑在座位层生效）。"""
    _patch_config_load(monkeypatch, BandiConfig(llm=LLMConfig()))
    captured: dict[str, Any] = {}
    _patch_run_agent(monkeypatch, captured)
    executor = EngineSeatExecutor(
        org_engine=_FakeOrgEngine(
            _employee({"default_model": "claude-sonnet-5", "api_key": "sk-test"})
        )
    )
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e1")
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    result = await executor.run_turn(runtime, item)
    assert result.success is True
    assert captured.get("provider_config") == {
        "default_model": "claude-sonnet-5",
        "api_key": "sk-test",
    }, "员工 llm_config 必须以 provider_config 形态透传 run_agent"


@pytest.mark.asyncio
async def test_seat_executor_without_llm_config_keeps_none(monkeypatch):
    """员工无 llm_config（或 org_engine 未装配）→ provider_config=None，现状零影响。"""
    _patch_config_load(monkeypatch, BandiConfig(llm=LLMConfig()))
    captured: dict[str, Any] = {}
    _patch_run_agent(monkeypatch, captured)
    executor = EngineSeatExecutor(org_engine=_FakeOrgEngine(_employee(None)))
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e1")
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    result = await executor.run_turn(runtime, item)
    assert result.success is True
    assert captured.get("provider_config") is None


@pytest.mark.asyncio
async def test_seat_executor_preflight_unknown_model_fails_closed(monkeypatch):
    """破坏者：未知模型且无 base_url → 预检 fail-closed，不调 run_agent、不静默回退。"""
    _patch_config_load(monkeypatch, BandiConfig(llm=LLMConfig()))
    captured: dict[str, Any] = {}
    _patch_run_agent(monkeypatch, captured)
    executor = EngineSeatExecutor(
        org_engine=_FakeOrgEngine(_employee({"default_model": "no-such-model-ex"}))
    )
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e1")
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    result = await executor.run_turn(runtime, item)
    assert captured == {}, "预检失败必须阻断执行（fail-closed），不得触达 LLM"
    assert result.success is False
    assert "no-such-model-ex" in result.output
    assert result.metadata is not None
    assert "provider_preflight_error" in result.metadata


@pytest.mark.asyncio
async def test_seat_executor_preflight_base_url_fallback_allows(monkeypatch):
    """base_url 已配 → openai_compat 回退通道可用，未知模型名放行。"""
    _patch_config_load(
        monkeypatch, BandiConfig(llm=LLMConfig(base_url="https://llm.example.com/v1"))
    )
    captured: dict[str, Any] = {}
    _patch_run_agent(monkeypatch, captured)
    executor = EngineSeatExecutor(
        org_engine=_FakeOrgEngine(_employee({"default_model": "private-tuned-model"}))
    )
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e1")
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    result = await executor.run_turn(runtime, item)
    assert result.success is True
    assert captured.get("provider_config") == {"default_model": "private-tuned-model"}


@pytest.mark.asyncio
async def test_seat_executor_invalid_llm_config_type_ignored(monkeypatch):
    """破坏者：llm_config 为非法类型（str）→ 忽略配置照常执行，不得崩、不得误传。"""
    _patch_config_load(monkeypatch, BandiConfig(llm=LLMConfig()))
    captured: dict[str, Any] = {}
    _patch_run_agent(monkeypatch, captured)
    executor = EngineSeatExecutor(org_engine=_FakeOrgEngine(_employee("claude-sonnet-5")))
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e1")
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    result = await executor.run_turn(runtime, item)
    assert result.success is True
    assert captured.get("provider_config") is None


@pytest.mark.skipif(
    not _REAL_LLM_ENABLED,
    reason="真实 run_agent 全链 E2E：需 BANDI_REAL_LLM_TESTS=1 + 可用 LLM 凭证"
    "（无凭证时 provider 回退 Mock，会跑满 MAX_TURNS）",
)
@pytest.mark.asyncio
async def test_seat_e2e_real_run_agent_with_employee_model(monkeypatch):
    """DoD-12 端到端：真实 run_agent 全链（不 mock）——员工 llm_config 经预检放行
    生产主链路跑通且产出落 work_item.runtime_metadata（最终产物断言）。"""
    _patch_config_load(monkeypatch, BandiConfig(llm=LLMConfig()))
    executor = EngineSeatExecutor(
        org_engine=_FakeOrgEngine(_employee({"default_model": "deepseek-v4-flash"}))
    )
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e1")
    item = create_work_item(title="EX-3 e2e", turn_type=WorkItemTurnType.EXECUTE)
    result = await executor.run_turn(runtime, item)
    assert result.success is True, f"端到端执行失败: {result.output}"
    assert item.runtime_metadata.get("_last_output"), "产出必须写入 _last_output（最终产物）"


@pytest.mark.asyncio
async def test_seat_executor_preflight_employee_base_url_allows(monkeypatch):
    """破坏者：员工 llm_config 自带 base_url（全局未配）→ 换大脑私有网关必须放行
    不得被全局预检误判 fail-closed 阻断合法配置。"""
    _patch_config_load(monkeypatch, BandiConfig(llm=LLMConfig()))
    captured: dict[str, Any] = {}
    _patch_run_agent(monkeypatch, captured)
    executor = EngineSeatExecutor(
        org_engine=_FakeOrgEngine(
            _employee(
                {
                    "default_model": "private-tuned-model",
                    "base_url": "https://private.example.com/v1",
                }
            )
        )
    )
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e1")
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    result = await executor.run_turn(runtime, item)
    assert result.success is True, result.output
    assert captured.get("provider_config") == {
        "default_model": "private-tuned-model",
        "base_url": "https://private.example.com/v1",
    }
