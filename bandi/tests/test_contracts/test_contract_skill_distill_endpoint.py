# 功能：契约——POST /api/skills/distill（自然语言 / 程序性记忆 → draft 技能卡）。
# 钉行为不钉实现：断言最终产物（落盘 .card.json 内容级 / 返回字段口径 / 统一错误体）
# 明确「只产 draft 卡」——不写 SKILL.md、不进技能审批暂存区（晋升仍走审批门）。
# 生产路径族：raw_content 模式（与 CLI skills distill 同口径）与 memory_source 模式
# 主链路 + 真实 tmp store；蒸馏器用桩（不调真实 LLM），provider 懒建/可注入。
from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.staging import get_skill_pending_store
from bandi.skills.card import SkillCard, SkillGraphNode
from bandi.skills.distiller import SkillDistillResponse
from bandi.skills.serializer import SkillCardSaveError, save_card
from bandi.tools.skill_tool import SkillDiscoverTool

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


class _FakeDistiller:
    """功能：契约桩——记录蒸馏请求并按生产语义落 draft 卡（不调真实 LLM）；
    落盘失败同生产蒸馏器吞为 warning 后返回草稿（红：端点仍 200）。"""

    def __init__(self, skills_dir: Path, *, fail: bool = False) -> None:
        self.skills_dir = skills_dir
        self.fail = fail
        self.requests: list[Any] = []
        self.session_ids: list[str | None] = []

    async def distill(
        self,
        request: Any,
        model_config: Any = None,
        *,
        session_id: str | None = None,
    ) -> SkillDistillResponse:
        self.requests.append(request)
        self.session_ids.append(session_id)
        if self.fail:
            raise RuntimeError("蒸馏引擎不可用")
        card = SkillCard(
            skill_id="sk_mem_distilled",
            name="记忆蒸馏流程",
            version="1.0.0",
            description="从记忆条目蒸馏的流程",
            nodes=[SkillGraphNode(node_id="n1", name="执行", instruction="按记忆条目执行流程。")],
            start_node_id="n1",
            terminal_node_ids=["n1"],
        )
        warnings = ["来自桩蒸馏"]
        try:
            save_card(card, self.skills_dir)
        except SkillCardSaveError as exc:
            warnings.append(f"技能卡已生成但落盘失败（不影响本次返回）：{exc}")
        return SkillDistillResponse(draft_skill=card, warnings=warnings, tool_suggestions=[])


@pytest.fixture()
def distill_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """功能：蒸馏端点隔离环境——tmp 技能目录/长期库 + 桩蒸馏器 + 单例重置。"""
    app_mod = importlib.import_module("bandi.api.app")
    skills_dir = tmp_path / "skills"
    long_term = LongTermMemory(str(tmp_path / "long_term.db"), decay_half_life_days=180.0)
    fake = _FakeDistiller(skills_dir)
    monkeypatch.setattr(app_mod, "_API_KEY", "test")
    monkeypatch.setattr(
        app_mod,
        "_skill_distill_overrides",
        {
            "distiller": fake,
            "long_term": long_term,
            "skills_dir": skills_dir,
            "available_tools": [],
        },
    )
    monkeypatch.setattr(app_mod, "_skill_distiller", None)
    monkeypatch.setattr(app_mod, "_skill_distill_long_term", None)
    monkeypatch.setattr(app_mod, "_skill_distill_tools", None)
    yield SimpleNamespace(app=app_mod, skills_dir=skills_dir, long_term=long_term, fake=fake)
    long_term.close()


def _store_memory(long_term: LongTermMemory, item_id: str, content: str, memory_type: str) -> None:
    long_term.store(
        MemoryItem(item_id, content, metadata={"memory_type": memory_type}),
        project_id="p1",
    )


class TestRawContentMode:
    def test_raw_content_distill_lands_draft_card_only(self, distill_env: SimpleNamespace) -> None:
        """契约（DISTILL-1，端到端）：raw_content 模式返回 CLI 口径字段，draft 卡落盘
        SKILL.md 零产出、技能审批暂存区零记录（只产 draft，不绕过审批门）。"""
        resp = client.post(
            "/api/skills/distill",
            json={"raw_content": "收到退款申请后先核对订单，再提交退款。"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "sk_mem_distilled"
        assert body["name"] == "记忆蒸馏流程"
        assert body["version"] == "1.0.0"
        assert body["warnings"] == ["来自桩蒸馏"]
        assert body["tool_suggestions"] == []
        assert body["persisted"] is True, "落卡成功必须显式声明已持久化"
        assert (distill_env.skills_dir / "sk_mem_distilled.card.json").is_file()
        assert not list(distill_env.skills_dir.rglob("SKILL.md"))
        assert get_skill_pending_store().pending_count() == 0
        request = distill_env.fake.requests[0]
        assert request.raw_content == "收到退款申请后先核对订单，再提交退款。"
        assert str(distill_env.fake.session_ids[0]).startswith("skill_distill:")

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"raw_content": "   "}, id="blank-raw-content"),
            pytest.param({}, id="missing-both-sources"),
            pytest.param({"raw_content": "文本", "memory_source": "procedural"}, id="both-sources"),
            pytest.param({"memory_source": "not-a-type"}, id="unknown-memory-source"),
        ],
    )
    def test_invalid_payload_rejected_with_frozen_error_body(
        self, distill_env: SimpleNamespace, payload: dict[str, Any]
    ) -> None:
        """契约（DISTILL-2，破坏者，参数化）：空内容/缺源/双源/未知记忆类型一律 400
        响应体为 冻结统一错误体形状。"""
        resp = client.post("/api/skills/distill", json=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert set(body) == {"code", "message", "detail", "request_id"}
        assert body["code"] == "VALIDATION_ERROR"
        assert distill_env.fake.requests == []


class TestMemorySourceMode:
    def test_memory_source_merges_only_matching_type(self, distill_env: SimpleNamespace) -> None:
        """契约（DISTILL-3，端到端）：memory_source 模式只合并 memory_type 匹配的
        条目（中文逐字进蒸馏请求），返回 memory_count；异类记忆不混入。"""
        _store_memory(distill_env.long_term, "m1", "流程一：先验票再发货。", "procedural")
        _store_memory(distill_env.long_term, "m2", "流程二：缺货先登记再补发。", "procedural")
        _store_memory(distill_env.long_term, "m3", "偏好：红色包装。", "preference")
        resp = client.post(
            "/api/skills/distill",
            json={"memory_source": "procedural", "limit": 5, "project_id": "p1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory_count"] == 2
        request = distill_env.fake.requests[0]
        assert "流程一：先验票再发货。" in request.raw_content
        assert "流程二：缺货先登记再补发。" in request.raw_content
        assert "偏好：红色包装。" not in request.raw_content

    def test_memory_type_is_case_sensitive_and_non_str_content_skipped(
        self, distill_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """契约（DISTILL-9，破坏者）：memory_type 精确匹配（大小写敏感，"PROCEDURAL"
        不命中）；content 非字符串条目跳过（防 None→"None"/int 污染蒸馏输入）；
        多条记忆拼接带条目分隔标记（[记忆 N]）。"""
        app_mod = importlib.import_module("bandi.api.app")

        class _Item:
            def __init__(self, content: Any, memory_type: str) -> None:
                self.content = content
                self.metadata = {"memory_type": memory_type}

        class _FakeLongTerm:
            def list_items(self, **kwargs: Any) -> list[_Item]:
                return [
                    _Item("流程一：先验票再发货。", "procedural"),
                    _Item(None, "procedural"),
                    _Item(123, "procedural"),
                    _Item("大写类型不命中", "PROCEDURAL"),
                ]

        overrides = dict(app_mod._skill_distill_overrides)
        overrides["long_term"] = _FakeLongTerm()
        monkeypatch.setattr(app_mod, "_skill_distill_overrides", overrides)
        resp = client.post("/api/skills/distill", json={"memory_source": "procedural"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory_count"] == 1
        raw = distill_env.fake.requests[0].raw_content
        assert "流程一：先验票再发货。" in raw
        assert "None" not in raw
        assert "123" not in raw
        assert "大写类型不命中" not in raw
        assert "[记忆 1]" in raw, "条目拼接必须带分隔标记"

    def test_memory_source_without_hits_returns_404(self, distill_env: SimpleNamespace) -> None:
        """契约（DISTILL-4，破坏者）：目标类型无命中 → 404 且不调用蒸馏器（不落卡）。"""
        _store_memory(distill_env.long_term, "m1", "偏好：绿色包装。", "preference")
        resp = client.post(
            "/api/skills/distill",
            json={"memory_source": "process_conclusion", "project_id": "p1"},
        )
        assert resp.status_code == 404
        assert distill_env.fake.requests == []
        assert not list(distill_env.skills_dir.glob("*.card.json"))

    def test_memory_source_limit_is_bounded(self, distill_env: SimpleNamespace) -> None:
        """契约（DISTILL-5，破坏者）：limit 非整数/小于 1/非整值浮点一律 400
        （不静默截断 2.9→2，也不回退默认值），保证「取几条记忆」可被前端与用户预期。"""
        for bad_limit in ("abc", 0, -3, 2.9, 200.5, True):
            resp = client.post(
                "/api/skills/distill",
                json={"memory_source": "procedural", "limit": bad_limit},
            )
            assert resp.status_code == 400, f"limit={bad_limit!r} 必须被拒绝"
        assert distill_env.fake.requests == []


class TestDistillFailureAndWiring:
    def test_distill_failure_returns_500_without_artifacts(
        self, distill_env: SimpleNamespace
    ) -> None:
        """契约（DISTILL-6，破坏者）：蒸馏器异常 → 500 统一错误体且保留异常语义；
        零落卡、零 SKILL.md、零审批暂存。"""
        distill_env.fake.fail = True
        resp = client.post("/api/skills/distill", json={"raw_content": "任何中文流程"})
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "INTERNAL_ERROR"
        assert "蒸馏引擎不可用" in body["message"]
        assert not list(distill_env.skills_dir.glob("*.card.json"))
        assert get_skill_pending_store().pending_count() == 0

    def test_unpersisted_distill_returns_500_without_claiming_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """契约（DISTILL-8，破坏者）：蒸馏器未能落卡（技能目录不可写、失败被吞成
        warning）→ 非 200 且零残留，不得返回 200 让调用方误以为技能可用（原红：200）。"""
        app_mod = importlib.import_module("bandi.api.app")
        blocked = tmp_path / "blocked-skills"
        blocked.write_text("not a directory", encoding="utf-8")
        fake = _FakeDistiller(blocked)
        long_term = LongTermMemory(":memory:")
        monkeypatch.setattr(app_mod, "_API_KEY", "test")
        monkeypatch.setattr(
            app_mod,
            "_skill_distill_overrides",
            {
                "distiller": fake,
                "long_term": long_term,
                "skills_dir": blocked,
                "available_tools": [],
            },
        )
        monkeypatch.setattr(app_mod, "_skill_distiller", None)
        monkeypatch.setattr(app_mod, "_skill_distill_long_term", None)
        monkeypatch.setattr(app_mod, "_skill_distill_tools", None)
        resp = client.post("/api/skills/distill", json={"raw_content": "退款流程说明"})
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "INTERNAL_ERROR"
        assert "SKILL_DISTILL_NOT_PERSISTED" in body["message"]
        assert not list(tmp_path.glob("*.card.json"))
        long_term.close()

    def test_memory_to_tool_surface_end_to_end(
        self,
        distill_env: SimpleNamespace,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """契约（DISTILL-7，端到端接线）：程序性记忆 → 蒸馏落卡 → 工具面注册表缓存刷新
        → skill_discover 立刻可见新卡——「记忆变成 Agent 可调用技能」的最小闭环。"""
        skill_tool_mod = importlib.import_module("bandi.tools.skill_tool")
        monkeypatch.setenv("BANDI_SKILLS_DIRS", str(distill_env.skills_dir))
        # 功能：白名单只放行本用例蒸馏产物——级联仍含真实 ~/.bandi/skills，
        # 不隔离则带技能的机器上空白技能库前置断言（count == 0）失效。
        monkeypatch.setenv("BANDI_SKILLS_ALLOW", "sk_mem_distilled")
        # 不得让「空白技能库（count=0）」前置断言失效；cwd 隔离到 tmp
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(skill_tool_mod, "_REGISTRY", None)
        first = asyncio.run(SkillDiscoverTool().execute({}))
        assert first["count"] == 0
        _store_memory(distill_env.long_term, "m1", "退款流程：先核对订单再提交退款。", "procedural")
        resp = client.post(
            "/api/skills/distill",
            json={"memory_source": "procedural", "project_id": "p1"},
        )
        assert resp.status_code == 200
        second = asyncio.run(SkillDiscoverTool().execute({}))
        assert [item["name"] for item in second["skills"]] == ["sk_mem_distilled"]


class TestDistillIsolationAndWiring:
    def test_api_reset_covers_distill_singletons(self) -> None:
        """契约（DISTILL-10）：conftest 的 autouse 重置覆盖蒸馏四单例
        （overrides/distiller/long_term/tools），残留注入不得跨测试泄漏；
        long_term 可关闭时由重置负责关闭。"""
        from tests.conftest import _reset_api_overrides

        app_mod = importlib.import_module("bandi.api.app")
        closed: list[bool] = []

        class _Closable:
            def close(self) -> None:
                closed.append(True)

        app_mod._skill_distill_overrides = {"distiller": object()}
        app_mod._skill_distiller = object()
        app_mod._skill_distill_long_term = _Closable()
        app_mod._skill_distill_tools = [{"name": "x"}]
        _reset_api_overrides()
        assert app_mod._skill_distill_overrides is None
        assert app_mod._skill_distiller is None
        assert app_mod._skill_distill_long_term is None
        assert app_mod._skill_distill_tools is None
        assert closed == [True]

    def test_default_distiller_skills_dir_aligns_with_cascade_first_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """契约（DISTILL-11，接线）：未注入 skills_dir 时默认蒸馏目录 = 技能发现
        级联首目录（BANDI_SKILLS_DIRS 首位）——蒸馏产物对工具面技能库立即可见。"""
        app_mod = importlib.import_module("bandi.api.app")
        cascade_first = tmp_path / "cascade-first-skills"
        monkeypatch.setenv("BANDI_SKILLS_DIRS", str(cascade_first))
        monkeypatch.setattr(app_mod, "_skill_distill_overrides", {})
        monkeypatch.setattr(app_mod, "_skill_distiller", None)
        captured: dict[str, Any] = {}

        class _StubDistiller:
            def __init__(self, provider: Any, skills_dir: str) -> None:
                captured["skills_dir"] = skills_dir

        monkeypatch.setattr("bandi.skills.distiller.SkillDistiller", _StubDistiller)
        monkeypatch.setattr("bandi.agent.factory.build_provider", lambda config: object())
        monkeypatch.setattr("bandi.core.config.BandiConfig.load", classmethod(lambda cls: object()))
        distiller = app_mod._get_skill_distiller()
        assert isinstance(distiller, _StubDistiller)
        assert captured["skills_dir"] == str(cascade_first)
