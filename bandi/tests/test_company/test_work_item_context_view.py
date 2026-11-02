from __future__ import annotations

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item
from bandi.company.work_item_context_view import WorkItemContextView


def _make_wi(metadata: dict | None = None) -> WorkItem:
    return create_work_item(
        title="test",
        turn_type=WorkItemTurnType.EXECUTE,
        metadata=metadata or {},
    )


def _make_runtime(metadata: dict | None = None) -> CompanyRuntime:
    rt = CompanyRuntime(
        project_id="p1",
        session_scope_id="s1",
        role_id="r1",
        employee_id="e1",
    )
    if metadata:
        rt.set_metadata("test_key", metadata.get("test_key", ""))
        for k, v in metadata.items():
            rt.set_metadata(k, v)
    return rt


class TestWorkItemContextView:
    def test_get_prefers_work_item_metadata(self):
        wi = _make_wi({"key": "wi_value"})
        rt = _make_runtime({"key": "rt_value"})
        view = WorkItemContextView(work_item=wi, runtime=rt)
        assert view.get("key") == "wi_value"

    def test_get_falls_back_to_runtime_metadata_for_unknown_key(self):
        wi = _make_wi({})
        rt = _make_runtime({"unknown_key": "rt_value"})
        view = WorkItemContextView(work_item=wi, runtime=rt)
        assert view.get("unknown_key") == "rt_value"

    def test_get_blocks_owned_runtime_key_when_work_item_present(self):
        wi = _make_wi({})
        rt = _make_runtime({"phase": "owned_by_wi"})
        view = WorkItemContextView(work_item=wi, runtime=rt)
        assert view.get("phase") is None

    def test_get_list_returns_list(self):
        wi = _make_wi({"items": ["a", "b"]})
        view = WorkItemContextView(work_item=wi)
        assert view.get_list("items") == ["a", "b"]

    def test_get_list_returns_empty_for_non_list(self):
        wi = _make_wi({"items": "not_a_list"})
        view = WorkItemContextView(work_item=wi)
        assert view.get_list("items") == []

    def test_get_dict_returns_dict(self):
        wi = _make_wi({"cfg": {"x": 1}})
        view = WorkItemContextView(work_item=wi)
        assert view.get_dict("cfg") == {"x": 1}

    def test_has_checks_work_item_first(self):
        wi = _make_wi({"wi_only": "yes"})
        view = WorkItemContextView(work_item=wi)
        assert view.has("wi_only")

    def test_as_dict_without_work_item(self):
        rt = _make_runtime({"rt_key": "rt_value"})
        view = WorkItemContextView(runtime=rt)
        d = view.as_dict()
        assert "rt_key" in d

    def test_metadata_keys(self):
        wi = _make_wi({"a": 1, "b": 2})
        view = WorkItemContextView(work_item=wi)
        keys = view.metadata_keys
        assert "a" in keys
        assert "b" in keys

    def test_has_work_item_property(self):
        wi = _make_wi({})
        view = WorkItemContextView(work_item=wi)
        assert view.has_work_item
        view2 = WorkItemContextView()
        assert not view2.has_work_item

    def test_get_int_returns_int(self):
        wi = _make_wi({"count": "42"})
        view = WorkItemContextView(work_item=wi)
        assert view.get_int("count") == 42

    def test_get_int_default(self):
        wi = _make_wi({})
        view = WorkItemContextView(work_item=wi)
        assert view.get_int("missing", 99) == 99

    def test_repr(self):
        wi = _make_wi({"k": "v"})
        view = WorkItemContextView(work_item=wi)
        r = repr(view)
        assert "WorkItemContextView" in r

    def test_runtime_without_snapshot_api_is_safe(self):
        class FakeRuntime:
            _metadata = {"k": "v"}

        view = WorkItemContextView(runtime=FakeRuntime())
        assert view.get("k") is None
        assert view.as_dict() == {}
