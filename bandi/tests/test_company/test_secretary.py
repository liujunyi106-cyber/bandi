from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bandi.company.secretary import (
    _TURNS_LOCK,
    PolicyRecord,
    PolicyScope,
    SecretaryService,
    SecretaryStore,
)
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


@pytest.fixture
def secretary(tmp_path):
    return SecretaryService(base_dir=str(tmp_path / "secretary"))


class TestSecretaryService:
    def test_capture_policy(self, secretary):
        record = secretary.capture_policy("preference", "Use Python", "user")
        assert record.policy_type == "preference"
        assert record.content == "Use Python"
        assert record.scope == PolicyScope.GLOBAL

    def test_list_policies(self, secretary):
        secretary.capture_policy("preference", "A", "user")
        secretary.capture_policy("strategy", "B", "user")
        assert len(secretary.list_policies()) == 2
        assert len(secretary.list_policies("preference")) == 1

    def test_get_policy(self, secretary):
        record = secretary.capture_policy("rule", "No sudo", "admin")
        assert secretary.get_policy(record.id) is not None
        assert secretary.get_policy(record.id).id == record.id
        assert secretary.get_policy("nonexistent") is None

    def test_search_policies(self, secretary):
        secretary.capture_policy("preference", "Use Python 3.11", "user")
        secretary.capture_policy("rule", "No direct root access", "admin")
        assert len(secretary.search_policies("Python")) == 1


class TestSecretaryPersistence:
    def test_persists_across_instances(self, tmp_path):
        base = str(tmp_path / "secretary")
        SecretaryService(base_dir=base).capture_policy("rule", "No sudo", "admin")
        records = SecretaryService(base_dir=base).list_policies()
        assert len(records) == 1
        assert records[0].content == "No sudo"

    def test_project_isolation(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.capture_policy(
            "rule", "project rule", "admin", scope=PolicyScope.PROJECT, project_id="p1"
        )
        assert (
            len(
                SecretaryService(base_dir=base).list_policies(
                    scope=PolicyScope.PROJECT, project_id="p1"
                )
            )
            == 1
        )
        assert (
            SecretaryService(base_dir=base).list_policies(
                scope=PolicyScope.PROJECT, project_id="p2"
            )
            == []
        )

    def test_session_policy(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.capture_policy(
            "strategy", "session hint", "user", scope=PolicyScope.SESSION, session_id="s1"
        )
        records = SecretaryService(base_dir=base).list_policies(
            scope=PolicyScope.SESSION, session_id="s1"
        )
        assert len(records) == 1
        assert records[0].content == "session hint"


class TestSecretaryMerged:
    def test_load_merged_combines_layers(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.capture_policy("rule", "global rule", "admin")
        secretary.capture_policy(
            "rule", "project rule", "admin", scope=PolicyScope.PROJECT, project_id="p1"
        )
        secretary.capture_policy(
            "rule", "session rule", "admin", scope=PolicyScope.SESSION, session_id="s1"
        )
        merged = secretary.load_merged(project_id="p1", session_id="s1")
        assert {r.content for r in merged} == {"global rule", "project rule", "session rule"}

    def test_merged_project_scoped(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.capture_policy("rule", "global rule", "admin")
        secretary.capture_policy(
            "rule", "other project rule", "admin", scope=PolicyScope.PROJECT, project_id="p2"
        )
        merged = secretary.load_merged(project_id="p1")
        assert [r.content for r in merged] == ["global rule"]

    def test_merged_ignores_session_when_absent(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.capture_policy("rule", "global rule", "admin")
        merged = secretary.load_merged(project_id="p1", session_id="s1")
        assert [r.content for r in merged] == ["global rule"]


class TestSecretaryTurns:
    def test_record_and_list_turns(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.record_turn("s1", "user", "hello")
        secretary.record_turn("s1", "assistant", "hi there")
        turns = secretary.list_turns("s1")
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert turns[0]["content"] == "hello"

    def test_turns_persist_across_instances(self, tmp_path):
        base = str(tmp_path / "secretary")
        SecretaryService(base_dir=base).record_turn("s1", "user", "hello")
        turns = SecretaryService(base_dir=base).list_turns("s1")
        assert len(turns) == 1
        assert turns[0]["content"] == "hello"

    def test_list_turns_unknown_session(self, tmp_path):
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        assert secretary.list_turns("nope") == []

    def test_record_turn_auto_creates_session(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.record_turn("s1", "user", "hello")
        assert secretary.list_sessions() == ["s1"]

    def test_list_sessions_project_filtered(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.record_turn("s1", "user", "a", project_id="p1")
        secretary.record_turn("s2", "user", "b", project_id="p2")
        assert set(secretary.list_sessions()) == {"s1", "s2"}
        assert secretary.list_sessions(project_id="p1") == ["s1"]

    def test_list_sessions_project_filter_reuses_turns_cache(self, tmp_path, monkeypatch):
        import bandi.company.secretary as sec

        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.record_turn("s1", "user", "a", project_id="p1")
        secretary.record_turn("s2", "user", "b", project_id="p2")
        real_load = sec.SecretaryStore._load_yaml
        reads = 0

        def counting_load(self, path):
            nonlocal reads
            reads += 1
            return real_load(self, path)

        monkeypatch.setattr(sec.SecretaryStore, "_load_yaml", counting_load)
        assert secretary.list_sessions(project_id="p1") == ["s1"]
        assert secretary.list_sessions(project_id="p1") == ["s1"]
        assert reads <= 2

    def test_build_session_context(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.record_turn("s1", "user", "remember this")
        secretary.record_turn("s1", "assistant", "got it")
        ctx = secretary.build_session_context("s1")
        assert "remember this" in ctx
        assert "got it" in ctx

    def test_build_session_context_unknown(self, tmp_path):
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        assert secretary.build_session_context("nope") == ""

    def test_turns_cache_lru_evicts_oldest(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path, max_cached_sessions=2)
        store.save_turns("s1", [{"role": "user", "content": "a"}])
        store.save_turns("s2", [{"role": "user", "content": "b"}])
        store.save_turns("s3", [{"role": "user", "content": "c"}])
        assert "s1" not in store._turns_cache
        assert "s2" in store._turns_cache
        assert "s3" in store._turns_cache

    def test_turns_cache_lru_touch_keeps_recent(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path, max_cached_sessions=2)
        store.save_turns("s1", [{"role": "user", "content": "a"}])
        store.save_turns("s2", [{"role": "user", "content": "b"}])
        store.load_turns("s1")
        store.save_turns("s3", [{"role": "user", "content": "c"}])
        assert "s1" in store._turns_cache
        assert "s2" not in store._turns_cache

    def test_store_save_turns_rejects_traversal_session(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path)
        with pytest.raises(ValueError):
            store.save_turns("../../evil", [{"role": "user", "content": "hi"}])
        assert not (tmp_path.parent / "evil.yaml").exists()

    def test_store_load_turns_rejects_traversal_session(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path)
        with pytest.raises(ValueError):
            store.load_turns("..\\..\\evil")

    def test_store_save_rejects_traversal_project_id(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path)
        with pytest.raises(ValueError):
            store.save(
                PolicyScope.PROJECT,
                [],
                project_id="../../evil-project",
            )
        assert not (tmp_path.parent / "evil-project.yaml").exists()


class TestSecretaryDescribePolicies:
    def test_describe_policies_empty(self, tmp_path):
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        assert secretary.describe_policies() == ""

    def test_describe_policies_renders_records(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.capture_policy("preference", "Use Python", "user")
        secretary.capture_policy("rule", "No sudo", "admin")
        text = secretary.describe_policies()
        assert "preference" in text
        assert "Use Python" in text
        assert "No sudo" in text

    def test_describe_policies_scoped(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        secretary.capture_policy("rule", "global rule", "admin")
        secretary.capture_policy(
            "rule", "project rule", "admin", scope=PolicyScope.PROJECT, project_id="p1"
        )
        text = secretary.describe_policies(project_id="p1")
        assert "global rule" in text
        assert "project rule" in text


class TestSecretaryHandleMessage:
    @pytest.mark.asyncio
    async def test_handle_message_without_llm_echoes(self, tmp_path):
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        result = await secretary.handle_message("hello secretary")
        assert result["response"] == "hello secretary"
        assert result["session_id"]
        assert result["applied_actions"] == []

    @pytest.mark.asyncio
    async def test_handle_message_without_llm_records_turns(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        result = await secretary.handle_message("hello", session_id="s1")
        turns = secretary.list_turns("s1")
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert result["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_handle_message_with_llm_parses_json(self, tmp_path):
        base = str(tmp_path / "secretary")
        llm = MockProvider()
        llm.add_response(LLMResponse(content='{"response": "ok", "actions": []}'))
        secretary = SecretaryService(base_dir=base, llm=llm)
        result = await secretary.handle_message("hi", session_id="s1")
        assert result["response"] == "ok"
        assert result["applied_actions"] == []

    @pytest.mark.asyncio
    async def test_handle_message_import_skill_action(self, tmp_path):
        base = str(tmp_path / "secretary")
        skills_dir = tmp_path / "skills"
        src = tmp_path / "src" / "demo"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text(
            "---\nname: demo\ndescription: demo skill\n---\n\n# Demo\n", encoding="utf-8"
        )
        llm = MockProvider()
        payload = (
            '{"response": "imported", "actions": ['
            '{"kind": "import_skill", "source": "path", "path": "'
            + str(src).replace("\\", "\\\\")
            + '"}]}'
        )
        llm.add_response(LLMResponse(content=payload))
        secretary = SecretaryService(base_dir=base, llm=llm, skills_dir=str(skills_dir))
        result = await secretary.handle_message("import demo", session_id="s1")
        assert len(result["applied_actions"]) == 1
        assert "demo" in result["applied_actions"][0]
        assert (skills_dir / "demo" / "SKILL.md").exists()

    @pytest.mark.asyncio
    async def test_handle_message_bad_json_falls_back_to_echo(self, tmp_path):
        base = str(tmp_path / "secretary")
        llm = MockProvider()
        llm.add_response(LLMResponse(content="this is not json"))
        secretary = SecretaryService(base_dir=base, llm=llm)
        result = await secretary.handle_message("hello", session_id="s1")
        assert result["response"] == "this is not json"
        assert result["applied_actions"] == []

    @pytest.mark.asyncio
    async def test_handle_message_action_without_project(self, tmp_path):
        base = str(tmp_path / "secretary")
        llm = MockProvider()
        llm.add_response(
            LLMResponse(
                content='{"response": "ok", "actions": ['
                '{"kind": "import_skill", "source": "path", "path": "C:/x"}]}'
            )
        )
        secretary = SecretaryService(base_dir=base, llm=llm)
        result = await secretary.handle_message("import", session_id="s1")
        assert len(result["applied_actions"]) == 1


class TestSecretaryImportSkill:
    def test_import_skill(self, tmp_path):
        base = str(tmp_path / "secretary")
        skills_dir = tmp_path / "skills"
        src = tmp_path / "src" / "demo"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text(
            "---\nname: demo\ndescription: demo skill\n---\n\n# Demo\n", encoding="utf-8"
        )
        secretary = SecretaryService(base_dir=base, skills_dir=str(skills_dir))
        result = secretary.import_skill(str(src))
        assert result.skill_name == "demo"
        assert (skills_dir / "demo" / "SKILL.md").exists()

    def test_import_skill_missing_raises(self, tmp_path):
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        with pytest.raises(Exception):
            secretary.import_skill(str(tmp_path / "missing"))


class TestCreateWorkItemAction:
    _PAYLOAD = (
        '{"response": "queued", "actions": [{"kind": "create_work_item", '
        '"title": "write docs", "turn_type": "execute", "assignee_role": "worker", '
        '"description": "draft"}]}'
    )

    @pytest.mark.asyncio
    async def test_invokes_creator_with_payload(self, tmp_path):
        captured: dict = {}

        def creator(payload):
            captured.update(payload)
            return "wi-1"

        llm = MockProvider()
        llm.add_response(LLMResponse(content=self._PAYLOAD))
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        result = await secretary.handle_message(
            "create a task", session_id="s1", llm=llm, on_create_work_item=creator
        )
        assert result["response"] == "queued"
        assert captured["title"] == "write docs"
        assert captured["turn_type"] == "execute"
        assert captured["assignee_role"] == "worker"
        assert captured["description"] == "draft"
        assert result["applied_actions"] == ["created work item `wi-1`"]

    @pytest.mark.asyncio
    async def test_without_creator_skips(self, tmp_path):
        llm = MockProvider()
        llm.add_response(LLMResponse(content=self._PAYLOAD))
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        result = await secretary.handle_message("create a task", session_id="s1", llm=llm)
        assert result["applied_actions"] == [
            "skipped work item creation because no creator was provided"
        ]

    @pytest.mark.asyncio
    async def test_without_title_skips(self, tmp_path):
        payload = (
            '{"response": "ok", "actions": [{"kind": "create_work_item", "turn_type": "execute"}]}'
        )
        llm = MockProvider()
        llm.add_response(LLMResponse(content=payload))
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        result = await secretary.handle_message(
            "create a task", session_id="s1", llm=llm, on_create_work_item=lambda p: "wi"
        )
        assert result["applied_actions"] == [
            "skipped work item creation because no title was provided"
        ]

    @pytest.mark.asyncio
    async def test_creator_error_reported(self, tmp_path):
        llm = MockProvider()
        llm.add_response(LLMResponse(content=self._PAYLOAD))

        def creator(payload):
            raise ValueError("bad role")

        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        result = await secretary.handle_message(
            "create a task", session_id="s1", llm=llm, on_create_work_item=creator
        )
        assert result["applied_actions"] == ["work item creation failed: bad role"]


class TestSecretaryPromptScopes:
    def test_session_policies_included_in_prompt(self, tmp_path):
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        secretary.capture_policy(
            "rule", "session-only rule", "test", scope=PolicyScope.SESSION, session_id="s1"
        )
        prompt = secretary._build_prompt("hello", project_id=None, session_id="s1")
        assert "session-only rule" in prompt

    def test_project_policies_included_in_prompt(self, tmp_path):
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        secretary.capture_policy(
            "rule", "project-only rule", "test", scope=PolicyScope.PROJECT, project_id="p1"
        )
        prompt = secretary._build_prompt("hello", project_id="p1", session_id="s1")
        assert "project-only rule" in prompt


class TestSecretarySkillImportGuard:
    @staticmethod
    def _skill_source(tmp_path):
        src = tmp_path / "src" / "demo"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text(
            "---\nname: demo\ndescription: demo skill\n---\n\n# Demo\n", encoding="utf-8"
        )
        return src

    @pytest.mark.asyncio
    async def test_file_path_rejected(self, tmp_path):
        src = self._skill_source(tmp_path)
        secretary = SecretaryService(
            base_dir=str(tmp_path / "secretary"), skills_dir=str(tmp_path / "skills")
        )
        applied = await secretary._apply_actions(
            [{"kind": "import_skill", "path": str(src / "SKILL.md")}], project_id=None
        )
        assert applied and "not a directory" in applied[0]

    @pytest.mark.asyncio
    async def test_outside_allowed_roots_denied(self, tmp_path):
        src = self._skill_source(tmp_path)
        secretary = SecretaryService(
            base_dir=str(tmp_path / "secretary"),
            skills_dir=str(tmp_path / "skills"),
            allowed_skill_import_roots=[str(tmp_path / "other")],
        )
        applied = await secretary._apply_actions(
            [{"kind": "import_skill", "path": str(src)}], project_id=None
        )
        assert applied and "outside allowed roots" in applied[0]

    @pytest.mark.asyncio
    async def test_allowed_root_permits(self, tmp_path):
        src = self._skill_source(tmp_path)
        secretary = SecretaryService(
            base_dir=str(tmp_path / "secretary"),
            skills_dir=str(tmp_path / "skills"),
            allowed_skill_import_roots=[str(src)],
        )
        applied = await secretary._apply_actions(
            [{"kind": "import_skill", "path": str(src)}], project_id=None
        )
        assert applied and "imported skill" in applied[0]


class TestSecretaryAtomicity:
    def test_record_turn_serialized_by_lock(self, tmp_path):
        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        release = threading.Event()

        def hold() -> None:
            with _TURNS_LOCK:
                release.wait()

        holder = threading.Thread(target=hold)
        holder.start()
        time.sleep(0.05)
        done = threading.Event()

        def call() -> None:
            secretary.record_turn("s1", "user", "x")
            done.set()

        worker = threading.Thread(target=call)
        worker.start()
        time.sleep(0.05)
        try:
            assert not done.is_set()
        finally:
            release.set()
            holder.join()
            worker.join()

    def test_record_turn_concurrent_no_loss(self, tmp_path):
        base = str(tmp_path / "secretary")
        secretary = SecretaryService(base_dir=base)
        threads = [
            threading.Thread(target=secretary.record_turn, args=("s1", "user", f"m{i}"))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(secretary.list_turns("s1")) == 8

    def test_save_turns_atomic_no_tmp_residue(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path)
        store.save_turns("s1", [{"role": "user", "content": "a"}])
        assert not list((Path(tmp_path) / "turns").glob("*.tmp"))
        assert len(store.load_turns("s1")) == 1

    def test_save_atomic_no_tmp_residue(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path)
        store.save(
            PolicyScope.GLOBAL,
            [
                PolicyRecord(
                    id="p1",
                    policy_type="rule",
                    content="x",
                    source="test",
                    created_at=datetime.now(UTC),
                )
            ],
        )
        assert not list(Path(tmp_path).glob("*.tmp"))
        assert len(store.load(PolicyScope.GLOBAL)) == 1


class TestSecretaryRobustness:
    def test_from_dict_handles_dirty_data(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path)
        path = tmp_path / "global.yaml"
        path.write_text(
            "---\n- id: ok\n policy_type: rule\n content: fine\n source: a\n created_at: 2026-08-01T00:00:00Z\n"
            "- created_at: not-a-date\n  metadata: 'a string'\n",
            encoding="utf-8",
        )
        records = store.load(PolicyScope.GLOBAL)
        assert len(records) == 2
        assert records[0].id == "ok"
        assert records[1].metadata == {}

    def test_load_ignores_non_dict_yaml_items(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path)
        path = tmp_path / "global.yaml"
        path.write_text("- just-a-string\n- 42\n", encoding="utf-8")
        assert store.load(PolicyScope.GLOBAL) == []

    def test_atomic_write_cleans_tmp_on_failure(self, tmp_path, monkeypatch):
        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr("bandi.company.secretary.os.replace", boom)
        with pytest.raises(OSError):
            store = SecretaryStore(base_dir=tmp_path)
            store.save(
                PolicyScope.GLOBAL,
                [
                    PolicyRecord(
                        id="p1",
                        policy_type="rule",
                        content="x",
                        source="test",
                        created_at=datetime.now(UTC),
                    )
                ],
            )
        assert not list(Path(tmp_path).glob("*.tmp"))

    def test_unknown_scope_raises_not_silent_global(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path)
        with pytest.raises(ValueError):
            store.load("typo-scope")
        with pytest.raises(ValueError):
            store.save("typo-scope", [])
        service = SecretaryService(base_dir=str(tmp_path / "s"))
        with pytest.raises(ValueError):
            service.list_policies(scope="typo-scope")

    def test_list_policies_none_scope_merges(self, tmp_path):
        service = SecretaryService(base_dir=str(tmp_path / "s"))
        service.capture_policy("rule", "global", "user")
        assert len(service.list_policies()) == 1

    def test_search_policies_null_fields_no_crash(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path)
        path = tmp_path / "global.yaml"
        path.write_text(
            "---\n- id: p1\n policy_type: rule\n content: null\n source: null\n created_at: 2026-08-01T00:00:00Z\n",
            encoding="utf-8",
        )
        service = SecretaryService(base_dir=str(tmp_path / "s"))
        service._store = store
        assert service.search_policies("anything") == []

    def test_search_policies_empty_query_returns_all(self, tmp_path):
        service = SecretaryService(base_dir=str(tmp_path / "s"))
        service.capture_policy("rule", "content", "source")
        assert len(service.search_policies("")) == 1
        assert len(service.search_policies(None)) == 1

    def test_build_session_context_limit_zero_returns_empty(self, tmp_path):
        service = SecretaryService(base_dir=str(tmp_path / "s"))
        service.record_turn("s1", "user", "hello")
        assert service.build_session_context("s1", limit=0) == ""
        assert service.build_session_context("s1", limit=-1) == ""

    def test_build_session_context_turn_missing_role(self, tmp_path):
        store = SecretaryStore(base_dir=tmp_path)
        path = tmp_path / "turns" / "s1.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("- content: lonely\n", encoding="utf-8")
        service = SecretaryService(base_dir=str(tmp_path / "s"))
        service._store = store
        assert "lonely" in service.build_session_context("s1")

    @pytest.mark.asyncio
    async def test_call_llm_falls_back_on_unexpected_error(self, tmp_path):
        service = SecretaryService(base_dir=str(tmp_path / "s"))
        mock = MockProvider()
        mock.add_response(KeyError)
        result = await service.handle_message("hi", llm=mock)
        assert "hi" in result["response"]
        assert result["applied_actions"] == []

    def test_create_work_item_callback_exception_bubbles_as_failed(self, tmp_path):
        service = SecretaryService(base_dir=str(tmp_path / "s"))

        def bad_callback(payload):
            raise TypeError("boom")

        result = service._create_work_item_from_action(
            {"title": "t", "turn_type": "execute"}, bad_callback
        )
        assert "failed" in result
