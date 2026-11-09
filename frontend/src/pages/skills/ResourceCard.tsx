/**
 * 功能：资源中心卡片（技能 / SOP / MCP 连接器）。
 */
import { Globe, Users } from "lucide-react";

import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import type { McpServer } from "@/services/settings";
import type { ResourceItem } from "@/services/resources";

function scopeText(item: ResourceItem): string {
  if (item.scope.employees.length === 0) return "全局";
  const first = item.scope.employees[0]?.name ?? "";
  const extra = item.scope.employees.length - 1;
  return extra > 0 ? `${first} +${extra}` : first;
}

export interface ResourceCardProps {
  item: ResourceItem;
  busy?: boolean;
  onOpen: () => void;
  onToggle: (enabled: boolean) => void;
  onEdit?: () => void;
}

/** 功能：技能 / SOP 卡片。 */
export function ResourceCard({ item, busy, onOpen, onToggle, onEdit }: ResourceCardProps) {
  const globalScope = item.scope.employees.length === 0;
  return (
    <article
      role="button"
      tabIndex={0}
      data-testid={`resource-card-${item.kind}-${item.id}`}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen();
        }
      }}
      className="group bg-card border-chat-hairline hover:bg-primary/[0.02] flex min-h-[132px] cursor-pointer flex-col rounded-2xl border p-4 transition-colors"
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3
            className={cn(
              "text-chat-ink truncate text-sm font-semibold",
              !item.enabled && "opacity-50",
            )}
          >
            {item.title}
          </h3>
          <p
            className={cn(
              "text-chat-muted mt-1.5 line-clamp-2 min-h-9 text-xs leading-[18px]",
              !item.enabled && "opacity-50",
            )}
          >
            {item.description || "暂无说明"}
          </p>
        </div>
        <Switch
          checked={item.enabled}
          disabled={busy}
          aria-label={`${item.enabled ? "停用" : "启用"} ${item.title}`}
          onClick={(event) => event.stopPropagation()}
          onCheckedChange={(next) => onToggle(next)}
        />
      </div>
      <div className="text-chat-muted mt-auto flex items-center gap-2 pt-3 text-[11px]">
        <span className="inline-flex min-w-0 items-center gap-1.5">
          {globalScope ? <Globe className="size-3" /> : <Users className="size-3" />}
          <span className="truncate">{scopeText(item)}</span>
        </span>
        {item.kind === "sop" && onEdit ? (
          <button
            type="button"
            className="bg-primary/5 text-chat-ink hover:bg-primary/10 ml-auto shrink-0 rounded-md px-2 py-0.5 text-[10px] font-medium"
            onClick={(event) => {
              event.stopPropagation();
              onEdit();
            }}
          >
            可视化编辑
          </button>
        ) : null}
      </div>
    </article>
  );
}

export interface ConnectorCardProps {
  item: McpServer;
  busy?: boolean;
  onOpen: () => void;
  onToggle: (enabled: boolean) => void;
}

/** 功能：MCP 连接器卡片（点击打开配置弹窗，无详情页）。 */
export function ConnectorCard({ item, busy, onOpen, onToggle }: ConnectorCardProps) {
  const enabled = item.enabled;
  const statusLabel = !enabled ? "已停用" : item.status === "connected" ? "已连接" : "空闲";
  const dotClass = !enabled
    ? "bg-[#B4B2A9]"
    : item.status === "connected"
      ? "bg-[#1D9E75]"
      : "bg-[#C49A4A]";
  const description =
    item.transport === "stdio"
      ? `stdio · ${[item.command, ...item.args].filter(Boolean).join(" ")}`
      : `${item.transport} · ${item.url}`;
  return (
    <article
      role="button"
      tabIndex={0}
      data-testid={`resource-card-mcp-${item.name}`}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen();
        }
      }}
      className="group bg-card border-chat-hairline hover:bg-primary/[0.02] flex min-h-[132px] cursor-pointer flex-col rounded-2xl border p-4 transition-colors"
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3
            className={cn(
              "text-chat-ink truncate text-sm font-semibold",
              !enabled && "opacity-50",
            )}
          >
            {item.name}
          </h3>
          <p
            className={cn(
              "text-chat-muted mt-1.5 line-clamp-2 min-h-9 text-xs leading-[18px]",
              !enabled && "opacity-50",
            )}
          >
            {description}
          </p>
        </div>
        <Switch
          checked={enabled}
          disabled={busy}
          aria-label={`${enabled ? "停用" : "启用"} ${item.name}`}
          onClick={(event) => event.stopPropagation()}
          onCheckedChange={(next) => onToggle(next)}
        />
      </div>
      <div className="text-chat-muted mt-auto flex items-center gap-2 pt-3 text-[11px]">
        <span className="inline-flex items-center gap-1.5">
          <Globe className="size-3" />
          全局
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className={cn("size-1.5 rounded-full", dotClass)} />
          {statusLabel}
        </span>
        <span aria-hidden="true">·</span>
        <span>{item.tool_count > 0 ? `${item.tool_count} 个工具` : "待连接"}</span>
      </div>
    </article>
  );
}
