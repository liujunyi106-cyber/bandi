/**
 * 功能：Composer 技能选择面板（`/skills` 命令）——列出已安装技能，点击插入显式调用语。
 */
import { useT } from "@/i18n";

export interface SkillOption {
  name: string;
  description?: string;
}

interface ComposerSkillsPanelProps {
  skills: SkillOption[];
  onSelect: (skill: SkillOption) => void;
  onClose: () => void;
}

export function ComposerSkillsPanel({ skills, onSelect, onClose }: ComposerSkillsPanelProps) {
  const t = useT();
  return (
    <div
      data-testid="composer-skills-panel"
      className="border-chat-hairline bg-card absolute bottom-full left-0 z-50 mb-2 max-h-72 w-80 overflow-y-auto rounded-xl border p-1 shadow-md"
    >
      <div className="text-chat-muted flex items-center justify-between px-2 py-1 text-[11px]">
        <span>{t("chat.composer.skillsTitle")}</span>
        <button type="button" className="hover:text-chat-ink" onClick={onClose}>
          {t("chat.composer.skillsClose")}
        </button>
      </div>
      {skills.length === 0 ? (
        <p className="text-chat-muted px-2 py-2 text-xs">{t("chat.composer.skillsEmpty")}</p>
      ) : (
        skills.map((skill) => (
          <button
            key={skill.name}
            type="button"
            data-testid="composer-skill-option"
            className="hover:bg-primary/5 text-chat-ink flex w-full flex-col items-start gap-0.5 rounded-lg px-2.5 py-1.5 text-left text-sm"
            onClick={() => onSelect(skill)}
          >
            <span className="truncate">{skill.name}</span>
            {skill.description ? (
              <span className="text-chat-muted w-full truncate text-[11px]">
                {skill.description}
              </span>
            ) : null}
          </button>
        ))
      )}
    </div>
  );
}
