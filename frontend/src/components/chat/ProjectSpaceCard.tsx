/**
 * 功能：项目空间卡片——新建会话、尚未开始对话时显示在输入框下方。
 *
 * 显示规则（父级判定，见 ChatView）：仅在「新建会话且未开始对话」时挂载；
 * - 无项目归属 → 触发位显示「选择项目空间」，点击展开列表；
 * - 已有项目归属 → 触发位直接显示项目名称字段。
 * 列表结构（自上而下）：搜索框 → 已建项目 → 分隔线 → 「＋ 新建项目空间」→「打开本地文件夹」。
 * 两条创建路径语义不同：新建项目空间 = 本地自动建同名文件夹（不以桌面文件为项目空间）；
 * 打开本地文件夹 = **调系统原生文件夹选择框**选取本机已有文件夹作为项目空间
 * （原生不可用时回落内置选择器，见 LocalFolderPickerDialog）。
 * 视觉：本组件作为 Composer「包含壳」的 below 槽内容渲染，与外层同壳同底色
 * 自身不再画描边/底（与权限申请卡同一套语言）。
 */
import { Check, ChevronDown, Folder, FolderOpen, FolderPlus, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { LocalFolderPickerDialog } from "@/components/chat/LocalFolderPickerDialog";
import { NewProjectSpaceDialog } from "@/components/chat/NewProjectSpaceDialog";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Separator } from "@/components/ui/separator";
import { useT } from "@/i18n";
import { nameFromPath, projectIdFromName } from "@/lib/project-space";
import { cn } from "@/lib/utils";
import { ApiError } from "@/services/api";
import { companyApi, type ProjectCreateInput, type ProjectRecord } from "@/services/company";
import { useChatStore } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

export function ProjectSpaceCard() {
  const t = useT();
  const activeProjectId = useChatStore((state) => state.activeProjectId);
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [picking, setPicking] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const { projects: list } = await companyApi.projects();
      setProjects(list);
      return list;
    } catch {
      return [];
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!menuOpen) return;
    setQuery("");
    void refresh();
  }, [menuOpen, refresh]);

  const activeProject = useMemo(
    () => projects.find((item) => item.project_id === activeProjectId) ?? null,
    [projects, activeProjectId],
  );
  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return projects;
    return projects.filter((item) => item.name.toLowerCase().includes(keyword));
  }, [projects, query]);

  /** 功能：选中项目空间（只改会话归属，不跳转视图——保住当前新建会话草稿）。 */
  const select = (projectId: string) => {
    useChatStore.getState().setActiveProject(projectId);
    // 功能：选项目后该项目才出现「助理」条目（条目不再常驻）
    useUiStore.getState().showAssistantEntry(projectId);
    setMenuOpen(false);
  };

  /** 功能：建项目空间（create_folder = 本地自动建同名文件夹）/ 登记已有文件夹（root_dir）。 */
  const create = async (input: ProjectCreateInput, successKey: string): Promise<boolean> => {
    try {
      const { project } = await companyApi.createProject(input);
      await refresh();
      useChatStore.getState().setActiveProject(project.project_id);
      useUiStore.getState().showAssistantEntry(project.project_id);
      useUiStore.getState().bumpProjectsVersion();
      toast.success(t(successKey).replace("{name}", project.name));
      return true;
    } catch (error) {
      // 已存在（同 id）→ 直接切过去，不当作失败
      if (error instanceof ApiError && error.status === 409) {
        const list = await refresh();
        const existing = list.find((item) => item.project_id === input.project_id);
        if (existing) {
          useChatStore.getState().setActiveProject(existing.project_id);
          useUiStore.getState().showAssistantEntry(existing.project_id);
          toast.info(t("chat.projectSpace.alreadyExists"));
          return true;
        }
      }
      toast.error(error instanceof ApiError ? error.message : t("chat.projectSpace.createFailed"));
      return false;
    }
  };

  const submitNew = async (name: string) => {
    const done = await create(
      {
        project_id: projectIdFromName(name, `space-${Date.now().toString(36)}`),
        name,
        create_folder: true,
      },
      "chat.projectSpace.created",
    );
    if (done) {
      setCreateOpen(false);
      setMenuOpen(false);
    }
    return done;
  };

  const submitLocalFolder = async (path: string) => {
    const name = nameFromPath(path, path);
    const done = await create(
      {
        project_id: projectIdFromName(name, `space-${Date.now().toString(36)}`),
        name,
        root_dir: path,
      },
      "chat.projectSpace.switched",
    );
    if (done) {
      setPickerOpen(false);
      setMenuOpen(false);
    }
  };

  /**
   * 功能：打开本地文件夹——优先系统原生选择框（真实桌面弹窗）；
   * 原生不可用（非桌面环境/无 GUI 依赖）时回落内置选择器。
   */
  const openLocal = async () => {
    if (picking) return;
    setPicking(true);
    try {
      const result = await companyApi.pickDirectory();
      if (result.fallback) {
        setPickerOpen(true);
        return;
      }
      if (result.path) await submitLocalFolder(result.path);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("chat.projectSpace.switchFailed"));
      setPickerOpen(true);
    } finally {
      setPicking(false);
    }
  };

  const label = activeProject?.name || activeProjectId || t("chat.projectSpace.choose");

  return (
    <>
      {/* 2026-09-29 对齐：上传按钮（⊕）为卡片内首个 `size-8` 圆圈，其左缘落在
          composer 的 px-4(16px) + 卡片 px-2(8px) = 24px；本槽（composer-below）无水平
 内边距 ⇒ 补 9px 让 icon 左缘与圆圈左缘同一条竖线。 */}
      <div className="flex items-center pl-[9px] pb-1.5" data-testid="project-space-card">
        <Popover open={menuOpen} onOpenChange={setMenuOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              data-testid="project-space-trigger"
              className={cn(
                // text-xs → text-sm、icon size-3.5 → size-4（用户要求放大）；
                // gap 同步加宽避免放大后字符贴图标
                "flex min-w-0 items-center gap-2 text-sm font-medium transition-colors",
                // （第三/五轮）：常态与「选择项目空间」占位同色（浅灰）；
                // hover 与菜单展开（选中）态变黑——两种反馈效果都要有
                "text-chat-muted/55 hover:text-chat-ink data-[state=open]:text-chat-ink",
              )}
            >
              {/* 2026-09-29：icon 与字段同色同透明度（用户反馈「太深」——此前 icon 用
                  满不透明 muted、字段用 ink，两处都偏重） */}
              <FolderOpen className="size-4 shrink-0 opacity-90" />
              <span className="max-w-[18rem] truncate">{label}</span>
              {/* 2026-09-29：默认向下（ChevronDown），展开后旋转指向菜单侧——此前恒 ChevronUp */}
              <ChevronDown
                className={cn("size-3.5 shrink-0 transition-transform", menuOpen && "rotate-180")}
              />
            </button>
          </PopoverTrigger>
          <PopoverContent
            side="top"
            align="start"
            className="w-64 gap-1 p-1.5"
            data-testid="project-space-menu"
          >
            <div className="relative">
              <Search className="text-chat-muted pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2" />
              <Input
                value={query}
                data-testid="project-space-search"
                placeholder={t("chat.projectSpace.searchPlaceholder")}
                className="h-8 py-0 pl-7 text-sm"
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>

            <div className="max-h-40 overflow-y-auto">
              {filtered.length === 0 ? (
                <p className="text-chat-muted px-2 py-3 text-center text-xs">
                  {t("chat.projectSpace.empty")}
                </p>
              ) : (
                filtered.map((project) => (
                  <button
                    key={project.project_id}
                    type="button"
                    data-testid={`project-space-item-${project.project_id}`}
                    className="hover:bg-primary/5 text-chat-ink flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm"
                    onClick={() => select(project.project_id)}
                  >
                    <Folder className="text-chat-muted/70 size-4 shrink-0" />
                    <span className="min-w-0 flex-1 truncate">{project.name}</span>
                    {project.project_id === activeProjectId ? (
                      <Check className="text-chat-ink size-4 shrink-0" />
                    ) : null}
                  </button>
                ))
              )}
            </div>

            <Separator />

            <button
              type="button"
              data-testid="project-space-new"
              className="hover:bg-primary/5 text-chat-ink flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm"
              onClick={() => {
                setMenuOpen(false);
                setCreateOpen(true);
              }}
            >
              <FolderPlus className="text-chat-muted/70 size-4 shrink-0" />
              <span>{t("chat.projectSpace.create")}</span>
            </button>
            <button
              type="button"
              data-testid="project-space-open-local"
              className="hover:bg-primary/5 text-chat-ink flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm"
              onClick={() => {
                setMenuOpen(false);
                void openLocal();
              }}
            >
              <FolderOpen className="text-chat-muted/70 size-4 shrink-0" />
              <span>{t("chat.projectSpace.openLocal")}</span>
            </button>
          </PopoverContent>
        </Popover>
      </div>

      <NewProjectSpaceDialog open={createOpen} onOpenChange={setCreateOpen} onSubmit={submitNew} />
      <LocalFolderPickerDialog
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        onPick={submitLocalFolder}
      />
    </>
  );
}
