"""契约：图片附件 → 模型视觉输入；模型不支持时降级为文字说明。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bandi.agent.states import _attach_image_blocks
from bandi.core.agent_runner import _format_attachment_notes, _inject_attachment_notes
from bandi.utils.token_counter import TokenCounter

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf"
    b"\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_vision_model_gets_image_block(tmp_path: Path) -> None:
    image = tmp_path / "cat.png"
    image.write_bytes(_PNG)
    ctx = SimpleNamespace(
        meta={},
        provider=SimpleNamespace(supports_vision=True, protocol="openai"),
    )
    _inject_attachment_notes(ctx, [{"name": "cat.png", "path": str(image), "mime": "image/png"}])
    assert ctx.meta["image_attachments"][0]["path"] == str(image)
    messages = [{"role": "user", "content": "看看这张图"}]
    _attach_image_blocks(ctx, messages)
    blocks = messages[0]["content"]
    assert isinstance(blocks, list)
    assert blocks[0] == {"type": "text", "text": "看看这张图"}
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_non_vision_model_gets_explicit_note_not_pixels() -> None:
    note = _format_attachment_notes(
        [{"name": "cat.png", "path": "C:/x/cat.png", "mime": "image/png"}],
        vision=False,
    )
    assert "cat.png" in note
    assert "不支持图像输入" in note
    ctx = SimpleNamespace(
        meta={},
        provider=SimpleNamespace(supports_vision=False, protocol="openai"),
    )
    messages = [{"role": "user", "content": "看看"}]
    _attach_image_blocks(ctx, messages)
    assert messages[0]["content"] == "看看"


def test_image_block_cost_is_bounded() -> None:
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 10000}},
        ],
    }
    assert TokenCounter.count_message(message) < 200
