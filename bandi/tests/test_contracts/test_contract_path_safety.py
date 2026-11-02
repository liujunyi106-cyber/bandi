# 契约原则：任何 project_id 派生目录必须在 Windows 可创建且不逃逸——保留名/
# 尾随点空格/穿越/分隔符/前缀点 全部拒绝或回退，绝不崩溃。
from __future__ import annotations

from bandi.memory.memory_manager import _safe_project_segment

# 拒绝集合（返回 None = 回退默认 store，绝不崩溃）
_REJECTED = [
    "..",
    "../escape",
    "a/../../evil",
    ".hidden",
    "CON",
    "NUL",
    "PRN",
    "AUX",
    "COM1",
    "COM9",
    "LPT1",
    "LPT9",
    "con",
    "nul",
    "proj.",
    "proj ",
]


def test_contract_rejected_project_ids_return_none() -> None:
    """契约 1：不安全 project_id 必须返回 None（回退），绝不生成可崩溃目录段。"""
    for pid in _REJECTED:
        assert _safe_project_segment(pid) is None, f"{pid!r} 必须拒绝（返回 None）"


def test_contract_safe_project_ids_pass_through() -> None:
    """契约 2：安全 project_id 直用（不替换不截断）。"""
    assert _safe_project_segment("proj-a_1") == "proj-a_1"


def test_contract_chinese_replaced_with_underscore() -> None:
    """契约 3：中文 project_id 替换为下划线（可用），且段长度 ≤ 64。"""
    seg = _safe_project_segment("恒星贸易项目")
    assert seg is not None and "_" in seg
    assert len(seg) <= 64


def test_contract_segment_never_contains_separator() -> None:
    """契约 4：派生段永不含路径分隔符（防穿越）。"""
    for pid in ("项目/a", "项目\\a", "a..b", "normal"):
        seg = _safe_project_segment(pid)
        if seg is not None:
            assert "/" not in seg and "\\" not in seg, f"{pid!r} 段不得含分隔符"


def test_contract_segment_never_starts_with_dot() -> None:
    """契约 5：派生段永不以 . 开头（防隐藏目录语义）。"""
    for pid in (".hidden", ".", ".."):
        assert _safe_project_segment(pid) is None


def test_contract_segment_no_collision_after_substitution() -> None:
    """
    64 截断前缀相同的不同 project_id 必须派生不同段（防跨项目记忆串写）。"""
    pairs = [
        ("proj..x", "proj__x"),
        ("a.b", "a_b"),
        ("A" * 64 + "x", "A" * 64 + "y"),
    ]
    for p1, p2 in pairs:
        s1 = _safe_project_segment(p1)
        s2 = _safe_project_segment(p2)
        assert s1 is not None and s2 is not None, f"{p1!r}/{p2!r} 不得回退"
        assert s1 != s2, f"{p1!r} 与 {p2!r} 派生段碰撞：{s1!r}"


def test_contract_case_variants_never_collide() -> None:
    """契约 7（批3-5）：大小写不同 project_id 必须派生在大小写不敏感文件系统
    （Windows NTFS）上也不碰撞的段——`ProjA`/`proja` 若只靠大小写区分，NTFS
    归一化到同一物理目录 → 跨项目记忆串写。修复：段统一小写 + 大小写变化
    追加哈希，保证两段 lower 后仍不同。"""
    variants = [
        ("ProjA", "proja"),
        ("ProjectAlpha", "projectalpha"),
        ("MiXeD", "mixed"),
    ]
    for p1, p2 in variants:
        s1 = _safe_project_segment(p1)
        s2 = _safe_project_segment(p2)
        assert s1 is not None and s2 is not None, f"{p1!r}/{p2!r} 不得回退"
        assert s1.lower() != s2.lower(), (
            f"大小写变体 {p1!r}/{p2!r} 在大小写不敏感文件系统上碰撞：{s1!r}/{s2!r}"
        )


def test_contract_case_variant_same_id_is_idempotent() -> None:
    """契约 8（批3-5）：同一 project_id 派生段幂等（含大小写哈希）。"""
    for pid in ("ProjA", "proja", "MiXeD"):
        s1 = _safe_project_segment(pid)
        s2 = _safe_project_segment(pid)
        assert s1 is not None and s1 == s2, f"{pid!r} 派生段必须幂等"
