/**
 * 功能：Composer ⊕ 聚合菜单（添加文件 / @员工 / 选择员工单聊 / @ 引用对话文件）。
 * 纯展示 + 回调；开合与外部点击关闭由 Composer 管理。
 * 「@员工」仅项目群聊会话提供；「选择员工」仅新建会话空状态提供。
 */
import { AtSign, Paperclip, UserRound, UserRoundPlus } from "lucide-react";

import { useT } from "@/i18n";

interface ComposerPlusMenuProps {
  onPickFiles: () => void;
  /** 增强模式专属（未提供不渲染「@员工」项）。 */
  onChooseTeam?: () => void;
  /** 只有空状态提供（未提供不渲染「选择员工」项）。 */
  onPickEmployee?: () => void;
  /** 增强模式专属（未提供不渲染「@ 引用对话文件」项）。 */
  onQuoteFiles?: () => void;
}

export function ComposerPlusMenu({
  onPickFiles,
  onChooseTeam,
  onPickEmployee,
  onQuoteFiles,
}: ComposerPlusMenuProps) {
  const t = useT();
  const items = [
    { key: "attach", icon: Paperclip, label: t("chat.composer.attach"), onClick: onPickFiles },
    ...(onChooseTeam
      ? [
          {
            key: "team",
            icon: UserRound,
            label: t("chat.composer.mentionEmployee"),
            onClick: onChooseTeam,
          },
        ]
      : []),
    ...(onPickEmployee
      ? [
          {
            key: "employee",
            icon: UserRoundPlus,
            label: t("chat.composer.chooseEmployee"),
            onClick: onPickEmployee,
          },
        ]
      : []),
    ...(onQuoteFiles
      ? [
          {
            key: "quote",
            icon: AtSign,
            label: t("chat.composer.quoteFiles"),
            onClick: onQuoteFiles,
          },
        ]
      : []),
  ];
  return (
    <div
      data-testid="composer-plus-menu"
      role="menu"
      className="border-chat-hairline bg-card absolute bottom-full left-0 z-50 mb-2 w-56 rounded-xl border p-1 shadow-md"
    >
      {items.map((item) =>
        item.key === "attach" ? (
          // 功能：原生 label 触发文件选择（不依赖脚本 click——桌面壳/部分浏览器更稳）
          <label
            key={item.key}
            htmlFor="composer-file-input"
            role="menuitem"
            data-testid="composer-plus-attach"
            className="hover:bg-primary/5 text-chat-ink flex w-full cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm"
            onClick={item.onClick}
          >
            <item.icon className="text-chat-muted size-4" />
            {item.label}
          </label>
        ) : (
          <button
            key={item.key}
            type="button"
            role="menuitem"
            data-testid={`composer-plus-${item.key}`}
            className="hover:bg-primary/5 text-chat-ink flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm"
            onClick={item.onClick}
          >
            <item.icon className="text-chat-muted size-4" />
            {item.label}
          </button>
        ),
      )}
    </div>
  );
}
