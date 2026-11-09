# 功能：Computer Use 运行时（解释器检测 / venv 创建 / 依赖安装 / 状态查询）
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.tools.computer_use.safety import screen_geometry

# 功能：运行时依赖（import 名 → pip 包名）
_IMPORT_TO_PACKAGE: dict[str, str] = {
    "mss": "mss",
    "pyautogui": "pyautogui",
    "PIL": "Pillow",
    "pyperclip": "pyperclip",
}
_PIP_PACKAGES: tuple[str, ...] = tuple(_IMPORT_TO_PACKAGE.values())

# 功能：Windows 下子进程不建控制台——否则每次调用都可能把控制台窗口抢到前台
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_PROBE_TIMEOUT = 40.0
_VENV_TIMEOUT = 180.0
_PIP_TIMEOUT = 900.0

_install_task: asyncio.Task[None] | None = None
_install_lock: asyncio.Lock | None = None
_version_cache: dict[str, str] = {}


class ComputerRuntimeError(RuntimeError):
    # 功能：运行时不可用（缺 venv / 缺依赖 / 子进程失败）——调用方按 fail-closed 处理
    pass


def _home() -> Path:
    # 功能：运行时根目录（调用时派生，随 Path.home 隔离）
    return Path.home() / ".bandi" / "runtime"


def _config() -> Any:
    # 功能：读取 computer_use 配置（读盘失败按默认关闭处理）
    try:
        from bandi.core.config import BandiConfig

        return BandiConfig.load().tools.computer_use
    except Exception:  # noqa: BLE001 - 配置坏了不能把工具面放开
        from bandi.core.config import ComputerUseConfig

        return ComputerUseConfig()


def venv_dir() -> Path:
    # 功能：运行时 venv 目录（配置优先，空则默认路径）
    configured = str(getattr(_config(), "venv_dir", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return _home() / "computer-venv"


def state_path() -> Path:
    # 功能：安装状态文件路径
    return _home() / "computer_use_state.json"


def install_log_path() -> Path:
    # 功能：安装日志路径
    return _home() / "computer_use_install.log"


def venv_python(directory: Path | None = None) -> Path:
    # 功能：venv 内解释器路径（Windows Scripts / 其余 bin）
    target = directory or venv_dir()
    if platform.system() == "Windows":
        return target / "Scripts" / "python.exe"
    return target / "bin" / "python"


def helper_script() -> Path:
    # 功能：venv 内执行的桌面操作脚本
    return Path(__file__).with_name("agent_script.py")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {"phase": "idle", "progress": 0, "message": "", "log": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"phase": "idle", "progress": 0, "message": "", "log": []}


def _write_state(**fields: Any) -> dict[str, Any]:
    # 功能：原子写安装状态（失败不抛——状态面坏了也不能阻断安装本身）
    state = _read_state()
    state.update(fields)
    state["updated_at"] = _now()
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return state


def _append_log(lines: list[str], text: str) -> None:
    # 功能：安装日志落盘（最多保留 200 行到状态里，文件全量追加）
    if not text:
        return
    lines.append(text)
    if len(lines) > 200:
        del lines[: len(lines) - 200]
    try:
        path = install_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def _python_version(python: str) -> str:
    # 功能：取解释器版本（进程内缓存，避免设置页轮询反复起子进程）
    if python in _version_cache:
        return _version_cache[python]
    try:
        out = subprocess.run(
            [python, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=_NO_WINDOW,
        )
        version = out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        version = ""
    if version:
        _version_cache[python] = version
    return version


def _candidates() -> list[str]:
    # 功能：解释器候选（配置 → 当前进程 → PATH）去重保序
    configured = str(getattr(_config(), "interpreter", "") or "").strip()
    raw = [configured, sys.executable]
    raw.extend(shutil.which(name) or "" for name in ("python3", "python", "py"))
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        text = (item or "").strip()
        if not text or text in seen:
            continue
        if not Path(text).exists():
            continue
        seen.add(text)
        out.append(text)
    return out


def detect_interpreter() -> dict[str, Any]:
    # 功能：自动检测解释器（返回首选路径与版本，供设置页回显）
    candidates = _candidates()
    if not candidates:
        return {"path": "", "version": "", "source": "none"}
    configured = str(getattr(_config(), "interpreter", "") or "").strip()
    path = candidates[0]
    source = "configured" if configured and path == configured else "auto"
    return {
        "path": path,
        "version": _python_version(path),
        "source": source,
        "candidates": [
            {"path": candidate, "version": _python_version(candidate)} for candidate in candidates
        ],
    }


def _probe(python: str) -> dict[str, Any]:
    # 功能：探测运行时能力（版本 / 依赖 / 屏幕几何 / DPI 感知 / 缩放比）
    script = (
        "import json,sys\n"
        "dpi=False; scale=1.0\n"
        "try:\n"
        "    import ctypes\n"
        "    ctypes.windll.shcore.SetProcessDpiAwareness(2); dpi=True\n"
        "    value=int(ctypes.windll.user32.GetDpiForSystem())\n"
        "    scale=round(max(0.5,min(4.0,value/96.0)),3) if value>0 else 1.0\n"
        "except Exception:\n"
        "    pass\n"
        "modules={}\n"
        "for name in ('mss','pyautogui','PIL','pyperclip'):\n"
        "    try:\n"
        "        __import__(name); modules[name]=True\n"
        "    except Exception:\n"
        "        modules[name]=False\n"
        "info={'version':'%d.%d.%d'%sys.version_info[:3],'modules':modules,"
        "'dpi_aware':dpi,'scale':scale,'coordinate_space':'physical'}\n"
        "try:\n"
        "    import pyautogui; info['screen']={'width':int(pyautogui.size()[0]),"
        "'height':int(pyautogui.size()[1])}\n"
        "except Exception as exc:\n"
        "    info['screen_error']=str(exc)[:200]\n"
        "print(json.dumps(info))\n"
    )
    try:
        out = subprocess.run(
            [python, "-c", script],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if out.returncode != 0:
        return {"ok": False, "error": (out.stderr or out.stdout or "probe failed").strip()[:400]}
    try:
        data = json.loads(out.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": "probe output not json"}
    data["ok"] = True
    return data


def missing_deps(modules: dict[str, bool] | None) -> list[str]:
    # 功能：列出未安装依赖的 pip 包名
    modules = modules or {}
    return [
        package for module, package in _IMPORT_TO_PACKAGE.items() if not bool(modules.get(module))
    ]


def runtime_status() -> dict[str, Any]:
    # 功能：运行时状态快照（设置页/工具面共用；不做安装副作用）
    cfg = _config()
    directory = venv_dir()
    python = venv_python(directory)
    exists = python.exists()
    probe = _probe(str(python)) if exists else {"ok": False, "error": "venv 未创建"}
    modules = probe.get("modules") if isinstance(probe.get("modules"), dict) else {}
    missing = missing_deps(modules) if exists else list(_PIP_PACKAGES)
    installed = bool(exists) and probe.get("ok") and not missing
    state = _read_state()
    return {
        "enabled": bool(getattr(cfg, "enabled", False)),
        "venv_dir": str(directory),
        "venv_exists": exists,
        "python": str(python) if exists else "",
        "python_version": str(probe.get("version") or "") if exists else "",
        "deps_installed": installed,
        "deps": {module: bool(modules.get(module)) for module in _IMPORT_TO_PACKAGE},
        "missing_packages": missing,
        "probe_error": "" if probe.get("ok") else str(probe.get("error") or ""),
        "screen": probe.get("screen") or screen_geometry(),
        "dpi_aware": bool(probe.get("dpi_aware")),
        # 功能：坐标系与缩放比（物理像素为准；逻辑坐标 × scale）
        "coordinate_space": "physical",
        "scale": float(probe.get("scale") or 1.0),
        "interpreter": detect_interpreter(),
        "install": state,
        "safe_mode": not bool(getattr(cfg, "allow_sensitive_windows", False)),
        "platform": platform.system(),
        "helper": str(helper_script()),
    }


def _require_ready() -> tuple[Path, dict[str, Any]]:
    # 功能：执行前门禁——venv 与依赖齐备才允许操作（缺一即 fail-closed）
    status = runtime_status()
    if not status["venv_exists"] or not status["deps_installed"]:
        raise ComputerRuntimeError(
            "运行时未就绪："
            f"venv_exists={status['venv_exists']} deps_installed={status['deps_installed']} "
            f"missing={status['missing_packages']}"
        )
    return Path(status["python"]), status


def run_action(action: str, params: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    # 功能：在运行时 venv 内执行一次桌面操作（子进程 + stdin JSON）
    python, _status = _require_ready()
    payload = json.dumps({"action": action, "params": params}, ensure_ascii=False)
    try:
        out = subprocess.run(
            [str(python), str(helper_script())],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise ComputerRuntimeError(f"桌面操作超时（{timeout:.0f}s）：{action}") from exc
    except OSError as exc:
        raise ComputerRuntimeError(f"无法启动运行时：{exc}") from exc
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip()[:500]
        raise ComputerRuntimeError(f"运行时返回 {out.returncode}：{detail}")
    try:
        result = json.loads((out.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ComputerRuntimeError("运行时输出非 JSON") from exc
    if not result.get("ok"):
        raise ComputerRuntimeError(str(result.get("error") or "操作失败"))
    return result


def _lock() -> asyncio.Lock:
    # 功能：懒创建安装锁（避免模块导入期绑定事件循环）
    global _install_lock
    if _install_lock is None:
        _install_lock = asyncio.Lock()
    return _install_lock


def install_running() -> bool:
    return _install_task is not None and not _install_task.done()


async def start_install(interpreter: str = "") -> dict[str, Any]:
    # 功能：启动后台安装（已在进行则复用，不重复开 venv）
    global _install_task
    if install_running():
        return {"started": False, "already_running": True, "state": _read_state()}
    if interpreter.strip():
        # 功能：解释器选择落配置（下次自动检测优先用它）
        try:
            from bandi.core.config import BandiConfig

            BandiConfig.load().save(
                patch={"tools": {"computer_use": {"interpreter": interpreter.strip()}}}
            )
        except Exception:  # noqa: BLE001 - 配置写失败不阻断安装（本次仍用该解释器）
            pass
    _write_state(
        phase="preparing",
        progress=5,
        message="准备安装环境",
        log=[],
        started_at=_now(),
        finished_at="",
        error="",
    )
    _install_task = asyncio.create_task(_install(interpreter.strip()), name="computer-use-install")
    return {"started": True, "already_running": False, "state": _read_state()}


def _env_without_proxy() -> dict[str, str]:
    # 功能：去掉代理环境变量（pip 的构建隔离子进程会继承 env，仅靠 --proxy 不够）
    blocked = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"}
    return {key: value for key, value in os.environ.items() if key.lower() not in blocked}


def _run_stream_sync(
    cmd: list[str], timeout: float, lines: list[str], env: dict[str, str] | None = None
) -> tuple[int, str]:
    # 功能：同步跑子进程并逐行收日志——以进程句柄退出为准（Windows 上 asyncio 的
    # 管道 EOF 会被孙进程拖住，pip 构建隔离正是这种情况）
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip()
            if text:
                _append_log(lines, text)

    threading.Thread(target=pump, name="computer-use-pump", daemon=True).start()
    try:
        return proc.wait(timeout=timeout), ""
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return 124, f"命令超时（{timeout:.0f}s）：{' '.join(cmd[:3])}"


async def _run_stream(
    cmd: list[str], timeout: float, lines: list[str], env: dict[str, str] | None = None
) -> tuple[int, str]:
    # 功能：异步包装（读日志线程不阻塞事件循环）
    return await asyncio.to_thread(_run_stream_sync, cmd, timeout, lines, env)


async def _install(interpreter: str = "") -> None:
    # 功能：建 venv → 装依赖 → 校验；每步写状态供设置页轮询
    async with _lock():
        lines: list[str] = []
        target = venv_dir()
        python = venv_python(target)
        try:
            base = interpreter or detect_interpreter().get("path") or sys.executable
            _append_log(lines, f"# 解释器: {base}")
            if not Path(base).exists():
                raise ComputerRuntimeError(f"解释器不存在：{base}")

            _write_state(phase="creating_env", progress=15, message="创建独立环境", log=list(lines))
            if not python.exists():
                code, error = await _run_stream(
                    [base, "-m", "venv", str(target)], _VENV_TIMEOUT, lines
                )
                if code != 0 or not python.exists():
                    raise ComputerRuntimeError(error or f"venv 创建失败（退出码 {code}）")
            else:
                _append_log(lines, "# 复用已存在的环境")

            _write_state(phase="installing", progress=40, message="安装依赖包", log=list(lines))
            pip_cmd = [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                # 代理可能已失效：首轮短超时快速失败，避免卡在重试
                "--retries",
                "1",
                "--timeout",
                "10",
                *_PIP_PACKAGES,
            ]
            code, error = await _run_stream(pip_cmd, _PIP_TIMEOUT, lines)
            if code != 0 and any("proxy" in line.lower() for line in lines[-40:]):
                # 功能：环境里的代理不可用（常见于代理软件退出后残留）时改直连重试一次
                _append_log(lines, "# 检测到代理不可用，改用直连重试")
                _write_state(
                    phase="installing",
                    progress=55,
                    message="代理不可用，改直连重试",
                    log=list(lines),
                )
                code, error = await _run_stream(
                    [
                        *pip_cmd[: -len(_PIP_PACKAGES)],
                        "--retries",
                        "3",
                        "--timeout",
                        "30",
                        "--proxy",
                        "",
                        *_PIP_PACKAGES,
                    ],
                    _PIP_TIMEOUT,
                    lines,
                    env=_env_without_proxy(),
                )
            if code != 0:
                raise ComputerRuntimeError(error or f"依赖安装失败（退出码 {code}）")

            _write_state(phase="verifying", progress=85, message="校验依赖", log=list(lines))
            probe = _probe(str(python))
            missing = missing_deps(
                probe.get("modules") if isinstance(probe.get("modules"), dict) else {}
            )
            if missing:
                raise ComputerRuntimeError(f"依赖校验未通过，仍缺：{missing}")

            _write_state(
                phase="ready",
                progress=100,
                message=f"运行时就绪（Python {probe.get('version') or '?'}）",
                log=list(lines),
                finished_at=_now(),
                error="",
            )
        except Exception as exc:  # noqa: BLE001 - 安装失败必须把原因写进状态
            _append_log(lines, f"# 失败: {type(exc).__name__}: {exc}")
            _write_state(
                phase="failed",
                progress=0,
                message="安装失败",
                log=list(lines),
                finished_at=_now(),
                error=f"{type(exc).__name__}: {exc}",
            )


def reset_install_state() -> None:
    # 功能：清空安装状态与锁（测试隔离 / 重装前重置）
    global _install_task, _install_lock
    _install_task = None
    _install_lock = None
    try:
        state_path().unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "ComputerRuntimeError",
    "detect_interpreter",
    "install_running",
    "reset_install_state",
    "run_action",
    "runtime_status",
    "start_install",
    "venv_dir",
    "venv_python",
]
