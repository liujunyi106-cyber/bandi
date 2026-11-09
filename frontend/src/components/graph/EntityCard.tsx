/**
 * 功能：选中实体的详情卡。
 * 只读呈现 + 关联实体可点（点即切换选中，形成图谱内的邻接漫游）。
 */
import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { entityTypeLabelKey, typeColorOf } from "@/components/graph/graph-visual";
import { useT } from "@/i18n";
import type { GraphEntity } from "@/services/graph";

export interface EntityCardProps {
  entity: GraphEntity;
  /** 直接关联实体（已去重、按名称排序）。 */
  neighbors: GraphEntity[];
  /** 无选中实体时展示的引导文案容器（由页面控制，这里只处理有选中）。 */
  onSelectNeighbor: (id: string) => void;
  onClose: () => void;
  /** 类型 → 颜色映射（与画布/图例同源）。 */
  typeColors: ReadonlyMap<string, string>;
}

export function EntityCard({
  entity,
  neighbors,
  onSelectNeighbor,
  onClose,
  typeColors,
}: EntityCardProps) {
  const t = useT();

  return (
    // 画布内浮动详情浮层：固定右上角出现、不挤占画布布局（定位由 GraphCanvas 插槽承担）
    <aside className="border-border bg-card/95 flex min-h-0 w-full flex-1 flex-col overflow-hidden rounded-lg border shadow-float backdrop-blur">
      <header className="flex items-start gap-2 p-4">
        <span
          className="mt-1 size-2.5 shrink-0 rounded-full"
          style={{ backgroundColor: typeColorOf(typeColors, entity.type) }}
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <h3 className="text-foreground truncate text-sm font-semibold" title={entity.name}>
            {entity.name}
          </h3>
          <p className="text-muted-foreground mt-0.5 text-xs">
            {t(entityTypeLabelKey(entity.type))}
          </p>
        </div>
        <Button variant="ghost" size="icon" aria-label={t("graph.detailClose")} onClick={onClose}>
          <X />
        </Button>
      </header>

      <Separator />

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 p-4 text-xs">
        <dt className="text-muted-foreground">{t("graph.detailId")}</dt>
        <dd className="text-foreground truncate font-mono" title={entity.id}>
          {entity.id}
        </dd>
        <dt className="text-muted-foreground">{t("graph.detailDegree")}</dt>
        <dd className="text-foreground tabular-nums">{neighbors.length}</dd>
      </dl>

      <Separator />

      <div className="flex min-h-0 flex-1 flex-col p-4">
        <h4 className="text-muted-foreground mb-2 text-xs font-medium">
          {t("graph.detailNeighbors")}
        </h4>
        {neighbors.length === 0 ? (
          <p className="text-muted-foreground text-xs">{t("graph.detailNoNeighbors")}</p>
        ) : (
          <ul className="-mx-1 min-h-0 flex-1 space-y-0.5 overflow-auto">
            {neighbors.map((neighbor) => (
              <li key={neighbor.id}>
                <button
                  type="button"
                  className="hover:bg-primary/5 focus-visible:ring-ring/50 flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs outline-none focus-visible:ring-2"
                  onClick={() => onSelectNeighbor(neighbor.id)}
                >
                  <span
                    className="size-2 shrink-0 rounded-full"
                    style={{ backgroundColor: typeColorOf(typeColors, neighbor.type) }}
                    aria-hidden="true"
                  />
                  <span className="text-foreground truncate" title={neighbor.name}>
                    {neighbor.name}
                  </span>
                  <span className="text-muted-foreground ml-auto shrink-0">
                    {t(entityTypeLabelKey(neighbor.type))}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
