"""功能：契约 — 落盘 PII 脱敏

钉的行为：
- ``sanitize_for_persist``：五类基础敏感值（sk-* key / Bearer token / 邮箱 / 手机号 /
  IP）落盘前替换为 <PII:TYPE> 占位符；
- 普通代码文本不受影响；
- 重叠命中（Bearer 后接 sk- key）合并替换，产物无敏感残留、无索引错位；
- 幂等：已脱敏文本重复处理不变；
- 落盘接线：ToolExecutor._persist_output 写文件前必过脱敏。
"""

from __future__ import annotations

from pathlib import Path

from bandi.core.message import ToolCall
from bandi.safety.pii_detector import sanitize_for_persist
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class TestSanitizeForPersist:
    def test_five_required_types_masked(self) -> None:
        text = (
            "key=sk-abcdefghij0123456789 "
            "auth: Bearer abcdefghij1234567890 "
            "mail: user@example.com phone: 13812345678 ip: 192.168.1.100"
        )
        out = sanitize_for_persist(text)
        assert "sk-abcdefghij0123456789" not in out
        assert "Bearer abcdefghij1234567890" not in out
        assert "user@example.com" not in out
        assert "13812345678" not in out
        assert "192.168.1.100" not in out
        assert "<PII:" in out

    def test_plain_text_untouched(self) -> None:
        text = "def add(a, b):\n    return a + b\n"
        assert sanitize_for_persist(text) == text

    def test_overlapping_bearer_sk_no_corruption(self) -> None:
        text = "Authorization: Bearer sk-abcdefghij0123456789"
        out = sanitize_for_persist(text)
        assert "sk-abcdefghij0123456789" not in out
        assert "abcdefghij0123456789" not in out

    def test_idempotent(self) -> None:
        once = sanitize_for_persist("mail: user@example.com")
        assert sanitize_for_persist(once) == once


class TestPersistOutputMasked:
    def test_persist_output_writes_masked_content(self, tmp_path: Path) -> None:
        executor = ToolExecutor(registry=ToolRegistry(), temp_dir=tmp_path)
        tc = ToolCall(id="t1", name="bash", arguments={})
        path = executor._persist_output(tc, "token=sk-abcdefghij0123456789 done")
        assert path is not None
        content = Path(path).read_text(encoding="utf-8")
        assert "sk-abcdefghij0123456789" not in content
        assert "<PII:" in content
