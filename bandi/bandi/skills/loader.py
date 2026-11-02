# 功能：技能加载（SkillLoader 解析技能定义/校验）
from __future__ import annotations

import logging
import os
import re

import yaml

from bandi.core.exceptions import BandiError
from bandi.skills.card import SkillCard
from bandi.skills.models import SkillDef
from bandi.skills.path_guard import validate_path_segment
from bandi.skills.serializer import SkillCardLoadError, load_card

logger = logging.getLogger(__name__)

_CARD_SUFFIX = ".card.json"
# 功能：frontmatter 触发词最小长度——单字词（搜/查/找）在子串匹配下会命中几乎任何输入
_TRIGGER_MIN_LEN = 2


def _coerce_enabled(raw: object) -> bool:
    """功能：frontmatter enabled 解析（缺省启用，false/0/no/off 视为禁用）。"""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"false", "0", "no", "off"}


def _allowed_skill_names() -> set[str] | None:
    """功能：读 ``BANDI_SKILLS_ALLOW`` 技能名白名单；缺省或空白返回 None（不过滤）。

    目录级装载无法表达「只要这几个技能」——同一目录下可能并存不需要的技能。
    """
    raw = os.environ.get("BANDI_SKILLS_ALLOW") or ""
    names = {token.strip() for token in raw.replace(";", ",").split(",") if token.strip()}
    return names or None


class SkillLoader:
    def __init__(self, skills_dir: str | None = None):
        self._skills_dir = skills_dir

    def _cascade_dirs(self) -> list[str]:
        """功能：技能发现级联——显式目录保持单目录；缺省顺序：
        `BANDI_SKILLS_DIRS`（pathsep 分隔，宿主定制）→ 项目 `<cwd>/.bandi/skills`
        → 项目 `<cwd>/.agents/skills`（pi 兼容，项目内共享）→ 用户 `~/.bandi/skills`；
        先到先得（前序目录同名覆盖后续）。

        **只读 Bandi 自管的目录**，不扫其他 Agent 工具的用户级私有目录
        （`~/.config/opencode/skills`、`~/.claude/skills` 等）。那些目录是别的
        工具的实现细节：格式不保证、随对方升级变、且里面多半是本产品用不上的
        技能
        检索与匹配）。

        想用别处的技能，走导入通道落进 `~/.bandi/skills` 即可——
        `bandi skills import <路径> [--team <团队>]`（可路由到员工卡）。
        """
        if self._skills_dir:
            return [self._skills_dir]
        candidates: list[str] = [
            entry
            for entry in (os.environ.get("BANDI_SKILLS_DIRS") or "").split(os.pathsep)
            if entry.strip()
        ]
        candidates.append(os.path.join(os.getcwd(), ".bandi", "skills"))
        candidates.append(os.path.join(os.getcwd(), ".agents", "skills"))
        candidates.append(os.path.expanduser(os.path.join("~", ".bandi", "skills")))
        seen: set[str] = set()
        dirs: list[str] = []
        for candidate in candidates:
            key = os.path.normcase(os.path.abspath(candidate))
            if key not in seen:
                seen.add(key)
                dirs.append(candidate)
        return dirs

    def load_all(self) -> list[SkillDef]:
        result: list[SkillDef] = []
        seen_names: set[str] = set()
        for skills_dir in self._cascade_dirs():
            self._load_dir(skills_dir, result, seen_names)
        allowed = _allowed_skill_names()
        if allowed is None:
            return result
        return [skill for skill in result if skill.name in allowed]

    def _load_dir(self, skills_dir: str, result: list[SkillDef], seen_names: set[str]) -> None:
        """功能：装载一个技能目录——先目录级 SKILL.md 技能、后 <skill_id>.card.json
        卡片技能；两族同名时 Markdown 技能优先（先到先得），卡片不参与自动触发。"""
        if not os.path.isdir(skills_dir):
            return
        entries = sorted(os.listdir(skills_dir))
        for entry in entries:
            skill_path = os.path.join(skills_dir, entry)
            if not os.path.isdir(skill_path):
                continue
            md_file = os.path.join(skill_path, "SKILL.md")
            if not os.path.isfile(md_file):
                continue
            skill = self._load_file(md_file)
            if skill is None or skill.name in seen_names:
                continue
            if not skill.enabled:
                continue
            seen_names.add(skill.name)
            skill.resource_paths = self._scan_resources(skill_path)
            result.append(skill)
        for entry in entries:
            if not entry.endswith(_CARD_SUFFIX):
                continue
            card = self._read_card(os.path.join(skills_dir, entry))
            if card is None or not card.enabled:
                continue
            skill = card_to_skill_def(card)
            if skill.name in seen_names:
                continue
            seen_names.add(skill.name)
            result.append(skill)

    def load(self, path: str) -> SkillDef | None:
        if str(path).endswith(_CARD_SUFFIX):
            card = self._read_card(path)
            return card_to_skill_def(card) if card is not None else None
        return self._load_file(path)

    @staticmethod
    def _read_card(path: str) -> SkillCard | None:
        """功能：读回技能卡并对 skill_id 做工具面键校验（与 save_card 同口径
        `ascii_only`：空/路径穿越/斜杠/非 ASCII 一律拒绝）——缺失/非法 JSON/坏图/
        非法 id 即跳过并记 warning，不阻断其余装载。"""
        try:
            card = load_card(path)
        except SkillCardLoadError as e:
            logger.warning("skill card load failed for %s: %s", path, e)
            return None
        try:
            validate_path_segment(
                card.skill_id, label="skill_id", error_cls=SkillCardLoadError, ascii_only=True
            )
        except BandiError as e:
            logger.warning("skill card rejected unsafe skill_id for %s: %s", path, e)
            return None
        return card

    def _load_file(self, path: str) -> SkillDef | None:
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("skill load failed for %s: %s", path, e)
            return None
        content = content.lstrip("\ufeff")  # BOM 兼容
        metadata, body = self._parse_frontmatter(content)
        sections = self._parse_sections(body)
        name = (
            metadata.get("name", "")
            or self._extract_name(body)
            or os.path.splitext(os.path.basename(path))[0]
        )
        description = metadata.get("description", "") or sections.get("_description", "")
        triggers = self._parse_list(
            sections.get("behavior", "")
        ) or self._parse_frontmatter_triggers(metadata)
        instructions = sections.get("instructions", "") or body.strip()
        allowed_tools = self._parse_tool_list(sections.get("tools", "")) or None
        constraints = self._parse_list(sections.get("constraints", ""))
        tags = self._parse_tags(metadata)
        team_id = str(metadata.get("team_id", "") or "").strip()
        fork = bool(metadata.get("fork", False))
        model = str(metadata.get("model", "") or "").strip()
        effort = str(metadata.get("effort", "") or "").strip()
        enabled = _coerce_enabled(metadata.get("enabled"))
        return SkillDef(
            name=name,
            description=description,
            triggers=triggers,
            instructions=instructions,
            allowed_tools=allowed_tools or None,
            constraints=constraints,
            tags=tags,
            team_id=team_id,
            fork=fork,
            model=model,
            effort=effort,
            enabled=enabled,
        )

    def _parse_frontmatter(self, content: str) -> tuple[dict, str]:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    metadata = yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError:
                    metadata = {}
                return metadata, parts[2].strip()
        return {}, content

    def _parse_sections(self, body: str) -> dict[str, str]:
        sections: dict[str, str] = {}
        current_section = "_description"
        current_lines: list[str] = []
        for line in body.split("\n"):
            if line.startswith("## "):
                sections[current_section] = "\n".join(current_lines).strip()
                key = line[3:].strip().lower().replace(" ", "_")
                current_section = key
                current_lines = []
            elif line.startswith("# ") and not line.startswith("## "):
                if current_section == "_description":
                    current_lines = []
            elif line.startswith("> ") and current_section == "_description":
                current_lines.append(line[2:])
            else:
                current_lines.append(line)
        sections[current_section] = "\n".join(current_lines).strip()
        return sections

    @staticmethod
    def _extract_name(body: str) -> str:
        for line in body.split("\n"):
            if line.startswith("# ") and not line.startswith("## "):
                return line[2:].strip()
        return ""

    @staticmethod
    def _parse_list(text: str) -> list[str]:
        return [
            line.strip().lstrip("-* ").strip() for line in text.strip().split("\n") if line.strip()
        ]

    @staticmethod
    def _parse_string_list(raw: object) -> list[str]:
        """功能：frontmatter 列表解析——支持字符串（逗号/换行）与字符串数组"""
        if isinstance(raw, str):
            return [part.strip() for part in re.split(r"[,\n]", raw) if part.strip()]
        if isinstance(raw, list):
            return [str(part).strip() for part in raw if str(part).strip()]
        return []

    @staticmethod
    def _parse_frontmatter_triggers(metadata: dict) -> list[str]:
        """功能：解析 frontmatter ``triggers``（opencode/Claude 风格）。

        逗号、顿号拆分。分类名本身不作触发词；**单字触发词丢弃**（「搜/查/找」拆成
        单字后会命中几乎任何输入）。``description`` 里的自然语言（"Triggers on…"）
        一律不解析：猜词即误匹配。
        """
        raw = metadata.get("triggers")
        collected: list[str] = []
        min_len = _TRIGGER_MIN_LEN

        def _push(value: object) -> None:
            if isinstance(value, str):
                for piece in re.split(r"[/、,，]", value):
                    token = piece.strip()
                    if len(token) >= min_len and token not in collected:
                        collected.append(token)
            elif isinstance(value, list):
                for item in value:
                    _push(item)
            elif isinstance(value, dict):
                for key in sorted(value, key=str):
                    _push(value[key])

        if isinstance(raw, str):
            _push(raw)
        elif isinstance(raw, list):
            for item in raw:
                _push(item)
        elif isinstance(raw, dict):
            for key in sorted(raw):
                _push(raw[key])
        return collected

    @staticmethod
    def _parse_tags(metadata: dict) -> list[str]:
        for key in ("tags", "domains"):
            value = metadata.get(key)
            if isinstance(value, list):
                return [str(t).strip() for t in value if str(t).strip()]
            if isinstance(value, str):
                return [t.strip() for t in value.split(",") if t.strip()]
        return []

    @staticmethod
    def _parse_tool_list(text: str) -> list[str] | None:
        tools = SkillLoader._parse_list(text)
        return tools if tools else None

    @staticmethod
    def _scan_resources(skill_dir: str) -> list[str]:
        result: list[str] = []
        try:
            for entry in os.listdir(skill_dir):
                full = os.path.join(skill_dir, entry)
                if os.path.isfile(full) and entry != "SKILL.md":
                    result.append(full)
        except OSError as e:
            logger.warning("skill resource scan failed for %s: %s", skill_dir, e)
        return result


def first_cascade_skills_dir() -> str:
    """功能：技能发现级联首目录（默认技能库）——蒸馏等写入端与工具面装载同源
    避免「写在一处、只装载另一处」的可见性断链。"""
    return SkillLoader()._cascade_dirs()[0]


def card_to_skill_def(card: SkillCard) -> SkillDef:
    """功能：SkillCard → SkillDef 转换（工具面可见）——name 取 card.skill_id（显式调用键）
    description 取 card.description，正文为节点确定性渲染；triggers 留空：蒸馏草稿
    不参与自动触发注入，仅 skill_discover/skill_invoke 显式可达。"""
    return SkillDef(
        name=card.skill_id,
        description=card.description,
        triggers=[],
        instructions=render_card_instructions(card),
        enabled=card.enabled,
    )


def render_card_instructions(card: SkillCard) -> str:
    """功能：技能卡 → 渐进披露正文（标题 + 描述 + 流程节点确定性渲染；中文逐字保真
    节点顺序即卡片声明顺序，空描述/空指令按固定规则省略）。"""
    parts: list[str] = [f"# {card.name}"]
    description = card.description.strip()
    if description:
        parts.append(description)
    node_lines: list[str] = []
    for node in card.nodes:
        label = node.name.strip() or node.node_id
        instruction = node.instruction.strip()
        node_lines.append(f"- {label}：{instruction}" if instruction else f"- {label}")
    if node_lines:
        parts.append("## 流程节点")
        parts.extend(node_lines)
    return "\n\n".join(parts)
