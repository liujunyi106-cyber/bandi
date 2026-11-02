/**
 * 功能：员工画廊卡片（头像 / 团队归属 / 模型 / 能力统计 / 更多菜单）。
 * 点击卡片打开员工档案；菜单操作与卡片点击相互隔离。
 */
import { MessageCircle, MoreHorizontal, Pencil, Power, Trash2 } from "lucide-react";

import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import type { EmployeeSummary } from "@/services/company";
import type { EmployeeStats } from "@/services/team";

export interface EmployeeCardProps {
  employee: EmployeeSummary;
  stats: EmployeeStats;
  onOpen: () => void;
  onChat: () => void;
  onEdit: () => void;
  onToggleStatus: () => void;
  onRetire: () => void;
}

/** 功能：员工卡片主体与操作菜单。 */
export function EmployeeCard({
  employee,
  stats,
  onOpen,
  onChat,
  onEdit,
  onToggleStatus,
  onRetire,
}: EmployeeCardProps) {
  const t = useT();
  const modelName = employee.llm_config?.default_model?.trim() || t("team.card.globalModel");
  const online = (employee.status ?? "active") === "active";
  const capabilities = [
    { key: "skills", value: stats.skills, label: t("team.card.skills") },
    { key: "knowledge", value: stats.knowledge, label: t("team.card.knowledge") },
    { key: "sop", value: stats.sop, label: t("team.card.sop") },
  ];

  return (
    <div
      role="button"
      tabIndex={0}
      data-testid={`employee-card-${employee.employee_id}`}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen();
        }
      }}
      className="group bg-card border-chat-hairline hover:bg-primary/[0.02] flex cursor-pointer flex-col rounded-2xl border p-4 transition-colors"
    >
      <div className="flex items-start gap-3">
        <EmployeeAvatar
          name={employee.name}
          metadata={employee.metadata}
          className="size-12 text-base"
        />
        <div className="min-w-0 flex-1">
          <strong className="text-chat-ink block truncate text-sm font-semibold">
            {employee.name}
          </strong>
          <p className="text-chat-muted mt-1 truncate text-[11px]">
            {t("team.card.model")} {modelName}
          </p>
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={t("team.card.more")}
              className="text-chat-muted shrink-0 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
              onClick={(event) => event.stopPropagation()}
            >
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            className="w-40"
            onClick={(event) => event.stopPropagation()}
          >
            <DropdownMenuItem onSelect={onChat} disabled={!online}>
              <MessageCircle />
              {t("team.card.chat")}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={onEdit}>
              <Pencil />
              {t("team.card.edit")}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={onToggleStatus}>
              <Power />
              {online ? t("team.card.offline") : t("team.card.online")}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onSelect={onRetire}>
              <Trash2 />
              {t("team.card.delete")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <p className="text-chat-muted mt-3 line-clamp-2 min-h-9 text-xs leading-[18px]">
        {employee.description || t("team.card.noDescription")}
      </p>

      <div className="border-chat-hairline mt-2.5 grid grid-cols-3 overflow-hidden rounded-lg border">
        {capabilities.map((item, index) => (
          <div
            key={item.key}
            className={cn(
              "flex flex-col items-center justify-center gap-0 py-1",
              index < capabilities.length - 1 && "border-chat-hairline border-r",
            )}
          >
            <strong className="text-chat-ink text-[13px] font-semibold leading-4">
              {item.value}
            </strong>
            <em className="text-chat-muted text-[9px] not-italic leading-3">{item.label}</em>
          </div>
        ))}
      </div>
    </div>
  );
}
