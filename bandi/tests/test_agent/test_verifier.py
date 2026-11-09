# 功能：Verifier 单元测试 — VER 验证闸门

import pytest

from bandi.agent.base import ExecutionContext
from bandi.agent.verifier import Verdict, Verifier, VerifierPacket, VerifierPolicy
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import LLMResponse, ToolResult
from bandi.llm.mock import MockProvider


@pytest.fixture
def ctx() -> ExecutionContext:
    return ExecutionContext(
        messages=[],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )


@pytest.fixture
def mock_verifier() -> Verifier:
    provider = MockProvider()
    return Verifier(provider=provider)


@pytest.mark.asyncio
async def test_evaluate_no_provider_default_fail_closed(ctx: ExecutionContext) -> None:
    v = Verifier()
    result = await v.evaluate(VerifierPacket(task_description="test", output="ok"))
    assert result.action == "fail"


@pytest.mark.asyncio
async def test_evaluate_no_provider_fail_closed(ctx: ExecutionContext) -> None:
    v = Verifier()
    result = await v.evaluate(VerifierPacket(task_description="test", output="ok"))
    assert result.action == "fail", f"默认必须 fail-closed，实际 {result.action}"

    v_open = Verifier(policy=VerifierPolicy(fail_open=True))
    result_open = await v_open.evaluate(VerifierPacket(task_description="test", output="ok"))
    assert result_open.action == "pass", "fail_open=True 时保持放行"


@pytest.mark.asyncio
async def test_judge_error_fail_closed(ctx: ExecutionContext) -> None:
    from bandi.llm.base import BaseProvider

    class BoomProvider(BaseProvider):
        def __init__(self) -> None:
            super().__init__(config=None)

        @property
        def model_name(self) -> str:
            return "boom"

        @property
        def protocol(self) -> str:
            return "openai"

        async def complete(self, messages, config=None):
            raise RuntimeError("judge down")

        async def stream(self, messages, config=None):
            yield ""

    v = Verifier(provider=BoomProvider())
    result = await v.evaluate(VerifierPacket(task_description="t", output="o"))
    assert result.action == "fail", f"judge 异常必须 fail-closed，实际 {result.action}"


@pytest.mark.asyncio
async def test_evaluate_judge_pass(mock_verifier: Verifier) -> None:
    mock_verifier._provider.add_response(  # type: ignore[union-attr]
        LLMResponse(content='{"action": "pass", "reason": "任务完成"}')
    )
    result = await mock_verifier.evaluate(
        VerifierPacket(task_description="计算 1+1", output="结果是 2")
    )
    assert result.action == "pass"
    assert result.reason == "任务完成"


@pytest.mark.asyncio
async def test_evaluate_judge_rework(mock_verifier: Verifier) -> None:
    mock_verifier._provider.add_response(  # type: ignore[union-attr]
        LLMResponse(content='{"action": "rework", "reason": "工具参数不正确"}')
    )
    result = await mock_verifier.evaluate(
        VerifierPacket(task_description="创建文件 test.txt", output="已创建")
    )
    assert result.action == "rework"
    assert result.reason == "工具参数不正确"


@pytest.mark.asyncio
async def test_evaluate_judge_escalate(mock_verifier: Verifier) -> None:
    mock_verifier._provider.add_response(  # type: ignore[union-attr]
        LLMResponse(content='{"action": "escalate", "reason": "检测到越权操作"}')
    )
    result = await mock_verifier.evaluate(
        VerifierPacket(task_description="删除系统文件", output="已删除")
    )
    assert result.action == "escalate"
    assert result.reason == "检测到越权操作"


@pytest.mark.asyncio
async def test_evaluate_judge_json_in_codeblock(mock_verifier: Verifier) -> None:
    mock_verifier._provider.add_response(  # type: ignore[union-attr]
        LLMResponse(content='```json\n{"action": "pass", "reason": "ok"}\n```')
    )
    result = await mock_verifier.evaluate(VerifierPacket(task_description="test", output="ok"))
    assert result.action == "pass"


@pytest.mark.asyncio
async def test_evaluate_judge_invalid_action_fallsback_to_pass(mock_verifier: Verifier) -> None:
    mock_verifier.policy.fail_open = True
    mock_verifier._provider.add_response(  # type: ignore[union-attr]
        LLMResponse(content='{"action": "invalid", "reason": "bad"}')
    )
    result = await mock_verifier.evaluate(VerifierPacket(task_description="test", output="ok"))
    assert result.action == "pass"


@pytest.mark.asyncio
async def test_evaluate_judge_invalid_action_fail_closed_to_fail(
    mock_verifier: Verifier,
) -> None:
    """
    异常路径 fail-closed 一致，LLM 输出格式错误不得绕过验证闸门。"""
    mock_verifier._provider.add_response(  # type: ignore[union-attr]
        LLMResponse(content='{"action": "invalid", "reason": "bad"}')
    )
    result = await mock_verifier.evaluate(VerifierPacket(task_description="test", output="ok"))
    assert result.action == "fail"


@pytest.mark.asyncio
async def test_evaluate_judge_unparseable_json_fail_closed(mock_verifier: Verifier) -> None:
    mock_verifier._provider.add_response(  # type: ignore[union-attr]
        LLMResponse(content="sorry, no json here")
    )
    result = await mock_verifier.evaluate(VerifierPacket(task_description="test", output="ok"))
    assert result.action == "fail"


@pytest.mark.asyncio
async def test_evaluate_judge_call_failure_fallsback_to_fail(mock_verifier: Verifier) -> None:
    mock_verifier._provider.add_response(RuntimeError)  # type: ignore[union-attr]
    result = await mock_verifier.evaluate(VerifierPacket(task_description="test", output="ok"))
    assert result.action == "fail"


def test_verification_required_empty_results(ctx: ExecutionContext) -> None:
    v = Verifier()
    assert v.verification_required(ctx) is False


def test_verification_required_write_tool(ctx: ExecutionContext) -> None:
    v = Verifier()
    ctx.tool_results = [
        ToolResult(tool_call_id="1", tool_name="bash", success=True, output="done"),
    ]
    assert v.verification_required(ctx) is True


def test_verification_required_read_tool_only(ctx: ExecutionContext) -> None:
    v = Verifier()
    ctx.tool_results = [
        ToolResult(tool_call_id="1", tool_name="web_search", success=True, output="results"),
    ]
    assert v.verification_required(ctx) is False


def test_verification_required_write_edit_tool(ctx: ExecutionContext) -> None:
    v = Verifier()
    for tool_name in ("write", "edit", "apply_patch"):
        ctx.tool_results = [
            ToolResult(tool_call_id="1", tool_name=tool_name, success=True, output="done"),
        ]
        assert v.verification_required(ctx) is True, f"{tool_name} 必须触发 VER"


def test_verification_disabled(ctx: ExecutionContext) -> None:
    v = Verifier(policy=VerifierPolicy(enabled=False))
    ctx.tool_results = [
        ToolResult(tool_call_id="1", tool_name="bash", success=True, output="done"),
    ]
    assert v.verification_required(ctx) is False


def test_stagnation_cap_force_escalate(ctx: ExecutionContext) -> None:
    v = Verifier()
    verdict = Verdict(action="rework", reason="needs fix")
    task_meta = {"last_verdict_outcome": "rework", "rework_count": 5}
    result = v.apply_stagnation_cap(task_meta, verdict)
    assert result.action == "escalate"


def test_stagnation_cap_below_limit(ctx: ExecutionContext) -> None:
    v = Verifier()
    verdict = Verdict(action="rework", reason="needs fix")
    task_meta = {"last_verdict_outcome": "rework", "rework_count": 1}
    result = v.apply_stagnation_cap(task_meta, verdict)
    assert result.action == "rework"


def test_stagnation_cap_uses_config_threshold(ctx: ExecutionContext) -> None:
    """stagnation_threshold 配置必须生效（默认 3，改为 1 后 1 次 rework 即升级）。"""
    v = Verifier(policy=VerifierPolicy(stagnation_threshold=1))
    verdict = Verdict(action="rework", reason="needs fix")
    task_meta = {"last_verdict_outcome": "rework", "rework_count": 1}
    result = v.apply_stagnation_cap(task_meta, verdict)
    assert result.action == "escalate", "stagnation_threshold=1 时 1 次 rework 必须升级"


def test_verification_required_code_edits_off(ctx: ExecutionContext) -> None:
    """require_on_code_edits=False 时代码编辑工具不单独触发验证。"""
    v = Verifier(policy=VerifierPolicy(require_on_code_edits=False, min_todos_for_verification=10))
    ctx.tool_results = [
        ToolResult(tool_call_id="1", tool_name="write", success=True, output="done"),
    ]
    assert v.verification_required(ctx) is False, "关闭代码编辑触发后 write 不得触发"

    v2 = Verifier(policy=VerifierPolicy(require_on_code_edits=True, min_todos_for_verification=10))
    assert v2.verification_required(ctx) is True, "开启代码编辑触发后 write 必须触发"


def test_allowed_actions(ctx: ExecutionContext) -> None:
    assert Verifier.ALLOWED_ACTIONS == ("pass", "rework", "escalate", "fail")


@pytest.mark.asyncio
async def test_parse_verdict_raw_json() -> None:
    result = Verifier._parse_verdict('{"action": "pass", "reason": "good"}')
    assert result.action == "pass"
    assert result.reason == "good"


@pytest.mark.asyncio
async def test_parse_verdict_codeblock() -> None:
    result = Verifier._parse_verdict('```json\n{"action": "rework", "reason": "fix"}\n```')
    assert result.action == "rework"


@pytest.mark.asyncio
async def test_parse_verdict_empty_fallback() -> None:
    assert Verifier._parse_verdict("").action == "fail"
    assert Verifier._parse_verdict("", fail_open=True).action == "pass"
