# 功能：资源中心目录（技能 / SOP / 知识文档的列举、编辑、启停、卸载与上传）。
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from bandi.resources import knowledge_sources
from bandi.skills.card import SkillCard
from bandi.skills.importer import SkillImporter, SkillImportError
from bandi.skills.loader import SkillLoader
from bandi.skills.serializer import SkillCardLoadError, load_card, save_card

_CARD_SUFFIX = ".card.json"
_SAFE_ARCHIVE_RE = re.compile(r"[^\w.-]+", re.UNICODE)
_FRONTMATTER_RE = re.compile(r"^\ufeff?\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class ResourceError(Exception):
    """资源中心基础错误。"""


class ResourceNotFoundError(ResourceError):
    """资源不存在。"""


class ResourceConflictError(ResourceError):
    """资源已存在。"""


class ResourceValidationError(ResourceError):
    """资源内容或参数不合法。"""


def skills_root() -> Path:
    """功能：用户技能目录（默认 ~/.bandi/skills）。"""
    override = os.environ.get("BANDI_SKILLS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bandi" / "skills"


def _archive_root(kind: str) -> Path:
    return Path.home() / ".bandi" / "_uninstalled" / kind


def _profile_manager() -> Any:
    from bandi.company.agent_profile import AgentProfileManager

    return AgentProfileManager(
        profiles_dir=str(Path.home() / ".bandi" / "agents" / "profiles"),
        skills_dir=str(skills_root()),
    )


def _profiles() -> list[Any]:
    try:
        return _profile_manager().list_profiles()
    except Exception:  # noqa: BLE001 - 档案损坏不得阻断资源列表
        return []


def _employee_name(profile: Any, names: dict[str, str]) -> str:
    return names.get(str(profile.employee_id)) or str(profile.name or profile.employee_id)


def _scope(
    resource_type: str,
    resource_id: str,
    profiles: list[Any],
    names: dict[str, str],
    *,
    team_id: str = "",
) -> dict[str, Any]:
    employees: list[dict[str, str]] = []
    seen: set[str] = set()
    for profile in profiles:
        for binding in getattr(profile, "resources", []) or []:
            if (
                str(getattr(binding, "resource_type", "")) == resource_type
                and str(getattr(binding, "resource_id", "")) == resource_id
                and str(getattr(binding, "status", "active")) == "active"
                and profile.employee_id not in seen
            ):
                seen.add(profile.employee_id)
                employees.append(
                    {"id": str(profile.employee_id), "name": _employee_name(profile, names)}
                )
    return {
        "global": not employees and not team_id,
        "employees": employees,
        "team_id": team_id,
    }


def _item(
    *,
    kind: str,
    resource_id: str,
    title: str,
    description: str,
    enabled: bool,
    scope: dict[str, Any],
    path: str,
    folder: str,
    updated_at: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": resource_id,
        "kind": kind,
        "title": title,
        "description": description,
        "enabled": enabled,
        "scope": scope,
        "path": path,
        "folder": folder,
        "updated_at": updated_at,
        "meta": meta or {},
    }


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
    except OSError:
        return ""


def _timestamp_iso(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value)).astimezone().isoformat()
    except (TypeError, ValueError, OSError):
        return str(value or "")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ResourceValidationError(f"文件不是 UTF-8 编码: {path.name}") from exc
    except OSError as exc:
        raise ResourceValidationError(f"读取文件失败: {path.name}") from exc


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ResourceValidationError(f"写入文件失败: {path.name}") from exc


def _skill_path(name: str) -> Path:
    root = skills_root()
    direct = root / name
    if name and Path(name).name == name and (direct / "SKILL.md").is_file():
        return direct / "SKILL.md"
    loader = SkillLoader(str(root))
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            md = entry / "SKILL.md"
            if not entry.is_dir() or not md.is_file():
                continue
            skill = loader.load(str(md))
            if skill is not None and skill.name == name:
                return md
    raise ResourceNotFoundError(f"技能不存在: {name}")


def _card_path(skill_id: str) -> Path:
    from bandi.skills.path_guard import validate_path_segment

    skill_id = validate_path_segment(skill_id, label="skill_id", error_cls=ResourceValidationError)
    root = skills_root()
    target = root / f"{skill_id}{_CARD_SUFFIX}"
    if target.is_file():
        return target
    if root.is_dir():
        for candidate in sorted(root.glob(f"*{_CARD_SUFFIX}")):
            try:
                if load_card(candidate).skill_id == skill_id:
                    return candidate
            except SkillCardLoadError:
                continue
    raise ResourceNotFoundError(f"SOP 不存在: {skill_id}")


def _skill_item(path: Path, profiles: list[Any], names: dict[str, str]) -> dict[str, Any]:
    skill = SkillLoader(str(skills_root())).load(str(path))
    if skill is None:
        raise ResourceValidationError(f"技能文件无法解析: {path.name}")
    return _item(
        kind="skill",
        resource_id=skill.name,
        title=skill.name,
        description=skill.description,
        enabled=skill.enabled,
        scope=_scope("general_skill", skill.name, profiles, names, team_id=skill.team_id),
        path=str(path),
        folder=str(path.parent),
        updated_at=_mtime_iso(path),
        meta={"team_id": skill.team_id, "tags": list(skill.tags)},
    )


def _sop_item(path: Path, profiles: list[Any], names: dict[str, str]) -> dict[str, Any]:
    try:
        card = load_card(path)
    except SkillCardLoadError as exc:
        raise ResourceValidationError(f"SOP 文件无法解析: {path.name}") from exc
    return _item(
        kind="sop",
        resource_id=card.skill_id,
        title=card.name or card.skill_id,
        description=card.description,
        enabled=card.enabled,
        scope=_scope("skill", card.skill_id, profiles, names),
        path=str(path),
        folder=str(path.parent),
        updated_at=_mtime_iso(path),
        meta={
            "version": card.version,
            "business_domain": card.business_domain or "",
        },
    )


def _doc_scope(
    base_id: str, employee_ids: list[str], profiles: list[Any], names: dict[str, str]
) -> dict[str, Any]:
    employees: list[dict[str, str]] = []
    seen: set[str] = set()
    for employee_id in employee_ids:
        value = str(employee_id or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        employees.append({"id": value, "name": names.get(value) or value})
    for profile in profiles:
        for binding in getattr(profile, "resources", []) or []:
            if (
                str(getattr(binding, "resource_type", "")) == "knowledge_base"
                and str(getattr(binding, "resource_id", "")) == base_id
                and str(getattr(binding, "status", "active")) == "active"
                and profile.employee_id not in seen
            ):
                seen.add(profile.employee_id)
                employees.append(
                    {"id": str(profile.employee_id), "name": _employee_name(profile, names)}
                )
    return {"global": not employees, "employees": employees, "team_id": ""}


def _doc_description(base_id: str, filename: str, bucket_count: int, chunk_count: int) -> str:
    text = knowledge_sources.read_source(base_id, filename)
    if text:
        body = text
        match = _FRONTMATTER_RE.match(text)
        if match:
            body = text[match.end() :]
        for line in body.splitlines():
            value = line.strip().lstrip("#>-* ").strip()
            if value:
                return value[:160]
    return f"{bucket_count} 个段落 · {chunk_count} 个片段"


def _doc_item(
    store: Any,
    document: Any,
    profiles: list[Any],
    names: dict[str, str],
    base: Any | None,
) -> dict[str, Any]:
    source = knowledge_sources.source_path(document.base_id, document.filename)
    employee_ids = [str(getattr(base, "employee_id", "") or "")] if base is not None else []
    return _item(
        kind="doc",
        resource_id=document.id,
        title=str(document.title or Path(document.filename).stem),
        description=_doc_description(
            document.base_id, document.filename, document.bucket_count, document.chunk_count
        ),
        enabled=True,
        scope=_doc_scope(document.base_id, employee_ids, profiles, names),
        path=str(source if source.is_file() else knowledge_sources.base_dir(document.base_id)),
        folder=str(knowledge_sources.base_dir(document.base_id)),
        updated_at=_timestamp_iso(document.updated_at),
        meta={
            "base_id": document.base_id,
            "filename": document.filename,
            "bucket_count": document.bucket_count,
            "chunk_count": document.chunk_count,
        },
    )


def list_skills(names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    profiles = _profiles()
    employee_names = names or {}
    root = skills_root()
    items: list[dict[str, Any]] = []
    if not root.is_dir():
        return items
    loader = SkillLoader(str(root))
    for entry in sorted(root.iterdir()):
        md = entry / "SKILL.md"
        if not entry.is_dir() or not md.is_file():
            continue
        skill = loader.load(str(md))
        if skill is None:
            continue
        items.append(_skill_item(md, profiles, employee_names))
    return items


def list_sops(names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    profiles = _profiles()
    employee_names = names or {}
    root = skills_root()
    items: list[dict[str, Any]] = []
    if not root.is_dir():
        return items
    for path in sorted(root.glob(f"*{_CARD_SUFFIX}")):
        try:
            items.append(_sop_item(path, profiles, employee_names))
        except ResourceValidationError:
            continue
    return items


def get_resource(
    kind: str, resource_id: str, store: Any, names: dict[str, str] | None = None
) -> dict[str, Any]:
    profiles = _profiles()
    employee_names = names or {}
    if kind == "skill":
        path = _skill_path(resource_id)
        return {"item": _skill_item(path, profiles, employee_names), "content": _read_text(path)}
    if kind == "sop":
        path = _card_path(resource_id)
        return {"item": _sop_item(path, profiles, employee_names), "content": _read_text(path)}
    if kind == "doc":
        document = store.get_document(resource_id)
        if document is None:
            raise ResourceNotFoundError(f"知识文档不存在: {resource_id}")
        bases = {base.id: base for base in store.list_bases()}
        content = knowledge_sources.read_source(document.base_id, document.filename)
        if content is None:
            content = "\n\n".join(
                chunk.content for chunk in store.list_chunks(document_id=document.id)
            )
        item = _doc_item(store, document, profiles, employee_names, bases.get(document.base_id))
        return {"item": item, "content": content}
    raise ResourceValidationError(f"未知资源类型: {kind}")


def save_resource(
    kind: str, resource_id: str, content: str, store: Any, names: dict[str, str] | None = None
) -> dict[str, Any]:
    if not content.strip():
        raise ResourceValidationError("内容不能为空")
    if kind == "skill":
        path = _skill_path(resource_id)
        existing = SkillLoader(str(skills_root())).load(str(path))
        if existing is None:
            raise ResourceValidationError(f"技能文件无法解析: {path.name}")
        content = _ensure_skill_identity(content, resource_id, existing.description)
        _write_text(path, content)
        return _skill_item(path, _profiles(), names or {})
    if kind == "sop":
        path = _card_path(resource_id)
        try:
            data = json.loads(content)
            card = SkillCard.model_validate(data)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ResourceValidationError(f"SOP JSON 不合法: {exc}") from exc
        if card.skill_id != resource_id:
            raise ResourceValidationError("SOP 的 skill_id 不能通过内容修改改变")
        save_card(card, skills_root())
        return _sop_item(path, _profiles(), names or {})
    if kind == "doc":
        document = store.get_document(resource_id)
        if document is None:
            raise ResourceNotFoundError(f"知识文档不存在: {resource_id}")
        knowledge_sources.write_source(document.base_id, document.filename, content)
        _ingest_document(
            store,
            document.base_id,
            document.filename,
            content,
            document.title,
            document_id=resource_id,
        )
        refreshed = store.get_document(resource_id)
        if refreshed is None:
            raise ResourceValidationError("文档保存后无法读回")
        bases = {base.id: base for base in store.list_bases()}
        return _doc_item(store, refreshed, _profiles(), names or {}, bases.get(refreshed.base_id))
    raise ResourceValidationError(f"未知资源类型: {kind}")


def set_enabled(
    kind: str, resource_id: str, enabled: bool, store: Any, names: dict[str, str] | None = None
) -> dict[str, Any]:
    if kind == "skill":
        path = _skill_path(resource_id)
        _set_skill_enabled(path, enabled)
        return _skill_item(path, _profiles(), names or {})
    if kind == "sop":
        path = _card_path(resource_id)
        try:
            card = load_card(path)
        except SkillCardLoadError as exc:
            raise ResourceValidationError(f"SOP 文件无法解析: {path.name}") from exc
        save_card(card.model_copy(update={"enabled": enabled}), skills_root())
        return _sop_item(path, _profiles(), names or {})
    if kind == "doc":
        raise ResourceValidationError("知识文档不支持启停")
    raise ResourceValidationError(f"未知资源类型: {kind}")


def uninstall(kind: str, resource_id: str, store: Any) -> dict[str, Any]:
    if kind == "skill":
        path = _skill_path(resource_id)
        archived = _archive_dir(path.parent, resource_id)
        _unbind("general_skill", resource_id)
        return {"archived": str(archived)}
    if kind == "sop":
        path = _card_path(resource_id)
        archived = _archive_file(path, _archive_root("sops"), resource_id)
        _unbind("skill", resource_id)
        return {"archived": str(archived)}
    if kind == "doc":
        document = store.get_document(resource_id)
        if document is None:
            raise ResourceNotFoundError(f"知识文档不存在: {resource_id}")
        archived = knowledge_sources.archive_source(document.base_id, document.filename)
        store.delete_document(resource_id)
        return {"archived": str(archived) if archived else ""}
    raise ResourceValidationError(f"未知资源类型: {kind}")


def folder_path(kind: str, resource_id: str, store: Any) -> str:
    if kind == "skill":
        return str(_skill_path(resource_id).parent)
    if kind == "sop":
        return str(_card_path(resource_id).parent)
    if kind == "doc":
        document = store.get_document(resource_id)
        if document is None:
            raise ResourceNotFoundError(f"知识文档不存在: {resource_id}")
        return str(knowledge_sources.base_dir(document.base_id, create=True))
    raise ResourceValidationError(f"未知资源类型: {kind}")


def import_skill(text: str, file_name: str, name: str = "") -> dict[str, Any]:
    metadata = _frontmatter(text)
    skill_name = str(metadata.get("name") or name or Path(file_name).stem).strip()
    if not skill_name or skill_name in {".", ".."} or "/" in skill_name or "\\" in skill_name:
        raise ResourceValidationError("技能名不合法")
    target = skills_root() / skill_name
    if (target / "SKILL.md").exists():
        raise ResourceConflictError(f"技能已存在: {skill_name}")
    if not text.lstrip("\ufeff").startswith("---"):
        text = f"---\nname: {skill_name}\ndescription: {skill_name}\n---\n\n{text}"
    with tempfile.TemporaryDirectory(prefix="bandi-resource-skill-") as tmp:
        skill_dir = Path(tmp) / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
        try:
            SkillImporter(skills_dir=str(skills_root())).import_skill(str(skill_dir))
        except SkillImportError as exc:
            raise ResourceValidationError(str(exc)) from exc
    path = target / "SKILL.md"
    return _skill_item(path, _profiles(), {})


def import_sop(text: str) -> dict[str, Any]:
    try:
        card = SkillCard.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ResourceValidationError(f"SOP JSON 不合法: {exc}") from exc
    from bandi.skills.path_guard import validate_path_segment

    validate_path_segment(
        card.skill_id,
        label="skill_id",
        error_cls=ResourceValidationError,
        ascii_only=True,
    )
    target = skills_root() / f"{card.skill_id}{_CARD_SUFFIX}"
    if target.exists():
        raise ResourceConflictError(f"SOP 已存在: {card.skill_id}")
    save_card(card, skills_root())
    return _sop_item(target, _profiles(), {})


def _ingest_document(
    store: Any,
    base_id: str,
    filename: str,
    text: str,
    title: str,
    *,
    document_id: str | None = None,
) -> Any:
    from bandi.knowledge.okf import build_okf_for_document

    result = store.ingest_document(
        base_id=base_id,
        filename=filename,
        text=text,
        title=title,
        document_id=document_id,
    )
    document = store.get_document(result.document_id)
    if document is not None:
        store.upsert_concepts(
            base_id, build_okf_for_document(document, store.list_buckets(document.id))
        )
    return result


def _ensure_skill_identity(text: str, skill_id: str, description: str) -> str:
    """功能：保存技能时保持 frontmatter name 与资源 id 一致（缺 name 时补齐）。"""
    metadata = _frontmatter(text)
    name = str(metadata.get("name") or "").strip()
    if name and name != skill_id:
        raise ResourceValidationError("技能名不能通过内容修改改变，请使用原名称")
    if name == skill_id:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    body = text.lstrip("\ufeff")
    desc_line = f"description: {json.dumps(description or skill_id, ensure_ascii=False)}"
    if body.startswith("---"):
        lines = body.splitlines(keepends=True)
        close = next(
            (index for index in range(1, len(lines)) if lines[index].strip() == "---"), None
        )
        if close is not None:
            frontmatter = list(lines[1:close])
            has_description = any(line.lstrip().startswith("description:") for line in frontmatter)
            frontmatter.insert(0, f"name: {skill_id}{newline}")
            if not has_description:
                frontmatter.insert(1, f"{desc_line}{newline}")
            return "".join([lines[0], *frontmatter, *lines[close:]])
    return f"---{newline}name: {skill_id}{newline}{desc_line}{newline}---{newline}{newline}{body}"


def _frontmatter(text: str) -> dict[str, Any]:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _set_skill_enabled(path: Path, enabled: bool) -> None:
    text = _read_text(path)
    newline = "\r\n" if "\r\n" in text else "\n"
    literal = "true" if enabled else "false"
    body = text.lstrip("\ufeff")
    lines = body.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        close = next(
            (index for index in range(1, len(lines)) if lines[index].strip() == "---"), None
        )
        if close is not None:
            frontmatter = list(lines[1:close])
            replaced = False
            for index, line in enumerate(frontmatter):
                stripped = line.lstrip()
                if stripped.startswith("enabled:"):
                    indent = line[: len(line) - len(stripped)]
                    frontmatter[index] = f"{indent}enabled: {literal}{newline}"
                    replaced = True
                    break
            if not replaced:
                frontmatter.insert(0, f"enabled: {literal}{newline}")
            _write_text(path, "".join([lines[0], *frontmatter, *lines[close:]]))
            return
    _write_text(path, f"---{newline}enabled: {literal}{newline}---{newline}{newline}{body}")


def _safe_archive_name(value: str) -> str:
    slug = _SAFE_ARCHIVE_RE.sub("_", str(value or "")).strip("._") or "resource"
    return slug[:64]


def _archive_dir(source: Path, name: str) -> Path:
    root = _archive_root("skills")
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    target = root / f"{_safe_archive_name(name)}-{stamp}"
    index = 1
    while target.exists():
        target = root / f"{_safe_archive_name(name)}-{stamp}-{index}"
        index += 1
    shutil.move(str(source), str(target))
    return target


def _archive_file(source: Path, root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    target = root / f"{_safe_archive_name(name)}-{stamp}{source.suffix}"
    index = 1
    while target.exists():
        target = root / f"{_safe_archive_name(name)}-{stamp}-{index}{source.suffix}"
        index += 1
    shutil.move(str(source), str(target))
    return target


def _unbind(resource_type: str, resource_id: str) -> None:
    manager = _profile_manager()
    try:
        profiles = manager.list_profiles()
    except Exception:  # noqa: BLE001
        return
    for profile in profiles:
        if any(
            str(getattr(item, "resource_type", "")) == resource_type
            and str(getattr(item, "resource_id", "")) == resource_id
            for item in getattr(profile, "resources", []) or []
        ):
            try:
                manager.unbind_resource(profile.profile_id, resource_type, resource_id)
            except Exception:  # noqa: BLE001 - 绑定已失效不阻断卸载
                continue
