/**
  * 功能：单页壳共享 WebSocket 客户端（@base docs/前端设计规划.md）。
 * - 视图切换不重连（模块级单例，全 App 共用一条连接）
 * - 断线自动重连：指数退避 + 抖动，attempt 有上限后转 closed 并等外部手动重连
 * - 事件订阅：按帧 `type` 分发（on(type, handler)），另有全局 onFrame
 * - 心跳：空闲期发 ping；超过阈值无任何入帧则主动断开触发重连
 * - 非 open 态 send 进待发队列，连上后按序 flush（上限保护，防内存膨胀）
 */

import { desktopApiKey } from "@/lib/desktop";

export type WsStatus = "idle" | "connecting" | "open" | "reconnecting" | "closed";

/** 内核下行帧（type 驱动的可辨识联合，字段随事件类型扩展）。 */
export interface WsFrame {
  type: string;
  [key: string]: unknown;
}

/** 上行帧（ws/company 的 content 帧不带 type；会话帧按后端契约带 `type: "chat"`）。 */
export interface OutboundFrame {
  type?: string;
  [key: string]: unknown;
}

type FrameHandler = (frame: WsFrame) => void;
type StatusHandler = (status: WsStatus) => void;

export interface BandiSocketOptions {
  /** 默认按当前页面推导同源 `/ws`。 */
  url?: string;
  baseDelayMs?: number;
  maxDelayMs?: number;
  /** 0 = 无限重试。 */
  maxAttempts?: number;
  heartbeatMs?: number;
  /** 待发队列上限。 */
  maxPendingFrames?: number;
  /**
   * 启用服务端 seq/resume 断线补发（公司通道专用；默认关闭）。
   * 启用后：记录下行帧 `seq` 水位 → 重连时发 `resume{last_seq}` 拉回断线期间错过的帧 →
   * 按水位去重、并对「已被服务端有界缓冲淘汰」造成的缺口发一次合成 `resume_gap` 帧。
   */
  seqResume?: boolean;
}

const DEFAULT_URL = (): string => resolveWsUrl(SHELL_WS_PATH);

/** 壳级下行通道（公司/组织消息流：project_id / target_role / message 帧）。 */
export const SHELL_WS_PATH = "/ws/company";

/** 会话级流式通道（单会话 chat 流；POST 同路径为 SSE 降级）。 */
export const SESSION_WS_PATH = "/api/agent/stream";

/** 功能：按路径推导同源 WS 地址（dev 由 vite proxy 转发，生产同源）。 */
function resolveWsUrl(path: string): string {
  const base =
    typeof window === "undefined"
      ? `ws://127.0.0.1:8766${path}`
      : `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}${path}`;
  const apiKey = desktopApiKey();
  if (!apiKey) return base;
  return `${base}${base.includes("?") ? "&" : "?"}token=${encodeURIComponent(apiKey)}`;
}

export class BandiSocket {
  private readonly options: Required<Omit<BandiSocketOptions, "url">> & { url: string };
  private socket: WebSocket | null = null;
  private status: WsStatus = "idle";
  private attempts = 0;
  private manualClose = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private lastInboundAt = 0;
  private pending: OutboundFrame[] = [];
  /** 已处理到的下行 seq 水位（无作用域的通道用，如公司广播流）。 */
  private lastSeq = 0;
  /**
   * 按作用域（会话 id）分别记的水位：会话通道一条连接服务多个会话
   * 共用单一水位会导致「A 会话的 seq 把 B 会话的帧判成过期而丢弃」（10.10）。
   */
  private readonly lastSeqByScope = new Map<string, number>();
  /** 当前作用域（会话通道 = 当前会话 id，切会话时由接线方更新）；重连据此补发。 */
  private resumeScope = "";
  /** 已就缺口告警过的作用域（每个作用域一次，防刷屏）。 */
  private readonly gapNotifiedScopes = new Set<string>();

  private readonly frameHandlers = new Map<string, Set<FrameHandler>>();
  private readonly globalFrameHandlers = new Set<FrameHandler>();
  private readonly statusHandlers = new Set<StatusHandler>();

  constructor(options: BandiSocketOptions = {}) {
    this.options = {
      url: options.url ?? DEFAULT_URL(),
      baseDelayMs: options.baseDelayMs ?? 500,
      maxDelayMs: options.maxDelayMs ?? 15_000,
      maxAttempts: options.maxAttempts ?? 8,
      heartbeatMs: options.heartbeatMs ?? 25_000,
      maxPendingFrames: options.maxPendingFrames ?? 100,
      seqResume: options.seqResume ?? false,
    };
  }

  getStatus(): WsStatus {
    return this.status;
  }

  /** 功能：设置当前作用域（会话通道切会话时调用；无作用域通道无需调用）。 */
  setResumeScope(scope: string): void {
    this.resumeScope = scope;
  }

  /** 功能：取某作用域的已处理水位（无作用域 → 通道级单水位）。 */
  private watermarkOf(scope: string): number {
    return scope ? (this.lastSeqByScope.get(scope) ?? 0) : this.lastSeq;
  }

  /** 功能：抬高某作用域的水位。 */
  private raiseWatermark(scope: string, seq: number): void {
    if (scope) this.lastSeqByScope.set(scope, seq);
    else this.lastSeq = seq;
  }

  private setStatus(next: WsStatus): void {
    if (this.status === next) return;
    this.status = next;
    for (const handler of this.statusHandlers) handler(next);
  }

  /** 功能：建立连接（幂等；已 open/connecting 时直接返回）。 */
  connect(): void {
    if (this.socket && (this.status === "open" || this.status === "connecting")) return;
    this.manualClose = false;
    this.open();
  }

  private open(): void {
    this.setStatus(this.attempts > 0 ? "reconnecting" : "connecting");
    let socket: WebSocket;
    try {
      socket = new WebSocket(this.options.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempts = 0;
      this.lastInboundAt = Date.now();
      this.gapNotifiedScopes.clear();
      this.setStatus("open");
      const watermark = this.watermarkOf(this.resumeScope);
      if (this.options.seqResume && watermark > 0) {
        // 10.9/10.10 重连续传：先请求补发断线期间错过的帧（服务端按 seq 有界回放），再冲刷
        // 待发队列；会话通道带 scope——服务端按会话分桶回放，避免串到别的会话
        this.send({
          type: "resume",
          last_seq: watermark,
          ...(this.resumeScope ? { scope: this.resumeScope } : {}),
        });
      }
      this.flushPending();
      this.startHeartbeat();
    };

    socket.onmessage = (event: MessageEvent) => {
      this.lastInboundAt = Date.now();
      const frame = parseFrame(event.data);
      if (!frame) return;
      if (this.options.seqResume && !this.acceptSeqFrame(frame)) return;
      for (const handler of this.globalFrameHandlers) handler(frame);
      const typed = this.frameHandlers.get(frame.type);
      if (typed) for (const handler of typed) handler(frame);
    };

    socket.onerror = () => {
      // onerror 之后浏览器必跟 onclose，重连交给 onclose 统一处理
    };

    socket.onclose = () => {
      this.stopHeartbeat();
      this.socket = null;
      if (this.manualClose) {
        this.setStatus("closed");
        return;
      }
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    const { baseDelayMs, maxDelayMs, maxAttempts } = this.options;
    if (maxAttempts > 0 && this.attempts >= maxAttempts) {
      this.setStatus("closed");
      return;
    }
    const backoff = Math.min(baseDelayMs * 2 ** this.attempts, maxDelayMs);
    const jitter = backoff * 0.25 * Math.random();
    this.attempts += 1;
    this.setStatus("reconnecting");
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.open(), backoff + jitter);
  }

  /**
   * 功能：seq 门（仅 seqResume 通道）——去重 + 缺口告知；返回 false = 该帧应丢弃。
   *
   * 水位按**作用域**（`frame.scope`；会话通道 = session id）分别记录：一条连接服务多个
   * 会话时，单一水位会让「A 会话已处理到 seq=100」把 B 会话的帧误判成过期而丢弃（10.10）。
   *
   * - `resumed`：以服务端上报的 `last_seq` 抬当前作用域水位（其后的 live 帧才不被误判为缺口）；
   * - 无 `seq` 的帧（pong / 端点直发 error / 业务响应）：不参与协议，直接放行；
   * - `seq <= 水位`：补发重叠（重复 resume / 断线前已处理）→ 丢弃，保证消费幂等；
   * - `seq > 水位 + 1`：断线窗口内有帧已被服务端有界回放缓冲淘汰（补不回来）→ 发一次合成
   * `resume_gap` 帧显式告知，避免「静默缺消息」。
   */
  private acceptSeqFrame(frame: WsFrame): boolean {
    const scope = String(frame.scope ?? "");
    const tracked = scope || this.resumeScope;
    if (frame.type === "resumed") {
      const reported = Number(frame.last_seq);
      if (Number.isFinite(reported)) {
        this.raiseWatermark(tracked, Math.max(this.watermarkOf(tracked), reported));
      }
      return true;
    }
    const seq = Number(frame.seq);
    if (!Number.isFinite(seq) || seq <= 0) return true;
    const watermark = this.watermarkOf(tracked);
    if (seq <= watermark) return false;
    if (watermark > 0 && seq > watermark + 1 && !this.gapNotifiedScopes.has(tracked)) {
      this.gapNotifiedScopes.add(tracked);
      this.emit({ type: "resume_gap", from: watermark + 1, to: seq - 1 });
    }
    this.raiseWatermark(tracked, seq);
    return true;
  }

  /** 功能：派发客户端本地合成帧（非服务端下行帧；走与下行帧一致的订阅面）。 */
  private emit(frame: WsFrame): void {
    for (const handler of this.globalFrameHandlers) handler(frame);
    const typed = this.frameHandlers.get(frame.type);
    if (typed) for (const handler of typed) handler(frame);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    const { heartbeatMs } = this.options;
    if (heartbeatMs <= 0) return;
    this.heartbeatTimer = setInterval(() => {
      if (this.status !== "open") return;
      if (Date.now() - this.lastInboundAt > heartbeatMs * 3) {
        this.socket?.close();
        return;
      }
      this.send({ type: "ping", ts: Date.now() });
    }, heartbeatMs);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
  }

  private flushPending(): void {
    if (this.pending.length === 0) return;
    const queued = this.pending;
    this.pending = [];
    for (const frame of queued) this.send(frame);
  }

  /** 功能：发送一帧（上行）；未就绪时入队，队列满则丢最旧（保新帧）。 */
  send(frame: OutboundFrame): boolean {
    if (this.socket && this.status === "open") {
      this.socket.send(JSON.stringify(frame));
      return true;
    }
    if (this.pending.length >= this.options.maxPendingFrames) this.pending.shift();
    this.pending.push(frame);
    if (this.status === "idle" || this.status === "closed") this.connect();
    return false;
  }

  /** 功能：主动关闭（不再自动重连）。 */
  close(code = 1000, reason = "client-close"): void {
    this.manualClose = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.stopHeartbeat();
    this.socket?.close(code, reason);
    this.socket = null;
    this.setStatus("closed");
  }

  /** 功能：订阅某类下行帧，返回取消订阅函数。 */
  on(type: string, handler: FrameHandler): () => void {
    let set = this.frameHandlers.get(type);
    if (!set) {
      set = new Set();
      this.frameHandlers.set(type, set);
    }
    set.add(handler);
    return () => set?.delete(handler);
  }

  /** 功能：订阅全部下行帧（宿主级聚合流用）。 */
  onFrame(handler: FrameHandler): () => void {
    this.globalFrameHandlers.add(handler);
    return () => this.globalFrameHandlers.delete(handler);
  }

  /** 功能：订阅连接状态变化，返回取消订阅函数。 */
  onStatus(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler);
    return () => this.statusHandlers.delete(handler);
  }
}

function parseFrame(data: unknown): WsFrame | null {
  if (typeof data !== "string") return null;
  try {
    const parsed: unknown = JSON.parse(data);
    if (parsed && typeof parsed === "object" && typeof (parsed as WsFrame).type === "string") {
      return parsed as WsFrame;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * 单页壳共享实例（导航切换不重连）。
 * 启用 10.9 seq/resume 断线补发——公司通道下行帧带单调 `seq`，服务端按 seq 有界回放。
 */
export const socket = new BandiSocket({ seqResume: true });

/** 会话级共享实例（ChatView 对话流；第二条常驻连接，切页/切会话不重连——方案 A 拍板）。 */
export const sessionSocket = new BandiSocket({
  url: resolveWsUrl(SESSION_WS_PATH),
  // 10.10：会话通道启用 seq/resume——断线重连后补发终态帧（done/error），消除「消息永久
  // 停在未定稿态 + 流式光标不消失」；作用域由接线方 setResumeScope(当前会话 id) 维护
  // 断线期间的正文本体不靠补 delta，而由收到补发帧后重载会话历史补齐（轮末已落库）
  seqResume: true,
});
