# -*- mode: python ; coding: utf-8 -*-
# 功能：bandi-sidecar 的 PyInstaller 打包配置（Day147 C-5 / ADR-42）。
#
# 产物：onedir 桌面 sidecar（`bandi-sidecar/bandi-sidecar.exe`），供 Electron 壳
# 在打包态 spawn（electron/sidecar.ts），提供 SPA + API + WS 单端口服务。
#
# 构建口径（Windows 实测，见 交付文档收集 §13）：
#   父解释器 + PYTHONPATH=<repo>/.venv/Lib/site-packages -m PyInstaller 本 spec
#   （venv 解释器本身在本机 spawn 异常，父解释器借 venv 的包跑 PyInstaller）。
# B-11 约束：原生二进制不得留在含中文的路径——distpath/workpath 必须指到
#   ASCII 路径（构建脚本默认 `~/.bandi/sidecar-build/`），运行时 exe 也放
#   `~/.bandi/sidecar/` 或应用安装目录。
#
# hiddenimports：uvicorn 0.51 运行时用 importlib 动态加载 loop/protocol/lifespan
#   组件（字符串拼接，静态分析抓不到），必须显式列出——漏了会在启动时报
#   ModuleNotFoundError 且壳侧表现为 sidecar 起不来。
import glob
import os

from PyInstaller.utils.hooks import collect_data_files

# rich._unicode_data.unicode17-0-0 等版本模块由字符串拼接动态 import_module
# （模块名含连字符，静态分析抓不到），从磁盘显式列出全部版本。
_RICH_UNICODE_DIR = os.path.join(SPECPATH, ".venv", "Lib", "site-packages", "rich", "_unicode_data")
_RICH_UNICODE_MODULES = [
    f"rich._unicode_data.{os.path.splitext(os.path.basename(p))[0]}"
    for p in glob.glob(os.path.join(_RICH_UNICODE_DIR, "unicode*.py"))
]

a = Analysis(
    ["bandi/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[
        # jsonschema_specifications 以 data 目录形式携带内置 schema 集，
        # PyInstaller 静态分析只收 .py 不收数据——启动时 iterdir 找不到目录即崩
        # （2026-11-02 sidecar exe 实测：_internal/jsonschema_specifications/schemas 缺失）。
        *collect_data_files("jsonschema_specifications"),
        # rich._unicode_data 的 unicode17-0-0 数据文件同理（实测启动即 ModuleNotFoundError）
        *collect_data_files("rich"),
        # certifi 的 CA 包：缺了会在建 TLS 连接时抛
        # FileNotFoundError: [Errno 2]（ssl.create_default_context）
        *collect_data_files("certifi"),
    ],
    hiddenimports=[
        # uvicorn 动态加载组件（auto.* 按环境二次分派，全量列入以防派生缺失）
        "uvicorn.loops",
        "uvicorn.loops.asyncio",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        # CLI 与配置
        "bandi.cli.app",
        # rich unicode 版本模块（动态 import，见文件头说明）
        *_RICH_UNICODE_MODULES,
        # bandi 数据工具惰性导入表（bandi/tools/__init__.py _LAZY_DATA_TOOLS，
        # importlib.import_module 字面量——防静态分析漏抓）
        "bandi.tools.data.data_aggregate",
        "bandi.tools.data.data_clean",
        "bandi.tools.data.data_merge",
        "bandi.tools.data.data_transform",
        "bandi.tools.data.data_validate",
        # anyio/httpcore 运行时按字符串拼接选 backend（同 rich 坑）
        "anyio._backends._asyncio",
        "anyio._backends._trio",
        "httpcore._backends.anyio",
        "httpcore._backends.asyncio",
        "httpcore._backends.auto",
        # tiktoken 通过 entry point 发现编码插件，冻结后 entry points 丢失会报
        # 「Unknown encoding cl100k_base. Plugins found: []」，必须显式列入
        "tiktoken_ext",
        "tiktoken_ext.openai_public",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 开发/构建工具不进 sidecar（减体积、防误报）
        "pytest",
        "_pytest",
        "ruff",
        "pip",
        "pip_audit",
        "setuptools",
        "PyInstaller",
        # GUI/无关大件：sidecar 是无界面后台服务
        "tkinter",
        "_tkinter",
        "future",
        "past",
        "curses",
        # 本机 base 环境的 ML 大件（bandi 不依赖；-S 隔离后通常已不可见，双保险）
        "paddle",
        "paddlex",
        "paddleocr",
        "modelscope",
        "torch",
        "transformers",
        "diffusers",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="bandi-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # stdout 管道承载 BANDI_SIDECAR_READY 就绪行，必须保留控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="bandi-sidecar",
)
