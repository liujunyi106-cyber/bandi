"""
契约族：事件日志文件名防碰撞（七-）。

背景：agent_runner._safe_filename 用 session_id[:32] 截断 + 字符清洗做事件
JSONL 文件名——公司 scoped session（前缀相同）会写同一文件，事件流串扰。

本契约钉的行为族（生产调用路径）：
- `core/agent_runner.py::_safe_filename`（唯一文件名生成点）
- `core/agent_runner.py::run_agent_turn` 事件日志路径构造（events-{name}.jsonl）

断言「最终产物」：不同 session_id（即便共享 32 字符前缀）必须产出不同文件名；
文件名必须为安全合法（无非法字符）；`_safe_filename` 对同一 id 幂等。
"""

from __future__ import annotations

from bandi.core.agent_runner import _safe_filename

_S1 = "company:prod:user-1234567890abcdefABCDEF"
_S2 = "company:prod:user-1234567890abcdefZYXWVU"  # 与 _S1 前 32 字符完全相同（截断碰撞场景）


def test_contract_event_filename_no_truncation_collision() -> None:
    """共享前缀的不同 session_id 必须产出不同文件名（防同 JSONL 串扰）。"""
    assert _S1[:32] == _S2[:32], "测试前提：两 id 前 32 字符必须相同（截断碰撞场景）"
    f1 = _safe_filename(_S1)
    f2 = _safe_filename(_S2)
    assert f1 != f2, f"文件名必须防截断碰撞，{f1!r} == {f2!r}"


def test_contract_event_filename_safe_chars() -> None:
    """文件名必须是安全字符（[A-Za-z0-9._-]，无冒号/斜杠等）。"""
    name = _safe_filename("company:prod:user-1234567890abcdef")
    assert name and all(c.isalnum() or c in "._-" for c in name), f"非法字符残留: {name!r}"
    assert ":" not in name and "/" not in name


def test_contract_event_filename_idempotent() -> None:
    """同一 session_id 必须产出同一文件名（日志文件复用）。"""
    assert _safe_filename(_S1) == _safe_filename(_S1)
