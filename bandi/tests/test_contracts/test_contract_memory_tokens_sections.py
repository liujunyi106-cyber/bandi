# 功能：契约——memory_section_tokens 计量覆盖层级记忆标题（
# 原 `_MEMORY_SECTION_HEADERS` 只有 runtime 两段，层级记忆（全局/项目/会话/员工）
# 与用户偏好段的注入 token 未计量 ⇒ memory_tokens 口径偏低）。
from bandi.agent.states import memory_section_tokens


def test_hierarchy_section_counted() -> None:
    """契约（核心）：层级记忆段（## 全局记忆）必须计入（红：现状 0）。"""
    msgs = [
        {
            "role": "system",
            "content": (
                "前言内容\n\n"
                "## 全局记忆\n\n[教训|team=a] 失败：rework｜工具：bash｜原因：参数错误\n\n"
                "## 其他无关标题\n\n后面内容不算"
            ),
        }
    ]
    assert memory_section_tokens(msgs) > 0, "层级记忆段未计量"


def test_scoped_sections_each_counted() -> None:
    """契约：四个层级 scope 标题（全局/项目/会话/员工）逐一可计量。"""
    for header in ("## 全局记忆", "## 项目记忆", "## 会话记忆", "## 员工记忆"):
        msgs = [{"role": "system", "content": f"{header}\n\n层级记忆内容ABC\n\n## 下段"}]
        assert memory_section_tokens(msgs) > 0, f"{header} 未计量"


def test_preferences_section_counted() -> None:
    """契约：用户偏好段（## 用户偏好）计入。"""
    msgs = [{"role": "system", "content": "## 用户偏好（直接应用）\n\n偏好内容XYZ\n\n## 下段"}]
    assert memory_section_tokens(msgs) > 0, "用户偏好段未计量"


def test_no_memory_sections_zero() -> None:
    """契约（对照）：无记忆段落的 system 消息计量为 0。"""
    msgs = [{"role": "system", "content": "普通系统提示，无任何记忆段。"}]
    assert memory_section_tokens(msgs) == 0
