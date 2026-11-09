# 功能：项目级 Markdown 记忆存储——<项目根>/.bandi/memory/+ entries/ 索引化长记忆

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from bandi.memory.episodic import tokenize_cn
from bandi.utils.db import connect_app_db

logger = logging.getLogger(__name__)


def _atomic_write_text(path: Path, content: str) -> None:
    """（5）：原子写（tmp + os.replace）——/index 半写损坏会瘫痪
    记忆入口；崩溃/中断下旧内容保持完整。"""
    tmp_path = path.with_name(f"{path.name}.{uuid4().hex[:8]}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(str(tmp_path), str(path))


# 定位为索引（每行一条 ≤150 字符指针）而非正文仓库——预算按索引体量给
# （约 12K 字符 ≈ 80 条指针 + 少量短正文），正文一律落 entries/<id>.md 按需展开。
PROJECT_MEMORY_MAX_CHARS = 12000
PROJECT_ENTRIES_MAX_COUNT = 200
# （行数维度；指针行超限按最旧淘汰，正文不自动裁剪）
MAX_ENTRYPOINT_LINES = 200
_ENTRY_HOOK_CHARS = 100

_STRUCTURED_BEGIN = "<!-- BANDI_STRUCTURED:BEGIN -->"
_STRUCTURED_END = "<!-- BANDI_STRUCTURED:END -->"

_BOOTSTRAP_TEMPLATE = """# 项目记忆

> 由 agent 维护，记录项目级长期经验与事实。禁止写入结构化/私有内容。
"""

BOOTSTRAP_TEMPLATE = _BOOTSTRAP_TEMPLATE
ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9\-]+$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class EntryPointer:
    """entries/<id>.md 指针——溢出内容的最小可发现单元。"""

    entry_id: str
    title: str
    created: str
    hook: str


def _rank_relevance(rank: Any) -> float:
    """功能：FTS5 bm25 rank（负值，越负越相关）→ [0,1) 相关度——对齐
    memory_provider._fts_relevance 口径（原公式 max(0, rank) 把负值
    全归零 → 所有命中恒为满分）。"""
    try:
        magnitude = max(0.0, -float(rank))
    except (TypeError, ValueError):
        return 0.0
    return magnitude / (1.0 + magnitude)


def _one_line(text: str, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    return collapsed[:limit]


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _pointer_line_within(ptr: EntryPointer, budget: int) -> str:
    """
    完整(含 hook) → 脱 hook → 截 title 三级收紧，`[..](entries/<id>.md)` 永不被硬截断。"""

    def link(title: str) -> str:
        return f"[{title}](entries/{ptr.entry_id}.md)"

    full = f"- {link(ptr.title)} — {ptr.hook}"
    if len(full) <= budget:
        return full
    no_hook = f"- {link(ptr.title)}"
    if len(no_hook) <= budget:
        return no_hook
    max_title = max(0, budget - len(f"- {link('')}"))
    return f"- {link(ptr.title[:max_title])}"


_MIN_TOKEN_LEN = 2
_MAX_TOKEN_LEN = 4


def _clean_alnum(text: str) -> str:
    """只保留字母数字字符——与 tokenize_cn 的清理规则一致（倒排 gram 建有界）。"""
    return "".join(c for c in text if c.isalnum())


def _ngram_tokens(text: str) -> list[str]:
    """对清洗后文本取全部连续 2-4 字窗口（倒排 postings 的 gram 全集）。"""
    cleaned = _clean_alnum(text)
    if not cleaned:
        return []
    out: list[str] = []
    for n in range(_MIN_TOKEN_LEN, _MAX_TOKEN_LEN + 1):
        for i in range(0, len(cleaned) - n + 1):
            out.append(cleaned[i : i + n])
    return out


class MarkdownMemoryStore:
    def __init__(
        self,
        store_dir: str | Path,
        project_root: str | Path | None = None,
        max_chars: int | None = None,
        max_entries: int | None = None,
    ) -> None:
        self._store_dir = Path(store_dir)
        self._project_root = self._resolve_project_root(project_root)
        # 项目可按需调整；溢出内容路由 entries 而非静默截断
        self._max_chars = PROJECT_MEMORY_MAX_CHARS if max_chars is None else max_chars
        self._max_entries = PROJECT_ENTRIES_MAX_COUNT if max_entries is None else max_entries
        self._lock = threading.Lock()
        self._memory_file = self._project_root / ".bandi" / "memory" / "MEMORY.md"
        self._entries_dir = self._project_root / ".bandi" / "memory" / "entries"
        # Memory.md 最终形态：持久化索引 index.json。entries 较多时冷启动不必逐 .md
        # 全文重读——index.json 缓存 entry 元数据+正文，用签名（mtime+size）判失效
        # 未签名变化时直接从磁盘载入，避免全目录全文扫描。
        self._index_path = self._entries_dir / "index.json"
        # inverted = gram(2-4字) -> {entry_id} 倒排 postings，search_entries 用来
        self._index_cache: tuple | None = None
        # 惰性连接，仅检索/重建时打开
        self._fts_path = self._project_root / ".bandi" / "memory" / "entries_index.db"
        self._fts_conn: Any | None = None
        self._fts_stale = False

    @property
    def entries_dir(self) -> Path:
        return self._entries_dir

    def _resolve_project_root(self, project_root: str | Path | None) -> Path:
        if project_root is None:
            return self._store_dir.resolve()
        root = Path(project_root)
        if ".." in root.parts:
            raise ValueError(f"project_root 不允许包含 '..' 路径段（路径穿越防御）：{root}")
        return root.resolve()

    def ensure_memory_file(self) -> Path:
        self._memory_file.parent.mkdir(parents=True, exist_ok=True)
        if not self._memory_file.exists():
            _atomic_write_text(self._memory_file, BOOTSTRAP_TEMPLATE)
        return self._memory_file

    def load_visible_text(self) -> str:
        with self._lock:
            if not self._memory_file.exists():
                return ""
            raw = self._memory_file.read_text(encoding="utf-8")
        return self.strip_structured_block(raw)

    def save_visible_text(self, content: str) -> None:
        self.ensure_memory_file()
        with self._lock:
            existing = self._memory_file.read_text(encoding="utf-8")
            structured = self.extract_structured_block(existing)
            budget = self._max_chars
            if structured:
                budget -= len(structured) + 2
            if len(content) > budget:
                # 全文落 entries/ 索引，只留指针行，结构化块完整保留
                ptr = self._save_entry_unlocked(content)
                content = _pointer_line_within(ptr, budget)
            if structured:
                content = f"{content.rstrip()}\n\n{structured}"
            _atomic_write_text(self._memory_file, content)
            self._enforce_entrypoint_lines_unlocked()

    def append_visible_entry(self, entry: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        section = f"## [{timestamp}] {entry}"
        with self._lock:
            existing = (
                self._memory_file.read_text(encoding="utf-8") if self._memory_file.exists() else ""
            )
            structured = self.extract_structured_block(existing)
            visible = self.strip_structured_block(existing)
            updated = f"{visible}\n\n{section}" if visible.strip() else section
            budget = self._max_chars
            if structured:
                budget -= len(structured) + 2

            if len(updated) > budget or _line_count(updated) > MAX_ENTRYPOINT_LINES:
                # 硬截断破坏，既有 visible 内容绝不裁剪丢弃
                ptr = self._save_entry_unlocked(entry)
                # 防止旧值（含被淘汰指针）写回复活死链；首次 append 文件尚不存在
                visible = self.strip_structured_block(
                    self._memory_file.read_text(encoding="utf-8")
                    if self._memory_file.exists()
                    else ""
                )
                pointer_line = _pointer_line_within(ptr, budget)
                updated = f"{visible}\n\n{pointer_line}" if visible.strip() else pointer_line
            if structured:
                updated = f"{updated.rstrip()}\n\n{structured}"
            self.ensure_memory_file()
            _atomic_write_text(self._memory_file, updated)
            self._enforce_entrypoint_lines_unlocked()

    def merge_markdown(self, incoming: str) -> None:
        current = self.load_visible_text()
        if incoming.strip() in current:
            return
        merged = (
            f"{current.rstrip()}\n\n{incoming.strip()}" if current.strip() else incoming.strip()
        )
        self.save_visible_text(merged)

    @staticmethod
    def strip_structured_block(content: str) -> str:
        pattern = re.compile(
            re.escape(_STRUCTURED_BEGIN) + r".*?" + re.escape(_STRUCTURED_END),
            re.DOTALL,
        )
        return pattern.sub("", content).strip()

    @staticmethod
    def extract_structured_block(content: str) -> str:
        pattern = re.compile(
            re.escape(_STRUCTURED_BEGIN) + r".*?" + re.escape(_STRUCTURED_END),
            re.DOTALL,
        )
        match = pattern.search(content)
        return "" if match is None else match.group(0).strip()

    def delete_all(self) -> None:
        """功能：清空该 Markdown 记忆域（入口文件 + entries + FTS 索引）。"""
        with self._lock:
            self.close()
            for path in (self._memory_file, self._fts_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("markdown_memory_delete_failed [%s]: %s", path, exc)
            try:
                shutil.rmtree(self._entries_dir, ignore_errors=True)
            except OSError as exc:
                logger.warning("markdown_entries_delete_failed [%s]: %s", self._entries_dir, exc)
            self._index_cache = None
            self._fts_stale = True

    @property
    def memory_file_path(self) -> Path:
        return self._memory_file

    def save_entry(self, title: str, content: str) -> EntryPointer:
        """写入一条索引化长记忆（frontmatter + 正文），返回指针。"""
        if not (title or "").strip():
            raise ValueError("entry title must be non-empty")
        if not (content or "").strip():
            raise ValueError("entry content must be non-empty")
        with self._lock:
            return self._save_entry_unlocked(content, title=title.strip())

    def _save_entry_unlocked(self, content: str, title: str = "") -> EntryPointer:
        now = datetime.now()
        body = (content or "").strip()
        resolved_title = (
            title or _one_line(body.splitlines()[0] if body else "", 40) or "untitled"
        ).strip()
        digest = hashlib.sha256(f"{resolved_title}\n{body}".encode()).hexdigest()[:8]
        entry_id = f"{now.strftime('%Y%m%d')}-{digest}"
        created = now.strftime("%Y-%m-%d")
        self._entries_dir.mkdir(parents=True, exist_ok=True)
        path = self._entries_dir / f"{entry_id}.md"
        text = f"---\ntitle: {resolved_title}\ncreated: {created}\n---\n\n{body}\n"
        _atomic_write_text(path, text)
        hook = _one_line(body, _ENTRY_HOOK_CHARS)
        self._index_cache = None
        self._prune_entries_unlocked(protect=entry_id)
        return EntryPointer(entry_id=entry_id, title=resolved_title, created=created, hook=hook)

    def _prune_entries_unlocked(self, protect: str = "") -> None:
        """

        淘汰 = 删文件 + 移除 对应指针行（防死链指向不存在文件）；
        <=0 上限视为不限（逃生门）。protect = 刚写入的新条目 id——同一时钟 tick
        下 mtime 不可区分时按文件名破平可能把 fresh 排进淘汰窗口，误删后其指针
        仍被调用方写回 成死链，故 fresh 永不参与淘汰。调用方须已持锁。
        """
        if self._max_entries is None or self._max_entries <= 0:
            return
        files = sorted(
            self._entries_dir.glob("*.md"),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
        )
        excess = len(files) - self._max_entries
        if excess <= 0:
            return  # 未超限不淘汰——负数 excess 不得走负切片（会误删最新条目）
        candidates = [p for p in files if p.stem != protect]
        for path in candidates[:excess]:
            self._remove_pointer_for_entry(path.stem)
            try:
                path.unlink()
            except OSError:
                pass

    def _enforce_entrypoint_lines_unlocked(self) -> None:
        """入口文件行数上限：超限时按
        最旧优先淘汰指针行；可见正文（非指针行）不自动裁剪（保用户内容）。
        调用方须已持锁。"""
        if not self._memory_file.exists():
            return
        raw = self._memory_file.read_text(encoding="utf-8")
        visible = self.strip_structured_block(raw)
        lines = visible.splitlines()
        excess = len(lines) - MAX_ENTRYPOINT_LINES
        if excess <= 0:
            return
        pointer_lines = [ln for ln in lines if ln.lstrip().startswith("- ") and "(entries/" in ln]
        if not pointer_lines:
            return
        remove = set(pointer_lines[:excess])
        kept = [ln for ln in lines if ln not in remove]
        structured = self.extract_structured_block(raw)
        content = "\n".join(kept).strip()
        if structured:
            content = f"{content}\n\n{structured}" if content else structured
        self._memory_file.write_text(content, encoding="utf-8")

    def _remove_pointer_for_entry(self, entry_id: str) -> None:
        """

        调用方须已持锁；按行过滤含 `entries/<id>.md` 的行，防残留死链。
        """
        if not self._memory_file.exists():
            return
        raw = self._memory_file.read_text(encoding="utf-8")
        structured = self.extract_structured_block(raw)
        visible = self.strip_structured_block(raw)
        marker = f"entries/{entry_id}.md"
        kept = [ln for ln in visible.splitlines() if marker not in ln]
        content = "\n".join(kept).strip()
        if structured:
            content = f"{content}\n\n{structured}" if content else structured
        self._memory_file.write_text(content, encoding="utf-8")

    def _entries_signature(self) -> tuple:
        """entries 目录内容指纹——文件集合 + mtime + 大小，用于索引缓存失效判定。"""
        if not self._entries_dir.exists():
            return ()
        sig = []
        for path in sorted(self._entries_dir.glob("*.md")):
            try:
                st = path.stat()
            except OSError:
                continue
            sig.append((path.name, st.st_mtime_ns, st.st_size))
        return tuple(sig)

    def _build_index(self) -> list[tuple[EntryPointer, str]]:
        if not self._entries_dir.exists():
            return []
        index: list[tuple[EntryPointer, str]] = []
        for path in sorted(self._entries_dir.glob("*.md")):
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            match = _FRONTMATTER_RE.match(raw)
            fields: dict[str, str] = {}
            if match:
                for line in match.group(1).splitlines():
                    if ":" in line:
                        key, _, value = line.partition(":")
                        fields[key.strip()] = value.strip()
            entry_id = path.stem
            title = fields.get("title") or entry_id
            body = _FRONTMATTER_RE.sub("", raw, count=1).strip() if match else raw.strip()
            index.append(
                (
                    EntryPointer(
                        entry_id=entry_id,
                        title=str(title),
                        created=str(fields.get("created", "")),
                        hook=_one_line(body, _ENTRY_HOOK_CHARS),
                    ),
                    body,
                )
            )
        index.sort(key=lambda pair: (pair[0].created, pair[0].entry_id), reverse=True)
        return index

    def _build_inverted(self, index: list[tuple[EntryPointer, str]]) -> dict[str, set[str]]:
        """基于已解析索引建倒排 postings：gram -> set(entry_id)（候选剪枝用）。"""
        inverted: dict[str, set[str]] = {}
        for ptr, body in index:
            haystack = f"{ptr.title} {ptr.hook} {body}"
            for gram in _ngram_tokens(haystack):
                inverted.setdefault(gram, set()).add(ptr.entry_id)
        return inverted

    def _fts(self) -> Any:
        """功能：惰性打开 FTS5 连接（entries 正文索引，中文 2-gram 预分词）。"""
        if self._fts_conn is None:
            self._fts_path.parent.mkdir(parents=True, exist_ok=True)
            self._fts_conn = connect_app_db(self._fts_path, timeout=10)
            self._fts_conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts "
                "USING fts5(entry_id UNINDEXED, title, hook, body, tokenize='unicode61')"
            )
        return self._fts_conn

    def close(self) -> None:
        """功能：关闭 FTS 连接（显式释放路径；关闭后检索会自动重开）。"""
        if self._fts_conn is not None:
            try:
                self._fts_conn.close()
            finally:
                self._fts_conn = None

    def _rebuild_fts(self, index: list[tuple[EntryPointer, str]]) -> None:
        """功能：全量重建 FTS 索引（entries 增删后经 _index 缓存失效触发）。"""
        try:
            conn = self._fts()
            from bandi.memory.long_term import _fts_preprocess

            conn.execute("DELETE FROM entries_fts")
            for ptr, body in index:
                blob = " ".join(_fts_preprocess(f"{ptr.title} {ptr.hook} {body}").split())
                if blob:
                    conn.execute(
                        "INSERT INTO entries_fts (entry_id, title, hook, body) VALUES (?,?,?,?)",
                        (ptr.entry_id, ptr.title, ptr.hook, blob),
                    )
            conn.commit()
        except Exception as e:  # noqa: BLE001 — FTS 重建失败降级 n-gram 检索
            logger.warning("entries FTS rebuild failed, falling back to n-gram: %s", e)
            self._fts_stale = False

    def _fts_search(self, query: str, limit: int) -> list[tuple[str, float]] | None:
        """功能：FTS5 检索 entries（2-gram AND；失败/空召回返回 None 走回退）。"""
        try:
            from bandi.memory.long_term import _CJK_RE, _fts_preprocess

            conn = self._fts()
            has_cjk = bool(_CJK_RE.search(query or ""))
            if has_cjk:
                grams = [g for g in _fts_preprocess(query).split() if g]
                fts_query = " AND ".join(f'"{g}"' for g in grams) if grams else " "
            else:
                fts_query = self._sanitize_fts_query(query)
            rows = conn.execute(
                "SELECT entry_id, rank FROM entries_fts "
                "WHERE entries_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, max(1, limit)),
            ).fetchall()
            if not rows:
                return None
            return [
                (
                    r[0],
                    _rank_relevance(r[1]) if r[1] is not None else 1.0,
                )
                for r in rows
            ]
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        from bandi.memory.long_term import _fts_simple_tokens

        return _fts_simple_tokens(query) or " "

    def _write_index_to_disk(self, signature: tuple, index: list[tuple[EntryPointer, str]]) -> None:
        """把重建后的索引持久化为 index.json（含正文，供冷启动免全读）。"""
        if not signature:
            try:
                if self._index_path.exists():
                    self._index_path.unlink()
            except OSError:
                pass
            return
        payload = {
            "signature": [list(item) for item in signature],
            "index": [
                {
                    "entry_id": ptr.entry_id,
                    "title": ptr.title,
                    "created": ptr.created,
                    "hook": ptr.hook,
                    "body": body,
                }
                for ptr, body in index
            ],
        }
        try:
            self._entries_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(self._index_path, json.dumps(payload, ensure_ascii=False))
        except OSError:
            pass

    def _load_index_from_disk(self, signature: tuple) -> list[tuple[EntryPointer, str]] | None:
        """签名一致则从 index.json 载入索引（免读 .md），否则返回 None 触发重建。

        签名比较必须规范化——JSON 往返把内层 tuple 变 list
        直接 `list(signature)` 比较恒不相等（磁盘缓存恒失效）。"""
        try:
            if not self._index_path.exists():
                return None
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
            saved = payload.get("signature") or []
            normalized = [list(item) for item in signature]
            if [list(item) for item in saved] != normalized:
                return None
            index: list[tuple[EntryPointer, str]] = []
            for item in payload.get("index", []):
                entry_id = str(item.get("entry_id", ""))
                if not ENTRY_ID_RE.match(entry_id):
                    return None
                index.append(
                    (
                        EntryPointer(
                            entry_id=entry_id,
                            title=str(item.get("title", entry_id)),
                            created=str(item.get("created", "")),
                            hook=str(item.get("hook", "")),
                        ),
                        str(item.get("body", "")),
                    )
                )
            if not index and signature:
                return None
            return index
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _index(
        self,
    ) -> tuple[list[tuple[EntryPointer, str]], dict[str, set[str]]]:
        """缓存命中则返回 (索引, 倒排)，否则重建；冷启动优先尝试 index.json 持久化索引。"""
        with self._lock:
            signature = self._entries_signature()
            if self._index_cache is None or self._index_cache[0] != signature:
                index = self._load_index_from_disk(signature)
                if index is None:
                    index = self._build_index()
                self._write_index_to_disk(signature, index)
                self._rebuild_fts(index)
                self._index_cache = (
                    signature,
                    (index, self._build_inverted(index)),
                )
            return self._index_cache[1]

    def list_entry_pointers(self) -> list[EntryPointer]:
        """扫描 entries 目录产出指针（脏文件降级为文件名标题，不崩）。"""
        return [ptr for ptr, _ in self._index()[0]]

    def read_entry(self, entry_id: str) -> str:
        """按 id 读 entry 全文（去 frontmatter）；拒绝路径穿越 id。"""
        if not ENTRY_ID_RE.match(entry_id or ""):
            raise ValueError(f"非法 entry id（仅允许字母数字与连字符）：{entry_id!r}")
        for ptr, body in self._index()[0]:
            if ptr.entry_id == entry_id:
                return body
        raise FileNotFoundError(f"entry 不存在：{entry_id}")

    def search_entries(self, query: str, limit: int = 5) -> list[tuple[EntryPointer, float]]:
        """FTS5 中文 2-gram 检索 entries（BM25 排序）；FTS 空召回/失败回退
        n-gram + 倒排剪枝（保可发现性）。limit<=0 返回空（显式 0 语义）。"""
        q = (query or "").strip()
        if not q or limit <= 0:
            # limit=0 错误翻转成 1 条（显式 0 应返回空，非兜底成 1）
            return []
        # 先触发索引缓存（含 FTS 重建），再检索
        index, _inverted = self._index()
        if not index:
            return []
        fts_hits = self._fts_search(q, limit)
        if fts_hits:
            by_id = {ptr.entry_id: ptr for ptr, _body in index}
            scored = [(by_id[eid], rel) for eid, rel in fts_hits if eid in by_id]
            if scored:
                return scored[:limit]
        _index, inverted = self._index()
        grams = tokenize_cn(q)
        if grams:
            candidates: set[str] = set()
            for t in grams:
                ids = inverted.get(t)
                if ids:
                    candidates.update(ids)
            if not candidates:
                return []
        tokens = grams or [q]
        scored: list[tuple[EntryPointer, float]] = []
        for ptr, body in _index:
            if grams and ptr.entry_id not in candidates:
                continue
            haystack = f"{ptr.title} {ptr.hook} {body}"
            hit_count = sum(1 for t in tokens if t in haystack)
            if hit_count:
                scored.append((ptr, hit_count / len(tokens)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]


__all__ = ["BOOTSTRAP_TEMPLATE", "EntryPointer", "MarkdownMemoryStore"]
