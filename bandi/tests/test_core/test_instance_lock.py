# 功能：契约——后端单实例锁必须阻止第二个进程/句柄写同一 ~/.bandi。
from __future__ import annotations

import pytest

from bandi.core.instance_lock import InstanceLock, InstanceLockError


def test_second_lock_fails_until_released(tmp_path) -> None:
    path = tmp_path / "bandi.lock"
    first = InstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(InstanceLockError):
            InstanceLock(path).acquire()
    finally:
        first.release()

    second = InstanceLock(path)
    second.acquire()
    second.release()
