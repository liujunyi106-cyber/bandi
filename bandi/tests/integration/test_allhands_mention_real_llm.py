"""运行方式（真烧 token，日常 verify 全 skip）：

    set BANDI_REAL_LLM_TESTS=1
    set BANDI_SMOKE_MODEL=agnes-2.5-flash
    set BANDI_SMOKE_PROVIDER=agnes
    pytest tests/integration/test_allhands_mention_real_llm.py -v

验证承诺（Day 129 复杀二批语义在真实 company 运行时下的落地）：
- `@allhands`（all 前缀角色）@mention 不被 `@all` 广播提权吞掉，建卡 assignee=allhands；
- 建卡经真实 OrgEngine + 真实 WorkItemStore + 真实 CommunicationManager
  inbox 落盘 route=user_to_agent（供 面板消费）；
- 对拍 `@all` 已废弃：在相同装配下不建卡、不落盘。

key 解析：BANDI_REAL_LLM_API_KEY env → opencode auth.json（agnes 段）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from bandi.api.company_chat import handle_chat_message
from bandi.company.communication import CommunicationManager, list_inbox
from bandi.company.group_chat_protocol import MENTION_DEPRECATED, MentionError
from bandi.company.org_engine import OrgEngine
from bandi.company.store import WorkItemStore
from bandi.company.work_item import Phase

_AGNES_BASE = "https://apihub.agnes-ai.com/v1"
_FORCED_MODEL = os.environ.get("BANDI_SMOKE_MODEL", "")
_FORCED_PROVIDER = os.environ.get("BANDI_SMOKE_PROVIDER", "")


def _resolve_agnes_key() -> str:
    """agnes key 解析：env 优先 → opencode auth.json agnes 段。"""
    forced = os.environ.get("BANDI_REAL_LLM_API_KEY", "")
    if forced:
        return forced
    auth_path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    return (auth.get("agnes", {}) or {}).get("key", "")


_AGNES_KEY = _resolve_agnes_key()


@pytest.fixture()
def allhands_env(tmp_path: Path) -> dict[str, Any]:
    """真实 company 运行时：OrgEngine(allhands) + real store + real comms + agnes key。"""
    org = OrgEngine()
    org.register_role("allhands", "allhands")
    org.register_role("worker", "worker")

    store = WorkItemStore(str(tmp_path / "wi.db"))
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
    )
    return {
        "project": tmp_path,
        "store": store,
        "communication": comm,
        "org_engine": org,
        "workspace": str(tmp_path / "ws"),
    }


@pytest.fixture(autouse=True)
def _rate_limit_gap():
    import time

    yield
    time.sleep(6)


def _probe_agnes() -> bool:
    """探针：agnes-2.5-flash 真实可达才让用例跑（不可达 = skip 而非 fail）。"""
    if not _AGNES_KEY:
        return False
    from bandi.core.config import LLMConfig
    from bandi.llm.providers.openai import OpenAICompatProvider

    async def _probe() -> bool:
        cfg = LLMConfig(api_key=_AGNES_KEY, base_url=_AGNES_BASE, default_model=_FORCED_MODEL)
        provider = OpenAICompatProvider(cfg, base_url=_AGNES_BASE)
        try:
            resp = await provider.complete([{"role": "user", "content": "reply OK"}])
            return bool((resp.content or "").strip())
        except Exception:  # noqa: BLE001 — 探针失败按不可达处理
            return False
        finally:
            await provider.close()

    import asyncio

    return asyncio.run(_probe())


_ALLHANDS_REACHABLE = _probe_agnes() and _AGNES_KEY

_REAL_ENABLED = (
    os.environ.get("BANDI_REAL_LLM_TESTS") == "1"
    and _FORCED_MODEL
    and _FORCED_PROVIDER == "agnes"
    and _ALLHANDS_REACHABLE
)

pytestmark = [
    pytest.mark.skipif(
        not _REAL_ENABLED,
        reason="@allhands 真实 LLM E2E：需 BANDI_REAL_LLM_TESTS=1 + BANDI_SMOKE_MODEL + BANDI_SMOKE_PROVIDER=agnes，且 agnes 探针可达",
    ),
    pytest.mark.asyncio,
]


async def test_allhands_mention_creates_card_for_all_prefix_role(
    allhands_env: dict[str, Any],
) -> None:
    """@allhands（all 前缀角色）→ 真实建卡 assignee=allhands，不被 @all 广播吞。

    破坏者反例（复杀二批）：裸子串 `"@all" in content` 会把 @allhands 吞成广播
    → 用户 @ 已注册角色却不建卡（任务无主）。E2E 在真实 OrgEngine/store/comms 下
    断言最终产物（建卡 assignee + 落盘 route），不 grep 实现。
    """
    env = allhands_env
    created = handle_chat_message(
        content="@allhands 汇总本日销售数据",
        sender_role="user",
        communication=env["communication"],
        store=env["store"],
        org_engine=env["org_engine"],
    )
    assert len(created) == 1, f"@allhands 必须建卡 1 张，实际 {len(created)}"
    card = created[0]
    assert card.assignee_role == "allhands", (
        f"assignee 必须命中 allhands 角色，实际 {card.assignee_role!r}"
    )
    assert card.phase == Phase.QUEUED, f"新建卡必须 QUEUED，实际 {card.phase}"
    assert card.metadata.get("source") == "chat_mention", (
        f"卡 source 必须 chat_mention，实际 {card.metadata.get('source')}"
    )
    assert card.description == "汇总本日销售数据", (
        f"落盘描述必须去 @ 语法（clean_message），实际 {card.description!r}"
    )

    store_back = env["store"].get(card.id)
    assert store_back is not None, "卡必须经真实 store 落盘（可读回）"
    assert store_back.assignee_role == "allhands", "store 读回的 assignee 必须一致"

    msgs = list_inbox(env["workspace"], "p1", "s1", "board")
    assert msgs, "@allhands 消息必须落盘 board inbox"
    assert msgs[0].metadata.get("route") == "user_to_agent", (
        f"命中 allhands → user_to_agent 标注，实际 {msgs[0].metadata.get('route')}"
    )


async def test_at_all_deprecated_not_shadowed_by_allhands(
    allhands_env: dict[str, Any],
) -> None:
    """对拍：@all 已废弃，不因注册了 allhands 而误建卡。

    破坏者反例：若调度端/路由把所有 all 前缀都当角色匹配，@all 会被误路由到
    allhands 建卡。E2E 断言 @all 不建卡、不落盘。
    """
    env = allhands_env
    with pytest.raises(MentionError) as exc:
        handle_chat_message(
            content="【全体】@all 明天十点复盘",
            sender_role="user",
            communication=env["communication"],
            store=env["store"],
            org_engine=env["org_engine"],
        )
    assert exc.value.code == MENTION_DEPRECATED
    msgs = list_inbox(env["workspace"], "p1", "s1", "board")
    assert not msgs, "@all 已废弃，不得落盘"
