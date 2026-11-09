# 功能：物理删除旧团队记忆（episodic team 分区 + team pending），不碰群/全局/员工/工作/长期记忆
from __future__ import annotations

import shutil

from bandi.memory.episodic import get_shared_episodic
from bandi.memory.staging import default_memory_store_dir


def main() -> None:
    store = get_shared_episodic()
    before = {
        "team": store.count_team_memory(),
        "group": store.count_group_memory(),
        "all": store.count(),
    }
    deleted = store.delete_team_memory()
    pending = default_memory_store_dir() / "pending" / "team_memory"
    shutil.rmtree(pending, ignore_errors=True)
    after = {
        "team": store.count_team_memory(),
        "group": store.count_group_memory(),
        "all": store.count(),
    }
    print({"deleted_team_episodes": deleted, "before": before, "after": after})


if __name__ == "__main__":
    main()
