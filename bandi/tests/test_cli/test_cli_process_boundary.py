# 功能：CLI 进程级边界测试批——真子进程（subprocess）跑 `python -m bandi`
# 钉行为族：exit code 族 / stdin 管道 EOF / UTF-8 中文渲染 / 中文 argv / Ctrl+C 中断。
# 进程内 CliRunner 覆盖不到的层：控制台编码、管道关闭、信号传播、真实 stdout 字节。
# 全部 HOME/USERPROFILE 隔离到 tmp_path（不碰真实 ~/.bandi）；全部带 timeout 保护。

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

CLI = [sys.executable, "-m", "bandi"]
# 功能：`-m bandi` 依赖 cwd 解析包——显式固定到包父目录（bandi/）
# 消除调用方 cwd 差异（从仓库根跑 pytest 会撞根目录同名 namespace package → 假红）。
_PKG_PARENT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.heavy


def _env(tmp_path) -> dict[str, str]:
    """隔离 HOME/USERPROFILE（store/secretary 全落 tmp），清 API key。"""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env.pop("BANDI_API_KEY", None)
    return env


def _run(args: list[str], tmp_path, *, timeout: int = 90, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*CLI, *args],
        capture_output=True,
        timeout=timeout,
        env=_env(tmp_path),
        cwd=str(_PKG_PARENT),
        **kwargs,
    )


def test_help_exit_zero_and_utf8_chinese(tmp_path):
    env = _env(tmp_path)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [*CLI, "--help"], capture_output=True, timeout=60, env=env, cwd=str(_PKG_PARENT)
    )
    assert proc.returncode == 0
    stdout = proc.stdout.decode("utf-8", errors="replace")
    assert "数字员工" in stdout, "UTF-8 管道下中文 help 不得 mojibake"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 控制台默认编码 cp936 边界")
def test_default_console_encoding_no_crash(tmp_path):
    # 不设 PYTHONIOENCODING（默认 cp936 locale）——中文 help 不得崩、exit 合规；
    proc = _run(["--help"], tmp_path)
    assert proc.returncode == 0
    assert "UnicodeEncodeError" not in proc.stderr.decode("utf-8", errors="replace")


def test_unknown_command_exit_nonzero(tmp_path):
    proc = _run(["definitely-not-a-command"], tmp_path)
    assert proc.returncode != 0, "未知命令必须 fail-closed 非零退出"


def test_unknown_chinese_command_no_unicode_crash(tmp_path):
    # 中文 argv（含非法命令名）→ typer 拒绝，但不许 UnicodeEncodeError 级崩溃
    proc = _run(["不存在的命令"], tmp_path)
    assert proc.returncode != 0
    stderr = proc.stderr.decode("utf-8", errors="replace")
    assert "UnicodeEncodeError" not in stderr


def test_company_kanban_closed_stdin_graceful(tmp_path):
    proc = _run(["company", "kanban"], tmp_path, input=b"")
    assert proc.returncode == 0
    stdout = proc.stdout.decode("utf-8", errors="replace")
    assert "No work items" in stdout


def test_company_unknown_subcommand_exit_nonzero(tmp_path):
    proc = _run(["company", "no-such-subcommand"], tmp_path)
    assert proc.returncode != 0


def test_stdout_redirect_chinese_readback(tmp_path):
    # 真重定向：stdout 写入文件（非管道）→ UTF-8 读回中文保真。
    # 钉：重定向场景下（Rich 非 tty 分支）中文内容不丢、不 mojibake。
    out_file = tmp_path / "help-out.txt"
    env = _env(tmp_path)
    env["PYTHONIOENCODING"] = "utf-8"
    with out_file.open("wb") as fh:
        proc = subprocess.run(
            [*CLI, "--help"],
            stdout=fh,
            stderr=subprocess.PIPE,
            timeout=60,
            env=env,
            cwd=str(_PKG_PARENT),
        )
    assert proc.returncode == 0
    content = out_file.read_text(encoding="utf-8", errors="replace")
    assert "数字员工" in content, "重定向读回必须含中文（不丢字节/不 mojibake）"


def test_long_stdin_pipe_no_crash(tmp_path):
    # 超长 stdin（200KB 中文）喂进 CL → exit 0、无栈崩；
    # 钉：stdin 超大输入不击穿管道读取（进程内 CliRunner 覆盖不到的字节级路径）
    proc = _run(["company", "kanban"], tmp_path, input=("任务点" * 66666).encode("utf-8"))
    assert proc.returncode == 0
    combined = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert "Traceback" not in combined


# ---- Ctrl+C 进程级中断（win32 CTRL_C_EVENT 专有） ----


@pytest.mark.skipif(sys.platform != "win32", reason="CTRL_BREAK_EVENT 为 Windows 语义")
def test_ctrl_c_interrupts_serve_gracefully(tmp_path):
    """真信号打进长驻 serve：进程必须退出（不挂死）且走优雅关闭。

    实现注记：CREATE_NEW_PROCESS_GROUP 创建的组默认屏蔽 CTRL_C_EVENT
    （Windows 经典坑），组内直达的是 CTRL_BREAK_EVENT；uvicorn win32
    监听 SIGINT+SIGBREAK 两者 → CTRL_BREAK 同样触发 graceful shutdown。
    """
    import time

    port = 18923  # 高位冷门端口，避免与本机服务冲突
    env = _env(tmp_path)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [*CLI, "serve", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=str(_PKG_PARENT),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        started = False
        deadline = time.time() + 90
        while True:
            line = proc.stdout.readline()
            if b"Uvicorn running" in line or b"Application startup" in line:
                started = True
                break
            if line == b"" or time.time() > deadline:  # EOF 或超时
                break
        assert started, "serve 未在 90s 内启动（测试前置失败）"

        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        proc.wait(timeout=15)
        output = (proc.stdout.read() or b"").decode("utf-8", errors="replace")
        # 优雅语义：出现 shutting down，且无未捕获栈崩
        assert "Shutting down" in output or proc.returncode == 0, output[-500:]
        assert "Traceback" not in output, "信号中断不得栈崩"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
