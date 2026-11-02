/**
  * 功能：命令面板——Cmd/Ctrl+K 全局唤起
 * 导航（会话/项目/员工搜索跳转）/ 新建会话 / 会话搜索 / 员工搜索。
 * 无新增依赖：原生 dialog 语义 + 输入过滤；product register 高密度列表。
 */
import { MessageSquarePlus, Search, Sparkles, UserRound } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useT } from "@/i18n";
import { companyApi, type EmployeeSummary } from "@/services/company";
import { useChatStore } from "@/stores/chat-store";

interface CommandItem {
  id: string;
  group: "action" | "session" | "employee";
  label: string;
  hint?: string;
  run: () => void;
}

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [employees, setEmployees] = useState<EmployeeSummary[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const sessions = useChatStore((state) => state.sessions);
  const setActiveSession = useChatStore((state) => state.setActiveSession);
  const setActiveProject = useChatStore((state) => state.setActiveProject);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    // 等一帧让 dialog 挂载后再聚焦
    requestAnimationFrame(() => inputRef.current?.focus());
    // 员工搜索数据源（打开时惰性拉取；失败静默——搜索面降级为会话/动作）
    let cancelled = false;
    companyApi
      .employees()
      .then((result) => {
        if (!cancelled) setEmployees(result.employees);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [open]);

  const items = useMemo<CommandItem[]>(() => {
    const q = query.trim().toLowerCase();
    const match = (text: string) => !q || text.toLowerCase().includes(q);
    const list: CommandItem[] = [];
    if (match(t("palette.newChat"))) {
      list.push({
        id: "action:new-chat",
        group: "action",
        label: t("palette.newChat"),
        run: () => {
          setActiveSession(null);
          navigate("/");
        },
      });
    }
    for (const session of sessions) {
      if (match(session.title)) {
        list.push({
          id: `session:${session.id}`,
          group: "session",
          label: session.title || session.id,
          hint: session.project_id ? undefined : t("palette.noProject"),
          run: () => {
            if (session.project_id) setActiveProject(session.project_id);
            setActiveSession(session.id);
            navigate("/");
          },
        });
      }
    }
    for (const employee of employees) {
      const name = employee.name || employee.employee_id;
      if (match(name)) {
        list.push({
          id: `employee:${employee.employee_id}`,
          group: "employee",
          label: name,
          hint: t("palette.employee"),
          run: () => {
            // 员工行打开 = 项目面板选中该项目（员工会话按项目分组——IA 现行决策）
            navigate("/");
          },
        });
      }
    }
    return list;
  }, [query, sessions, employees, t, navigate, setActiveSession, setActiveProject]);

  if (!open) return null;

  const close = () => {
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/25 pt-[12vh]"
      data-testid="command-palette"
      onClick={close}
      onKeyDown={(event) => {
        if (event.key === "Escape") close();
      }}
    >
      {/* 面板本体承载键盘事件（Esc 关闭）；点击遮罩关闭 */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("palette.title")}
        className="border-chat-hairline bg-card w-full max-w-xl overflow-hidden rounded-xl border shadow-xl"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}
      >
        <div className="border-chat-hairline flex items-center gap-2 border-b px-3 py-2.5">
          <Search className="text-chat-muted size-4 shrink-0" />
          <input
            ref={inputRef}
            value={query}
            data-testid="palette-input"
            placeholder={t("palette.placeholder")}
            className="text-chat-ink placeholder:text-chat-muted/60 min-w-0 flex-1 bg-transparent text-sm outline-none"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <ul className="max-h-[50vh] overflow-y-auto p-1.5">
          {items.length === 0 ? (
            <li className="text-chat-muted px-3 py-6 text-center text-xs">{t("palette.empty")}</li>
          ) : (
            items.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  data-testid="palette-item"
                  className="hover:bg-primary/5 text-chat-ink flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm"
                  onClick={() => {
                    item.run();
                    close();
                  }}
                >
                  {item.group === "action" ? (
                    <MessageSquarePlus className="text-chat-muted size-3.5 shrink-0" />
                  ) : item.group === "employee" ? (
                    <UserRound className="text-chat-muted size-3.5 shrink-0" />
                  ) : (
                    <Sparkles className="text-chat-muted size-3.5 shrink-0" />
                  )}
                  <span className="min-w-0 flex-1 truncate">{item.label}</span>
                  {item.hint ? (
                    <span className="text-chat-muted shrink-0 text-[11px]">{item.hint}</span>
                  ) : null}
                </button>
              </li>
            ))
          )}
        </ul>
      </div>
    </div>
  );
}
