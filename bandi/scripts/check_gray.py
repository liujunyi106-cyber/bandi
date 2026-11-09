"""门禁：灰区符号（生产零引用、只有测试撑着）。

判据（零依赖，只读 AST）：
- 候选 = bandi/bandi 下全部 .py 的顶层 def/class，跳过带装饰器的注册项与下划线私有名。
- 生产使用 = 定义所在文件内除定义节点本身以外的出现 + 任何其他生产文件里的出现
  （Name / Attribute / ImportFrom 的 name 与 asname / Import 末段 / 小于 80 字符的字符串常量）。
- 测试与脚本使用 = bandi/tests、bandi/scripts 下同样的登记。
- 灰区 = 生产使用为 0 的公开符号：有测试或脚本引用 = GRAY，无任何引用 = DEAD。

策略：DEAD 一律不允许登记，必须删除；GRAY 只能登记在 GRAY_ALLOWED（键 =
`bandi/bandi/xxx.py:符号名`，值 = 一句裁决理由），未登记的 GRAY 直接 exit 1。

用法：python scripts/check_gray.py [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = os.path.join(REPO_ROOT, "bandi")
REF_ROOTS = [os.path.join(REPO_ROOT, "tests"), os.path.join(REPO_ROOT, "scripts")]
SKIP_DIRS = {"__pycache__", ".venv", "node_modules", "tests"}
STRING_MAX_LEN = 80

# 已登记 GRAY：`bandi/bandi/文件.py:符号名` → 一句裁决理由（符号删除后请同步删本行）
GRAY_ALLOWED: dict[str, str] = {
    "bandi/agent/subagent.py:clear_completed_tasks": (
        "装配点：已完成任务注册表重置，测试 fixture 隔离 FIFO 淘汰与上限，生产注册表由运行期写入"
    ),
    "bandi/agent/subagent.py:get_completed_task": (
        "对外契约：子 Agent 任务注册表单条查询 API，列表接口供 API 层，单条按 id 取结果"
    ),
    "bandi/company/collaboration_policy.py:render_ownership_contract": (
        "对外契约：所有权契约提示段渲染 API，装配层当前只读契约键，测试锁定写范围/协作者目标/空契约"
    ),
    "bandi/company/phase_hooks.py:clear_session_focus": (
        "装配点：会话焦点清理入口，测试重置焦点表以隔离停滞检测"
    ),
    "bandi/company/phase_hooks.py:get_serial_queue": (
        "装配点：串行队列只读访问器，测试断言 runnable hook 的入队顺序与深度"
    ),
    "bandi/company/phase_hooks.py:get_session_focus": (
        "装配点：会话焦点只读访问器，测试断言 hook 维护的焦点表"
    ),
    "bandi/company/phase_hooks.py:reconcile_role_serial_queues": (
        "装配点：串行队列对账入口，按 done 集交叉校正，供队列管理面按需调用，测试锁校正结果"
    ),
    "bandi/company/phase_hooks.py:set_session_focus": (
        "装配点：会话焦点测试装配口，生产由 runnable hook 内联写入，测试预置焦点驱动停滞/清理判定"
    ),
    "bandi/company/project_archive.py:parse_archive_points": (
        "对外契约：归档摘要小标题解析 API，写入端落原文，解析器供归档点读取，测试锁分节/去重/缺节"
    ),
    "bandi/core/permission/builtin/entity_policy.py:EntityScopeCondition": (
        "对外契约：实体范围条件类（Condition 公共扩展点），与 EntityPolicy 成对，测试锁类型匹配与白名单"
    ),
    "bandi/core/permission/roles.py:check_role": (
        "对外契约：内置角色独立检查入口（管理面按角色建引擎），测试锁四档角色放行/ASK 语义"
    ),
    "bandi/knowledge/okf.py:okf_citations_for_concepts": (
        "对外契约：OKF 概念引用构建 API（知识前端概念页），测试锁 (concept,target) 去重与字段"
    ),
    "bandi/llm/cost_tracker.py:reset_shared_cost_tracker": (
        "装配点：进程级成本追踪单例重置，conftest 与计费契约隔离账本"
    ),
    "bandi/llm/profiles.py:reset_store_singleton": (
        "装配点：模型档案单例重置/注入，测试用临时 profile 文件隔离单例"
    ),
    "bandi/memory/long_term.py:reset_scope_strict_misses": (
        "装配点：维度漏传计数重置，测试隔离计数快照"
    ),
    "bandi/memory/long_term.py:scope_strict_misses": (
        "装配点：维度漏传只读观测点（生产消费方是 observability 计数器），测试断言维护类写入不计漏传"
    ),
    "bandi/memory/staging.py:get_skill_pending_store": (
        "装配点：技能暂存区只读观测口（无写入端），测试断言技能链路不写暂存区"
    ),
    "bandi/memory/tbox.py:relation_vocabulary_prompt": (
        "对外契约：记忆关系词表提示词渲染 API，供提示词装配按需注入，测试锁全部谓词与对称/传递标志"
    ),
    "bandi/observability/stdlib_bridge.py:uninstall_logger_bridge": (
        "对外契约：与 install_logger_bridge 配对的卸载 API，测试锁 handler 解绑与幂等"
    ),
    "bandi/observability/write_failures.py:reset_registry": (
        "装配点：写失败注册表重置，conftest 隔离进程级统计"
    ),
    "bandi/safety/denial_tracker.py:reset_denial_tracker": (
        "装配点：拒绝追踪器重置，conftest 隔离拒绝计数"
    ),
    "bandi/safety/write_snapshot.py:restore_snapshot": (
        "对外契约：快照还原配对 API（创建端已接执行链），测试锁按 manifest 回写原路径"
    ),
    "bandi/tools/approval.py:parse_approval_decision": (
        "对外契约：审批回复归一化公共 API（中英文/JSON token），测试锁 allow_once/allow_session/deny/None"
    ),
    "bandi/tools/approval.py:reset_approval_store": (
        "装配点：审批队列单例重置，测试隔离真实用户授权数据"
    ),
    "bandi/tools/web/_robots.py:clear_robots_cache": (
        "装配点：robots 缓存重置，测试隔离同进程缓存"
    ),
}


def iter_py(root: str) -> list[str]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                files.append(os.path.join(dirpath, name))
    return files


def name_hits_string(name: str, strings: set[str]) -> bool:
    """字符串常量里按整词出现即算引用，覆盖 getattr / 注册表这类动态取用。"""
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
    return any(pattern.search(text) for text in strings)


def collect(tree: ast.AST) -> tuple[set[str], set[str]]:
    """返回（名字引用集合, 短字符串常量集合）。"""
    names: set[str] = set()
    strings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) < STRING_MAX_LEN:
                strings.add(node.value)
    return names, strings


def scan() -> dict[str, object]:
    defs: list[tuple[str, str, str, int]] = []
    prod_uses: dict[str, set[str]] = defaultdict(set)
    prod_strings: set[str] = set()
    ref_uses: dict[str, set[str]] = defaultdict(set)
    ref_strings: set[str] = set()
    file_count = 0

    for path in iter_py(SRC_ROOT):
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError):
            continue
        file_count += 1
        rel = os.path.relpath(path, REPO_ROOT).replace("\\", "/")
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if getattr(node, "decorator_list", None):
                    continue
                if node.name.startswith("_"):
                    continue
                defs.append((rel, node.name, type(node).__name__, node.lineno))
        names, strings = collect(tree)
        for name in names:
            prod_uses[name].add(rel)
        prod_strings |= strings

    self_path = os.path.abspath(__file__)
    for ref_root in REF_ROOTS:
        for path in iter_py(ref_root):
            # 本文件的登记键里写着符号名，不能让它反过来把 DEAD 符号「引用」成 GRAY
            if os.path.abspath(path) == self_path:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (OSError, SyntaxError):
                continue
            names, strings = collect(tree)
            rel = os.path.relpath(path, REPO_ROOT).replace("\\", "/")
            for name in names:
                ref_uses[name].add(rel)
            ref_strings |= strings

    gray: list[dict[str, object]] = []
    dead: list[dict[str, object]] = []
    for rel, name, kind, lineno in defs:
        if prod_uses.get(name) or name_hits_string(name, prod_strings):
            continue
        item = {"key": f"{rel}:{name}", "name": name, "kind": kind, "line": lineno}
        if ref_uses.get(name) or name_hits_string(name, ref_strings):
            gray.append(item)
        else:
            dead.append(item)

    gray.sort(key=lambda item: str(item["key"]))
    dead.sort(key=lambda item: str(item["key"]))
    return {"files": file_count, "symbols": len(defs), "gray": gray, "dead": dead}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args()

    result = scan()
    gray = result["gray"]
    dead = result["dead"]
    unregistered = [g for g in gray if str(g["key"]) not in GRAY_ALLOWED]
    stale = [key for key in GRAY_ALLOWED if key not in {str(g["key"]) for g in gray}]

    if args.json:
        print(
            json.dumps(
                {
                    "files": result["files"],
                    "symbols": result["symbols"],
                    "gray": gray,
                    "dead": dead,
                    "allowed": len(GRAY_ALLOWED),
                    "unregistered_gray": unregistered,
                    "stale_allowlist": stale,
                },
                ensure_ascii=False,
                indent=1,
            )
        )
    else:
        print(
            f"scanned={result['files']} symbols={result['symbols']} "
            f"gray={len(gray)} dead={len(dead)} allowed={len(GRAY_ALLOWED)}"
        )
        for item in gray:
            mark = "OK " if str(item["key"]) in GRAY_ALLOWED else "NEW"
            print(f"  {mark} GRAY {item['key']} ({item['kind']} L{item['line']})")
        for item in dead:
            print(f"  DEAD {item['key']} ({item['kind']} L{item['line']})")
        for key in stale:
            print(f"  STALE ALLOWLIST（已不再是 GRAY，请删条目）: {key}")

    if unregistered or dead:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
