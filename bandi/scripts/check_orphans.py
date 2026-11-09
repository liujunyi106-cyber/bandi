"""门禁：孤儿定义（建了没接线）与跨文件重复常量表（同一语义多套实现）。

判据（零依赖，只读 AST）：
- 孤儿 = 全仓（含 tests / scripts 的引用）零引用、无装饰器注册、未出现在字符串
  （注册表 / getattr / __all__）里的顶层函数与类。
- 重复 = 两个及以上模块各自定义内容完全相同的字符串常量表（≥3 项）。

基线策略：只降不升。新增孤儿或新增重复直接 exit 1；白名单里的是已登记项，
清理掉之后必须同步从白名单删除（否则「清理了却没登记」不会失败，但会留下死条目）。

用法：python scripts/check_orphans.py [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = os.path.join(REPO_ROOT, "bandi")
REF_ROOTS = [os.path.join(REPO_ROOT, "tests"), os.path.join(REPO_ROOT, "scripts")]
SKIP_DIRS = {"__pycache__", ".venv", "node_modules", "tests"}
SPECIAL_NAMES = {"main", "setup", "__init__", "__all__", "__main__"}

# 已登记孤儿：文件:名字 → 原因（清理后请同步删除本行）
ALLOWED_ORPHANS: dict[str, str] = {}

# 已登记同值不同语义的常量表：<a>|<b> → 原因（禁止合并）
ALLOWED_DUP_PAIRS: dict[str, str] = {
    "bandi/tools/gen/checklist_tool.py:_PRIORITIES|bandi/tools/mcp_client.py:_MCP_RISK_LEVELS": (
        "同值不同语义：MCP 风险等级 vs 任务优先级，合并会造成耦合，禁止 DRY"
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


def scan() -> dict[str, object]:
    defs: dict[str, list[tuple[str, int, str, bool]]] = defaultdict(list)
    uses: dict[str, set[str]] = defaultdict(set)
    strings: list[str] = []
    consts: dict[tuple[str, ...], list[tuple[str, int, str]]] = defaultdict(list)
    file_count = 0

    def collect_uses(tree: ast.AST, rel: str) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                uses[node.id].add(rel)
            elif isinstance(node, ast.Attribute):
                uses[node.attr].add(rel)
            elif isinstance(node, ast.ImportFrom):
                # 关键：`from m import foo as bar` 之后只出现 bar，foo 这个原始名
                # 不会以 Name 节点出现——不在此登记会把被别名导入的符号误判成孤儿
                # （2026-11 实测：因此误删过 company/_utils.py 的 as_mapping）。
                for alias in node.names:
                    uses[alias.name].add(rel)
                    if alias.asname:
                        uses[alias.asname].add(rel)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    uses[alias.name.split(".")[-1]].add(rel)
                    if alias.asname:
                        uses[alias.asname].add(rel)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if len(node.value) < 200:
                    strings.append(node.value)

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
                decorated = bool(getattr(node, "decorator_list", None))
                defs[node.name].append((rel, node.lineno, type(node).__name__, decorated))
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and isinstance(
                    node.value, (ast.Tuple, ast.List, ast.Set)
                ):
                    values = [
                        elt.value
                        for elt in node.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    ]
                    if len(values) >= 3 and len(values) == len(node.value.elts):
                        consts[tuple(values)].append((rel, node.lineno, target.id))
        collect_uses(tree, rel)

    for ref_root in REF_ROOTS:
        for path in iter_py(ref_root):
            try:
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (OSError, SyntaxError):
                continue
            collect_uses(tree, "<ref>")

    # 动态引用只认「字符串整串等于符号名」：注册表 / getattr / __all__ 都是整串，
    # 而带路径的长串（例如本脚本的白名单常量）不得把孤儿「引用」掉。
    string_names = {text for text in strings if len(text) < 80}
    orphans: list[dict[str, object]] = []
    for name, entries in defs.items():
        if name.startswith("__") or name in SPECIAL_NAMES:
            continue
        if uses.get(name):
            continue
        if name in string_names:
            continue
        for rel, lineno, kind, decorated in entries:
            if decorated:
                continue
            orphans.append({"key": f"{rel}:{name}", "kind": kind, "line": lineno})

    duplicates: list[dict[str, object]] = []
    for fingerprint, sites in consts.items():
        if len({site[0] for site in sites}) < 2:
            continue
        keys = [f"{rel}:{name}" for rel, _ln, name in sites]
        duplicates.append({"keys": sorted(keys)[:2], "size": len(fingerprint)})

    orphans.sort(key=lambda item: str(item["key"]))
    duplicates.sort(key=lambda item: str(item["keys"]))
    return {"files": file_count, "orphans": orphans, "duplicates": duplicates}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args()

    result = scan()
    orphans = result["orphans"]
    duplicates = result["duplicates"]

    unexpected_orphans = [o for o in orphans if str(o["key"]) not in ALLOWED_ORPHANS]
    unexpected_dups = [
        d for d in duplicates if "|".join(str(k) for k in d["keys"]) not in ALLOWED_DUP_PAIRS
    ]
    stale_orphans = [key for key in ALLOWED_ORPHANS if key not in {str(o["key"]) for o in orphans}]
    stale_dups = [
        key
        for key in ALLOWED_DUP_PAIRS
        if key not in {"|".join(str(k) for k in d["keys"]) for d in duplicates}
    ]

    if args.json:
        print(
            json.dumps(
                {
                    "files": result["files"],
                    "orphans": orphans,
                    "duplicates": duplicates,
                    "unexpected_orphans": unexpected_orphans,
                    "unexpected_duplicates": unexpected_dups,
                    "stale_allowlist": stale_orphans + stale_dups,
                },
                ensure_ascii=False,
                indent=1,
            )
        )
    else:
        print(
            f"scanned={result['files']} orphans={len(orphans)} "
            f"duplicates={len(duplicates)} allowed={len(ALLOWED_ORPHANS)}"
        )
        for item in unexpected_orphans:
            print(f"  NEW ORPHAN {item['key']} ({item['kind']} L{item['line']})")
        for item in unexpected_dups:
            print(f"  NEW DUP {item['keys']} ({item['size']} items)")
        for key in stale_orphans:
            print(f"  STALE ALLOWLIST（已清理，请删白名单条目）: {key}")
        for key in stale_dups:
            print(f"  STALE ALLOWLIST（已清理，请删白名单条目）: {key}")

    if unexpected_orphans or unexpected_dups:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
