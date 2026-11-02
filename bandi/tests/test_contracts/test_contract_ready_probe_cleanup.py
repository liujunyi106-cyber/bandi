"""契约（TB-T6-4）：就绪探针的强杀残留清扫。

行为不变式：
  1. 超龄 `.ready_probe.*` 残留（>600s，进程强杀遗留形态）在下次探针时被
     清扫，且探针本身仍正常返回 True。
  2. 破坏者：新鲜探针文件（并发探针的活动文件形态）不得被清扫——否则重演
     并发互删假 503（审查 2 线程 41% / 32 线程 99.97% 假阴性）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def probe_home(tmp_path: Path, monkeypatch: Any) -> Path:
    # 口径与实现一致：探测路径走 BANDI_HOME
    bandi_home = tmp_path / ".bandi"
    monkeypatch.setenv("BANDI_HOME", str(bandi_home))
    base = bandi_home / "data"
    base.mkdir(parents=True)
    return base


def test_stale_probe_swept_and_probe_still_ok(probe_home: Path) -> None:
    from bandi.api.app import _probe_storage_writable

    stale = probe_home / ".ready_probe.stale-残留"
    stale.write_text("old", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    assert _probe_storage_writable() is True
    assert not stale.exists(), "超龄残留必须被清扫（强杀遗留不再累积）"


def test_fresh_probe_file_not_swept(probe_home: Path) -> None:
    from bandi.api.app import _probe_storage_writable

    fresh = probe_home / ".ready_probe.fresh-活动中"
    fresh.write_text("active", encoding="utf-8")
    assert _probe_storage_writable() is True
    assert fresh.exists(), "新鲜文件（并发活动形态）不得被清扫（防互删假 503）"
