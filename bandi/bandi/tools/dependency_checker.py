# 功能：工具依赖检查（DependencyChecker 缺失依赖探测）
from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import time

from bandi.tools.base import ToolDependency

logger = logging.getLogger(__name__)

# 运行期新装依赖（如 pip install 后）永远探测不到
_CACHE_TTL_SECONDS = 60.0


class DependencyChecker:  # 功能：工具依赖检查（binary/python_package/env_var/file 四种类型）
    def __init__(self):
        self._cache: dict[str, tuple[float, bool]] = {}

    def check(self, dep: ToolDependency) -> bool:  # 功能：检查单个依赖是否满足，结果缓存 60 秒
        cache_key = f"{dep.type}:{dep.name}"
        cached = self._cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

        result = self._check_one(dep)
        self._cache[cache_key] = (time.monotonic(), result)
        return result

    def _check_one(self, dep: ToolDependency) -> bool:
        if dep.type == "binary":
            return shutil.which(dep.name) is not None
        if dep.type == "python_package":
            return importlib.util.find_spec(dep.name) is not None
        if dep.type == "env_var":
            return dep.name in os.environ
        if dep.type == "file":
            return os.path.exists(dep.name)
        logger.warning("unknown dependency type: %s", dep.type)
        return False

    def describe_missing(
        self, deps: list[ToolDependency]
    ) -> list[str]:  # 功能：过滤出未满足的依赖并返回人类可读的缺失描述列表
        return [f"  - [{d.type}] {d.name}: {d.help_hint}" for d in deps if not self.check(d)]
