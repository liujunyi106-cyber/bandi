"""

enricher（记忆增强）、hallucination_checker（L1 实体锚定）、context_loader（@提及解析）
都只消费「按名字/ID 找实体 → 读 name」这一最小只读面；本模块用同一适配覆盖三方。
"""

from __future__ import annotations

from dataclasses import dataclass

from bandi.ontology.triple_store import OntologyStore


@dataclass(frozen=True)
class EntityLookupRow:
    """功能：只读实体行（旧消费方读取 name/description 的等价形状）。"""

    id: str
    name: str
    entity_type: str = ""
    description: str = ""


class OntologyEntityLookup:
    """功能：三元组本体的实体查询适配——get_entity(按 ID) + find_entities(按名字模糊)。"""

    def __init__(self, store: OntologyStore) -> None:
        self._store = store

    def get_entity(self, entity_id: str) -> EntityLookupRow | None:
        """功能：按实体 ID 精确取一行；不存在返回 None。"""
        entity = self._store.graph().entities.get(str(entity_id or "").strip())
        return self._row(entity) if entity is not None else None

    def find_entities(
        self, query: str = "", limit: int = 5, project_id: str | None = None
    ) -> list[EntityLookupRow]:
        """功能：按 ID/中文名子串模糊匹配（大小写不敏感）；空查询返回空。

        `project_id` 为旧调用方保留的参数——三元组本体实体是全局层，不透传。
        """
        keyword = str(query or "").strip().lower()
        if not keyword:
            return []
        rows: list[EntityLookupRow] = []
        entities = sorted(self._store.graph().entities.values(), key=lambda item: item.id)
        for entity in entities:
            label = str(entity.label or "")
            if keyword not in label.lower() and keyword not in entity.id.lower():
                continue
            rows.append(self._row(entity))
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    @staticmethod
    def _row(entity: object) -> EntityLookupRow:
        """功能：实体 → 只读行（name=label 优先，类名列表进 description）。"""
        classes = getattr(entity, "classes", ()) or ()
        entity_id = str(getattr(entity, "id", ""))
        label = str(getattr(entity, "label", "") or entity_id)
        entity_type = str(classes[0]) if classes else ""
        return EntityLookupRow(
            id=entity_id,
            name=label,
            entity_type=entity_type,
            description=f"类：{'、'.join(classes)}" if classes else "",
        )


__all__ = ["EntityLookupRow", "OntologyEntityLookup"]
