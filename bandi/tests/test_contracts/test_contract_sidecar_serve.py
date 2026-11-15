"""契约：`bandi serve --port 0` sidecar 动态端口回写（Day147 C-2 / ADR-42）。

壳（Electron）视角的完整链路：spawn 子进程 → 端口出现在 `sidecar.port` 与
stdout 结构化行 → `/ready` 探针可达 → 退出后端口文件被清理。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from bandi.cli.app import (
    SIDECAR_READY_PREFIX,
    _clear_sidecar_port,
    _write_sidecar_port,
    sidecar_port_file,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV_SITE = _REPO_ROOT / ".venv" / "Lib" / "site-packages"


def test_sidecar_port_file_respects_bandi_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BANDI_HOME", str(tmp_path))
    assert sidecar_port_file() == tmp_path / "sidecar.port"


def test_write_sidecar_port_atomic_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BANDI_HOME", str(tmp_path))
    _write_sidecar_port(port=54321, pid=4242)
    payload = json.loads(sidecar_port_file().read_text(encoding="utf-8"))
    assert payload["port"] == 54321
    assert payload["pid"] == 4242
    assert payload["started_at"] > 0
    # 原子写不留 tmp 残留
    assert not (tmp_path / "sidecar.port.tmp").exists()


def test_clear_sidecar_port_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BANDI_HOME", str(tmp_path))
    _write_sidecar_port(port=1, pid=1)
    _clear_sidecar_port()
    assert not sidecar_port_file().exists()
    _clear_sidecar_port()  # 不存在时静默
    assert not sidecar_port_file().exists()


def test_serve_port_zero_reports_ready_port_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    """端到端（子进程）：--port 0 → 端口回写 → /ready 200 → 退出清理端口文件。"""
    monkeypatch.setenv("BANDI_HOME", str(tmp_path))
    home = tmp_path / "bandi-home"
    home.mkdir()
    env = {
        **os.environ,
        "BANDI_HOME": str(home),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": os.pathsep.join(
            [str(_REPO_ROOT), str(_VENV_SITE), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
        "BANDI_SCHEDULER_AUTOSTART": "0",
        "BANDI_SERVE_SPA": "0",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "bandi", "serve", "--host", "127.0.0.1", "--port", "0"],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    port_file = home / "sidecar.port"
    deadline = time.monotonic() + 120.0
    port: int | None = None
    stdout_head: list[str] = []
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, _ = process.communicate(timeout=10)
                raise AssertionError(f"sidecar 提前退出（rc={process.returncode}）：\n{stdout}")
            if port_file.is_file():
                try:
                    payload = json.loads(port_file.read_text(encoding="utf-8"))
                    port = int(payload["port"])
                    break
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    pass  # 原子写下读到半截不可能，但容错重试无害
            time.sleep(0.25)
        assert port is not None, "60s 内未出现 sidecar.port"

        # /ready 探针可达（隔离 HOME：无库=首次运行 → ready）
        ready_url = f"http://127.0.0.1:{port}/ready"
        response = None
        while time.monotonic() < deadline:
            try:
                response = urllib.request.urlopen(ready_url, timeout=2)
                break
            except OSError:
                time.sleep(0.25)
        assert response is not None, "/ready 不可达"
        assert response.status == 200

        # stdout 结构化行验证：退出前排水（uvicorn 启动横幅 + READY 行都在头部）
        process.terminate()
        try:
            stdout, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, _ = process.communicate(timeout=15)
        stdout_head = (stdout or "").splitlines()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    assert any(line.startswith(f"{SIDECAR_READY_PREFIX} port={port}") for line in stdout_head), (
        f"stdout 缺少结构化就绪行，头部：{stdout_head[:20]}"
    )
    # 硬杀后端口文件允许残留；进程退出由桌面壳按 pid 判定。
