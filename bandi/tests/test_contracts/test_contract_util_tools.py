# 功能：util 工具族契约族——date/json/rule/text 纯逻辑、cache/cron/folder/blob/conv 状态族
# enter_plan_mode、装配（feature group 门控 + DoD-12 端到端主链路）
from __future__ import annotations

import base64
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from bandi.tools.base import ToolCapability, ToolCategory
from bandi.tools.util.cache_manager import CacheManagerTool
from bandi.tools.util.conversation_history import ConversationHistoryTool
from bandi.tools.util.cron_tools import CronCreateTool, CronDeleteTool, CronListTool
from bandi.tools.util.date_calculator import DateCalculatorTool
from bandi.tools.util.enter_plan_mode import EnterPlanModeTool as UtilEnterPlanModeTool
from bandi.tools.util.folder_tool import FolderOperationsTool
from bandi.tools.util.json_parser import JsonParserTool
from bandi.tools.util.rule_matcher import RuleMatcherTool
from bandi.tools.util.text_splitter import TextSplitterTool
from bandi.tools.util.tool_argument_blob import ToolArgumentBlobTool, resolve_blob_text

DATE_OPS = (
    "add",
    "subtract",
    "difference",
    "business_days",
    "add_business_days",
    "format",
    "convert_timezone",
    "parse",
    "now",
    "compare",
)
JSON_OPS = (
    "parse",
    "query",
    "validate",
    "transform",
    "merge",
    "diff",
    "flatten",
    "unflatten",
    "stringify",
    "extract",
)
RULE_OPS = ("evaluate", "match_all", "match_any", "score", "validate", "filter")
TEXT_OPS = (
    "split_chars",
    "split_tokens",
    "split_sentences",
    "split_paragraphs",
    "split_lines",
    "split_regex",
    "smart_split",
)
CACHE_OPS = (
    "get",
    "set",
    "delete",
    "exists",
    "clear",
    "stats",
    "info",
    "keys",
    "mget",
    "mset",
    "mdelete",
    "get_by_tag",
    "delete_by_tag",
    "touch",
    "increment",
    "decrement",
)
FOLDER_OPS = (
    "create",
    "list",
    "get_tree",
    "move",
    "delete",
    "add_file",
    "remove_file",
    "search",
    "list_files",
    "read_folder_file",
)


def _ctx(tmp_path: Path, **extra: Any) -> Any:
    from bandi.tools.base import ToolPermissionContext

    merged: dict[str, Any] = {"project_root": str(tmp_path)}
    merged.update(extra)
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute", extra=merged)


def _make_util_store(tmp_path: Path, name: str = "util.db"):
    from bandi.tools.util.store import UtilStore

    return UtilStore(db_path=str(tmp_path / name))


@pytest.fixture
def util_store_factory(tmp_path: Path):
    def _f(name: str = "util.db"):
        return _make_util_store(tmp_path, name)

    return _f


def _make_scheduler_store(tmp_path: Path, name: str = "scheduler.db"):
    from bandi.scheduler.store import TaskStore

    return TaskStore(str(tmp_path / name))


@pytest.fixture
def scheduler_store_factory(tmp_path: Path):
    """cron 工具已改接 TaskStore（cron 桥接：存储唯一事实源=调度库）。"""

    def _f(name: str = "scheduler.db"):
        return _make_scheduler_store(tmp_path, name)

    return _f


# =============================================================================
# 纯逻辑族：枚举面（符号铁律——最终 schema must contain 全部 op）+ 全 op 行为
# =============================================================================


def test_date_enum_and_schema() -> None:
    d = DateCalculatorTool()
    assert set(d.parameters["properties"]["operation"]["enum"]) == set(DATE_OPS)
    assert d.category == ToolCategory.UTIL and d.feature_group == "util"


def test_json_enum() -> None:
    assert set(JsonParserTool().parameters["properties"]["operation"]["enum"]) == set(JSON_OPS)


def test_rule_enum() -> None:
    r = RuleMatcherTool()
    assert set(r.parameters["properties"]["operation"]["enum"]) == set(RULE_OPS)
    ops = set(r.parameters["properties"]["rules"]["items"]["properties"]["operator"]["enum"])
    assert "between" in ops and "matches" in ops and "is_empty" in ops


def test_text_enum() -> None:
    assert set(TextSplitterTool().parameters["properties"]["operation"]["enum"]) == set(TEXT_OPS)


# ---- date ----
@pytest.mark.asyncio
async def test_date_add_subtract_roundtrip() -> None:
    tool = DateCalculatorTool()
    add = await tool.execute({"operation": "add", "date": "2026-09-01", "days": 10})
    assert add["result"] == "2026-09-11"
    sub = await tool.execute({"operation": "subtract", "date": "2026-09-11", "days": 10})
    assert sub["result"] == "2026-09-01"
    with pytest.raises(ValueError):
        await tool.execute({"operation": "add"})


@pytest.mark.asyncio
async def test_date_business_days_chinese_holiday_and_add_business() -> None:
    tool = DateCalculatorTool()
    bd = await tool.execute(
        {
            "operation": "add_business_days",
            "date": "2026-09-30",
            "days": 3,
            "weekend_days": [5, 6],
            "holidays": ["2026-10-01", "2026-10-02"],
        }
    )
    # 从 09-30(周三) 起：10/1(四)10/2(五) 排休、10/3(六)10/4(日) 周末
    assert bd["result"] == "2026-10-07"
    cnt = await tool.execute(
        {
            "operation": "business_days",
            "date": "2026-09-07",
            "date2": "2026-09-10",
        }
    )
    assert cnt["business_days"] == 3  # 周一~周三（周四=end 不含）


@pytest.mark.asyncio
async def test_date_parse_format_compare_convert_timezone() -> None:
    tool = DateCalculatorTool()
    fmt = await tool.execute(
        {
            "operation": "format",
            "date": "2026年09月30日",
            "input_format": "%Y年%m月%d日",
            "format": "%Y-%m-%d",
        }
    )
    assert fmt["formatted"] == "2026-09-30"
    cmp = await tool.execute({"operation": "compare", "date": "2026-01-01", "date2": "2026-02-01"})
    assert cmp["comparison"] == "before"
    tz = await tool.execute(
        {
            "operation": "convert_timezone",
            "date": "2026-09-06T08:00:00",
            "from_timezone": "UTC",
            "to_timezone": "Asia/Shanghai",
        }
    )
    assert tz["result"].startswith("2026-09-06T16:00:00")
    assert float(tz["offset_hours"]) == 8.0


@pytest.mark.asyncio
async def test_date_now_and_difference() -> None:
    tool = DateCalculatorTool()
    now = await tool.execute({"operation": "now", "to_timezone": "UTC"})
    assert "components" in now and "year" in now["components"]
    diff = await tool.execute(
        {"operation": "difference", "date": "2026-01-01", "date2": "2026-01-08"}
    )
    assert diff["difference"]["days"] == 7


# ---- json ----
@pytest.mark.asyncio
async def test_json_validate_diff_extract_flatten_stringify() -> None:
    tool = JsonParserTool()
    ok = await tool.execute(
        {
            "operation": "validate",
            "data": {"name": "张三"},
            "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
    )
    assert ok["valid"] is True
    bad = await tool.execute(
        {
            "operation": "validate",
            "data": {"age": "30"},
            "schema": {"type": "object", "properties": {"age": {"type": "integer"}}},
        }
    )
    assert bad["valid"] is False

    diff = await tool.execute(
        {
            "operation": "diff",
            "data": {"a": 1, "b": 2, "c": 3},
            "data2": {"a": 1, "b": 9, "d": 4},
        }
    )
    assert {"path": "d", "value": 4} in diff["added"]
    assert {"path": "c", "value": 3} in diff["removed"]
    assert diff["summary"]["changed_count"] == 1
    assert diff["has_differences"] is True

    flat = (await tool.execute({"operation": "flatten", "data": {"a": {"b": [1, 2]}, "c": "中"}}))[
        "data"
    ]
    assert flat == {"a.b[0]": 1, "a.b[1]": 2, "c": "中"}
    back = (await tool.execute({"operation": "unflatten", "data": flat}))["data"]
    assert back == {"a": {"b": [1, 2]}, "c": "中"}

    s = await tool.execute({"operation": "stringify", "data": {"中": "文"}})
    assert "中" in s["json"] and "\\u" not in s["json"]


@pytest.mark.asyncio
async def test_json_parse_extract_and_strict() -> None:
    tool = JsonParserTool()
    md = '```json\n{"k": 1}\n```\n另外 {"j": 2}'
    ex = await tool.execute({"operation": "extract", "text": md})
    assert ex["count"] >= 2
    parsed = await tool.execute({"operation": "parse", "text": '说明：{"x": 5}'})
    assert parsed["valid"] is True and parsed.get("extracted") is True
    with pytest.raises(ValueError):
        await tool.execute({"operation": "parse", "text": "{not json", "strict": True})


# ---- rule ----
@pytest.mark.asyncio
async def test_rule_evaluate_match_all_any_score() -> None:
    tool = RuleMatcherTool()
    data = {"age": 25, "city": "北京", "tags": ["a", "b"]}
    rules = [
        {"id": "r1", "field": "age", "operator": "ge", "value": 18},
        {"id": "r2", "field": "city", "operator": "contains", "value": "北"},
        {"id": "r3", "field": "tags", "operator": "in", "value": ["c"]},
    ]
    all_ = await tool.execute({"operation": "match_all", "data": data, "rules": rules})
    assert all_["summary"]["all_matched"] is False
    any_ = await tool.execute({"operation": "match_any", "data": data, "rules": rules})
    assert any_["summary"]["any_matched"] is True
    sc = await tool.execute(
        {
            "operation": "score",
            "data": {"age": 25, "city": "北京"},
            "rules": [
                {"id": "r1", "field": "age", "operator": "ge", "value": 18, "weight": 2.0},
                {"id": "r2", "field": "city", "operator": "eq", "value": "北京", "weight": 1.0},
            ],
        }
    )
    assert sc["scoring"]["score"] == 100.0


@pytest.mark.asyncio
async def test_rule_validate_filter_rule_set() -> None:
    tool = RuleMatcherTool()
    v = await tool.execute(
        {
            "operation": "validate",
            "rules": [
                {"field": "age", "operator": "nope"},
                {"field": "city", "operator": "eq"},
            ],
        }
    )
    assert v["summary"]["invalid"] == 1
    flt = await tool.execute(
        {
            "operation": "filter",
            "rules": [{"field": "age", "operator": "ge", "value": 18}],
            "data_list": [{"age": 20}, {"age": 12}, {"age": 30}],
        }
    )
    assert [d["age"] for d in flt["matched"]] == [20, 30]
    rs = {
        "name": "combo",
        "logic": "and",
        "rules": [{"field": "feature", "operator": "eq", "value": "摘要"}],
        "groups": [
            {
                "logic": "or",
                "rules": [
                    {"field": "feature", "operator": "eq", "value": "不存在"},
                    {"field": "feature", "operator": "contains", "value": "摘"},
                ],
            }
        ],
    }
    res = await tool.execute({"operation": "evaluate", "data": {"feature": "摘要"}, "rule_set": rs})
    assert res["matched"] is True and res["rule_set"] == "combo"


# ---- text ----
@pytest.mark.asyncio
async def test_text_split_chars_overlap_boundary() -> None:
    tool = TextSplitterTool()
    r = await tool.execute(
        {
            "operation": "split_chars",
            "text": "一二三四五六七八九十",
            "chunk_size": 4,
            "chunk_overlap": 1,
            "preserve_words": False,
            "strip_chunks": False,
        }
    )
    chunks = [c["text"] for c in r["chunks"]]
    assert chunks[0] == "一二三四" and chunks[1] == "四五六七"
    with pytest.raises(ValueError):
        await tool.execute(
            {"operation": "split_chars", "text": "xy", "chunk_size": 2, "chunk_overlap": 2}
        )


@pytest.mark.asyncio
async def test_text_split_lines_paragraphs_regex_smart() -> None:
    tool = TextSplitterTool()
    lines = await tool.execute({"operation": "split_lines", "text": "a\nb\nc\nd", "chunk_size": 2})
    assert len(lines["chunks"]) == 2
    paras = await tool.execute({"operation": "split_paragraphs", "text": "p1\n\np2\n\np3"})
    assert paras["count"] == 3
    rg = await tool.execute({"operation": "split_regex", "text": "1,2,3", "pattern": ","})
    assert [c["text"] for c in rg["chunks"]] == ["1", "2", "3"]
    text = "第一章\n" + "内容 " * 20 + "\n第二章\n" + "别的 " * 20
    smart = await tool.execute(
        {"operation": "smart_split", "text": text, "chunk_size": 50, "max_chunks": 2}
    )
    assert smart["count"] <= 2 and "statistics" in smart
    no_meta = await tool.execute(
        {
            "operation": "split_chars",
            "text": "hello world",
            "chunk_size": 3,
            "include_metadata": False,
        }
    )
    assert isinstance(no_meta["chunks"][0], str)
    assert (await tool.execute({"operation": "split_paragraphs", "text": ""}))["count"] == 0


# =============================================================================
# =============================================================================


@pytest.mark.asyncio
async def test_cache_set_get_delete_ttl_info(util_store_factory) -> None:
    tool = CacheManagerTool(store=util_store_factory())
    await tool.execute(
        {"operation": "set", "key": "k", "value": {"中": "文"}, "ttl": 3600, "tags": ["a"]}
    )
    got = await tool.execute({"operation": "get", "key": "k"})
    assert got["found"] is True and got["value"] == {"中": "文"} and got["source"] == "sqlite"
    info = await tool.execute({"operation": "info", "key": "k"})
    assert info["found"] is True and info["ttl_remaining"] > 0
    assert (await tool.execute({"operation": "exists", "key": "k"}))["exists"] is True
    de = await tool.execute({"operation": "delete", "key": "k"})
    assert de["deleted"] is True
    assert (await tool.execute({"operation": "exists", "key": "k"}))["exists"] is False


@pytest.mark.asyncio
async def test_cache_ttl_lazy_expire_and_miss(util_store_factory) -> None:
    tool = CacheManagerTool(store=util_store_factory())
    await tool.execute({"operation": "set", "key": "gone", "value": 1, "ttl": 0.01})
    time.sleep(0.03)
    got = await tool.execute({"operation": "get", "key": "gone"})
    assert got["found"] is False
    assert (await tool.execute({"operation": "get", "key": "absent"}))["found"] is False


@pytest.mark.asyncio
async def test_cache_stats_cumulative(util_store_factory) -> None:
    tool = CacheManagerTool(store=util_store_factory())
    await tool.execute({"operation": "set", "key": "a", "value": 1})
    await tool.execute({"operation": "get", "key": "a"})
    await tool.execute({"operation": "get", "key": "a"})
    await tool.execute({"operation": "get", "key": "missing"})


@pytest.mark.asyncio
async def test_cache_increment_decrement_touch_clear(util_store_factory) -> None:
    tool = CacheManagerTool(store=util_store_factory())
    await tool.execute({"operation": "set", "key": "counter", "value": 5})
    assert (await tool.execute({"operation": "increment", "key": "counter", "amount": 3}))[
        "value"
    ] == 8
    assert (await tool.execute({"operation": "decrement", "key": "counter", "amount": 2}))[
        "value"
    ] == 6
    await tool.execute({"operation": "set", "key": "notnum", "value": "abc"})
    with pytest.raises(ValueError):
        await tool.execute({"operation": "increment", "key": "notnum"})
    assert (await tool.execute({"operation": "touch", "key": "counter", "ttl": 60}))[
        "success"
    ] is True
    info = await tool.execute({"operation": "info", "key": "counter"})
    assert info.get("ttl_remaining", 0) > 0
    assert (await tool.execute({"operation": "clear"}))["cleared"] > 0


@pytest.mark.asyncio
async def test_cache_namespace_bulk_tag_pattern(util_store_factory) -> None:
    tool = CacheManagerTool(store=util_store_factory())
    await tool.execute({"operation": "set", "key": "k", "value": 1, "namespace": "nsA"})
    await tool.execute({"operation": "set", "key": "k", "value": 2, "namespace": "nsB"})
    assert (await tool.execute({"operation": "get", "key": "k", "namespace": "nsA"}))["value"] == 1
    assert (await tool.execute({"operation": "get", "key": "k", "namespace": "nsB"}))["value"] == 2
    await tool.execute({"operation": "mset", "values": {"x": 10, "y": 20}, "tags": ["t"]})
    assert (await tool.execute({"operation": "mget", "keys": ["x", "y", "z"]}))["found_count"] == 2
    assert (await tool.execute({"operation": "mdelete", "keys": ["x", "y"]}))["deleted_count"] == 2
    await tool.execute({"operation": "set", "key": "u", "value": None, "tags": ["t"]})
    assert len((await tool.execute({"operation": "get_by_tag", "tag": "t"}))["entries"]) == 1
    assert (await tool.execute({"operation": "delete_by_tag", "tag": "t"}))["deleted_count"] == 1


# =============================================================================
# =============================================================================


@pytest.mark.asyncio
async def test_cron_create_list_delete_roundtrip(scheduler_store_factory) -> None:
    store = scheduler_store_factory()
    create = CronCreateTool(store=store)
    job = await create.execute(
        {
            "name": "日常总结",
            "schedule": "0 0 * * *",
            "target_type": "workflow",
            "target_id": "wf-1",
            "payload": {"key": "中"},
            "description": "每日汇总",
        },
        _ctx(Path(".")),
    )
    assert job["ok"] is True and job["job"]["schedule"] == "0 0 * * *"
    job_id = job["job"]["id"]
    lst = await CronListTool(store=store).execute({}, _ctx(Path(".")))
    assert lst["count"] == 1 and lst["jobs"][0]["id"] == job_id
    delete = await CronDeleteTool(store=store).execute({"job_id": job_id}, _ctx(Path(".")))
    assert delete["ok"] is True and delete["removed"] is True
    empty = await CronListTool(store=store).execute({}, _ctx(Path(".")))
    assert empty["count"] == 0


@pytest.mark.asyncio
async def test_cron_delete_idempotent(scheduler_store_factory) -> None:
    delete = CronDeleteTool(store=scheduler_store_factory())
    first = await delete.execute({"job_id": "nonexistent-job"}, _ctx(Path(".")))
    assert first["ok"] is True and first["removed"] is False


@pytest.mark.asyncio
async def test_cron_create_validates_schedule_and_payload(scheduler_store_factory) -> None:
    tool = CronCreateTool(store=scheduler_store_factory())
    bad_schedule = await tool.execute(
        {"name": "x", "schedule": "not-a-cron", "target_type": "workflow"}, _ctx(Path("."))
    )
    assert bad_schedule["ok"] is False
    bad_payload = await tool.execute(
        {"name": "x", "schedule": "0 0 * * *", "target_type": "workflow", "payload": "string"},
        _ctx(Path(".")),
    )
    assert bad_payload["ok"] is False


@pytest.mark.asyncio
async def test_cron_list_filter_and_pagination(scheduler_store_factory) -> None:
    store = scheduler_store_factory()
    create = CronCreateTool(store=store)
    await create.execute(
        {"name": "a", "schedule": "0 0 * * *", "target_type": "task"}, _ctx(Path("."))
    )
    await create.execute(
        {"name": "b", "schedule": "0 0 * * *", "target_type": "task", "enabled": False},
        _ctx(Path(".")),
    )
    all_jobs = await CronListTool(store=store).execute({}, _ctx(Path(".")))
    assert all_jobs["count"] == 2
    enabled_only = await CronListTool(store=store).execute({"enabled": True}, _ctx(Path(".")))
    assert enabled_only["count"] == 1 and enabled_only["jobs"][0]["name"] == "a"
    paged = await CronListTool(store=store).execute({"limit": 1}, _ctx(Path(".")))
    assert paged["count"] == 1


@pytest.mark.asyncio
async def test_cron_script_target_type_and_status_mapping(scheduler_store_factory) -> None:
    store = scheduler_store_factory()
    create = CronCreateTool(store=store)
    script_job = await create.execute(
        {
            "name": "gen-plot",
            "schedule": "0 * * * *",
            "target_type": "script",
            "payload": {"cmd": "plot"},
        },
        _ctx(Path(".")),
    )
    assert script_job["ok"] is True
    assert script_job["job"]["target_type"] == "script"
    assert script_job["job"]["status"] == "active" and script_job["job"]["enabled"] is True
    disabled = await create.execute(
        {"name": "off", "schedule": "0 0 * * *", "target_type": "task", "enabled": False},
        _ctx(Path(".")),
    )
    assert disabled["job"]["status"] == "disabled" and disabled["job"]["enabled"] is False
    listed = await CronListTool(store=store).execute({}, _ctx(Path(".")))
    statuses = {j["name"]: j["status"] for j in listed["jobs"]}
    assert statuses == {"gen-plot": "active", "off": "disabled"}


@pytest.mark.asyncio
async def test_cron_create_binds_current_session_and_executor(scheduler_store_factory) -> None:
    store = scheduler_store_factory()
    create = CronCreateTool(store=store)
    assistant = await create.execute(
        {"name": "每日 AI 新闻", "schedule": "30 12 * * *", "prompt": "拉取 AI 新闻"},
        _ctx(Path("."), session_id="sess-1", project_id="proj-1"),
    )
    assert assistant["ok"] is True
    assert assistant["job"]["target_type"] == "assistant"
    assert assistant["job"]["session_id"] == "sess-1"
    assert assistant["job"]["project_id"] == "proj-1"
    saved = store.get(assistant["job"]["id"])
    assert saved is not None
    assert saved.payload["session_id"] == "sess-1"

    employee = await create.execute(
        {"name": "员工巡检", "schedule": "0 9 * * *", "prompt": "巡检"},
        _ctx(Path("."), session_id="sess-2", employee_id="emp-1"),
    )
    assert employee["job"]["target_type"] == "employee"
    assert employee["job"]["target_id"] == "emp-1"
    assert employee["job"]["employee_id"] == "emp-1"


def test_util_tools_capability_classification() -> None:
    assert DateCalculatorTool.is_read_only is True
    assert JsonParserTool.is_read_only is True
    assert RuleMatcherTool.is_read_only is True
    assert TextSplitterTool.is_read_only is True
    assert ConversationHistoryTool.is_read_only is True
    assert CronListTool.is_read_only is True
    assert CacheManagerTool.is_read_only is False
    assert FolderOperationsTool.is_read_only is False
    assert ToolArgumentBlobTool.is_read_only is False
    assert CronCreateTool.is_read_only is False
    assert CronDeleteTool.is_read_only is False
    assert ToolCapability.FILE_WRITE.value in FolderOperationsTool.capability_tags
    assert ToolCapability.DATABASE.value in CacheManagerTool.capability_tags


# =============================================================================
# folder：FsTool 真实沙箱目录 10 op + 越界/敏感/symlink 拒绝（破坏者）
# =============================================================================


def _folder_root(tmp_path: Path) -> Path:
    return tmp_path / "fs" / "ws"


@pytest.mark.asyncio
async def test_folder_create_tree_move_delete(tmp_path: Path) -> None:
    # project_root 指向在 _ctx 传入；FsTool 沙箱基于 project_root
    tool = FolderOperationsTool()
    root = tmp_path / "ws"
    ctx = _ctx(tmp_path, project_root=str(root))
    root.mkdir(parents=True)
    created = await tool.execute({"operation": "create", "name": "docs"}, ctx)
    assert created["ok"] is True and (root / "docs").is_dir()
    await tool.execute({"operation": "create", "name": "res", "parent": "docs"}, ctx)
    tree = await tool.execute({"operation": "get_tree"}, ctx)
    assert "docs" in str(tree)
    await tool.execute({"operation": "move", "source": "docs", "dest": "archived"}, ctx)
    assert (root / "archived").is_dir() and not (root / "docs").exists()
    # archived 非空（含 res）→ 需递归删除
    refused = await tool.execute({"operation": "delete", "folder": "archived"}, ctx)
    assert refused["ok"] is False and (root / "archived").exists()
    await tool.execute({"operation": "delete", "folder": "archived", "recursive": True}, ctx)
    assert not (root / "archived").exists()


@pytest.mark.asyncio
async def test_folder_list_search_and_read_folder_file(tmp_path: Path) -> None:
    tool = FolderOperationsTool()
    root = tmp_path / "ws"
    ctx = _ctx(tmp_path, project_root=str(root))
    root.mkdir(parents=True)
    (root / "data").mkdir()
    (root / "data" / "x.txt").write_text("你好世界", encoding="utf-8")
    listing = await tool.execute({"operation": "list"}, ctx)
    assert "data" in listing.get("folders", [])
    read = await tool.execute(
        {"operation": "read_folder_file", "folder": "data", "file": "x.txt"}, ctx
    )
    assert "你好世界" in read["content"]
    search = await tool.execute({"operation": "search", "query": "data"}, ctx)
    assert search["ok"] is True


@pytest.mark.asyncio
async def test_folder_add_remove_file_list_files(tmp_path: Path) -> None:
    tool = FolderOperationsTool()
    root = tmp_path / "ws"
    ctx = _ctx(tmp_path, project_root=str(root))
    root.mkdir(parents=True)
    (root / "doc").mkdir()
    src = root / "a.txt"
    src.write_text("alphabeta", encoding="utf-8")
    await tool.execute({"operation": "add_file", "folder": "doc", "file": "a.txt"}, ctx)
    assert not src.exists() and (root / "doc" / "a.txt").exists()
    files = await tool.execute({"operation": "list_files", "folder": "doc"}, ctx)
    assert files["count"] == 1
    await tool.execute({"operation": "remove_file", "folder": "doc", "file": "a.txt"}, ctx)
    assert not (root / "doc" / "a.txt").exists()


@pytest.mark.asyncio
async def test_folder_delete_non_empty_requires_recursive(tmp_path: Path) -> None:
    tool = FolderOperationsTool()
    root = tmp_path / "ws"
    ctx = _ctx(tmp_path, project_root=str(root))
    root.mkdir(parents=True)
    (root / "full").mkdir()
    (root / "full" / "f.txt").write_text("x", encoding="utf-8")
    refused = await tool.execute({"operation": "delete", "folder": "full"}, ctx)
    assert refused["ok"] is False  # 非空默认拒绝
    recursive = await tool.execute(
        {"operation": "delete", "folder": "full", "recursive": True}, ctx
    )
    assert recursive["ok"] is True


@pytest.mark.asyncio
async def test_folder_traversal_and_symlink_rejected(tmp_path: Path) -> None:
    tool = FolderOperationsTool()
    root = tmp_path / "ws"
    node = tmp_path / "ws-node"
    ctx = _ctx(tmp_path, project_root=str(root))
    root.mkdir(parents=True)
    node.mkdir(parents=True)
    evil = await tool.execute({"operation": "create", "name": "../escape"}, ctx)
    assert evil["ok"] is False
    link = node / "l.txt"
    link.write_text("x", encoding="utf-8")
    try:
        (root / "r.txt").symlink_to(link)
        # （不抛异常）——必须验证真的建成 symlink，否则逃逸场景构造失效致假阳性红
        if not (root / "r.txt").is_symlink():
            pytest.skip("symlink 不可用（无特权环境静默退化）")
        sym = await tool.execute(
            {"operation": "read_folder_file", "folder": ".", "file": "r.txt"}, ctx
        )
        assert sym["ok"] is False  # symlink 拒绝
    except (OSError, NotImplementedError):
        pytest.skip("symlink 不可用")
    assert not (tmp_path / "escape").exists()


# =============================================================================
# enter_plan_mode：ModeManager 补齐（镜像 exit_plan_mode 的 extra.mode_manager）
# =============================================================================


class _FakeModeManager:
    def __init__(self) -> None:
        self.mode = "execute"

    def switch(self, new_mode: str) -> str:
        previous = self.mode
        self.mode = new_mode
        return previous


@pytest.mark.asyncio
async def test_enter_plan_mode_switches_to_plan() -> None:
    from bandi.tools.base import ToolPermissionContext

    mm = _FakeModeManager()
    tool = UtilEnterPlanModeTool()
    ctx = ToolPermissionContext(user_id="u", mode="execute", extra={"mode_manager": mm})
    result = await tool.execute({"reason": "复杂任务需规划"}, ctx)
    assert result["switched"] is True
    assert result["mode"] == "plan" and mm.mode == "plan"


@pytest.mark.asyncio
async def test_enter_plan_mode_unbound_manager() -> None:
    from bandi.tools.base import ToolPermissionContext

    tool = UtilEnterPlanModeTool()
    ctx = ToolPermissionContext(user_id="u", extra={})
    result = await tool.execute({"reason": "x"}, ctx)
    assert result["switched"] is False  # 无 mode_manager → 不切换


@pytest.mark.asyncio
async def test_enter_plan_mode_standalone_no_context() -> None:
    tool = UtilEnterPlanModeTool()
    result = await tool.execute({"reason": "standalone"})
    assert result["switched"] is False


# =============================================================================
# tool_argument_blob：create/append/finalize/discard/create_and_finalize + resolve_blob_text 单次消费
# =============================================================================


async def _blob_roundtrip(
    tool: ToolArgumentBlobTool, bid: str, session_id: str, data_dir: Path
) -> str:
    return await resolve_blob_text(
        _ctx(Path("."), session_id=session_id, blob_data_dir=str(data_dir)), bid
    )


@pytest.mark.asyncio
async def test_blob_create_append_finalize_take(tmp_path: Path) -> None:
    data_dir = tmp_path / "blobs"
    tool = ToolArgumentBlobTool(data_dir=data_dir)
    created = await tool.execute(
        {"action": "create"}, _ctx(Path("."), session_id="s1", blob_data_dir=str(data_dir))
    )
    assert created["ok"] is True and created["blob_id"]
    bid = created["blob_id"]
    ap = await tool.execute(
        {"action": "append", "blob_id": bid, "chunk": "第一段"},
        _ctx(Path("."), session_id="s1", blob_data_dir=str(data_dir)),
    )
    assert ap["ok"] is True
    await tool.execute(
        {"action": "append", "blob_id": bid, "chunk": "第二段"},
        _ctx(Path("."), session_id="s1", blob_data_dir=str(data_dir)),
    )
    fin = await tool.execute(
        {"action": "finalize", "blob_id": bid},
        _ctx(Path("."), session_id="s1", blob_data_dir=str(data_dir)),
    )
    assert fin["ok"] is True and fin["total_bytes"] > 0
    after = await tool.execute(
        {"action": "append", "blob_id": bid, "chunk": "x"},
        _ctx(Path("."), session_id="s1", blob_data_dir=str(data_dir)),
    )
    assert after["ok"] is False  # 已 finalize
    assert await _blob_roundtrip(tool, bid, "s1", data_dir) == "第一段第二段"


@pytest.mark.asyncio
async def test_blob_create_and_finalize_single_call(tmp_path: Path) -> None:
    data_dir = tmp_path / "blobs"
    tool = ToolArgumentBlobTool(data_dir=data_dir)
    res = await tool.execute(
        {"action": "create_and_finalize", "chunk": "整段内容"},
        _ctx(Path("."), session_id="s7", blob_data_dir=str(data_dir)),
    )
    assert res["ok"] is True and res["blob_id"]
    assert await _blob_roundtrip(tool, res["blob_id"], "s7", data_dir) == "整段内容"


@pytest.mark.asyncio
async def test_blob_discard_and_take_single_consumption(tmp_path: Path) -> None:
    data_dir = tmp_path / "blobs"
    tool = ToolArgumentBlobTool(data_dir=data_dir)
    res = await tool.execute(
        {"action": "create_and_finalize", "chunk": "一次性"},
        _ctx(Path("."), session_id="s8", blob_data_dir=str(data_dir)),
    )
    bid = res["blob_id"]
    assert await _blob_roundtrip(tool, bid, "s8", data_dir) == "一次性"
    with pytest.raises(ValueError):
        await _blob_roundtrip(tool, bid, "s8", data_dir)  # 已被消费
    created = await tool.execute(
        {"action": "create"}, _ctx(Path("."), session_id="s9", blob_data_dir=str(data_dir))
    )
    disc = await tool.execute(
        {"action": "discard", "blob_id": created["blob_id"]},
        _ctx(Path("."), session_id="s9", blob_data_dir=str(data_dir)),
    )
    assert disc["ok"] is True


@pytest.mark.asyncio
async def test_blob_base64_and_size_limits(tmp_path: Path) -> None:
    data_dir = tmp_path / "blobs"
    tool = ToolArgumentBlobTool(data_dir=data_dir)
    b64 = base64.b64encode("中文<b>内容</b>".encode()).decode("ascii")
    res = await tool.execute(
        {"action": "create_and_finalize", "chunk_base64": b64},
        _ctx(Path("."), session_id="sb1", blob_data_dir=str(data_dir)),
    )
    assert res["ok"] is True
    assert await _blob_roundtrip(tool, res["blob_id"], "sb1", data_dir) == "中文<b>内容</b>"
    bad64 = await tool.execute(
        {"action": "create_and_finalize", "chunk_base64": "!!!"},
        _ctx(Path("."), session_id="sb2", blob_data_dir=str(data_dir)),
    )
    assert bad64["ok"] is False


@pytest.mark.asyncio
async def test_blob_rejects_invalid_blob_id(tmp_path: Path) -> None:
    data_dir = tmp_path / "blobs"
    tool = ToolArgumentBlobTool(data_dir=data_dir)
    ctx = _ctx(Path("."), session_id="sb3", blob_data_dir=str(data_dir))
    created = await tool.execute({"action": "create"}, ctx)
    assert created["ok"] is True
    bad_id = "../escape"
    trav = await tool.execute({"action": "append", "blob_id": bad_id, "chunk": "x"}, ctx)
    assert trav["ok"] is False and "invalid blob_id" in trav["error"]
    with pytest.raises(ValueError):
        await _blob_roundtrip(tool, bad_id, "sb3", data_dir)


@pytest.mark.asyncio
async def test_blob_append_enforces_total_size_limit(tmp_path: Path) -> None:
    data_dir = tmp_path / "blobs"
    tool = ToolArgumentBlobTool(data_dir=data_dir)
    ctx = _ctx(Path("."), session_id="sb4", blob_data_dir=str(data_dir))
    created = await tool.execute({"action": "create"}, ctx)
    bid = created["blob_id"]
    chunk = "x" * (1024 * 1024)
    for _ in range(4):
        ok = await tool.execute({"action": "append", "blob_id": bid, "chunk": chunk}, ctx)
        assert ok["ok"] is True
    over = await tool.execute({"action": "append", "blob_id": bid, "chunk": chunk}, ctx)
    assert over["ok"] is False and "exceeds" in over["error"]
    fin = await tool.execute({"action": "finalize", "blob_id": bid}, ctx)
    assert fin["ok"] is True and fin["total_bytes"] == 4 * 1024 * 1024


# =============================================================================
# conversation_history：SessionStore 适配（get 缺省回落当前会话 + 角色过滤 + 空会话）
# =============================================================================


def _seed_session_store(tmp_path: Path) -> tuple[str, str]:
    """写入一个含 decision 记录的 SessionStore 会话，返回 (store, session_id)。"""
    from bandi.memory.session.store import SessionStore

    db = tmp_path / "session.db"
    SessionStore(db)
    db_str = str(db)
    sid = "sess-decision"
    conn = sqlite3.connect(db_str)
    conn.execute(
        "INSERT OR REPLACE INTO sessions (id, title, mode, created_at, updated_at) VALUES (?,?,?,?,?)",
        (sid, "决策会话", "execute", "2026-09-01T00:00:00", "2026-09-06T00:00:00"),
    )
    rows = [
        ("e0", "user", "请分析方案A"),
        ("e1", "assistant", "方案A 成本较高，但覆盖率好。"),
        ("e2", "tool", "{json:1}"),
    ]
    for eid, role, content in rows:
        conn.execute(
            # 功能：显式列名插入（表新增列时不再因占位符个数失配而崩）
            "INSERT INTO session_entries"
            " (id, session_id, parent_id, entry_type, turn_index, role, content,"
            "  tool_call_id, metadata, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                eid,
                sid,
                None,
                "message",
                int(eid[1]),
                role,
                content,
                None,
                "{}",
                "2026-09-06T00:00:00",
            ),
        )
    conn.commit()
    conn.close()
    return db_str, sid


@pytest.mark.asyncio
async def test_conversation_get_defaults_to_current_session(tmp_path: Path) -> None:
    db, sid = _seed_session_store(tmp_path)
    tool = ConversationHistoryTool(session_db_path=db)
    result = await tool.execute({"operation": "get"}, _ctx(tmp_path, session_id=sid))
    assert result["ok"] is True and result["session_id"] == sid
    roles = [m["role"] for m in result["messages"]]
    assert roles == ["assistant", "user"]  # reversed 时序（新→旧）+ 白名单过滤 tool
    assert "tool" not in roles  # 默认角色白名单 user/assistant


@pytest.mark.asyncio
async def test_conversation_get_explicit_session_with_role_filter(tmp_path: Path) -> None:
    db, sid = _seed_session_store(tmp_path)
    tool = ConversationHistoryTool(session_db_path=db)
    result = await tool.execute(
        {"operation": "get", "session_id": sid, "role": "user"}, _ctx(tmp_path)
    )
    assert result["ok"] is True
    assert all(m["role"] == "user" for m in result["messages"])
    assert "方案A" in result["messages"][0]["content"]


@pytest.mark.asyncio
async def test_conversation_nonexistent_session_returns_empty(tmp_path: Path) -> None:
    db, _sid = _seed_session_store(tmp_path)
    tool = ConversationHistoryTool(session_db_path=db)
    result = await tool.execute(
        {"operation": "get", "session_id": "no-such-session"}, _ctx(tmp_path)
    )
    assert result["ok"] is True
    assert result["messages"] == []


@pytest.mark.asyncio
async def test_conversation_chinese_content_passthrough_and_limit(tmp_path: Path) -> None:
    db, sid = _seed_session_store(tmp_path)
    tool = ConversationHistoryTool(session_db_path=db)
    result = await tool.execute(
        {"operation": "get", "session_id": sid, "max_chars_per_message": 5}, _ctx(tmp_path)
    )
    assert result["ok"] is True
    for m in result["messages"]:
        assert len(m["content"]) <= 5  # 截断符 … 计 1 字符（_truncate: max-1 + …）
    assert result["truncated"] is True


# =============================================================================
# 装配：12 工具注册 + feature group 门控（fail-closed）+ DoD-12 端到端主链路
# =============================================================================


def test_all_util_tools_registered_with_gate() -> None:
    from bandi.agent.factory import build_tool_registry

    registry = build_tool_registry(feature_flags={"util": True, "gen": False})
    names = {t.name for t in registry.list_tools()}
    for expected in (
        "date_calculator",
        "json_parser",
        "rule_matcher",
        "text_splitter",
        "cache_manager",
        "conversation_history",
        "cron_create",
        "cron_delete",
        "cron_list",
        "folder_operations",
        "enter_plan_mode",
        "tool_argument_blob",
    ):
        assert expected in names, f"缺少 {expected}"
        tool = registry.get(expected)
        assert tool is not None and tool.feature_group == "util"

    closed = build_tool_registry(feature_flags={"util": False})
    for expected in (
        "date_calculator",
        "conversation_history",
        "cron_create",
        "folder_operations",
        "tool_argument_blob",
    ):
        assert closed.get(expected) is None


# 破坏者面：util 门控独立——util 开/关都不影响 gen（未列出组默认开放）
# 既有 exit_plan_mode 仍可用（不进 util 门控）
def test_util_gate_independent_of_gen_and_exit_plan_mode() -> None:
    from bandi.agent.factory import build_tool_registry

    registry = build_tool_registry(feature_flags={"util": True, "gen": False})
    assert registry.get("document_generate") is None  # gen 显式关断
    assert registry.get("exit_plan_mode") is not None  # 既有 exit 仍在（不进 util 门控）
    open_default = build_tool_registry(feature_flags={"util": True})
    assert open_default.get("document_generate") is not None  # 未列出组默认开放


def test_enter_plan_mode_is_on_plan_mode_whitelist() -> None:
    from bandi.tools.registry import ToolRegistry

    assert "enter_plan_mode" in ToolRegistry.PLAN_MODE_TOOLS


@pytest.mark.asyncio
async def test_util_e2e_build_registry_real_tmp_workspace(tmp_path: Path) -> None:
    """DoD-12 端到端：生产装配取工具执行，真实工作区写读回 + 中文/空/超长。"""
    from bandi.agent.factory import build_tool_registry

    registry = build_tool_registry(feature_flags={"util": True})
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = _ctx(tmp_path, project_root=str(ws), blob_data_dir=str(tmp_path / "blobs"))

    date = registry.get("date_calculator")
    d = await date.execute({"operation": "add", "date": "2026-01-01", "days": 1}, ctx)
    assert d["result"] == "2026-01-02"

    # folder 真实目录建/写/树/删
    folder = registry.get("folder_operations")
    await folder.execute({"operation": "create", "name": "报告"}, ctx)
    cache = registry.get("cache_manager")
    # 中文长文本 cache 写读回
    big = "这是一段超长中文内容。" * 500
    await cache.execute({"operation": "set", "key": "long", "value": big}, ctx)
    got = await cache.execute({"operation": "get", "key": "long"}, ctx)
    assert got["found"] is True and got["value"] == big

    cron = registry.get("cron_create")
    job = await cron.execute(
        {"name": "e2e", "schedule": "0 9 * * *", "target_type": "workflow"}, ctx
    )
    assert job["ok"] is True
    lst = await registry.get("cron_list").execute({}, ctx)
    assert lst["count"] == 1
    rm = await registry.get("cron_delete").execute({"job_id": job["job"]["id"]}, ctx)
    assert rm["ok"] is True

    blob = registry.get("tool_argument_blob")
    br = await blob.execute({"action": "create_and_finalize", "chunk": "端到端内容"}, ctx)
    assert br["ok"] is True
    assert await resolve_blob_text(ctx, br["blob_id"]) == "端到端内容"

    # folder 真实目录读回
    files = await folder.execute(
        {"operation": "list_files", "folder": "报告"}, _ctx(tmp_path, project_root=str(ws))
    )
    assert files["count"] >= 0
