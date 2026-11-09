# 功能：KnowledgeStore — 员工知识库存储（SQLite 三层实体 + chunk FTS5 检索）。

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from bandi.knowledge.okf import extract_citations, extract_links, parse_okf_markdown
from bandi.knowledge.schema import (
    IngestResult,
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDocument,
)
from bandi.memory.long_term import _fts_preprocess
from bandi.utils.db import connect_app_db

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHUNK_CHARS = 1200
_DEFAULT_SEARCH_LIMIT = 8
_MAX_SEARCH_LIMIT = 20
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _split_sections(text: str) -> list[tuple[str, str]]:
    """功能：按 Markdown 标题切节——
    返回 (标题, 节正文) 列表；标题前导内容作为无标题首节保留不丢。"""
    sections: list[tuple[str, str]] = []
    current_title = ""
    lines: list[str] = []
    started = False
    for line in text.splitlines():
        if _HEADING_RE.match(line):
            if started and any(ln.strip() for ln in lines):
                sections.append((current_title, "\n".join(lines).strip()))
            current_title = _HEADING_RE.sub("", line, count=1).strip()
            lines = []
            started = True
        else:
            lines.append(line)
            started = True
    if any(ln.strip() for ln in lines):
        sections.append((current_title, "\n".join(lines).strip()))
    return [(t, b) for t, b in sections if b]


def _paragraph_blocks(text: str) -> list[str]:
    """功能：空行分段（段落块为装箱最小单元）。"""
    return [p for p in (blk.strip() for blk in re.split(r"\n\s*\n", text)) if p]


def _hard_split(text: str, max_chars: int) -> list[str]:
    """功能：超长段落硬切（切分拼接与原文逐字相等——不静默丢内容）。"""
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def _chunk_text(text: str, max_chars: int = _DEFAULT_MAX_CHUNK_CHARS) -> list[str]:
    """功能：段块装箱分块——段块顺序拼接
    超长段块硬切；单块不超 max_chars（除非单段本身超长则独占硬切）。"""
    chunks: list[str] = []
    buffer = ""
    for block in _paragraph_blocks(text):
        if len(block) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_hard_split(block, max_chars))
            continue
        candidate = f"{buffer}\n\n{block}" if buffer else block
        if len(candidate) > max_chars and buffer:
            chunks.append(buffer)
            buffer = block
        else:
            buffer = candidate
    if buffer:
        chunks.append(buffer)
    return chunks


def _summarize(text: str, max_chars: int = 200) -> str:
    """功能：截断摘要（压缩空白，超限省略号收尾）。"""
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= max_chars:
        return compact
    return compact[: max(0, max_chars - 1)].rstrip() + "…"


class KnowledgeStore:
    def __init__(self, db_path: str | Path = ":memory:"):
        self._lock = threading.Lock()
        self._conn = connect_app_db(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_bases ("
                "  id TEXT PRIMARY KEY,"
                "  name TEXT NOT NULL DEFAULT '',"
                "  employee_id TEXT DEFAULT NULL,"
                "  project_id TEXT DEFAULT NULL,"
                "  metadata TEXT DEFAULT '{}',"
                "  created_at REAL NOT NULL,"
                "  updated_at REAL NOT NULL"
                ")"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_documents ("
                "  id TEXT PRIMARY KEY,"
                "  base_id TEXT NOT NULL,"
                "  filename TEXT NOT NULL,"
                "  title TEXT NOT NULL DEFAULT '',"
                "  status TEXT NOT NULL DEFAULT 'ready',"
                "  bucket_count INTEGER NOT NULL DEFAULT 0,"
                "  chunk_count INTEGER NOT NULL DEFAULT 0,"
                "  node_id TEXT DEFAULT NULL,"
                "  metadata TEXT DEFAULT '{}',"
                "  created_at REAL NOT NULL,"
                "  updated_at REAL NOT NULL,"
                "  UNIQUE(base_id, filename)"
                ")"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_buckets ("
                "  id TEXT PRIMARY KEY,"
                "  base_id TEXT NOT NULL,"
                "  document_id TEXT NOT NULL,"
                "  bucket_key TEXT NOT NULL,"
                "  title TEXT NOT NULL DEFAULT '',"
                "  summary TEXT NOT NULL DEFAULT '',"
                "  chunk_count INTEGER NOT NULL DEFAULT 0,"
                "  metadata TEXT DEFAULT '{}',"
                "  created_at REAL NOT NULL,"
                "  updated_at REAL NOT NULL"
                ")"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_concepts ("
                "  id TEXT PRIMARY KEY,"
                "  base_id TEXT NOT NULL,"
                "  document_id TEXT NOT NULL DEFAULT '',"
                "  concept_id TEXT NOT NULL,"
                "  concept_type TEXT NOT NULL DEFAULT 'Topic',"
                "  title TEXT NOT NULL DEFAULT '',"
                "  description TEXT NOT NULL DEFAULT '',"
                "  content_md TEXT NOT NULL DEFAULT '',"
                "  frontmatter TEXT DEFAULT '{}',"
                "  links TEXT DEFAULT '[]',"
                "  citations TEXT DEFAULT '[]',"
                "  source_refs TEXT DEFAULT '[]',"
                "  status TEXT NOT NULL DEFAULT 'active',"
                "  created_at REAL NOT NULL,"
                "  updated_at REAL NOT NULL,"
                "  UNIQUE(base_id, concept_id)"
                ")"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_chunks ("
                "  id TEXT PRIMARY KEY,"
                "  base_id TEXT NOT NULL,"
                "  document_id TEXT NOT NULL,"
                "  bucket_id TEXT NOT NULL,"
                "  chunk_index INTEGER NOT NULL,"
                "  content TEXT NOT NULL,"
                "  summary TEXT NOT NULL DEFAULT '',"
                "  source_ref TEXT NOT NULL DEFAULT '',"
                "  metadata TEXT DEFAULT '{}',"
                "  created_at REAL NOT NULL,"
                "  updated_at REAL NOT NULL"
                ")"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_doc ON knowledge_chunks(document_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_buckets_doc ON knowledge_buckets(document_id)"
            )
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts "
                "USING fts5(id UNINDEXED, content)"
            )
            self._ensure_document_node_column()
            self._conn.commit()

    def _ensure_document_node_column(self) -> None:
        # 功能：旧库补 node_id / node_employee_id 列——节点关联与员工可见域过滤。
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(knowledge_documents)").fetchall()
        }
        if columns and "node_id" not in columns:
            self._conn.execute(
                "ALTER TABLE knowledge_documents ADD COLUMN node_id TEXT DEFAULT NULL"
            )
        if columns and "node_employee_id" not in columns:
            self._conn.execute(
                "ALTER TABLE knowledge_documents ADD COLUMN node_employee_id TEXT DEFAULT NULL"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_node ON knowledge_documents(node_id)"
        )

    # -- 摄取 ---------------------------------------------------------------

    def ensure_base(
        self,
        base_id: str,
        name: str = "",
        employee_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """功能：确保知识库容器存在（幂等）——scope 维度取首建值。"""
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM knowledge_bases WHERE id = ?", (base_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO knowledge_bases "
                    "(id, name, employee_id, project_id, metadata, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, '{}', ?, ?)",
                    (base_id, name or base_id, employee_id, project_id, now, now),
                )
                self._conn.commit()

    def ingest_document(
        self,
        base_id: str,
        filename: str,
        text: str,
        title: str = "",
        metadata: dict[str, Any] | None = None,
        document_id: str | None = None,
    ) -> IngestResult:
        """功能：文档摄取三层投影——分节成桶、段块分 chunk、FTS 建索引；
        指定 document_id 时按 id 整组替换（引用保持稳定），否则按同 base 同 filename 替换。"""
        if not (text or "").strip():
            raise ValueError("ingest_document: text is blank")
        self.ensure_base(base_id)
        now = _now()
        # 重摄取是整组替换：以连接上下文管理事务（异常自动 rollback）——中途失败
        # （如 metadata 不可 JSON 序列化）不得留下「旧文档已删、新文档未写」的中间态
        with self._lock, self._conn:
            if document_id:
                old = self._conn.execute(
                    "SELECT id FROM knowledge_documents WHERE id = ?", (document_id,)
                ).fetchone()
                if old is None:
                    old = self._conn.execute(
                        "SELECT id FROM knowledge_documents WHERE base_id = ? AND filename = ?",
                        (base_id, filename),
                    ).fetchone()
            else:
                old = self._conn.execute(
                    "SELECT id FROM knowledge_documents WHERE base_id = ? AND filename = ?",
                    (base_id, filename),
                ).fetchone()
            if old is not None:
                self._delete_document_rows(old[0])
            doc_id = str(document_id or "").strip() or _new_id("kdoc")
            self._conn.execute(
                "INSERT INTO knowledge_documents "
                "(id, base_id, filename, title, status, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'ready', ?, ?, ?)",
                (
                    doc_id,
                    base_id,
                    filename,
                    title or Path(filename).stem,
                    json.dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            bucket_count = 0
            chunk_count = 0
            for section_title, body in _split_sections(text):
                bucket_id = _new_id("kbucket")
                bucket_key = _bucket_key(section_title, bucket_count)
                bucket_title = section_title or _summarize(body, 40)
                self._conn.execute(
                    "INSERT INTO knowledge_buckets "
                    "(id, base_id, document_id, bucket_key, title, summary, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bucket_id,
                        base_id,
                        doc_id,
                        bucket_key,
                        bucket_title,
                        _summarize(body),
                        now,
                        now,
                    ),
                )
                pieces = _chunk_text(body)
                for index, piece in enumerate(pieces):
                    chunk_id = _new_id("kchunk")
                    self._conn.execute(
                        "INSERT INTO knowledge_chunks "
                        "(id, base_id, document_id, bucket_id, chunk_index, content, "
                        " summary, source_ref, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            chunk_id,
                            base_id,
                            doc_id,
                            bucket_id,
                            index,
                            piece,
                            _summarize(piece),
                            filename,
                            now,
                            now,
                        ),
                    )
                    self._conn.execute(
                        "INSERT INTO knowledge_chunks_fts (id, content) VALUES (?, ?)",
                        (chunk_id, _fts_preprocess(piece)),
                    )
                self._conn.execute(
                    "UPDATE knowledge_buckets SET chunk_count = ? WHERE id = ?",
                    (len(pieces), bucket_id),
                )
                bucket_count += 1
                chunk_count += len(pieces)
            self._conn.execute(
                "UPDATE knowledge_documents SET bucket_count = ?, chunk_count = ? WHERE id = ?",
                (bucket_count, chunk_count, doc_id),
            )
            self._conn.commit()
        return IngestResult(document_id=doc_id, bucket_count=bucket_count, chunk_count=chunk_count)

    # -- 检索 ---------------------------------------------------------------

    def search_chunks(
        self,
        query: str,
        base_id: str | None = None,
        limit: int = _DEFAULT_SEARCH_LIMIT,
        base_ids: list[str] | None = None,
    ) -> list[KnowledgeChunk]:
        """功能：chunk FTS5 检索——2-gram AND 语义；FTS 异常或中文空命中
        回退 LIKE 保召回；base_id 单库过滤 / base_ids 多库可见域过滤（None=
        不过滤，回退 LIKE 路径同收口）。检索链绝不抛异常。"""
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit), _MAX_SEARCH_LIMIT))
        has_cjk = bool(_CJK_RE.search(query))
        if has_cjk:
            grams = [g for g in _fts_preprocess(query).split() if g]
            fts_query = " AND ".join(f'"{g}"' for g in grams) if grams else " "
        else:
            fts_query = self._sanitize_fts_query(query)
        rows = None
        with self._lock:
            base_clause, base_args = self._base_clause(base_id, base_ids=base_ids)
            try:
                rows = self._conn.execute(
                    "SELECT c.id, c.base_id, c.document_id, c.bucket_id, c.chunk_index, "
                    " c.content, c.summary, c.source_ref, c.metadata, c.created_at, c.updated_at "
                    "FROM knowledge_chunks_fts f JOIN knowledge_chunks c ON f.id = c.id "
                    "WHERE knowledge_chunks_fts MATCH ?"
                    f"{base_clause} "
                    "ORDER BY rank LIMIT ?",
                    (fts_query, *base_args, limit),
                ).fetchall()
            except Exception:  # noqa: BLE001
                rows = None
        if rows is None or (not rows and has_cjk):
            return self._search_like(query, base_id=base_id, limit=limit, base_ids=base_ids)
        return [self._row_to_chunk(r) for r in rows]

    def _search_like(
        self,
        query: str,
        base_id: str | None,
        limit: int,
        base_ids: list[str] | None = None,
    ) -> list[KnowledgeChunk]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            base_clause, base_args = self._base_clause(base_id, base_ids=base_ids)
            rows = self._conn.execute(
                "SELECT id, base_id, document_id, bucket_id, chunk_index, content, "
                " summary, source_ref, metadata, created_at, updated_at "
                "FROM knowledge_chunks c "
                "WHERE c.content LIKE ? ESCAPE '\\'"
                f"{base_clause} "
                "ORDER BY document_id, chunk_index LIMIT ?",
                (f"%{escaped}%", *base_args, limit),
            ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    @staticmethod
    def _base_clause(
        base_id: str | None, base_ids: list[str] | None = None
    ) -> tuple[str, list[str]]:
        if base_id:
            return " AND c.base_id = ?", [base_id]
        if base_ids is not None:
            if not base_ids:
                # fail-closed：可见域显式为空 = 全禁（占位条件永假，防参数化 IN 空列表 SQL 错误）
                return " AND 1 = 0", []
            placeholders = ", ".join("?" for _ in base_ids)
            return f" AND c.base_id IN ({placeholders})", list(base_ids)
        return " AND c.base_id IN (SELECT id FROM knowledge_bases)", []

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        query = "".join(c for c in query if c.isalnum() or c in ' "*.-_')
        return query.strip() or " "

    # -- 读回 / 删除 ---------------------------------------------------------

    def has_base(self, base_id: str) -> bool:
        """功能：判断知识库容器是否存在（能力档案 knowledge_base 绑定校验消费）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM knowledge_bases WHERE id = ?", (base_id,)
            ).fetchone()
        return row is not None

    def list_bases(self, employee_id: str | None = None) -> list[KnowledgeBase]:
        """功能：知识库容器列表（可选按员工归属过滤——个体员工私有桶数据源）。"""
        sql = (
            "SELECT id, name, employee_id, project_id, created_at, updated_at FROM knowledge_bases"
        )
        params: list[Any] = []
        if employee_id is not None:
            sql += " WHERE employee_id = ?"
            params.append(employee_id)
        sql += " ORDER BY created_at, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            KnowledgeBase(
                id=row[0],
                name=row[1],
                employee_id=row[2],
                project_id=row[3],
                created_at=row[4] or 0.0,
                updated_at=row[5] or 0.0,
            )
            for row in rows
        ]

    def get_document(self, document_id: str) -> KnowledgeDocument | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, base_id, filename, title, status, bucket_count, chunk_count, "
                " node_employee_id, metadata, created_at, updated_at "
                "FROM knowledge_documents WHERE id = ?",
                (document_id,),
            ).fetchone()
        return self._row_to_document(row) if row else None

    def list_documents(self, base_id: str) -> list[KnowledgeDocument]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, base_id, filename, title, status, bucket_count, chunk_count, "
                " node_employee_id, metadata, created_at, updated_at "
                "FROM knowledge_documents WHERE base_id = ? ORDER BY created_at",
                (base_id,),
            ).fetchall()
        return [self._row_to_document(r) for r in rows]

    def list_all_documents(self, base_ids: list[str] | None = None) -> list[KnowledgeDocument]:
        """功能：跨知识库全量文档清单（catalog 工具消费，按库+时间序）；
        base_ids 非空 = 可见域过滤（None = 不过滤）。"""
        if base_ids is None:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, base_id, filename, title, status, bucket_count, chunk_count, "
                    " node_employee_id, metadata, created_at, updated_at "
                    "FROM knowledge_documents ORDER BY base_id, created_at"
                ).fetchall()
        else:
            if not base_ids:
                return []
            placeholders = ", ".join("?" for _ in base_ids)
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, base_id, filename, title, status, bucket_count, chunk_count, "
                    " node_employee_id, metadata, created_at, updated_at "
                    f"FROM knowledge_documents WHERE base_id IN ({placeholders}) "
                    "ORDER BY base_id, created_at",
                    list(base_ids),
                ).fetchall()
        return [self._row_to_document(r) for r in rows]

    def list_buckets(self, document_id: str) -> list[KnowledgeBucket]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, base_id, document_id, bucket_key, title, summary, chunk_count, "
                " metadata, created_at, updated_at "
                "FROM knowledge_buckets WHERE document_id = ? ORDER BY created_at",
                (document_id,),
            ).fetchall()
        return [self._row_to_bucket(r) for r in rows]

    def list_chunks(
        self, document_id: str | None = None, bucket_id: str | None = None
    ) -> list[KnowledgeChunk]:
        conditions: list[str] = []
        args: list[Any] = []
        if document_id:
            conditions.append("document_id = ?")
            args.append(document_id)
        if bucket_id:
            conditions.append("bucket_id = ?")
            args.append(bucket_id)
        clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, base_id, document_id, bucket_id, chunk_index, content, "
                f" summary, source_ref, metadata, created_at, updated_at "
                f"FROM knowledge_chunks {clause} ORDER BY document_id, chunk_index",
                args,
            ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, base_id, document_id, bucket_id, chunk_index, content, "
                " summary, source_ref, metadata, created_at, updated_at "
                "FROM knowledge_chunks WHERE id = ?",
                (chunk_id,),
            ).fetchone()
        return self._row_to_chunk(row) if row else None

    def delete_document(self, document_id: str) -> None:
        """功能：级联删除文档三层（chunk 主表 + FTS 行 + bucket + document）。"""
        with self._lock:
            self._delete_document_rows(document_id)
            self._conn.commit()

    def set_document_node(self, document_id: str, node_id: str | None) -> None:
        # 功能：绑定文档到资料库节点——节点删除时按 node_id 级联清理。
        with self._lock:
            self._conn.execute(
                "UPDATE knowledge_documents SET node_id = ?, updated_at = ? WHERE id = ?",
                (node_id, _now(), document_id),
            )
            self._conn.commit()

    def set_document_employee(self, document_id: str, employee_id: str | None) -> None:
        # 功能：设置文档的员工可见域——检索时按它收窄（None = 共享）。
        with self._lock:
            self._conn.execute(
                "UPDATE knowledge_documents SET node_employee_id = ?, updated_at = ? WHERE id = ?",
                (employee_id, _now(), document_id),
            )
            self._conn.commit()

    def document_id_for_node(self, node_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT id FROM knowledge_documents WHERE node_id = ?", (node_id,)
        ).fetchone()
        return str(row[0]) if row else None

    def document_id_for_filename(self, base_id: str, filename: str) -> str | None:
        # 功能：同名文档归属查询——资料库跨文件夹同名时用它决定是否加节点后缀。
        row = self._conn.execute(
            "SELECT id FROM knowledge_documents WHERE base_id = ? AND filename = ?",
            (base_id, filename),
        ).fetchone()
        return str(row[0]) if row else None

    def rename_document(self, document_id: str, filename: str, title: str = "") -> bool:
        # 功能：节点重命名后同步文档名——同名冲突（UNIQUE）返回 False，调用方保留旧名。
        import sqlite3

        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE knowledge_documents SET filename = ?, title = ?, updated_at = ?"
                    " WHERE id = ?",
                    (filename, title or filename.rsplit(".", 1)[0], _now(), document_id),
                )
                self._conn.commit()
        except sqlite3.IntegrityError:
            return False
        return True

    def delete_documents_for_nodes(self, node_ids: list[str]) -> list[str]:
        # 功能：按节点批量级联删除文档（chunks + FTS + buckets + concepts + documents）。
        if not node_ids:
            return []
        placeholders = ",".join("?" for _ in node_ids)
        rows = self._conn.execute(
            f"SELECT id FROM knowledge_documents WHERE node_id IN ({placeholders})",
            node_ids,
        ).fetchall()
        document_ids = [str(row[0]) for row in rows]
        with self._lock:
            for document_id in document_ids:
                self._delete_document_rows(document_id)
            self._conn.commit()
        return document_ids

    def _delete_document_rows(self, document_id: str) -> None:
        # 功能：逐行删除 FTS 再删三层正文——大文档批量 IN 子查询会触发 FTS5 合并错误。
        chunk_ids = [
            str(row[0])
            for row in self._conn.execute(
                "SELECT id FROM knowledge_chunks WHERE document_id = ?", (document_id,)
            )
        ]
        for chunk_id in chunk_ids:
            self._conn.execute("DELETE FROM knowledge_chunks_fts WHERE id = ?", (chunk_id,))
        self._conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        self._conn.execute("DELETE FROM knowledge_buckets WHERE document_id = ?", (document_id,))
        self._conn.execute("DELETE FROM knowledge_concepts WHERE document_id = ?", (document_id,))
        self._conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))

    # -- 概念（OKF）----------------------------------------------------------

    def upsert_concepts(
        self, base_id: str, concepts: list[dict[str, Any]]
    ) -> list[KnowledgeConcept]:
        """功能：概念 upsert（幂等）——同 (base_id, concept_id) 更新不重复；
        concept_id 归一化 + content_md 解析回填 frontmatter/正文；无效项丢弃。"""
        from bandi.knowledge.okf import upsert_candidates

        now = _now()
        rows: list[KnowledgeConcept] = []
        with self._lock:
            for item in upsert_candidates(concepts):
                concept_id = item["concept_id"]
                parsed = parse_okf_markdown(concept_id, item["content_md"])
                frontmatter, body = parsed.frontmatter, parsed.body
                content_md = parsed.content_md
                title = str(
                    frontmatter.get("title") or item.get("title") or concept_id.rsplit("/", 1)[-1]
                ).strip()
                concept_type = str(frontmatter.get("type") or item.get("concept_type") or "Topic")
                description = str(frontmatter.get("description") or item.get("description") or "")
                document_id = str(item.get("document_id") or "")
                source_refs = (
                    item.get("source_refs") if isinstance(item.get("source_refs"), list) else []
                )
                links = extract_links(body)
                citations = extract_citations(content_md)
                existing = self._conn.execute(
                    "SELECT id, created_at FROM knowledge_concepts "
                    "WHERE base_id = ? AND concept_id = ?",
                    (base_id, concept_id),
                ).fetchone()
                if existing is not None:
                    row_id, created_at = existing
                    self._conn.execute(
                        "UPDATE knowledge_concepts SET document_id = ?, concept_type = ?, "
                        "title = ?, description = ?, content_md = ?, frontmatter = ?, "
                        "links = ?, citations = ?, source_refs = ?, status = 'active', "
                        "updated_at = ? WHERE id = ?",
                        (
                            document_id,
                            concept_type,
                            title,
                            description,
                            content_md,
                            _dumps(frontmatter),
                            _dumps(links),
                            _dumps(citations),
                            _dumps(source_refs),
                            now,
                            row_id,
                        ),
                    )
                else:
                    row_id = _new_id("kcpt")
                    created_at = now
                    self._conn.execute(
                        "INSERT INTO knowledge_concepts "
                        "(id, base_id, document_id, concept_id, concept_type, title, "
                        " description, content_md, frontmatter, links, citations, "
                        " source_refs, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                        (
                            row_id,
                            base_id,
                            document_id,
                            concept_id,
                            concept_type,
                            title,
                            description,
                            content_md,
                            _dumps(frontmatter),
                            _dumps(links),
                            _dumps(citations),
                            _dumps(source_refs),
                            created_at,
                            now,
                        ),
                    )
                rows.append(
                    KnowledgeConcept(
                        id=row_id,
                        base_id=base_id,
                        document_id=document_id,
                        concept_id=concept_id,
                        concept_type=concept_type,
                        title=title,
                        description=description,
                        content_md=content_md,
                        frontmatter=frontmatter,
                        links=links,
                        citations=citations,
                        source_refs=source_refs,
                        created_at=created_at,
                        updated_at=now,
                    )
                )
            self._conn.commit()
        return rows

    def list_concepts(self, base_id: str) -> list[KnowledgeConcept]:
        """功能：列知识库全部概念（created_at 序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, base_id, document_id, concept_id, concept_type, title, "
                " description, content_md, frontmatter, links, citations, source_refs, "
                " status, created_at, updated_at "
                "FROM knowledge_concepts WHERE base_id = ? ORDER BY created_at",
                (base_id,),
            ).fetchall()
        return [self._row_to_concept(r) for r in rows]

    def list_all_concepts(self, base_ids: list[str] | None = None) -> list[KnowledgeConcept]:
        """功能：跨知识库全量概念清单（检索面消费）；base_ids 非空 = 可见域过滤
        （None = 不过滤）。"""
        if base_ids is None:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, base_id, document_id, concept_id, concept_type, title, "
                    " description, content_md, frontmatter, links, citations, source_refs, "
                    " status, created_at, updated_at "
                    "FROM knowledge_concepts ORDER BY base_id, created_at"
                ).fetchall()
        else:
            if not base_ids:
                return []
            placeholders = ", ".join("?" for _ in base_ids)
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, base_id, document_id, concept_id, concept_type, title, "
                    " description, content_md, frontmatter, links, citations, source_refs, "
                    " status, created_at, updated_at "
                    f"FROM knowledge_concepts WHERE base_id IN ({placeholders}) "
                    "ORDER BY base_id, created_at",
                    list(base_ids),
                ).fetchall()
        return [self._row_to_concept(r) for r in rows]

    @staticmethod
    def _row_to_concept(row: tuple) -> KnowledgeConcept:
        return KnowledgeConcept(
            id=row[0],
            base_id=row[1],
            document_id=row[2],
            concept_id=row[3],
            concept_type=row[4],
            title=row[5],
            description=row[6],
            content_md=row[7],
            frontmatter=_loads(row[8]),
            links=_loads_list(row[9]),
            citations=_loads_list(row[10]),
            source_refs=_loads_list(row[11]),
            status=row[12],
            created_at=row[13],
            updated_at=row[14],
        )

    # -- 行映射 --------------------------------------------------------------

    @staticmethod
    def _row_to_document(row: tuple) -> KnowledgeDocument:
        return KnowledgeDocument(
            id=row[0],
            base_id=row[1],
            filename=row[2],
            title=row[3],
            status=row[4],
            bucket_count=int(row[5]),
            chunk_count=int(row[6]),
            node_employee_id=(str(row[7]) if row[7] else None),
            metadata=_loads(row[8]),
            created_at=row[9],
            updated_at=row[10],
        )

    @staticmethod
    def _row_to_bucket(row: tuple) -> KnowledgeBucket:
        return KnowledgeBucket(
            id=row[0],
            base_id=row[1],
            document_id=row[2],
            bucket_key=row[3],
            title=row[4],
            summary=row[5],
            chunk_count=int(row[6]),
            metadata=_loads(row[7]),
            created_at=row[8],
            updated_at=row[9],
        )

    @staticmethod
    def _row_to_chunk(row: tuple) -> KnowledgeChunk:
        return KnowledgeChunk(
            id=row[0],
            base_id=row[1],
            document_id=row[2],
            bucket_id=row[3],
            chunk_index=int(row[4]),
            content=row[5],
            summary=row[6],
            source_ref=row[7],
            metadata=_loads(row[8]),
            created_at=row[9],
            updated_at=row[10],
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_SHARED_STORE: KnowledgeStore | None = None
_SHARED_STORE_LOCK = threading.Lock()


def get_shared_store() -> KnowledgeStore:
    """功能：进程级共享知识库单例（~/.bandi/data/knowledge.db）——
    工具面/API 面/检索面同一存储，避免多连接写竞争；data 目录不存在时自动创建。"""
    global _SHARED_STORE
    with _SHARED_STORE_LOCK:
        if _SHARED_STORE is None:
            data_dir = Path.home() / ".bandi" / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            _SHARED_STORE = KnowledgeStore(db_path=str(data_dir / "knowledge.db"))
        return _SHARED_STORE


def _bucket_key(section_title: str, index: int) -> str:
    """功能：桶键——标题 slug 归一（非空）否则序号键，保证同文档内稳定可定位。"""
    slug = re.sub(r"\s+", "-", section_title.strip()) if section_title else ""
    return slug or f"section-{index}"


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _loads_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)
