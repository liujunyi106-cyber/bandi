/**
  * 功能：Composer @ 引用菜单。
 * 纯展示 + onSelect；过滤与键盘导航由 Composer 处理。
 */
import { AtSign, FileText, User } from "lucide-react";

import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

export type MentionKind = "attachment" | "file" | "employee";

export interface MentionItem {
  kind: MentionKind;
  /** attachment/file 为 upload_id 或路径；employee 为 role_id。 */
  id: string;
  label: string;
  /** 补充说明（文件相对路径 / 员工角色名 / 附件大小）。 */
  sub?: string;
  /** 附件字节数（attachment 引用复用；随 select 入草稿 chips）。 */
  size?: number;
}

const ICONS: Record<MentionKind, typeof FileText> = {
  attachment: FileText,
  file: FileText,
  employee: User,
};

interface ComposerMentionsProps {
  items: MentionItem[];
  activeIndex: number;
  onSelect: (item: MentionItem) => void;
}

export function ComposerMentions({ items, activeIndex, onSelect }: ComposerMentionsProps) {
  const t = useT();
  if (!items.length) {
    return (
      <div
        data-testid="composer-mentions"
        className="border-chat-hairline bg-card text-chat-muted absolute bottom-full left-0 z-50 mb-2 w-72 rounded-xl border p-3 text-xs shadow-md"
      >
        {t("chat.composer.mentions.empty")}
      </div>
    );
  }
  const groups: { kind: MentionKind; title: string; items: MentionItem[] }[] = [
    { kind: "attachment", title: t("chat.composer.mentions.attachments"), items: [] },
    { kind: "file", title: t("chat.composer.mentions.projectFiles"), items: [] },
    { kind: "employee", title: t("chat.composer.mentions.employees"), items: [] },
  ];
  for (const item of items) {
    groups.find((group) => group.kind === item.kind)?.items.push(item);
  }
  let flatIndex = -1;
  return (
    <div
      data-testid="composer-mentions"
      role="listbox"
      className="border-chat-hairline bg-card absolute bottom-full left-0 z-50 mb-2 max-h-72 w-72 overflow-y-auto rounded-xl border p-1 shadow-md"
    >
      {groups
        .filter((group) => group.items.length)
        .map((group) => (
          <div key={group.kind}>
            <p className="text-chat-muted px-2.5 pt-2 pb-1 text-[10px] font-medium tracking-wide uppercase">
              {group.title}
            </p>
            {group.items.map((item) => {
              flatIndex += 1;
              const index = flatIndex;
              const Icon = ICONS[item.kind];
              return (
                <button
                  key={`${item.kind}:${item.id}`}
                  type="button"
                  role="option"
                  aria-selected={index === activeIndex}
                  data-testid={`mention-item-${item.kind}-${item.id}`}
                  className={cn(
                    "text-chat-ink flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm",
                    index === activeIndex ? "bg-primary/5" : "hover:bg-primary/5",
                  )}
                  onClick={() => onSelect(item)}
                >
                  <AtSign className="text-chat-muted size-3.5 shrink-0" />
                  <Icon className="text-chat-muted size-3.5 shrink-0" />
                  <span className="truncate">{item.label}</span>
                  {item.sub ? (
                    <span className="text-chat-muted ml-auto truncate text-xs">{item.sub}</span>
                  ) : null}
                </button>
              );
            })}
          </div>
        ))}
    </div>
  );
}
