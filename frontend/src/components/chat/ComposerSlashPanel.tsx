/**
 * 功能：Composer 斜杠命令面板（输入 / 触发；键盘导航由 Composer 处理——Day137b-2）。
 * 命令清单与过滤见 `lib/slash-commands.ts`。
 */
import { useT } from "@/i18n";
import type { SlashCommand } from "@/lib/slash-commands";
import { cn } from "@/lib/utils";

interface ComposerSlashPanelProps {
  commands: SlashCommand[];
  activeIndex: number;
  onSelect: (command: SlashCommand) => void;
}

export function ComposerSlashPanel({ commands, activeIndex, onSelect }: ComposerSlashPanelProps) {
  const t = useT();
  if (!commands.length) return null;
  return (
    <div
      data-testid="composer-slash-panel"
      role="listbox"
      className="border-chat-hairline bg-card absolute bottom-full left-0 z-50 mb-2 w-64 rounded-xl border p-1 shadow-md"
    >
      {commands.map((command, index) => (
        <button
          key={command.name}
          type="button"
          role="option"
          aria-selected={index === activeIndex}
          data-testid={`slash-item-${command.name}`}
          className={cn(
            "text-chat-ink flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm",
            index === activeIndex ? "bg-primary/5" : "hover:bg-primary/5",
          )}
          onClick={() => onSelect(command)}
        >
          <span className="text-chat-muted font-mono text-xs">/{command.name}</span>
          <span className="truncate">{t(command.label)}</span>
        </button>
      ))}
    </div>
  );
}
