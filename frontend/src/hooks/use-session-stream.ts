/**
  * 功能：会话流式接线（@base docs/前端设计规划.md；方案 A 两条常驻连接）。
 * 挂在壳层（AppShell）：sessionSocket 只连一次；下行帧按 stream 槽归并进 chat store
 * （keyed 增量：content_delta 累积 / 工具事件挂过程节点 / done·error 定稿）。
 * 职责边界：本 hook 只做「数据归并」；通知中心在 use-host-events（避免双推）。
 */
import { useEffect } from "react";

import { translate } from "@/i18n";
import type { InteractionFrame } from "@/services/sessions";
import { sessionsApi } from "@/services/sessions";
import { sessionSocket, type WsFrame } from "@/services/ws";
import { entriesToMessages, normalizeTaskLedger, useChatStore } from "@/stores/chat-store";

/** 流静默判定：超过此时长无任何帧到达即认为上游已死。 */
export const STREAM_SILENCE_TIMEOUT_MS = 45_000;
/** 看门狗轮询间隔。 */
const STREAM_WATCHDOG_POLL_MS = 5_000;
import { useUiStore } from "@/stores/ui-store";

function frameData(frame: WsFrame): Record<string, unknown> {
  return frame.data && typeof frame.data === "object"
    ? (frame.data as Record<string, unknown>)
    : {};
}

export function useSessionStream(): void {
  useEffect(() => {
    // 10.10：会话通道的 resume 作用域 = 当前会话 id（切会话即更新；重连按此补发该会话
    // 的终态帧，避免一条连接服务多会话时水位互相污染）
    sessionSocket.setResumeScope(useChatStore.getState().activeSessionId ?? "");
    const offScope = useChatStore.subscribe((state, previous) => {
      if (state.activeSessionId !== previous.activeSessionId) {
        sessionSocket.setResumeScope(state.activeSessionId ?? "");
      }
    });
    const offFrame = sessionSocket.onFrame((frame) => {
      const store = useChatStore.getState();
      const data = frameData(frame);
      // 帧自带 session_id 优先（后端各帧已下发）；缺失时回退流槽/活跃会话——
      // 一条连接服务多会话（多轮回合、断线补发）下按连接归属兜底会串会话
      // session_id 兼容两种形态：后端生产帧在 data 内，部分测试/旧帧在顶层
      const frameSessionId = String(
        data.session_id ?? (frame as { session_id?: unknown }).session_id ?? "",
      );
      const slotSession = frameSessionId || store.activeSessionId || "";
      switch (frame.type) {
        case "content_delta":
          if (slotSession) {
          store.appendDelta(slotSession, String(data.text ?? ""), { createPlaceholder: true });
        }
          break;
        case "thinking_delta":
          if (slotSession) {
            store.applyThinking(slotSession, String(data.text ?? ""), {
              createPlaceholder: true,
            });
          }
          break;
        case "skill_injected": {
          if (slotSession) {
            const skills = Array.isArray(data.skills)
              ? (data.skills as unknown[]).map((item) => String(item))
              : [];
            if (skills.length > 0) store.applySkillInjected(slotSession, skills);
          }
          break;
        }
        case "note": {
          if (slotSession) store.applyNote(slotSession, String(data.note_type ?? ""));
          break;
        }
        case "subagent_progress": {
          if (slotSession) {
            const subagent = (data.subagent ?? {}) as {
              id: string;
              task?: string;
              parent_tool_call_id?: string;
            };
            store.applySubagentProgress(
              slotSession,
              subagent,
              String(data.kind ?? ""),
              data as Record<string, unknown>,
            );
          }
          break;
        }
        case "memory_written": {
          if (slotSession) {
            const channels = Array.isArray(data.channels)
              ? (data.channels as unknown[]).map((item) => String(item))
              : [];
            store.applyMemoryWritten(slotSession, channels);
          }
          break;
        }
        case "task_ledger": {
          if (slotSession) {
            store.setTaskLedger(slotSession, normalizeTaskLedger(data.items));
          }
          break;
        }
        case "tool_call_start":
          if (slotSession) {
            store.applyToolStart(
              slotSession,
              String(data.tool_name ?? ""),
              (data.tool_input as Record<string, unknown> | undefined) ?? {},
              String(data.tool_call_id ?? ""),
            );
          }
          break;
        case "tool_progress":
          if (slotSession) {
            store.applyToolProgress(slotSession, {
              toolName: String(data.tool_name ?? ""),
              progressType: String(data.progress_type ?? ""),
              data: (data.data as Record<string, unknown> | undefined) ?? {},
            });
          }
          break;
        case "tool_result":
          if (slotSession) {
            store.applyToolResult(slotSession, {
              toolName: String(data.tool_name ?? ""),
              success: Boolean(data.success),
              preview: String(data.output_preview ?? ""),
              durationMs: Number(data.duration_ms ?? 0),
              toolCallId: String(data.tool_call_id ?? ""),
              citations: Array.isArray(data.citations) ? data.citations : [],
            });
          }
          break;
        case "interaction": {
          const interaction = data as unknown as InteractionFrame;
          if (!interaction.interaction_id) break;
          // -notify = 非交互提示——进通知中心，**不得**进
          // 挂起交互槽（否则永久占住决策 dock 且遮蔽后续真实审批卡；交互槽只收
          // 等待用户决策的三类：permission/confirm/input）
          const payload = (interaction.payload ?? {}) as Record<string, unknown>;
          if (interaction.kind === "notify" || payload.notify === true) {
            store.pushNotification({
              kind: "info",
              title: String(payload.message ?? ""),
              sessionId: interaction.session_id,
            });
            break;
          }
          store.addInteraction(interaction);
          break;
        }
        case "done": {
          const terminal = String(data.terminal_reason ?? "");
          // 团队接管回合（team_routed）：员工产出经线程回写到达，本槽空占位移除
          if (terminal === "team_routed") {
            // 移交成功（员工异步执行中）——记录成功终态，防「中断横幅」误报
            const handedOff = String(data.session_id ?? slotSession ?? "");
            if (handedOff) {
              useChatStore.getState().markSessionTerminal(handedOff, "team_routed");
            }
            useChatStore.getState().abortLocalStream(handedOff);
            // 转交过渡态：占位消失到员工过程出现之间不能空白——
            // 以系统注记给出「已转交员工、等待接单」的状态，衔接员工过程块
            if (handedOff) {
              const note = translate(useUiStore.getState().locale, "chat.note.teamHandoff");
              useChatStore.getState().appendSystemNote(handedOff, note);
            }
            void sessionsApi
              .list()
              .then((list) => useChatStore.getState().setSessions(list))
              .catch(() => undefined);
            break;
          }
          // 被拒回合（SESSION_BUSY）不承载流式槽——不得终结进行中的回合：否则
          // finalizeStream 会清掉 stream 槽，进行中回复的后续增量全部被 updatePending
          // 丢弃（回复被截断 + 永久流式光标），且用户零提示
          if (terminal.includes("SESSION_BUSY")) {
            if (slotSession) {
              const note = translate(useUiStore.getState().locale, "chat.note.sessionBusy");
              useChatStore.getState().appendSystemNote(slotSession, note);
            }
            break;
          }
          const sessionId = String(data.session_id ?? slotSession);
          // 帧自带 session_id 权威——按会话定稿，不再回退全局槽
          const active = sessionId;
          // 记录终态原值——中断横幅据此分档（模型不可用 / 超时 / 安全拦截 / 上下文超长…）
          if (sessionId) {
            useChatStore.getState().markSessionTerminal(sessionId, terminal);
          }
          // 用户中断 → 过程消息「用户中断了」注记（先于定稿，槽未释放）
          if (active && terminal.includes("INTERRUPT")) {
            useChatStore.getState().applyNote(active, "interrupt");
          }
          if (active) {
            const turnIndex = Number(data.turn_index ?? 0);
            useChatStore.getState().finalizeStream(active, turnIndex > 0 ? { turnIndex } : {});
          }
          // 会话列表刷新（新建会话/标题/消息数；best-effort 不阻断）
          void sessionsApi
            .list()
            .then((list) => useChatStore.getState().setSessions(list))
            .catch(() => undefined);
          break;
        }
        case "resumed": {
          // 10.10：断线期间补发了终态帧（done/error）⇒ 该轮已定稿，但本地正文只到断线点；服务端
          // 轮末已落库 ⇒ 重载会话历史拿到完整正文（已定稿消息被服务端版本整体替换 = 天然去重
          // 仍未定稿的尾部由 loadMessages 的 pending 过滤保留）
          const replayed = Number(frame.replayed ?? 0);
          if (replayed > 0 && slotSession) {
            void sessionsApi
              .detail(slotSession)
              .then((detail) => {
                useChatStore
                  .getState()
                  .loadMessages(slotSession, entriesToMessages(slotSession, detail.messages));
              })
              .catch(() => undefined);
          }
          break;
        }
        case "error": {
          const message = String(data.message ?? "会话执行失败");
          if (slotSession) store.failStream(slotSession, message);
          break;
        }
        default:
          break;
      }
      // 按帧所属会话刷新看门狗计时（无 session_id 的帧不续命任何会话）
      if (slotSession) lastFrameAtBySession.set(slotSession, Date.now());
    });

    /**
     * 看门狗：后端终止但未发 `done`/`error` 帧时（上游 SSE 断流
     * → finish_reason=incomplete → 状态机终止 → **未下发 done 帧**）
     * `stream` 槽永不释放，UI 永久「正在回复中…」。
     *
     * 原兜底 `abortLocalStream` 只由「用户按停止」或「abort 404」触发，没有
     * 被动检测。此处按**静默时长**判定：帧流停了且槽还在 ⇒ 本地收尾。
     * 有内容 ⇒ 定稿为 done（保留已产出部分）；空占位 ⇒ 移除。
     */
    // 看门狗按会话计时——A 会话的活跃帧不得为静默的 B 会话续命。
    const lastFrameAtBySession = new Map<string, number>();
    const watchdog = window.setInterval(() => {
      const state = useChatStore.getState();
      const now = Date.now();
      for (const [sessionId, slot] of Object.entries(state.streams)) {
        if (!lastFrameAtBySession.has(sessionId)) {
          // 槽刚建立、尚无帧：从此刻开始计静默时长
          lastFrameAtBySession.set(sessionId, now);
          continue;
        }
        if (now - (lastFrameAtBySession.get(sessionId) ?? now) < STREAM_SILENCE_TIMEOUT_MS) {
          continue;
        }
        const pending = state.messagesBySession[sessionId]?.find(
          (message) => message.id === slot.pendingId,
        );
        // 工具执行中可能长时间无帧（bash 上限 60s）；看门狗不得误杀长工具。
        const runningTool = pending?.process?.some(
          (node) => node.kind === "tool" && node.status === "running",
        );
        if (runningTool) {
          lastFrameAtBySession.set(sessionId, now);
          continue;
        }
        const hasOutput = Boolean(
          pending && (pending.content.trim() || (pending.process?.length ?? 0) > 0),
        );
        if (hasOutput) state.applyNote(sessionId, "watchdog");
        state.abortLocalStream(sessionId);
        lastFrameAtBySession.delete(sessionId);
      }
      // 清理已释放槽的计时，防 Map 无限增长
      for (const sessionId of lastFrameAtBySession.keys()) {
        if (!state.streams[sessionId]) lastFrameAtBySession.delete(sessionId);
      }
    }, STREAM_WATCHDOG_POLL_MS);

    sessionSocket.connect();
    return () => {
      window.clearInterval(watchdog);
      offScope();
      offFrame();
    };
  }, []);
}
