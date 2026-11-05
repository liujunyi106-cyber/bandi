/**
 * 功能：宿主级聚合流（events.host，@base docs/前端设计规划.md §8 应用内通知中心）。
 * 把两条 WS 通道的关键事件聚合为「宿主感」输出：通知中心条目（完成/出错/交互/断线补发）
 * + 会话连接状态镜像（ui store）。数据归并在 use-session-stream；本 hook 只管通知与状态。
 */
import { useEffect, useRef } from "react";

import { translate } from "@/i18n";
import { normalizeProjectScope } from "@/lib/progress-key";
import type { GroupRoundSnapshot } from "@/services/company";
import type { InteractionFrame } from "@/services/sessions";
import { sessionSocket, socket, type WsFrame } from "@/services/ws";
import { useChatStore } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

/** 功能：宿主通知文案取词（locale 跟随 ui store；`{name}` 占位由调用侧替换——
 * 与项目既有 translate 无插值参数的口径一致；-整文件迁 i18n）。 */
function hostText(key: string, vars?: Record<string, string | number>): string {
  let text = translate(useUiStore.getState().locale, `chat.host.${key}`);
  for (const [name, value] of Object.entries(vars ?? {})) {
    text = text.replace(`{${name}}`, String(value));
  }
  return text;
}

function frameData(frame: WsFrame): Record<string, unknown> {
  return frame.data && typeof frame.data === "object"
    ? (frame.data as Record<string, unknown>)
    : {};
}

/**
 * 员工过程槽兜底过期窗口：完成/出错后保留完成态过程块等回复接手；
 * 正常路径下回复到达即经 `consumeEmployeeProgress` 固化并清槽，本窗口只兜底
 * 「无人消费」（用户不在单聊/群聊/团队会话，或该工作项未产出线程消息）的残留。
 *
 * 计时起点 = **`done`/`error` 终态帧到达**（不是执行开始、不是首帧）：
 * 执行进行中不挂定时器 ⇒ 长任务（耗时超过本窗口）不会被误清。
 * 由 放宽到 2min：给「产出写回 + 帧到达 + 视图挂载」留足余量
 * 降低「回复晚到 → 过程块已被兜底清掉」的丢失概率。
 */
const EMPLOYEE_PROGRESS_EXPIRE_MS = 120_000;

function sessionLabel(sessionId: string): string {
  const found = useChatStore.getState().sessions.find((item) => item.id === sessionId);
  const title = found?.title?.trim();
  return title || `${sessionId.slice(0, 8)}…`;
}

export function useHostEvents(): void {
  // -项目切换 effect 与「链路 open」共用同一门控发送器（避免两处两套口径）
  const sendSubscribeRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    const setSessionWsStatus = useUiStore.getState().setSessionWsStatus;

    const offSessionStatus = sessionSocket.onStatus(setSessionWsStatus);
    setSessionWsStatus(sessionSocket.getStatus());

    // 公司链路断开 → 过程槽结算。过程帧中间态不进补发缓冲，断线期间错过的
    // 中间态补不回来 ⇒ 该员工的过程块会永久停在「执行中」；断线瞬间给一个诚实终态
    // （附「连接中断，过程可能不完整」注记）+ 挂兜底过期定时器。
    // 若该次执行其实还在跑：后续帧让槽清空重建（首帧清 done 标记）⇒ 自愈，旧定时器 no-op。
    // 公司链路每次进入 open 时（首次连接 + 每次重连）重发项目订阅——
    // 服务端按连接记订阅，重连后是新连接（旧记录已随断连清理），不重发就会退回 fail-open。
    // -能力握手状态——服务端 hello 声明 capabilities 后才发 subscribe。
    // null = 尚未收到 hello（旧后端/握手在途）：**不发**（旧后端会把 subscribe 落进
    // chat 分支回 "content required" ⇒ 通知中心噪音；未订阅 = fail-open 收全部过程帧
    // 与旧行为一致）。收到 hello 后按声明决定；重连时服务端会再发一次 hello。
    let serverCapabilities: Set<string> | null = null;
    let subscribeWanted = false;
    // -挂给「项目切换」effect 的门控入口（未握手不发送，握手后补发）
    sendSubscribeRef.current = () => sendProgressSubscription();
    const sendProgressSubscription = (): void => {
      // 未握手：记下意图，等 hello 到达再决定（新后端 hello 紧随 open 到达）
      if (serverCapabilities === null) {
        subscribeWanted = true;
        return;
      }
      if (!serverCapabilities.has("progress_scope")) return;
      subscribeWanted = false;
      const { activeProjectId } = useChatStore.getState();
      socket.send({
        type: "subscribe",
        // 归一化后下单值（无选中项目 → "company"，与后端 `_progress_scope` 同口径）
        // ⚠️ 依赖：`activeProjectId` 必须与「视图实际显示的项目」一致——
        // `?project=` 路由参数由 AppSidebar / SidebarContextList 的 effect 同步进 store
        // （见 SidebarContextList「URL 项目参数同步到 store」）。若将来新增「打开某项目视图
        // 但不设置 activeProjectId」的入口，这里会把它项目的过程帧静默滤掉。
        project_ids: [normalizeProjectScope(activeProjectId)],
      });
    };
    const offCompanyStatus = socket.onStatus((status) => {
      // 功能：断线不结算在跑过程槽；短暂抖动不覆盖过程块，回复落卡时由 thread_message 重建。
      if (status === "open") sendProgressSubscription();
    });

    const offSession = sessionSocket.onFrame((frame) => {
      const push = useChatStore.getState().pushNotification;
      const data = frameData(frame);
      const sessionId = String(data.session_id ?? "");
      if (frame.type === "done") {
        if (String(data.terminal_reason ?? "") === "SESSION_BUSY") {
          push({
            kind: "error",
            title: hostText("busyTitle"),
            body: hostText("busyBody"),
            sessionId,
          });
        }
        // 功能：回复完成不再推通知——改由会话条目右侧圆点呈现（见 chat-store.sessionActivity）
        useChatStore.getState().markSessionActivity(sessionId, "done");
      } else if (frame.type === "error") {
        push({
          kind: "error",
          title: hostText("errorTitle", { session: sessionLabel(sessionId) }),
          body: String(data.message ?? ""),
          sessionId,
        });
      } else if (frame.type === "interaction") {
        // -修复：notify 为非交互提示（会话内 use-session-stream 已进通知中心）——
        // 宿主层跳过，避免重复推送且误报「待确认交互」遮蔽真审批
        const interactionKind = String(data.kind ?? "");
        const payload = (data.payload ?? {}) as Record<string, unknown>;
        if (interactionKind === "notify" || payload.notify === true) return;
        // 宿主通道必须**留帧**，不能只发通知。
        // 会话通道（use-session-stream）只投递「当前活动会话」的交互帧；用户切到别的
        // 会话时该会话的审批帧只经宿主通道到达，此前仅推一条通知 → 切回该会话后
        // 决策区无卡可渲染，Agent 永久阻塞在等待决策（死路）。
        // 帧按 interaction_id 入 store（与 addInteraction 同一键，重复到达幂等）。
        const interactionId = String(data.interaction_id ?? "");
        if (interactionId) {
          useChatStore.getState().addInteraction({
            ...(data as unknown as InteractionFrame),
            interaction_id: interactionId,
            session_id: String(data.session_id ?? sessionId ?? ""),
          });
        }
        // 交互卡本身已在决策区呈现——不再向通知中心推送
        // （每个交互申请都在消息中心多一条「等待交互」，属重复噪音）
      }
    });

    const offShell = socket.onFrame((frame) => {
      if (frame.type === "hello") {
        const caps = frameData(frame).capabilities;
        serverCapabilities = new Set(Array.isArray(caps) ? caps.map((item) => String(item)) : []);
        // 握手完成：补发此前被推迟的订阅意图
        if (subscribeWanted) sendProgressSubscription();
        return;
      }
      if (frame.type === "resumed") {
        const replayed = Number((frame as { replayed?: number }).replayed ?? 0);
        if (replayed > 0) {
          useChatStore.getState().pushNotification({
            kind: "info",
            title: hostText("replayedTitle", { count: replayed }),
          });
        }
        return;
      }
      if (frame.type === "resume_gap") {
        // 重连补发后仍有缺口（seq 已被有界回放窗口淘汰）——静默记录日志，不再推通知
        // （断线补发提示常驻消息中心属噪音；刷新/切会话即自愈）
        const from = Number((frame as { from?: number }).from ?? 0);
        const to = Number((frame as { to?: number }).to ?? 0);
        console.debug("[ws] resume gap", { from, to });
        return;
      }
      if (frame.type === "group_round") {
        const data = frameData(frame);
        const roundSessionId = String(data.session_id ?? "");
        if (roundSessionId) {
          useChatStore.getState().applyRound(data as unknown as GroupRoundSnapshot);
        }
        return;
      }
      if (frame.type === "thread_progress") {
        // Day137b-1 插队批：员工执行过程帧 → 过程槽归并（单聊/群聊实时过程块数据源）；
        // 槽键含 thread_id（= 「这一次执行」的身份，与产出回写同源）
        const data = frameData(frame);
        const inner = data.event as { type?: unknown; data?: unknown } | undefined;
        const projectId = String(data.project_id ?? "");
        const threadId = String(data.thread_id ?? "");
        const roleId = String(data.role_id ?? "");
        const eventType = String(inner?.type ?? "");
        if (projectId && roleId && eventType) {
          const store = useChatStore.getState();
          store.applyEmployeeProgress(projectId, threadId, roleId, {
            type: eventType,
            data: (inner?.data as Record<string, unknown> | undefined) ?? {},
          });
          if (eventType === "done" || eventType === "error") {
            // 完成/出错：保留过程块（完成态可回顾——与主对话「消息挂过程块」语义一致）
            // 仅置标记；新执行首帧到达自动清槽重建，页面刷新自然清空。
            // 另起兜底定时器：完成态若无人消费（用户不在消费视图 /
            // 该工作项未产出线程消息）则到期清槽，避免过程块永久悬浮；
            // 新执行已开始 → 标记被重置 → 该动作 no-op，不会误清在跑的过程
            store.finishEmployeeProgress(projectId, threadId, roleId);
            window.setTimeout(() => {
              useChatStore.getState().expireEmployeeProgressIfDone(projectId, threadId, roleId);
            }, EMPLOYEE_PROGRESS_EXPIRE_MS);
          }
        }
        return;
      }
      if (frame.type === "interaction") {
        // 员工链路多问卡/权限卡（seat_executor 真挂起 → /ws/company 广播）：帧按
        // interaction_id 入 store（会话内 ChatView 按 session_id 过滤渲染决策卡）；
        // notify 盲帧跳过（与会话通道同口径——只进通知中心，不占决策槽）。
        const data = frameData(frame);
        const interactionKind = String(data.kind ?? "");
        const payload = (data.payload ?? {}) as Record<string, unknown>;
        if (interactionKind === "notify" || payload.notify === true) return;
        const interactionId = String(data.interaction_id ?? "");
        if (interactionId) {
          useChatStore.getState().addInteraction({
            ...(data as unknown as InteractionFrame),
            interaction_id: interactionId,
            session_id: String(data.session_id ?? ""),
          });
        }
        return;
      }
      if (frame.type === "error") {
        // 遗漏修复：公司 WS 错误帧（消息未持久化 / 修正被拒 / 引用非法等）
        // 此前无任何 UI 消费 → 操作静默失败；统一进通知中心显式告知
        const raw = frame.data;
        const message =
          typeof raw === "string"
            ? raw
            : String(
                (raw as Record<string, unknown> | undefined)?.message ??
                  hostText("fallbackMessage"),
              );
        useChatStore.getState().pushNotification({
          kind: "error",
          title: hostText("companyErrorTitle"),
          body: message,
        });
      }
    });

    return () => {
      offSessionStatus();
      offSession();
      offCompanyStatus();
      offShell();
    };
  }, []);

  // 选中项目变化 → 重发过程帧订阅（服务端只把该项目的 thread_progress
  // 发到本连接；业务帧不受影响）。未连接时 send 会自动入队，连上后随 flushPending 发出。
  const activeProjectId = useChatStore((state) => state.activeProjectId);
  useEffect(() => {
    // -改走能力门控（原为无条件发送——首帧即发，旧后端必回
    // "content required" ⇒ 每次连接/切项目一条噪音通知）
    sendSubscribeRef.current?.();
  }, [activeProjectId]);
}
