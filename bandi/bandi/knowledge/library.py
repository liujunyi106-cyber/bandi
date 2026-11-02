# 功能：资料库服务——上传落盘、文本抽取 ingest、节点删除级联（文档三层 + 视图态 + 缓存 + 文件）。

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from bandi.knowledge.extract import extract_text
from bandi.knowledge.files import PreviewService, detect_kind, normalize_text
from bandi.knowledge.nodes import KnowledgeNode, KnowledgeNodeStore
from bandi.knowledge.storage import NodeFileStore
from bandi.knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)

BACKGROUND_INDEX_BYTES = 200 * 1024


class KnowledgeLibraryService:
    # 功能：资料库写操作入口——节点树与文档/缓存/磁盘文件保持一致。
    def __init__(
        self,
        nodes: KnowledgeNodeStore,
        documents: KnowledgeStore,
        files_root: str | Path,
        preview: PreviewService | None = None,
        files: NodeFileStore | None = None,
    ) -> None:
        self._nodes = nodes
        self._documents = documents
        self._root = Path(files_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._preview = preview
        self._files = files or NodeFileStore(self._root)

    def upload(
        self,
        base_id: str,
        parent_id: str,
        filename: str,
        data: bytes,
        employee_id: str | None = None,
    ) -> KnowledgeNode:
        # 功能：校验文件 → 建节点 → 原子写盘 → 抽文本 ingest → 回填 storage_path/size。
        kind = detect_kind(filename, data)
        if kind in {"md", "csv", "html"}:
            data = normalize_text(data)
        if employee_id is None and parent_id:
            parent = self._nodes.get_node(parent_id)
            employee_id = parent.employee_id if parent is not None else None
        node = self._nodes.create_node(
            base_id,
            filename,
            kind,
            parent_id,
            employee_id=employee_id,
        )
        self._files.write(node.id, node.name, data)
        updated = self._nodes.set_storage(node.id, f"{node.id}/{node.name}", len(data))
        assert updated is not None
        if len(data) > BACKGROUND_INDEX_BYTES:
            self.reindex_async(updated)
        else:
            self.reindex(updated, text=extract_text(kind, data))
        return updated

    def rename(self, node_id: str, name: str) -> KnowledgeNode:
        # 功能：重命名节点——先移磁盘文件与 .bak，再改节点行与索引文件名。
        node = self._nodes.get_node(node_id)
        if node is None:
            raise ValueError(f"node not found: {node_id}")
        new_name = self._nodes.unique_name(node.base_id, node.parent_id, name, exclude_id=node_id)
        if new_name != node.name:
            self._files.rename(node.id, node.name, new_name)
        updated = self._nodes.rename_node(node_id, new_name)
        self.refresh_document_name(updated)
        return updated

    def reindex(self, node: KnowledgeNode, text: str | None = None) -> Any:
        # 功能：把节点内容同步进检索索引——沿用原 document_id，文件名冲突时加节点后缀。
        if node.kind == "folder":
            return None
        payload = text
        if payload is None:
            source = self._root / node.storage_path if node.storage_path else None
            if source is None or not source.exists():
                return None
            payload = extract_text(node.kind, source.read_bytes())
        if not (payload or "").strip():
            return None
        document_id = self._documents.document_id_for_node(node.id)
        filename = self.indexed_filename(node, document_id)
        result = self._documents.ingest_document(
            node.base_id,
            filename,
            payload,
            title=Path(node.name).stem,
            document_id=document_id,
        )
        self._documents.set_document_node(result.document_id, node.id)
        self._documents.set_document_employee(result.document_id, node.employee_id)
        return result

    def reindex_async(self, node: KnowledgeNode, text: str | None = None) -> None:
        # 功能：后台线程重建索引——大文件不阻塞保存/上传请求。
        def run() -> None:
            try:
                self.reindex(node, text=text)
            except Exception:
                logger.exception("background knowledge index failed: %s", node.id)

        threading.Thread(
            target=run,
            name=f"knowledge-index-{node.id}",
            daemon=True,
        ).start()

    def save_snapshot(self, node: KnowledgeNode, html: str) -> float:
        # 功能：保存链接快照到节点 assets，返回快照 mtime。
        self._files.save_asset(node.id, "snapshot.html", html.encode("utf-8"))
        return self._files.asset_path(node.id, "snapshot.html").stat().st_mtime

    def snapshot(self, node: KnowledgeNode) -> tuple[str, float] | None:
        # 功能：读取已保存的链接快照；无快照返回 None。
        path = self._files.asset_path(node.id, "snapshot.html")
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8", errors="replace"), path.stat().st_mtime

    def refresh_document_name(self, node: KnowledgeNode) -> None:        # 功能：节点改名后同步索引里的文件名/标题——同名冲突时保留带后缀的唯一名。
        document_id = self._documents.document_id_for_node(node.id)
        if not document_id:
            return
        filename = self.indexed_filename(node, document_id)
        self._documents.rename_document(document_id, filename, Path(node.name).stem)

    def indexed_filename(self, node: KnowledgeNode, document_id: str | None) -> str:
        # 功能：索引文件名——跨文件夹同名时追加节点短 id，避免 UNIQUE(base, filename) 互覆盖。
        filename = node.name
        owner = self._documents.document_id_for_filename(node.base_id, filename)
        if not owner or owner == document_id:
            return filename
        stem, dot, ext = filename.rpartition(".")
        tag = f" [{node.id[:6]}]"
        if dot and stem and len(ext) <= 10:
            return f"{stem}{tag}.{ext}"
        return f"{filename}{tag}"

    def delete_node(self, node_id: str) -> dict[str, int]:
        # 功能：硬删节点子树——先级联文档三层/视图态/预览缓存，再删节点行与物理目录。
        node = self._nodes.get_node(node_id)
        if node is None:
            return {"nodes": 0, "documents": 0}
        removed_ids = self._nodes.descendant_ids(node_id) + [node_id]
        document_ids = self._documents.delete_documents_for_nodes(removed_ids)
        if self._preview is not None:
            for removed_id in removed_ids:
                self._preview.purge(f"node-{removed_id}")
            for document_id in document_ids:
                self._preview.purge(document_id)
        self._nodes.delete_views(removed_ids)
        self._nodes.delete_node(node_id)
        for removed_id in removed_ids:
            self._remove_node_dir(removed_id)
        return {"nodes": len(removed_ids), "documents": len(document_ids)}

    def _remove_node_dir(self, node_id: str) -> None:
        # 功能：删除节点目录——Windows 瞬时占用（安全软件/预览）时重试三次，失败仅告警。
        target = self._root / node_id
        for attempt in range(3):
            try:
                shutil.rmtree(target)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt == 2:
                    logger.warning("knowledge node dir cleanup failed: %s", target)
                else:
                    time.sleep(0.2)
