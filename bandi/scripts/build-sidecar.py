#!/usr/bin/env python3
"""功能：构建 bandi-sidecar（PyInstaller onedir）。

口径
  父解释器借 venv 的包跑 PyInstaller（本机 venv 解释器 spawn 异常）：
    PYTHONPATH=<repo>/.venv/Lib/site-packages  <parent_python> -m PyInstaller bandi-sidecar.spec
  本脚本自动完成：venv site-packages 解析、PYTHONPATH 注入、distpath/workpath
  指到 ASCII 路径（B-11 约束：原生二进制不落中文路径）。

用法：
  python scripts/build-sidecar.py            # 默认输出 ~/.bandi/sidecar-build/dist/bandi-sidecar
  python scripts/build-sidecar.py --dist-dir <dir>

产物：bandi-sidecar/bandi-sidecar.exe（同目录含 _internal/）。
验收：exe 所在目录整体拷到 ~/.bandi/sidecar/ 或随 electron-builder 打进安装目录；
  双击 exe（或 `bandi-sidecar.exe serve --port 0`）能起 HTTP 服务、stdout 出现
  `BANDI_SIDECAR_READY port=` 行。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]  # bandi/ 仓库根
SPEC = REPO_ROOT / "bandi-sidecar.spec"


def default_ascii_dir() -> Path:
    """B-11：构建产物与工作目录放 ASCII 路径（~/.bandi/sidecar-build）。"""
    home = Path(os.environ.get("BANDI_HOME", str(Path.home() / ".bandi")))
    return home / "sidecar-build"


def resolve_venv_site() -> Path | None:
    """功能：定位仓库内 venv 的 site-packages（Windows/Linux 口径）。"""
    venv = REPO_ROOT / ".venv"
    for candidate in (
        venv / "Lib" / "site-packages",
        venv / "lib" / "site-packages",
    ):
        if candidate.is_dir():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 bandi-sidecar")
    parser.add_argument(
        "--dist-dir", default=None, help="产物目录（默认 ~/.bandi/sidecar-build/dist）"
    )
    args = parser.parse_args()

    if not SPEC.is_file():
        print(f"[build] spec 不存在：{SPEC}", file=sys.stderr)
        return 2

    site = resolve_venv_site()
    if site is None:
        print(
            "[build] 未找到 .venv site-packages；请先在 bandi/ 建 venv 并安装依赖", file=sys.stderr
        )
        return 2

    ascii_dir = default_ascii_dir()
    dist_dir = Path(args.dist_dir).expanduser() if args.dist_dir else ascii_dir / "dist"
    work_dir = ascii_dir / "build"

    env = {
        **os.environ,
        "PYTHONPATH": str(site) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    # B-11：PyInstaller 自己也是原生子进程，工作目录放 ASCII 路径
    os.makedirs(ascii_dir, exist_ok=True)

    # 解释器优先级：venv python（干净 sys.path，产物最小）> 父解释器 -S + PYTHONPATH
    # -S：跳过 base 解释器的 site-packages——否则 PyInstaller 会扫进本机全部无关包
    #（paddle/modelscope 等 ML 大件），既拖慢构建（20min+）又可能因 import 探测
    # 崩溃，产物也虚胖数百 MB。
    venv_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if os.name != "nt":
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    interpreter = [sys.executable]
    if venv_python.is_file():
        probe = subprocess.run([str(venv_python), "-c", "pass"], capture_output=True, timeout=15)
        if probe.returncode == 0:
            interpreter = [str(venv_python)]
            env.pop("PYTHONPATH", None)  # venv 自含全部依赖，不混父环境
            print("[build] 解释器 = venv python（干净环境）")
        else:
            print("[build] venv python 不可用，回退父解释器 -S + PYTHONPATH 口径")
            interpreter = [sys.executable, "-S"]
            env["PYTHONPATH"] = (
                str(REPO_ROOT) + os.pathsep + str(site) + os.pathsep + env.get("PYTHONPATH", "")
            )

    cmd = [
        *interpreter,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        str(SPEC),
    ]
    print(f"[build] PyInstaller {pyinstaller_version_label()}")
    print(f"[build] dist = {dist_dir}")
    print(f"[build] work = {work_dir}")
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env)
    if result.returncode != 0:
        print(f"[build] FAILED rc={result.returncode}", file=sys.stderr)
        return result.returncode

    product = (
        dist_dir / "bandi-sidecar" / ("bandi-sidecar.exe" if os.name == "nt" else "bandi-sidecar")
    )
    if product.is_file():
        size_mb = (
            sum(f.stat().st_size for f in (dist_dir / "bandi-sidecar").rglob("*") if f.is_file())
            / 1e6
        )
        print(f"[build] OK：{product}（整体 {size_mb:.1f} MB）")

        # 同步到 electron-builder extraResources 源目录（frontend/build/sidecar/），
        # 与 electron-builder.yml 的 extraResources 配置对齐。
        staging = REPO_ROOT.parent / "frontend" / "build" / "sidecar"
        target = staging / "bandi-sidecar"
        try:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(dist_dir / "bandi-sidecar", target)
            print(f"[build] 已同步到 electron-builder 资源目录：{target}")
        except OSError as exc:
            print(f"[build] 同步到 {target} 失败（不阻断构建）：{exc}", file=sys.stderr)

        print("[build] 后续：bandi-sidecar/ 也可整体拷到 ~/.bandi/sidecar/（外部替换口径）")
        return 0
    print(f"[build] 构建完成但未找到 {product}", file=sys.stderr)
    return 1


def pyinstaller_version_label() -> str:
    try:
        import PyInstaller

        return f"v{PyInstaller.__version__}"
    except ImportError:
        return "（未安装——将由子进程报错）"


if __name__ == "__main__":
    raise SystemExit(main())
