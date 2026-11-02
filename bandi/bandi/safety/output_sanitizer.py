# 功能：输出规范 — 长度截断/Markdown 闭合/AIGC 标识/HTLM 转义（支持 YAML 配置）

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OutputSanitizerConfig:
    """输出净化器配置。"""

    max_chars: int = 8000
    max_lines: int = 500
    sanitize_markdown: bool = True
    escape_html: bool = False
    truncation_message: str = (
        "\n\n[输出过长已被截断。如需完整内容：文件类结果用 read 按 offset/limit 分段读取；"
        "命令类结果缩小输出范围（head/tail/过滤）或提高工具输出上限后重新获取。]"
    )
    # 成为噪音并干扰模型（拒答诱因之一）；需要合规标识时显式配置
    aigc_notice: str = ""

    @classmethod
    def from_config(
        cls, max_chars: int | None = None, max_lines: int | None = None
    ) -> OutputSanitizerConfig:
        """从外部配置创建 OutputSanitizerConfig。"""
        return cls(
            max_chars=max_chars or cls().max_chars,
            max_lines=max_lines or cls().max_lines,
        )


class OutputSanitizer:
    def __init__(self, config: OutputSanitizerConfig | None = None) -> None:
        self._config = config or OutputSanitizerConfig()

    def sanitize(self, text: str | None) -> str:
        """对 Agent 最终输出应用格式规范。"""
        if text is None:
            return ""

        # 0. HTML 转义（在截断和 Markdown 处理之前执行）
        if self._config.escape_html:
            text = self._escape_html(text)

        # 1. 长度截断
        if len(text) > self._config.max_chars:
            text = text[: self._config.max_chars] + self._config.truncation_message

        # 2. 行数截断
        lines = text.split("\n")
        if len(lines) > self._config.max_lines:
            text = "\n".join(lines[: self._config.max_lines]) + self._config.truncation_message

        # 3. Markdown 代码块未闭合检测
        if self._config.sanitize_markdown:
            text = self._close_unclosed_code_blocks(text)

        # 4. AIGC 标识（可配置；默认空=不追加，防多轮上下文噪音）
        notice = self._config.aigc_notice
        if notice:
            tagged = f"\n\n*{notice}*"
            if not text.strip().endswith(tagged.strip()):
                text = f"{text}{tagged}"

        return text

    @staticmethod
    def _close_unclosed_code_blocks(text: str) -> str:
        """用状态机扫描 ``` fence，在末尾补齐未闭合的代码块。"""
        in_fence = False
        i = 0
        while i < len(text):
            if text[i : i + 3] == "```":
                in_fence = not in_fence
                i += 3
                if in_fence:
                    while i < len(text) and text[i] != "\n":
                        i += 1
            else:
                i += 1
        if in_fence:
            text += "\n```"
        return text

    @staticmethod
    def _escape_html(text: str) -> str:
        """转义 HTLM 特殊字符。"""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
