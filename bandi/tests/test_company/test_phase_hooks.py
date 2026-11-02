from __future__ import annotations

from datetime import UTC, datetime

from bandi.company.phase import Phase
from bandi.company.phase_hooks import (
    HookContext,
    HookRegistry,
    refresh_dependents_hook,
    signal_dispatcher_hook,
    sync_task_status_hook,
)


class TestHookRegistry:
    def test_register_and_execute(self):
        registry = HookRegistry()
        calls: list[str] = []

        def my_hook(ctx: HookContext) -> None:
            calls.append(ctx.target_phase.value)

        registry.register("test", my_hook)
        ctx = HookContext(
            work_item_id="1",
            source_phase=Phase.QUEUED,
            target_phase=Phase.READY,
            reason="",
            timestamp=datetime.now(UTC),
        )
        registry.execute("test", ctx)
        assert calls == ["ready"]

    def test_unregister(self):
        registry = HookRegistry()
        calls: list[str] = []

        def my_hook(ctx: HookContext) -> None:
            calls.append("called")

        registry.register("test", my_hook)
        registry.unregister("test", my_hook)
        ctx = HookContext(
            work_item_id="1",
            source_phase=Phase.QUEUED,
            target_phase=Phase.READY,
            reason="",
            timestamp=datetime.now(UTC),
        )
        registry.execute("test", ctx)
        assert calls == []

    def test_execute_nonexistent_group(self):
        registry = HookRegistry()
        ctx = HookContext(
            work_item_id="1",
            source_phase=Phase.QUEUED,
            target_phase=Phase.READY,
            reason="",
            timestamp=datetime.now(UTC),
        )
        registry.execute("nonexistent", ctx)

    def test_built_in_hooks(self):
        registry = HookRegistry()
        registry.register("sync_task_status", sync_task_status_hook)
        registry.register("signal_dispatcher", signal_dispatcher_hook)
        registry.register("refresh_dependents", refresh_dependents_hook)

        ctx = HookContext(
            work_item_id="1",
            source_phase=Phase.QUEUED,
            target_phase=Phase.READY,
            reason="",
            timestamp=datetime.now(UTC),
        )
        registry.execute("sync_task_status", ctx)
        registry.execute("signal_dispatcher", ctx)
        registry.execute("refresh_dependents", ctx)

    def test_multiple_hooks_same_group(self):
        registry = HookRegistry()
        results: list[str] = []

        def hook_a(ctx: HookContext) -> None:
            results.append("a")

        def hook_b(ctx: HookContext) -> None:
            results.append("b")

        registry.register("group", hook_a)
        registry.register("group", hook_b)
        ctx = HookContext(
            work_item_id="1",
            source_phase=Phase.QUEUED,
            target_phase=Phase.READY,
            reason="",
            timestamp=datetime.now(UTC),
        )
        registry.execute("group", ctx)
        assert results == ["a", "b"]

    def test_hook_context_extra(self):
        ctx = HookContext(
            work_item_id="1",
            source_phase=Phase.QUEUED,
            target_phase=Phase.READY,
            reason="test",
            timestamp=datetime.now(UTC),
            extra={"user": "alice"},
        )
        assert ctx.reason == "test"
        assert ctx.extra == {"user": "alice"}


class TestPhaseHooksAdvanced:
    def setup_method(self) -> None:
        from bandi.company import phase_hooks

        phase_hooks._session_focus.clear()
        phase_hooks._focus_start.clear()
        phase_hooks._done_ids.clear()
        phase_hooks._serial_queues.clear()
        phase_hooks._DISPATCHER_WAKE_HOOKS.clear()

    def test_signal_dispatcher_wake_called(self):
        from bandi.company.phase_hooks import register_dispatcher_wake

        woke: list[bool] = []

        def wake() -> None:
            woke.append(True)

        register_dispatcher_wake(wake)
        ctx = HookContext("w1", Phase.QUEUED, Phase.READY, "", datetime.now(UTC))
        signal_dispatcher_hook(ctx)
        assert woke

    def test_sync_task_status_logs_completion(self):
        ctx = HookContext("w1", Phase.RUNNING, Phase.APPROVED, "done", datetime.now(UTC))
        sync_task_status_hook(ctx)

    def test_clear_session_focus_on_done(self):
        from bandi.company.phase_hooks import (
            clear_session_focus_on_terminal_hook,
            get_session_focus,
            set_session_focus,
        )

        set_session_focus("sess1", "w1")
        assert get_session_focus() == {"sess1": "w1"}
        ctx = HookContext(
            "w1",
            Phase.RUNNING,
            Phase.APPROVED,
            "done",
            datetime.now(UTC),
            extra={"session_id": "sess1"},
        )
        clear_session_focus_on_terminal_hook(ctx)
        assert "sess1" not in get_session_focus()

    def test_clear_session_focus_skips_non_done(self):
        from bandi.company.phase_hooks import (
            clear_session_focus_on_terminal_hook,
            get_session_focus,
            set_session_focus,
        )

        set_session_focus("sess1", "w1")
        ctx = HookContext("w1", Phase.QUEUED, Phase.READY, "", datetime.now(UTC))
        clear_session_focus_on_terminal_hook(ctx)
        assert "sess1" in get_session_focus()

    def test_enqueue_when_session_busy(self):
        from bandi.company.phase_hooks import (
            enqueue_session_work_on_runnable_hook,
            get_serial_queue,
            set_session_focus,
        )

        set_session_focus("sess1", "w1")
        ctx = HookContext(
            "w2",
            Phase.QUEUED,
            Phase.READY,
            "",
            datetime.now(UTC),
            extra={"session_id": "sess1", "role_key": "dev"},
        )
        enqueue_session_work_on_runnable_hook(ctx)
        queue = get_serial_queue("dev")
        assert "w2" in queue

    def test_enqueue_skips_when_no_session_focus(self):
        from bandi.company.phase_hooks import (
            enqueue_session_work_on_runnable_hook,
            get_serial_queue,
        )

        ctx = HookContext(
            "w2",
            Phase.QUEUED,
            Phase.READY,
            "",
            datetime.now(UTC),
            extra={"session_id": "sess_free", "role_key": "dev"},
        )
        enqueue_session_work_on_runnable_hook(ctx)
        assert get_serial_queue("dev") == []

    def test_reconcile_serial_queues(self):
        from bandi.company.phase_hooks import (
            enqueue_session_work_on_runnable_hook,
            get_serial_queue,
            reconcile_role_serial_queues,
            set_session_focus,
        )

        set_session_focus("sess1", "w1")
        ctx1 = HookContext(
            "w2",
            Phase.QUEUED,
            Phase.READY,
            "",
            datetime.now(UTC),
            extra={"session_id": "sess1", "role_key": "dev"},
        )
        enqueue_session_work_on_runnable_hook(ctx1)
        assert get_serial_queue("dev")
        reconciled = reconcile_role_serial_queues()
        assert "dev" in reconciled

    def test_get_done_ids_tracks_done_items(self):
        from bandi.company.phase_hooks import (
            _get_done_ids,
            clear_session_focus_on_terminal_hook,
        )

        assert not _get_done_ids()
        ctx = HookContext(
            "w-done",
            Phase.RUNNING,
            Phase.APPROVED,
            "done",
            datetime.now(UTC),
            extra={"session_id": "sess1"},
        )
        clear_session_focus_on_terminal_hook(ctx)
        assert "w-done" in _get_done_ids()

    def test_get_done_ids_skips_non_terminal(self):
        from bandi.company.phase_hooks import (
            _get_done_ids,
            clear_session_focus_on_terminal_hook,
        )

        ctx = HookContext(
            "w-ongoing",
            Phase.QUEUED,
            Phase.READY,
            "",
            datetime.now(UTC),
            extra={"session_id": "sess1"},
        )
        clear_session_focus_on_terminal_hook(ctx)
        assert "w-ongoing" not in _get_done_ids()

    def test_check_stuck_returns_when_fresh(self):
        from bandi.company.phase_hooks import (
            check_stuck_focused_sessions,
            set_session_focus,
        )

        set_session_focus("sess1", "w1")
        stuck = check_stuck_focused_sessions()
        assert stuck == []

    def test_focus_start_cleared_with_focus(self):
        from bandi.company.phase_hooks import (
            clear_session_focus,
            get_session_focus,
            set_session_focus,
        )

        set_session_focus("sess1", "w1")
        assert get_session_focus() == {"sess1": "w1"}
        clear_session_focus("sess1")
        assert "sess1" not in get_session_focus()

    def test_done_ids_bounded(self):
        from bandi.company import phase_hooks
        from bandi.company.phase_hooks import (
            _DONE_IDS_LIMIT,
            clear_session_focus_on_terminal_hook,
        )

        for i in range(_DONE_IDS_LIMIT + 100):
            ctx = HookContext(
                f"w{i}",
                Phase.RUNNING,
                Phase.APPROVED,
                "done",
                datetime.now(UTC),
                extra={"session_id": f"s{i}"},
            )
            clear_session_focus_on_terminal_hook(ctx)
        assert len(phase_hooks._done_ids) <= _DONE_IDS_LIMIT

    def test_concurrent_focus_ops_safe(self):
        import threading

        from bandi.company.phase_hooks import clear_session_focus, set_session_focus

        errors: list[Exception] = []

        def worker(worker_id: int) -> None:
            try:
                for _ in range(50):
                    set_session_focus(f"s{worker_id}", f"w{worker_id}")
                    clear_session_focus(f"s{worker_id}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
