/**
  * 功能：Composer（@base docs/前端设计规划.md 交互规范）。
 * 布局：附件 chips（输入上方）→ 输入栏（可增高至 240px）→ 功能栏
 * （⊕ 聚合菜单 / 模式圆圈 / 模型·思考控制条 / 发送·停止）。
 * textarea：Enter 发送、Shift+Enter 换行（IME 保护）；/ 触发斜杠面板、@ 触发引用菜单（键盘导航）。
 * 增强（enhanced=true——ChatView 主会话）：附件上传（⊕/拖拽；立即上传三态 chips）+
  * @ 引用（对话附件/项目文件/员工）+ 斜杠命令 + 模型/思考会话级覆盖。
 * 模式切换：圆圈内「计划/执行」——点击或 Shift+Tab 切换；showMode=false（派单视图）隐藏。
 * 终止：busy 时发送键原位变停止态——点击两次确认（3s），Esc 直接停止。
 */
import { FileText, Plus, Send, Square, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { AttachmentChips } from "@/components/chat/AttachmentChips";
import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
import { ComposerControls } from "@/components/chat/ComposerControls";
import { ComposerDropOverlay } from "@/components/chat/ComposerDropOverlay";
import { ComposerMentions, type MentionItem } from "@/components/chat/ComposerMentions";
import { ComposerPlusMenu } from "@/components/chat/ComposerPlusMenu";
import { ComposerSlashPanel } from "@/components/chat/ComposerSlashPanel";
import { ComposerSkillsPanel, type SkillOption } from "@/components/chat/ComposerSkillsPanel";
import { Button } from "@/components/ui/button";
import { useEmployees } from "@/hooks/use-employees";
import { useT } from "@/i18n";
import { formatSize, readyAttachments, uploadAttachments } from "@/lib/attachments";
import { splitMentions } from "@/lib/markdown";
import { mergeConfiguredProfiles } from "@/lib/model-options";
import { filteredSlashCommands, type SlashCommand } from "@/lib/slash-commands";
import { cn } from "@/lib/utils";
import { api } from "@/services/api";
import { companyApi, type EmployeeSummary } from "@/services/company";
import { sessionsApi } from "@/services/sessions";
import { settingsApi, type ModelOption, type ProfileSummary } from "@/services/settings";
import { useChatStore, type ChatAttachment } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

const COMPOSER_MAX_HEIGHT_PX = 240;
const STOP_DOUBLE_TAP_WINDOW_MS = 1500;
const MENTION_FILE_LIMIT = 50;

/** 功能：检测触发符（@ 或 /）——触发符在行首或前一个字符为空白，query 不含空白。 */
function detectTrigger(value: string, pos: number, token: string): string | null {
  const before = value.slice(0, pos);
  const at = before.lastIndexOf(token);
  if (at === -1) return null;
  if (at > 0 && !/\s/.test(before.charAt(at - 1))) return null;
  const query = before.slice(at + 1);
  if (/\s/.test(query)) return null;
  return query;
}

interface ComposerProps {
  busy: boolean;
  /** 功能：群聊有员工任务在执行（非本会话流）——同样显示停止键与双 Esc 停止。 */
  teamRunning?: boolean;
  disabled?: boolean;
  /** 是否显示模式切换控件（单聊/群聊派单视图传 false——mode 不被 /ws/company 消费）。 */
  showMode?: boolean;
  /** 输入占位文案（缺省用会话链路的默认文案）。 */
  placeholder?: string;
  /* 增强模式。 */
  enhanced?: boolean;
  /** 附件模式（仅 ⊕ 上传/拖拽/chips——单聊/群聊派单链路； 轮三-5 新增）。 */
  attachmentsEnabled?: boolean;
  /** 功能：⊕ 菜单是否提供「选择员工」（@团队成员）—— 仅项目群聊会话
   * 显示；员工单聊/项目秘书会话/普通会话置 false。 */
  allowTeamPicker?: boolean;
  /** 功能：@ 面板「员工」分组可见性（项目团队成员 role_id 列表；缺省 = 全量员工）。 */
  mentionRoleIds?: string[];
  /** 功能：空状态已选员工（单聊胶囊；X 取消）。 */
  pendingEmployee?: EmployeeSummary | null;
  /** 功能：空状态 ⊕ 菜单「选择员工」入口。 */
  onPickEmployee?: () => void;
  /** 功能：取消已选员工。 */
  onClearEmployee?: () => void;
  /** 功能：是否显示「选择员工」（仅新建会话空状态）。 */
  allowEmployeePicker?: boolean;
  /** 附件归属项目（上传/文件引用数据源）。 */
  projectId?: string;
  /** 功能：员工独立模型（单聊/员工视角）——非空时控制条显示该员工实际模型并锁定切换。 */
  employeeModel?: string;
  onSend: (text: string, attachments?: ChatAttachment[]) => void;
  onSteer: (text: string) => void;
  onStop: () => void;
  /** 功能：决策区（统一决策卡/恢复提示）内容——包含模式：与输入框同壳（输入框在上、决策在下的
   * 「对话框在顶部、决策卡在底部」结构）。 */
  above?: React.ReactNode;
  /** 功能：包含壳的底色（随决策类型：中断=浅红 / 交互卡=主色淡底）。 */
  aboveTone?: string;
  /** 功能：输入框下方附加区（新建会话的项目空间卡片； ）。 */
  below?: React.ReactNode;
}

export function Composer({
  busy,
  teamRunning = false,
  disabled,
  showMode = true,
  placeholder,
  enhanced = false,
  attachmentsEnabled = false,
  allowTeamPicker = true,
  mentionRoleIds,
  pendingEmployee = null,
  onPickEmployee,
  onClearEmployee,
  allowEmployeePicker = false,
  projectId = "",
  employeeModel = "",
  onSend,
  onSteer,
  onStop,
  above,
  aboveTone,
  below,
}: ComposerProps) {
  const t = useT();
  const chatMode = useUiStore((state) => state.chatMode);
  const setChatMode = useUiStore((state) => state.setChatMode);
  const sessionThinking = useUiStore((state) => state.sessionThinking);
  const setSessionThinking = useUiStore((state) => state.setSessionThinking);
  const planMode = chatMode === "plan";
  // 功能：停止键可用性——本会话在流式回复，或群聊有员工任务在执行
  const showStop = busy || teamRunning;
  // 附件能力（主会话 enhanced 隐含开启；单聊/群聊显式 attachmentsEnabled——轮三-5）
  const withAttachments = enhanced || attachmentsEnabled;
  const draftAttachments = useChatStore((state) => state.draftAttachments);
  const draftReferences = useChatStore((state) => state.draftReferences);
  const sessionMessages = useChatStore((state) =>
    state.activeSessionId ? state.messagesBySession[state.activeSessionId] : undefined,
  );
  const { employees } = useEmployees();
  const [text, setText] = useState("");
  // 按会话隔离的草稿：切换会话后输入框内容不得串场——
  // 切走时快照暂存、切回时恢复；发送/清空后的空态自然随快照同步）
  const composerSessionId = useChatStore((state) => state.activeSessionId) ?? "";
  const draftBySessionRef = useRef<Map<string, string>>(new Map());
  const lastComposerSessionRef = useRef<string>(composerSessionId);
  useEffect(() => {
    if (composerSessionId === lastComposerSessionRef.current) return;
    draftBySessionRef.current.set(lastComposerSessionRef.current, text);
    lastComposerSessionRef.current = composerSessionId;
    setText(draftBySessionRef.current.get(composerSessionId) ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 草稿按切换瞬间快照，不随输入触发
  }, [composerSessionId]);
  // **发送前**输入框里的 @提及也要是蓝色——用「镜像层」渲染带色文本
  // textarea 自身文字透明（保留 caret 与 placeholder），两层共享同一字体/内边距/换行规则
  // 以保证逐字对齐；滚动由 onScroll 同步（长文本时两层不错位）。
  const mirrorRef = useRef<HTMLDivElement>(null);
  const mentionNames = useMemo(
    () => employees.flatMap((employee) => [employee.name, employee.role_id]),
    [employees],
  );
  const composerTokens = useMemo(() => splitMentions(text, mentionNames), [text, mentionNames]);
  const [stopArmed, setStopArmed] = useState(false);
  const stopArmedRef = useRef(false);
  const [plusOpen, setPlusOpen] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [mentionOpen, setMentionOpen] = useState(false);
  const [mentionQuery, setMentionQuery] = useState("");
  // 功能：@ 面板入口模式——手打 @ 全量（三组）；⊕「选择员工」「引用文件」各限一组
  const [mentionMode, setMentionMode] = useState<"all" | "files" | "employee">("all");
  const [mentionIndex, setMentionIndex] = useState(0);
  const [slashOpen, setSlashOpen] = useState(false);
  const [slashQuery, setSlashQuery] = useState("");
  const [slashIndex, setSlashIndex] = useState(0);
  // 功能：`/skills` 技能选择面板（列出已安装技能，点选后插入显式调用语）
  const [skillPickerOpen, setSkillPickerOpen] = useState(false);
  const [skillOptions, setSkillOptions] = useState<SkillOption[]>([]);
  const skillPanelRef = useRef<HTMLDivElement>(null);
  const [projectFiles, setProjectFiles] = useState<MentionItem[]>([]);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [globalModel, setGlobalModel] = useState("");
  const [activeProfile, setActiveProfile] = useState<ProfileSummary | null>(null);
  // （第五轮）：已配置可用模型键集合（归一化）——模型切换列表只显示这些
  /** 功能：已配置档案（带凭证）——候选列表需并入网关私有模型。
   * 只取合成候选项所需的最少字段（model/provider），避开 ProfileSummary 的掩码字段。 */
  const [configuredProfiles, setConfiguredProfiles] = useState<
    Array<{ model: string; provider: string }>
  >([]);
  /** 功能：可切换的档案全集（A 方案：点候选 = 激活该档案）。 */
  const [switchableProfiles, setSwitchableProfiles] = useState<ProfileSummary[]>([]);
  const [modelsVersion, setModelsVersion] = useState(0);
  const [openModelSignal, setOpenModelSignal] = useState(0);

  const composingRef = useRef(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const stopTimerRef = useRef<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const plusMenuRef = useRef<HTMLDivElement>(null);
  const dragDepthRef = useRef(0);

  const uploading =
    withAttachments && draftAttachments.some((attachment) => attachment.status === "uploading");
  const canSubmit = text.trim().length > 0 && !disabled && !uploading;
  // 功能：切换对话模式——落库到会话（execute 时后端同时释放 plan 门挂起的派发卡）
  const activateMode = (next: "plan" | "execute") => {
    if (next === chatMode) return;
    setChatMode(next);
    const sessionId = useChatStore.getState().activeSessionId;
    if (!sessionId) return;
    void companyApi.setChatMode(sessionId, next).catch(() => undefined);
  };
  const toggleMode = () => activateMode(planMode ? "execute" : "plan");

  // ── 发送 ──────────────────────────────────────────────────────────────────

  const resetHeight = () => {
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const submit = () => {
    const value = text.trim();
    if (!value) return;
    const attachments = withAttachments ? readyAttachments() : [];
    // 引用文件转 @路径 文本注入（Agent 按路径读取；与既有 @文件 语义一致）
    const references = withAttachments ? draftReferences : [];
    const payload = references.length
      ? `${value} ${references.map((ref) => `@${ref.path}`).join(" ")}`.trim()
      : value;
    // 上传未完成不发送（清草稿会静默丢弃附件——-3 修复）
    if (uploading) {
      toast.info(t("chat.composer.attachmentUploading"));
      return;
    }
    // 流槽按会话隔离——别的会话回复中不再阻塞本会话发送
    if (busy) {
      // 转向通道只支持文本：带附件时本轮不发送，并保留附件草稿给下一条消息
      if (attachments.length > 0) {
        toast.info(t("chat.composer.attachmentBusy"));
        return;
      }
      onSteer(payload);
      setText("");
      if (references.length) useChatStore.getState().clearDraftReferences();
      resetHeight();
      return;
    }
    if (attachments.length) onSend(payload, attachments);
    else onSend(payload);
    setText("");
    // 只清已发送的 done 附件；上传失败（error）项保留供用户处理/移除（-3 修复）
    if (withAttachments) useChatStore.getState().clearSentAttachments();
    if (references.length) useChatStore.getState().clearDraftReferences();
    resetHeight();
  };

  // 轮次结束 → 清停止待触发态（含群聊任务执行期间）
  useEffect(() => {
    if (!showStop) {
      setStopArmed(false);
      stopArmedRef.current = false;
      if (stopTimerRef.current !== null) {
        window.clearTimeout(stopTimerRef.current);
        stopTimerRef.current = null;
      }
    }
  }, [showStop]);

  // ⊕ 菜单外部点击关闭
  useEffect(() => {
    if (!plusOpen) return;
    const onMouseDown = (event: MouseEvent) => {
      if (plusMenuRef.current && !plusMenuRef.current.contains(event.target as Node)) {
        setPlusOpen(false);
      }
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [plusOpen]);

  // 控制条数据（增强模式；候选 + 全局默认模型 + 已配置可用模型集合）
  // 档案版本号改由 ui-store 提供——设置页「应用」档案后 bump
  // 会话框模型控制条随之重取（此前只有 Composer 内模型弹窗会通知）
  const storeModelsVersion = useUiStore((state) => state.modelsVersion);
  useEffect(() => {
    if (!enhanced) return;
    let cancelled = false;
    settingsApi
      .model()
      .then((data) => {
        if (cancelled) return;
        setModelOptions(data.available_models);
        // 档案激活时 effective_model = 档案模型（展示口径与实际生效一致）
        setGlobalModel(data.effective_model || data.current_model);
        setActiveProfile(data.active_profile ?? null);
      })
      .catch(() => undefined);
    // （第五轮）：模型切换列表仅显示「已配置可用」模型——
    // 带凭证的档案模型（网关前缀归一后与目录键匹配）；未配置模型不再出现
    settingsApi
      .profiles()
      .then(({ profiles }) => {
        if (cancelled) return;
        const usable = profiles.filter((profile) => profile.has_api_key);
        setConfiguredProfiles(usable);
        setSwitchableProfiles(
          profiles.map((profile) => ({
            profile_id: profile.profile_id,
            name: profile.name,
            provider: profile.provider,
            model: profile.model,
            base_url: profile.base_url,
            api_key_masked: profile.api_key,
            has_api_key: profile.has_api_key,
          })),
        );
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [enhanced, modelsVersion, storeModelsVersion]);

  // 候选列表 = 目录模型 ∪ 已配置档案模型（合成逻辑见 lib/model-options）
  const mergedModelOptions = useMemo<ModelOption[]>(
    () => mergeConfiguredProfiles(modelOptions, configuredProfiles),
    [modelOptions, configuredProfiles],
  );

  // ── 附件（⊕/拖拽 → 立即上传） ──────────────────────────────────────────────

  const handleFiles = useCallback(
    (files: FileList | File[] | null) => {
      if (!files) return;
      const list = Array.from(files);
      if (list.length) void uploadAttachments(list, projectId);
    },
    [projectId],
  );

  const hasFiles = (event: React.DragEvent) =>
    Array.from(event.dataTransfer.types ?? []).includes("Files");

  const onDragEnter = (event: React.DragEvent) => {
    if (!withAttachments || disabled || !hasFiles(event)) return;
    event.preventDefault();
    dragDepthRef.current += 1;
    setDragActive(true);
  };
  const onDragOver = (event: React.DragEvent) => {
    if (!withAttachments || disabled || !hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };
  const onDragLeave = (event: React.DragEvent) => {
    if (!withAttachments || !hasFiles(event)) return;
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragActive(false);
  };
  const onDrop = (event: React.DragEvent) => {
    if (!withAttachments || disabled || !hasFiles(event)) return;
    event.preventDefault();
    dragDepthRef.current = 0;
    setDragActive(false);
    handleFiles(event.dataTransfer.files);
  };

  // ── @ 引用菜单 ────────────────────────────────────────────────────────────

  const attachmentItems = useMemo<MentionItem[]>(() => {
    if (!enhanced || !mentionOpen) return [];
    const seen = new Set<string>();
    const items: MentionItem[] = [];
    for (const message of sessionMessages ?? []) {
      for (const item of message.attachments ?? []) {
        if (seen.has(item.upload_id)) continue;
        seen.add(item.upload_id);
        items.push({
          kind: "attachment",
          id: item.upload_id,
          label: item.name,
          sub: formatSize(item.size),
          size: item.size,
        });
      }
    }
    return items;
  }, [enhanced, mentionOpen, sessionMessages]);

  useEffect(() => {
    if (!enhanced || !mentionOpen || !projectId) {
      setProjectFiles([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      companyApi
        .files(projectId, mentionQuery)
        .then((data) => {
          if (cancelled) return;
          setProjectFiles(
            data.files
              .slice(0, MENTION_FILE_LIMIT)
              .map((file) => ({ kind: "file", id: file.path, label: file.name, sub: file.path })),
          );
        })
        .catch(() => undefined);
    }, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [enhanced, mentionOpen, mentionQuery, projectId]);

  const employeeItems = useMemo<MentionItem[]>(() => {
    if (!enhanced || !mentionOpen) return [];
    const query = mentionQuery.toLowerCase();
    // 项目群聊会话下 @ 面板只列团队成员（mentionRoleIds 提供时过滤）
    const source = mentionRoleIds?.length
      ? employees.filter((employee) => mentionRoleIds.includes(employee.role_id))
      : employees;
    return source
      .filter((employee) => !query || employee.name.toLowerCase().includes(query))
      .slice(0, 20)
      .map((employee) => ({
        kind: "employee",
        id: employee.role_id,
        label: employee.name,
        sub: employee.role_id,
      }));
  }, [enhanced, mentionOpen, mentionQuery, employees, mentionRoleIds]);

  const mentionItems = useMemo(() => {
    const items = [...attachmentItems, ...projectFiles, ...employeeItems];
    // 「引用文件」入口不出现员工；「选择员工」入口只出现员工
    if (mentionMode === "files") return items.filter((item) => item.kind !== "employee");
    if (mentionMode === "employee") return items.filter((item) => item.kind === "employee");
    return items;
  }, [attachmentItems, projectFiles, employeeItems, mentionMode]);

  const replaceTrigger = (token: string, replacement: string) => {
    const element = textareaRef.current;
    const pos = element?.selectionStart ?? text.length;
    const before = text.slice(0, pos);
    const at = before.lastIndexOf(token);
    // 修复：⊕ 菜单入口打开 @ 面板时 textarea 无触发符——原实现 lastIndexOf
    // === -1 直接 return，导致「引用对话中的文件」选择后无任何效果（静默失效）。
    // 无触发符时在光标处直接插入替换文本。
    const start = at === -1 ? pos : at;
    const next = `${text.slice(0, start)}${replacement}${text.slice(pos)}`;
    setText(next);
    requestAnimationFrame(() => {
      const cursor = start + replacement.length;
      element?.setSelectionRange(cursor, cursor);
      element?.focus();
    });
  };

  const selectMention = (item: MentionItem) => {
    setMentionOpen(false);
    if (item.kind === "attachment") {
      const exists = useChatStore
        .getState()
        .draftAttachments.some((draft) => draft.upload_id === item.id);
      if (!exists) {
        useChatStore.getState().addDraftAttachment({
          upload_id: item.id,
          name: item.label,
          size: item.size ?? 0,
          status: "done",
        });
      }
      replaceTrigger("@", "");
      return;
    }
    if (item.kind === "file") {
      // 项目文件引用 = 草稿 chips（与上传文件同款视觉）——发送时转 @路径 文本；
      // 不再直接文本注入（原实现在 ⊕ 入口下静默失效，且用户无法管理已选引用）
      useChatStore.getState().addDraftReference({ path: item.id, name: item.label });
      replaceTrigger("@", "");
      return;
    }
    replaceTrigger("@", `@${item.label} `);
  };

  // ── 斜杠命令 ──────────────────────────────────────────────────────────────

  const slashCommands = useMemo(
    () => (slashOpen ? filteredSlashCommands(slashQuery) : []),
    [slashOpen, slashQuery],
  );

  const runSlash = (command: SlashCommand) => {
    setSlashOpen(false);
    setText("");
    resetHeight();
    const store = useChatStore.getState();
    switch (command.name) {
      case "new":
      case "clear":
        store.setActiveSession(null);
        break;
      case "plan":
        activateMode("plan");
        break;
      case "execute":
        activateMode("execute");
        break;
      case "stop":
        if (busy) onStop();
        break;
      case "model":
        setOpenModelSignal((value) => value + 1);
        break;
      case "usage":
        void showUsage();
        break;
      case "compact":
        void runCompact();
        break;
      case "skills":
        void openSkills();
        break;
      case "help":
        toast.info(t("chat.composer.helpToast"));
        break;
      default:
        break;
    }
  };

  const openSkills = async () => {
    try {
      const data = await api.get<{ skills: { name: string; description?: string }[] }>("/skills");
      setSkillOptions(
        (data.skills ?? []).map((skill) => ({
          name: skill.name,
          description: skill.description,
        })),
      );
      setSkillPickerOpen(true);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    }
  };

  const insertSkill = (skill: SkillOption) => {
    setSkillPickerOpen(false);
    const element = textareaRef.current;
    const pos = element?.selectionStart ?? text.length;
    const prefix = `请使用技能「${skill.name}」：`;
    const next = `${text.slice(0, pos)}${prefix}${text.slice(pos)}`;
    setText(next);
    requestAnimationFrame(() => {
      const cursor = pos + prefix.length;
      element?.setSelectionRange(cursor, cursor);
      element?.focus();
    });
  };

  // 功能：技能面板外部点击 / Esc 关闭
  useEffect(() => {
    if (!skillPickerOpen) return;
    const onDown = (event: MouseEvent) => {
      if (skillPanelRef.current && !skillPanelRef.current.contains(event.target as Node)) {
        setSkillPickerOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSkillPickerOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [skillPickerOpen]);

  const runCompact = async () => {    const sid = useChatStore.getState().activeSessionId;
    if (!sid) {
      toast.info(t("chat.composer.noSession"));
      return;
    }
    try {
      const result = await sessionsApi.compact(sid);
      if (result.compacted) {
        useChatStore.getState().upsertSession({
          id: result.new_session_id,
          title: "",
          created_at: "",
          message_count: 0,
          project_id: projectId,
        });
        useChatStore.getState().setActiveSession(result.new_session_id);
        toast.success(
          t("chat.composer.compactDone")
            .replace("{before}", String(result.before))
            .replace("{after}", String(result.after)),
        );
      } else {
        toast.info(t("chat.composer.compactNoop"));
      }
    } catch {
      toast.error(t("chat.composer.compactNoop"));
    }
  };

  const showUsage = async () => {
    const sid = useChatStore.getState().activeSessionId;
    if (!sid) {
      toast.info(t("chat.composer.noSession"));
      return;
    }
    try {
      // 台账返回 {session_cost:{calls,input_tokens,output_tokens}}——旧实现按顶层
      // `total_tokens`/`total_cost` 读取（两个键都不存在）⇒ 提示恒为 `¥0.0000 · 0 tokens`
      const data = await api.get<{
        session_cost?: { input_tokens?: number; output_tokens?: number };
      }>(`/cost/sessions/${encodeURIComponent(sid)}`);
      const tokens =
        Number(data.session_cost?.input_tokens ?? 0) +
        Number(data.session_cost?.output_tokens ?? 0);
      toast.info(t("chat.composer.usageSummary").replace("{tokens}", String(tokens)));
    } catch {
      toast.info(t("chat.composer.usageUnavailable"));
    }
  };

  // ── 停止 ──────────────────────────────────────────────────────────────────

  // 取消「变红 + tooltip 再点一次」的二次确认交互，改为**双触发**
  // ——连点两次停止按钮、或连按两次 Esc 才真正停止。理由：原实现的唯一提示是 title
  // tooltip（点第一下视觉上等于没反应，用户会误判为「停止坏了」）。
  // 防误点语义保留在「双触发」里，但不再有确认态文案与破坏性配色。
  // 用 ref 承载判定（state 只驱动描边视觉），避免 useEffect/handler 闭包读到过期值。
  const armStop = useCallback(() => {
    if (stopArmedRef.current) {
      stopArmedRef.current = false;
      setStopArmed(false);
      if (stopTimerRef.current !== null) {
        window.clearTimeout(stopTimerRef.current);
        stopTimerRef.current = null;
      }
      onStop();
      return;
    }
    stopArmedRef.current = true;
    setStopArmed(true);
    stopTimerRef.current = window.setTimeout(() => {
      stopArmedRef.current = false;
      setStopArmed(false);
      stopTimerRef.current = null;
    }, STOP_DOUBLE_TAP_WINDOW_MS);
  }, [onStop]);

  // ── 键盘 ──────────────────────────────────────────────────────────────────

  // Esc 全局化——原监听挂在输入容器上，焦点在消息区/页面
  // 任意处时 Esc 无效；改 window 级（Composer 内 capture 分支仍保留，经
  // defaultPrevented 守卫防双触发）
  useEffect(() => {
    if (!showStop) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
      // 仅弹层「可见」时让位（无匹配时列表为空、弹层不渲染——此时 Esc 应直达停止）
      if ((mentionOpen && mentionItems.length) || (slashOpen && slashCommands.length) || plusOpen)
        return;
      // Esc 与停止按钮同属「双触发」，单次 Esc 不停
      armStop();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    showStop,
    armStop,
    mentionOpen,
    mentionItems.length,
    slashOpen,
    slashCommands.length,
    plusOpen,
  ]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (mentionOpen && mentionItems.length) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setMentionIndex((index) => (index + 1) % mentionItems.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setMentionIndex((index) => (index - 1 + mentionItems.length) % mentionItems.length);
        return;
      }
      if (event.key === "Enter" && !composingRef.current) {
        event.preventDefault();
        selectMention(mentionItems[Math.min(mentionIndex, mentionItems.length - 1)]);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setMentionOpen(false);
        return;
      }
    }
    if (slashOpen && slashCommands.length) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setSlashIndex((index) => (index + 1) % slashCommands.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setSlashIndex((index) => (index - 1 + slashCommands.length) % slashCommands.length);
        return;
      }
      if (event.key === "Enter" && !composingRef.current) {
        event.preventDefault();
        runSlash(slashCommands[Math.min(slashIndex, slashCommands.length - 1)]);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setSlashOpen(false);
        return;
      }
    }
    if (
      event.key === "Enter" &&
      (useUiStore.getState().sendKey === "ctrl+enter"
        ? event.ctrlKey || event.metaKey
        : !event.shiftKey && !event.ctrlKey && !event.metaKey)
    ) {
      if (composingRef.current) return;
      event.preventDefault();
      submit();
    }
  };

  const handleChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = event.target.value;
    setText(value);
    const element = event.target;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, COMPOSER_MAX_HEIGHT_PX)}px`;
    if (!enhanced) return;
    const pos = element.selectionStart ?? value.length;
    const mention = detectTrigger(value, pos, "@");
    if (mention !== null) {
      setMentionMode("all");
      setMentionQuery(mention);
      setMentionIndex(0);
      setMentionOpen(true);
      setSlashOpen(false);
      return;
    }
    setMentionOpen(false);
    const slash = detectTrigger(value, pos, "/");
    if (slash !== null && value.trimStart().startsWith("/")) {
      setSlashQuery(slash);
      setSlashIndex(0);
      setSlashOpen(true);
    } else {
      setSlashOpen(false);
    }
  };

  // **档案是唯一配置面**——无激活档案即「未配置模型」（字段不显示
  // config.yaml 兜底的模型名，否则用户视角与界面不一致）；config 兜底仍由后端生效。
  const modelUnconfigured = !activeProfile;
  // A 方案：移除会话级覆盖——切换模型 = 全局切换档案
  const currentModel = modelUnconfigured ? "" : employeeModel || globalModel;
  // 卡片在输入框下方时同样走「包含壳」——会话框在顶部、卡片在底部
  // 与权限申请卡同壳同底色（不再是壳外独立卡片，需与会话框嵌合）
  const shelled = Boolean(above) || Boolean(below);

  return (
    <div className="px-4 pt-1 pb-5" data-testid="composer">
      <div
        className={cn(
          "relative mx-auto w-full max-w-4xl",
          // 决策区「包含模式」——外壳包住决策内容与输入框；
          // 决策内容在上、输入框在下（壳内边距四周一致 p-1）
          shelled && cn("flex flex-col rounded-2xl p-1", aboveTone ?? "bg-chat-rail/60"),
        )}
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        {dragActive ? <ComposerDropOverlay /> : null}
        {above ? (
          <div
            data-testid="decision-dock"
            className="flex flex-col pb-1.5 [&>*]:mx-0 [&>*]:mb-0 [&>*]:rounded-none [&>*]:bg-transparent"
          >
            {above}
          </div>
        ) : null}
        <div
          data-testid="composer-box"
          className={cn(
            // 聚焦不描边；圆角恒定（包含模式下保持自身圆角）；边缘线 2px
            "border-primary/5 bg-card flex w-full flex-col rounded-xl border-2 px-2 py-2 transition-colors",
          )}
          onKeyDownCapture={(event) => {
            if (showMode && event.key === "Tab" && event.shiftKey) {
              event.preventDefault();
              event.stopPropagation();
              toggleMode();
              return;
            }
            // 中文输入法联想态（isComposing）不再豁免——双按窗口已防误触
            // （IME 占用 Esc 会致「连续 ESC 停不下来」）
            if (event.key === "Escape" && showStop) {
              if (mentionOpen || slashOpen || plusOpen) return;
              event.preventDefault();
              event.stopPropagation();
              // 与停止按钮同一套双触发（单次 Esc 不停）
              armStop();
            }
          }}
        >
          {pendingEmployee ? (
            <div className="mb-1.5 flex px-2" data-testid="composer-employee-chip">
              <span className="border-chat-hairline bg-chat-bg text-chat-ink flex items-center gap-1.5 rounded-lg border px-2 py-1 text-xs">
                <EmployeeAvatar
                  name={pendingEmployee.name}
                  metadata={pendingEmployee.metadata}
                  className="size-5 rounded-full text-[10px]"
                />
                <span className="max-w-40 truncate">{pendingEmployee.name}</span>
                <button
                  type="button"
                  className="text-chat-muted hover:text-chat-ink"
                  title={t("chat.composer.employeeChipRemove")}
                  onClick={onClearEmployee}
                >
                  <X className="size-3" />
                </button>
              </span>
            </div>
          ) : null}
          {withAttachments ? (
            <AttachmentChips
              attachments={draftAttachments}
              className="mb-1.5 px-2"
              onRemove={(uploadId) => useChatStore.getState().removeDraftAttachment(uploadId)}
            />
          ) : null}
          {/* 2026-09-29：引用文件 chips（@ 选项目文件——与上传附件同款视觉；发送时转 @路径 文本） */}
          {draftReferences.length ? (
            <div className="mb-1.5 flex flex-wrap gap-1.5 px-2" data-testid="composer-references">
              {draftReferences.map((ref) => (
                <span
                  key={ref.path}
                  className="border-chat-hairline bg-chat-bg text-chat-ink flex items-center gap-1.5 rounded-lg border px-2 py-1 text-xs"
                  data-testid="reference-chip"
                >
                  <FileText className="text-chat-muted size-3.5" />
                  <span className="max-w-40 truncate" title={ref.path}>
                    {ref.name}
                  </span>
                  <button
                    type="button"
                    className="text-chat-muted hover:text-chat-ink"
                    title={t("chat.composer.attachRemove")}
                    onClick={() => useChatStore.getState().removeDraftReference(ref.path)}
                  >
                    <X className="size-3" />
                  </button>
                </span>
              ))}
            </div>
          ) : null}
          <div className="relative">
            {/* 镜像层（发送前 @提及蓝色）：绝对定位于 textarea 之下，同字体/内边距/换行；
                `\u200b` 兜底让**结尾换行**也能占位（否则最后一行对不齐）；aria-hidden 防重复朗读 */}
            <div
              ref={mirrorRef}
              aria-hidden="true"
              data-testid="composer-mirror"
              className="text-chat-ink pointer-events-none absolute inset-0 overflow-hidden px-2 py-2 text-sm break-words whitespace-pre-wrap"
            >
              {composerTokens.map((token, index) =>
                token.mention ? (
                  <span key={index} className="text-mention font-medium">
                    {token.text}
                  </span>
                ) : (
                  <span key={index}>{token.text}</span>
                ),
              )}
              {"\u200b"}
            </div>
            <textarea
              ref={textareaRef}
              className="placeholder:text-chat-muted caret-chat-ink block max-h-60 min-h-16 w-full resize-none bg-transparent px-2 py-2 text-sm text-transparent outline-none"
              placeholder={
                busy
                  ? t("chat.composer.steerPlaceholder")
                  : (placeholder ?? t("chat.composer.placeholder"))
              }
              rows={1}
              value={text}
              disabled={disabled}
              onChange={handleChange}
              onScroll={(event) => {
                if (mirrorRef.current) mirrorRef.current.scrollTop = event.currentTarget.scrollTop;
              }}
              onCompositionStart={() => {
                composingRef.current = true;
              }}
              onCompositionEnd={() => {
                composingRef.current = false;
              }}
              onKeyDown={handleKeyDown}
            />
          </div>
          <div className="mt-1 flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5">
              {withAttachments ? (
                <div className="relative" ref={plusMenuRef}>
                  <button
                    type="button"
                    data-testid="composer-plus"
                    aria-label={t("chat.composer.attach")}
                    aria-expanded={plusOpen}
                    className={cn(
                      // 悬浮/选中**不加强描边**——加号颜色保持不变
                      // 改为圆圈**内部填充**，且填充色与「多选卡」卡片底同源（`bg-primary/5`）。
                      "no-focus-ring border-primary/5 text-chat-muted hover:bg-primary/5 flex size-8 shrink-0 items-center justify-center rounded-full border-[1.5px] transition-colors",
                      plusOpen && "bg-primary/5",
                    )}
                    onClick={() => {
                      setMentionOpen(false);
                      setSlashOpen(false);
                      setPlusOpen((value) => !value);
                    }}
                  >
                    <Plus className="size-4" />
                  </button>
                  {plusOpen ? (
                    <ComposerPlusMenu
                      onPickFiles={() => undefined}
                      onPickEmployee={
                        allowEmployeePicker
                          ? () => {
                              setPlusOpen(false);
                              onPickEmployee?.();
                            }
                          : undefined
                      }
                      onChooseTeam={
                        enhanced && allowTeamPicker
                          ? () => {
                              // 点击即出现团队成员列表（与「引用对话
                              // 中的文件」同款交互）——插入 @ 并显式打开 @ 面板（原实现只插 @
                              // 不弹面板，用户看不到员工）
                              setPlusOpen(false);
                              setText(
                                (value) => `${value}${value && !value.endsWith(" ") ? " " : ""}@`,
                              );
                              setMentionMode("employee");
                              setMentionQuery("");
                              setMentionIndex(0);
                              setMentionOpen(true);
                              textareaRef.current?.focus();
                            }
                          : undefined
                      }
                      onQuoteFiles={
                        enhanced
                          ? () => {
                              setPlusOpen(false);
                              setMentionMode("files");
                              setMentionQuery("");
                              setMentionIndex(0);
                              setMentionOpen(true);
                              textareaRef.current?.focus();
                            }
                          : undefined
                      }
                    />
                  ) : null}
                </div>
              ) : null}
              {showMode ? (
                <button
                  type="button"
                  data-testid="composer-mode-toggle"
                  data-mode={chatMode}
                  className={cn(
                    "flex size-8 shrink-0 items-center justify-center rounded-full border-[1.5px] text-[10px] font-medium transition-colors",
                    planMode
                      ? "border-primary/5 text-chat-muted hover:bg-primary/5"
                      : "border-primary bg-primary text-primary-foreground",
                  )}
                  title={t("chat.composer.modeSwitch")}
                  onClick={toggleMode}
                >
                  {planMode
                    ? t("chat.composer.modePlanShort")
                    : t("chat.composer.modeExecuteShort")}
                </button>
              ) : null}
            </div>
            <div className="flex min-w-0 items-center gap-1.5">
              {enhanced ? (
                <ComposerControls
                  model={currentModel}
                  options={mergedModelOptions}
                  thinking={sessionThinking}
                  sessionId={composerSessionId}
                  usageRefreshKey={showStop ? 1 : 0}
                  activeProfile={activeProfile}
                  profiles={switchableProfiles}
                  employeeModel={employeeModel}
                  onActivateProfile={(profileId) => {
                    void settingsApi
                      .activateProfile(profileId)
                      .then(() => {
                        useUiStore.getState().bumpModelsVersion();
                      })
                      .catch(() => undefined);
                  }}
                  openSignal={openModelSignal}
                  onThinking={(mode) => setSessionThinking(mode)}
                  onProfilesChanged={() => setModelsVersion((version) => version + 1)}
                />
              ) : null}
              {showStop ? (
                <Button
                  size="icon"
                  data-testid="composer-stop"
                  className={cn(
                    // 去掉二次确认的破坏性红态；双触发待命只用一圈
                    // 轻描边示意「已收到，再来一次」，避免首击看起来像按钮坏了
                    "bg-chat-ink hover:bg-chat-ink/90 shrink-0 rounded-full text-white",
                    stopArmed && "ring-2 ring-chat-ink/40 ring-offset-2 ring-offset-chat-bg",
                  )}
                  title={t("chat.composer.stop")}
                  onClick={armStop}
                >
                  {/* 2026-09-24 用户拍板：中断 icon 用方形（lucide Square 自带 rx=2 小圆角）实心 */}
                  <Square className="size-3.5 fill-current" />
                </Button>
              ) : (
                <Button
                  size="icon"
                  className={cn("shrink-0", !canSubmit && "opacity-40")}
                  disabled={!canSubmit}
                  title={t("chat.composer.send")}
                  onClick={submit}
                >
                  {/* 2026-09-22 用户拍板：纸飞机右移 1px 视觉居中（原 -2px 过左） */}
                  <Send className="size-3.5 -translate-x-[1px] translate-y-[1px] fill-current" />
                </Button>
              )}
            </div>
          </div>
        </div>
        {/* 2026-09-29：输入框下方附加区（项目空间卡片）——与决策区对称，同在外壳内 */}
        {below ? (
          <div
            data-testid="composer-below"
            className="flex flex-col pt-1.5 [&>*]:mx-0 [&>*]:mt-0 [&>*]:mb-0 [&>*]:rounded-none [&>*]:bg-transparent"
          >
            {below}
          </div>
        ) : null}
        {mentionOpen ? (
          <ComposerMentions
            items={mentionItems}
            activeIndex={mentionIndex}
            onSelect={selectMention}
          />
        ) : null}
        {slashOpen && slashCommands.length ? (
          <ComposerSlashPanel
            commands={slashCommands}
            activeIndex={slashIndex}
            onSelect={runSlash}
          />
        ) : null}
        {skillPickerOpen ? (
          <div ref={skillPanelRef} className="relative">
            <ComposerSkillsPanel
              skills={skillOptions}
              onSelect={insertSkill}
              onClose={() => setSkillPickerOpen(false)}
            />
          </div>
        ) : null}
      </div>
      <input
        ref={fileInputRef}
        id="composer-file-input"
        type="file"
        multiple
        hidden
        data-testid="composer-file-input"
        onChange={(event) => {
          setPlusOpen(false);
          handleFiles(event.target.files);
          event.target.value = "";
        }}
      />
    </div>
  );
}
