from __future__ import annotations

import os
import tempfile

from bandi.skills.loader import SkillLoader


def _write_skill(skill_dir: str, name: str, content: str) -> str:
    path = os.path.join(skill_dir, name, "SKILL.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


SAMPLE_SKILL_FULL = """---
name: code-review
description: "Review code changes for quality and correctness"
---

# Code Review

> A structured code review skill.

## Behavior
- user asks to review code
- user says "review this"
- re:review\\s+pr|merge\\s+request

## Instructions
You are a code reviewer.
Check for bugs, style issues, and security problems.

## Tools
- read_file
- grep_tool

## Constraints
- Do not modify files
- Only analyze
"""

SAMPLE_SKILL_MINIMAL = """---
name: translator
description: "Translate text between languages"
---

## Behavior
- translate this
- 翻译

## Instructions
You are a translator.
Translate accurately and preserve formatting.

## Tools
- web_search
"""

SAMPLE_SKILL_NO_FRONTMATTER = """# Greeter

> A simple greeting skill.

## Behavior
- say hello
- greet me

## Instructions
You are a friendly assistant.
Respond with a warm greeting.
"""

SAMPLE_SKILL_EMPTY_TRIGGERS = """---
name: helper
description: "A general helper skill"
---

## Behavior
- help

## Instructions
You are a helpful assistant.
"""


class TestSkillLoader:
    def test_load_single_full_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_skill(tmp, "code-review", SAMPLE_SKILL_FULL)
            loader = SkillLoader(tmp)
            skills = loader.load_all()
            assert len(skills) == 1
            skill = skills[0]
            assert skill.name == "code-review"
            assert skill.description == "Review code changes for quality and correctness"
            assert len(skill.triggers) == 3
            assert "user asks to review code" in skill.triggers
            assert 'user says "review this"' in skill.triggers
            assert "re:review\\s+pr|merge\\s+request" in skill.triggers
            assert "bugs" in skill.instructions
            assert skill.allowed_tools == ["read_file", "grep_tool"]
            assert "Do not modify files" in skill.constraints
            assert "Only analyze" in skill.constraints

    def test_load_minimal_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_skill(tmp, "translator", SAMPLE_SKILL_MINIMAL)
            loader = SkillLoader(tmp)
            skills = loader.load_all()
            assert len(skills) == 1
            skill = skills[0]
            assert skill.name == "translator"
            assert skill.description == "Translate text between languages"
            assert "翻译" in skill.triggers
            assert skill.allowed_tools == ["web_search"]
            assert skill.constraints == []

    def test_load_no_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_skill(tmp, "greeter", SAMPLE_SKILL_NO_FRONTMATTER)
            loader = SkillLoader(tmp)
            skills = loader.load_all()
            assert len(skills) == 1
            skill = skills[0]
            assert skill.name == "Greeter"
            assert skill.description == "A simple greeting skill."
            assert "say hello" in skill.triggers

    def test_load_nonexistent_directory(self):
        loader = SkillLoader("/nonexistent/path/to/skills")
        skills = loader.load_all()
        assert skills == []

    def test_load_missing_file(self):
        loader = SkillLoader()
        result = loader.load("/nonexistent/skill.md")
        assert result is None

    def test_load_by_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_skill(tmp, "test", SAMPLE_SKILL_FULL)
            sk_file = os.path.join(tmp, "test", "SKILL.md")
            loader = SkillLoader(tmp)
            skill = loader.load(sk_file)
            assert skill is not None
            assert skill.name == "code-review"

    def test_scan_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_skill(tmp, "my_skill", SAMPLE_SKILL_MINIMAL)
            resource_path = os.path.join(tmp, "my_skill", "template.txt")
            with open(resource_path, "w") as f:
                f.write("template content")
            loader = SkillLoader(tmp)
            skills = loader.load_all()
            assert len(skills) == 1
            assert len(skills[0].resource_paths) == 1
            assert skills[0].resource_paths[0] == resource_path

    def test_invalid_yaml_frontmatter(self):
        content = """---
invalid yaml: [unbalanced
---

# Bad Skill

## Behavior
- test

## Instructions
Do nothing.
"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_skill(tmp, "bad", content)
            loader = SkillLoader(tmp)
            skills = loader.load_all()
            assert len(skills) == 1
            assert skills[0].name == "Bad Skill"

    def test_empty_skills_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            loader = SkillLoader(tmp)
            skills = loader.load_all()
            assert skills == []

    def test_multiple_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_skill(tmp, "skill_a", SAMPLE_SKILL_FULL)
            _write_skill(tmp, "skill_b", SAMPLE_SKILL_MINIMAL)
            loader = SkillLoader(tmp)
            skills = loader.load_all()
            assert len(skills) == 2
            names = {s.name for s in skills}
            assert names == {"code-review", "translator"}

    def test_non_skill_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            readme = os.path.join(tmp, "README.md")
            with open(readme, "w") as f:
                f.write("# Not a skill")
            loader = SkillLoader(tmp)
            skills = loader.load_all()
            assert skills == []
