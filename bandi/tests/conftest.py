# 全局测试隔离守卫（autouse 重置进程级单例 + 用户目录隔离）。
from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import pytest


def _pin_tempdir_outside_repo() -> None:
    """功能：把测试临时根目录收敛到专用目录——系统 TEMP 失效时 tempfile 会回落到 CWD，
    使 mkdtemp/pytest basetemp 散落进工作区。"""
    repo_root = Path(__file__).resolve().parents[2]
    try:
        active = Path(tempfile.gettempdir()).resolve()
    except OSError:
        active = repo_root
    if active != repo_root and repo_root not in active.parents:
        return
    for candidate in (repo_root.parent / ".bandi-pytest-tmp", repo_root / ".pytest-tmp"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue
        tempfile.tempdir = str(candidate)
        os.environ["TEMP"] = os.environ["TMP"] = str(candidate)
        return


_pin_tempdir_outside_repo()


def _reset_module_containers() -> None:
    from bandi.context.sources.memory_source import (
        _HIERARCHY_LOCK,
        _SEEN_LOCK,
        HIERARCHY_INJECTED_BY_SESSION,
        SEEN_BY_SESSION,
    )

    with _HIERARCHY_LOCK:
        HIERARCHY_INJECTED_BY_SESSION.clear()
    with _SEEN_LOCK:
        SEEN_BY_SESSION.clear()


def _reset_write_failures() -> None:
    from bandi.observability.counters import reset as reset_counters
    from bandi.observability.write_failures import reset_registry

    reset_registry()
    reset_counters()


def _reset_api_overrides() -> None:
    """功能：重置 API 层测试注入单例——反馈目标/重放目录/技能落盘/蒸馏
    四单例一并归零（long_term 可关闭时关闭），防测试间残留把写入引向真实用户库。"""
    app_mod = importlib.import_module("bandi.api.app")
    for attr in ("_memory_feedback_target", "_skill_distill_long_term"):
        target = getattr(app_mod, attr, None)
        closer = getattr(target, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 — 关闭失败不得阻断其余重置
                pass
    app_mod._memory_feedback_target = None
    app_mod._memory_feedback_overrides = None
    app_mod._skill_write_overrides = None
    app_mod._memory_replay_overrides = None
    app_mod._skill_distill_overrides = None
    app_mod._skill_distiller = None
    app_mod._skill_distill_long_term = None
    app_mod._skill_distill_tools = None


@pytest.fixture(autouse=True)
def _reset_process_singletons(tmp_path, monkeypatch, request):
    """每个测试前后重置进程级单例 + 隔离用户目录——忘写的测试也被兜住。

    （tmp + 内存双保险）：
    - 记忆待批准队列默认目录隔离到 tmp（env + reset）；
    - seen 状态持久化路径隔离到 tmp（路径 patch + SEEN 内存清理）——
      防 crossturn 等测试写穿真实 ~/.bandi/seen_memory.json；
      验证「路径随 Path.home 派生」的测试用 `@pytest.mark.bandi_real_seen_path`
      退出该 patch，由测试自己隔离；
    - API 层反馈/重放/技能 override 单例归零。
    """
    from bandi.context.sources import memory_source as memory_source_mod
    from bandi.llm.cost_tracker import reset_shared_cost_tracker
    from bandi.memory import staging as staging_mod
    from bandi.observability.cost_store import reset_shared_cost_stores
    from bandi.safety.denial_tracker import reset_denial_tracker
    from bandi.tools import approval as approval_mod
    from bandi.utils.token_counter import TokenCounter

    monkeypatch.setenv("BANDI_MEMORY_PENDING_DIR", str(tmp_path / "memory-pending"))
    # 技能发现/白名单隔离：宿主 shell 注入的 BANDI_SKILLS_DIRS/ALLOW（运行时装载用）
    # 会把平台技能测试裁空/改向——测试统一从干净缺省起步，用例自行设值时用 monkeypatch
    monkeypatch.delenv("BANDI_SKILLS_DIRS", raising=False)
    monkeypatch.delenv("BANDI_SKILLS_ALLOW", raising=False)
    # 审批单例隔离：expanduser 对 env 不吃 Path.home patch——不隔离会把测试
    # pending 写穿真实 ~/.bandi/approvals.json（泄漏为生产 UI 幽灵审批卡）
    monkeypatch.setenv("BANDI_APPROVAL_STORE_PATH", str(tmp_path / "approvals.json"))
    monkeypatch.setattr(approval_mod, "_STORE", None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if request.node.get_closest_marker("bandi_real_seen_path") is None:
        monkeypatch.setattr(
            memory_source_mod, "_seen_store_path", lambda: tmp_path / "seen_memory.json"
        )
    staging_mod.reset_pending_store()
    reset_shared_cost_tracker()
    reset_shared_cost_stores()
    reset_denial_tracker()
    TokenCounter.set_active_model("")
    TokenCounter._calibrations.clear()
    _reset_module_containers()
    _reset_write_failures()
    _reset_api_overrides()
    yield
    staging_mod.reset_pending_store()
    reset_shared_cost_tracker()
    reset_shared_cost_stores()
    reset_denial_tracker()
    TokenCounter.set_active_model("")
    TokenCounter._calibrations.clear()
    _reset_module_containers()
    _reset_write_failures()
    _reset_api_overrides()


@pytest.fixture(autouse=True, scope="session")
def _disable_models_dev_startup_sync():
    """测试确定性：禁用启动期 models.dev 真实同步（缓存/网络）。

    API/CLI 启动经 trigger_models_dev_sync merge BUILTIN_MODELS，跨测试污染
    目录断言（catalog 两例全量跑红、定向跑绿根因）；测试期统一禁用。
    经 fetcher 注入或直调 sync_models_dev 的专测不走 trigger 入口，不受影响。
    """
    from bandi.llm import models_dev

    original = models_dev.trigger_models_dev_sync
    models_dev.trigger_models_dev_sync = lambda: None
    yield
    models_dev.trigger_models_dev_sync = original
