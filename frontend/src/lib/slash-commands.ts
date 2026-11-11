/**
 * 功能：Web 端本地斜杠命令清单（cc-haha 全清单映射；/compact 走后端端点——Day137b-2）。
 */
export interface SlashCommand {
  /** 命令名（不含斜杠；如 new / compact）。 */
  name: string;
  /** i18n 文案 key。 */
  label: string;
}

export const SLASH_COMMANDS: { name: string; key: string }[] = [
  { name: "new", key: "chat.composer.slash.new" },
  { name: "clear", key: "chat.composer.slash.clear" },
  { name: "plan", key: "chat.composer.slash.plan" },
  { name: "execute", key: "chat.composer.slash.execute" },
  { name: "stop", key: "chat.composer.slash.stop" },
  { name: "model", key: "chat.composer.slash.model" },
  { name: "usage", key: "chat.composer.slash.usage" },
  { name: "compact", key: "chat.composer.slash.compact" },
  { name: "skills", key: "chat.composer.slash.skills" },
  { name: "help", key: "chat.composer.slash.help" },
];

/** 功能：按 query 前缀过滤（空 query = 全量前 8 条）。 */
export function filteredSlashCommands(query: string): SlashCommand[] {
  const q = query.trim().toLowerCase();
  return SLASH_COMMANDS.map((item) => ({ name: item.name, label: item.key }))
    .filter((item) => !q || item.name.startsWith(q))
    .slice(0, 8);
}
