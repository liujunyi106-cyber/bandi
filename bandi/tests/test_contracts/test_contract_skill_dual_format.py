# 功能：契约——SkillLoader 双格式兼容（opencode/Claude 风格「frontmatter + 全文」与
# 风格「## Behavior / ## Instructions」并存）。
#
# 钉行为不钉实现：
# - 正文回落：无 ## Instructions 小节时，frontmatter 之后的全文作为 instructions
#   （否则技能注册成功但正文为空 → 命中也注入不出内容）；
# - 触发词多源且显式：## Behavior 小节优先，其次 frontmatter triggers
#   （支持 list 与「分类: 词/词/词」嵌套 dict 两种写法）；
# - 两者皆无时 triggers 为空（**不猜 description 里的自然语言**——猜即误匹配）；
# 格式行为不变（回归护栏，双格式不得互相污染）。
from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.skills.loader import SkillLoader
from bandi.skills.manager import SkillInjector
from bandi.skills.registry import SkillRegistry

OPENCODE_STYLE = """---
name: demo-web
description: 把网页转成干净 markdown
---

# Demo Web

正文第一段：先 npm install -g demo。

## 用法

运行 `demo --md <url>`。
"""

OPENCODE_WITH_TRIGGER_LIST = """---
name: demo-search
description: 检索工具
triggers:
  - 搜索
  - search
---

# Demo Search

正文。
"""

OPENCODE_WITH_NESTED_TRIGGERS = """---
name: demo-reach
description: 多平台检索
triggers:
  search: 搜索/search/查一查
  social:
    - 小红书
    - xiaohongshu
---

# Demo Reach

正文。
"""

OPENCODE_WITH_TRIGGER_LIST_OF_MAPS = """---
name: demo-reach2
description: 多平台检索
triggers:
  - search: 搜索/search/查一查
  - social:
      - 小红书
      - xiaohongshu/xhs
---

# Demo Reach2

正文。
"""

STAFFDECK_STYLE = """---
name: demo-both
description: 双格式技能
---

## Behavior

- 触发词甲
- 触发词乙

## Instructions

只注入这一段。

## Constraints

- 不得越权
"""

NO_TRIGGER_STYLE = """---
name: demo-notrig
description: 没有任何显式触发词
---

# Demo NoTrig

正文。
"""


OPENCODE_WITH_SINGLE_CHAR_TRIGGERS = """---
name: demo-single
description: 触发词里混了单字
triggers:
  search: 搜/查/找/search/搜索
---

# Demo Single

正文。
"""


def _plain_skill(name: str) -> str:
    """功能：构造 opencode 风格技能（frontmatter name + 全文正文）。"""
    return f"---\nname: {name}\ndescription: {name} 描述\n---\n\n# {name}\n\n正文。\n"


def _write(root: Path, name: str, content: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


def _load_one(root: Path, name: str):
    skills = SkillLoader(str(root)).load_all()
    matched = [s for s in skills if s.name == name]
    assert matched, f"未装载技能 {name}（实际装载 {[s.name for s in skills]}）"
    return matched[0]


def test_opencode_style_body_becomes_instructions(tmp_path: Path) -> None:
    # 功能：opencode 风格（frontmatter + 全文）无 ## Instructions 时，全文作为正文
    _write(tmp_path, "demo-web", OPENCODE_STYLE)

    skill = _load_one(tmp_path, "demo-web")

    assert skill.instructions.strip(), "正文不得为空"
    assert "正文第一段" in skill.instructions
    assert "demo --md" in skill.instructions


def test_staffdeck_style_unchanged(tmp_path: Path) -> None:
    # 功能：格式行为不变——只注入 ## Instructions 段，不混入 Behavior/Constraints
    _write(tmp_path, "demo-both", STAFFDECK_STYLE)

    skill = _load_one(tmp_path, "demo-both")

    assert skill.instructions.strip() == "只注入这一段。"
    assert skill.triggers == ["触发词甲", "触发词乙"]
    assert skill.constraints == ["不得越权"]


def test_staffdeck_style_wins_over_frontmatter_triggers(tmp_path: Path) -> None:
    # 功能：## Behavior 与 frontmatter triggers 并存时以小节为准（格式优先级不漂移）
    _write(
        tmp_path,
        "demo-both",
        "---\nname: demo-both\ndescription: 双格式\ntriggers:\n  - 外来触发词\n---\n\n"
        "## Behavior\n\n- 触发词甲\n\n## Instructions\n\n正文。\n",
    )

    skill = _load_one(tmp_path, "demo-both")

    assert skill.triggers == ["触发词甲"], "小节触发词必须优先于 frontmatter"


def test_frontmatter_trigger_list_parsed(tmp_path: Path) -> None:
    # 功能：frontmatter triggers 为字符串列表时可解析
    _write(tmp_path, "demo-search", OPENCODE_WITH_TRIGGER_LIST)

    skill = _load_one(tmp_path, "demo-search")

    assert skill.triggers == ["搜索", "search"]


def test_single_char_triggers_dropped(tmp_path: Path) -> None:
    # 功能：单字触发词必须丢弃——「搜/查/找」拆成单字后几乎命中任何输入
    _write(tmp_path, "demo-single", OPENCODE_WITH_SINGLE_CHAR_TRIGGERS)

    skill = _load_one(tmp_path, "demo-single")

    assert "搜" not in skill.triggers
    assert "查" not in skill.triggers
    assert "找" not in skill.triggers
    assert all(len(t) >= 2 for t in skill.triggers), f"仍有单字触发词：{skill.triggers}"
    assert "搜索" in skill.triggers, "多字触发词必须保留"
    assert "search" in skill.triggers


def test_allow_env_filters_by_skill_name(tmp_path: Path, monkeypatch: Any) -> None:
    # 功能：BANDI_SKILLS_ALLOW 按技能名白名单过滤——目录级装载无法表达「只要某几个」
    _write(tmp_path, "keep-me", _plain_skill("keep-me"))
    _write(tmp_path, "drop-me", _plain_skill("drop-me"))
    monkeypatch.setenv("BANDI_SKILLS_ALLOW", "keep-me")

    names = [s.name for s in SkillLoader(str(tmp_path)).load_all()]

    assert names == ["keep-me"], f"白名单未生效：{names}"


def test_allow_env_blank_means_no_filter(tmp_path: Path, monkeypatch: Any) -> None:
    # 功能：白名单缺省或空白 = 不过滤（保持既有行为）
    _write(tmp_path, "a-skill", _plain_skill("a-skill"))
    _write(tmp_path, "b-skill", _plain_skill("b-skill"))
    monkeypatch.delenv("BANDI_SKILLS_ALLOW", raising=False)

    names = sorted(s.name for s in SkillLoader(str(tmp_path)).load_all())

    assert names == ["a-skill", "b-skill"]

    monkeypatch.setenv("BANDI_SKILLS_ALLOW", "   ")
    names_blank = sorted(s.name for s in SkillLoader(str(tmp_path)).load_all())
    assert names_blank == ["a-skill", "b-skill"], "空白白名单不得等于「全部拒绝」"


def test_allow_env_tolerates_unknown_names(tmp_path: Path, monkeypatch: Any) -> None:
    # 功能：白名单含不存在的技能名时不得报错（宿主配置可先写后装）
    _write(tmp_path, "real-skill", _plain_skill("real-skill"))
    monkeypatch.setenv("BANDI_SKILLS_ALLOW", "real-skill,not-installed-yet")

    names = [s.name for s in SkillLoader(str(tmp_path)).load_all()]

    assert names == ["real-skill"]


def test_frontmatter_trigger_list_of_maps_expanded(tmp_path: Path) -> None:
    # 功能：frontmatter triggers 为「列表套字典」（agent-reach 真实写法）时同样展开
    _write(tmp_path, "demo-reach2", OPENCODE_WITH_TRIGGER_LIST_OF_MAPS)

    skill = _load_one(tmp_path, "demo-reach2")

    for expected in ("搜索", "search", "查一查", "小红书", "xiaohongshu", "xhs"):
        assert expected in skill.triggers, f"缺触发词 {expected}：实际 {skill.triggers}"
    for category in ("search:", "social:"):
        assert not any(t.startswith(category) for t in skill.triggers), "分类名不应混入触发词"


def test_frontmatter_nested_trigger_dict_expanded(tmp_path: Path) -> None:
    # 功能：frontmatter triggers 为「分类: 词/词/词」嵌套字典时展开为独立触发词
    _write(tmp_path, "demo-reach", OPENCODE_WITH_NESTED_TRIGGERS)

    skill = _load_one(tmp_path, "demo-reach")

    for expected in ("搜索", "search", "查一查", "小红书", "xiaohongshu"):
        assert expected in skill.triggers, f"缺触发词 {expected}：实际 {skill.triggers}"
    assert "social" not in skill.triggers, "分类名不应混入触发词"


def test_no_trigger_keeps_body_and_empty_triggers(tmp_path: Path) -> None:
    # 功能：既无 Behavior 小节也无 frontmatter triggers 时——正文可用、触发词为空
    _write(tmp_path, "demo-notrig", NO_TRIGGER_STYLE)

    skill = _load_one(tmp_path, "demo-notrig")

    assert skill.triggers == []
    assert "正文" in skill.instructions


def test_description_is_not_parsed_as_triggers(tmp_path: Path) -> None:
    # 功能：不得从 description 的自然语言（Triggers on…/Use when…）里猜触发词
    _write(
        tmp_path,
        "demo-notrig",
        "---\nname: demo-notrig\n"
        'description: Triggers on "premium landing page", use when building a dashboard\n---\n\n正文。\n',
    )

    skill = _load_one(tmp_path, "demo-notrig")

    assert skill.triggers == [], "自然语言触发词不得被解析（猜即误匹配）"


def test_frontmatter_triggers_reach_matching_and_injection(tmp_path: Path) -> None:
    # 功能：端到端——frontmatter triggers 的技能能被用户输入命中并注入正文
    _write(tmp_path, "demo-search", OPENCODE_WITH_TRIGGER_LIST)
    _write(tmp_path, "demo-web", OPENCODE_STYLE)
    registry = SkillRegistry()
    registry.register_all(SkillLoader(str(tmp_path)).load_all())
    catalog = SkillInjector(registry, team_id="").build_catalog()

    assert "demo-search" in catalog, "frontmatter 技能必须进目录"
    assert "正文" not in catalog, "F 模式不注入正文"
