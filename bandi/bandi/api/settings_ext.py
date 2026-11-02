# 功能：设置面服务层——系统信息、存储占用、权限档位、保留清理与检索凭据
"""端点只做参数校验与错误映射，业务口径集中在本模块，便于契约直接用真实配置验证。

写入统一走 `BandiConfig.load() → cfg.save(patch=...)`（读-改-写，不把多层合并结果
整盘落盘）；进程内加锁，避免两个请求同时读改写互相覆盖。
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from bandi.core.config import BandiConfig
from bandi.core.permission.rules import parse_permission_rule
from bandi.core.security.secret_protect import is_protected, protect, unprotect

PROCESS_START = time.time()
_PREFS_LOCK = threading.Lock()

# 功能：权限档位预设——只动「询问范围」，deny 底线（deny_tools / deny_commands）永不覆盖
PRESETS: dict[str, dict[str, Any]] = {
    "loose": {
        "ask_risk_levels": ["critical"],
        "ask_tools": ["bash", "write"],
        "bypass_tools": [
            "read",
            "web_search",
            "glob",
            "grep",
            "ontology_query",
            "todo_read",
            "todo_write",
        ],
    },
    "standard": {
        "ask_risk_levels": ["high", "critical"],
        "ask_tools": ["bash", "write", "ontology_define", "ontology_assert", "memory_assert"],
        "bypass_tools": ["read", "web_search", "glob", "grep", "ontology_query"],
    },
    "strict": {
        "ask_risk_levels": ["medium", "high", "critical"],
        "ask_tools": [
            "bash",
            "write",
            "ontology_define",
            "ontology_assert",
            "memory_assert",
            "read",
            "web_search",
            "glob",
            "grep",
        ],
        "bypass_tools": [],
    },
}
RETENTION_CHOICES: tuple[int, ...] = (30, 90, 365)

# 功能：WebSearch 凭据字段 → 环境变量兜底名
WEB_SEARCH_ENV: dict[str, str] = {
    "tavily_api_key": "TAVILY_API_KEY",
    "brave_api_key": "BRAVE_API_KEY",
}


class PreferenceError(ValueError):
    """功能：非法设置项（端点映射 400，不写盘）。"""


def app_version() -> str:
    # 功能：版本号单一来源 = pyproject.toml（与打包产物同源）
    path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        return str(data.get("project", {}).get("version") or "0.0.0")
    except (OSError, tomllib.TOMLDecodeError):
        return "0.0.0"


def _package_version(name: str) -> str:
    # 功能：依赖包版本（缺失返回空串，不抛）
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def system_info(*, data_dir: Path) -> dict[str, Any]:
    # 功能：关于页与排障用的运行环境信息（不含任何密钥/令牌）
    version = app_version()
    return {
        "version": version,
        "python": platform.python_version(),
        "uvicorn": _package_version("uvicorn"),
        "platform": f"{platform.system()} {platform.release()}",
        "data_dir": str(data_dir),
        "uptime_s": int(time.time() - PROCESS_START),
    }


def storage_info(*, data_dir: Path) -> dict[str, Any]:
    # 功能：数据目录占用（各库大小 + 磁盘总量/已用/剩余）
    dbs: list[dict[str, Any]] = []
    if data_dir.exists():
        for item in sorted(data_dir.glob("*.db")):
            try:
                stat = item.stat()
            except OSError:
                continue
            dbs.append(
                {
                    "name": item.name,
                    "bytes": int(stat.st_size),
                    "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                }
            )
    # 功能：目录尚未创建时回溯到存在的祖先，避免 disk_usage 抛 FileNotFoundError
    target = data_dir
    while not target.exists() and target.parent != target:
        target = target.parent
    usage = shutil.disk_usage(str(target))
    return {
        "data_dir": str(data_dir),
        "dbs": dbs,
        "total_bytes": sum(item["bytes"] for item in dbs),
        "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
    }


def current_preferences(cfg: BandiConfig | None = None) -> dict[str, Any]:
    # 功能：当前设置出参（WebSearch 只回是否已配置，永不回明文）
    config = cfg or BandiConfig.load()
    return {
        "permission": {
            "ask_risk_levels": list(config.permissions.ask_risk_levels),
            "ask_tools": list(config.permissions.ask_tools),
            "bypass_tools": list(config.permissions.bypass_tools),
            "deny_tools": list(config.permissions.deny_tools),
            "deny_commands": list(config.permissions.deny_commands),
        },
        "retention_days": int(getattr(config.storage, "retention_days", 90)),
        "web_search": {
            "tavily_configured": bool(resolve_web_search_key(config, "tavily_api_key")),
            "brave_configured": bool(resolve_web_search_key(config, "brave_api_key")),
        },
    }


def preset_payload(preset: str) -> dict[str, Any]:
    # 功能：档位目标值（非法档位 fail-closed）
    if preset not in PRESETS:
        raise PreferenceError(f"unknown permission preset: {preset}")
    policy = PRESETS[preset]
    return {
        "ask_risk_levels": list(policy["ask_risk_levels"]),
        "ask_tools": list(policy["ask_tools"]),
        "bypass_tools": list(policy["bypass_tools"]),
    }


def resolve_web_search_key(cfg: BandiConfig, field: str) -> str:
    # 功能：检索凭据取值——配置优先（自动解密），空则回落环境变量
    raw = str(getattr(cfg.tools.web_search, field, "") or "")
    value = unprotect(raw) if raw else ""
    if value:
        return value
    return os.environ.get(WEB_SEARCH_ENV.get(field, ""), "")


def _protect_if_needed(value: str) -> str:
    # 功能：落盘前加密（已是密文则保持；空串表示清空）
    if not value:
        return ""
    return value if is_protected(value) else protect(value)


def apply_preferences(
    *,
    preset: str | None = None,
    retention_days: int | None = None,
    web_search: dict[str, str] | None = None,
) -> dict[str, Any]:
    """功能：应用设置变更（档位 / 保留天数 / 检索凭据），返回变更后的现状。

    白名单校验在本函数内完成——调用方不得把未知键透传进来。
    """
    patch: dict[str, Any] = {}
    if preset is not None:
        policy = preset_payload(preset)
        patch["permissions"] = policy
    if retention_days is not None:
        if int(retention_days) not in RETENTION_CHOICES:
            raise PreferenceError(f"invalid retention_days: {retention_days}")
        patch["storage"] = {"retention_days": int(retention_days)}
    if web_search is not None:
        unknown = set(web_search) - set(WEB_SEARCH_ENV)
        if unknown:
            raise PreferenceError(f"unknown web_search fields: {sorted(unknown)}")
        patch["tools"] = {
            "web_search": {
                field: _protect_if_needed(str(value or "").strip())
                for field, value in web_search.items()
            }
        }
    if not patch:
        raise PreferenceError("no preference provided")

    with _PREFS_LOCK:
        cfg = BandiConfig.load()
        cfg.save(patch=patch)
    return current_preferences(BandiConfig.load())


def list_permission_rules(cfg: BandiConfig | None = None) -> dict[str, Any]:
    """功能：权限规则列表（行为/工具/模式/来源），供设置页规则表"""
    config = cfg or BandiConfig.load()
    rules: list[dict[str, Any]] = []
    for entry in config.permissions.rules or []:
        parsed = parse_permission_rule(entry, source="user")
        if parsed is None:
            continue
        rules.append(
            {
                "pattern": parsed.pattern,
                "action": parsed.action,
                "kind": parsed.kind,
                "tool": parsed.tool,
                "content": parsed.content,
                "source": parsed.source,
            }
        )
    return {"rules": rules}


def apply_permission_rules(rules: object) -> dict[str, Any]:
    """功能：整表替换用户权限规则；非法条目 fail-closed 400"""
    if not isinstance(rules, list):
        raise PreferenceError("rules must be a list")
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(rules):
        parsed = parse_permission_rule(entry, source="user")
        if parsed is None:
            raise PreferenceError(f"invalid permission rule at index {index}: {entry!r}")
        normalized.append(
            {
                "pattern": parsed.pattern,
                "action": parsed.action,
                "source": "user",
            }
        )
    with _PREFS_LOCK:
        config = BandiConfig.load()
        config.save(patch={"permissions": {"rules": normalized}})
    return list_permission_rules(BandiConfig.load())
