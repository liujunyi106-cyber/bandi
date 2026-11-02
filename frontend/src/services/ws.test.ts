/**
 * 功能：BandiSocket 断线补发协议测试。
 * 语义：seqResume 通道记录下行 seq 水位 → 重连请求 `resume{last_seq}` 拉回断线帧 →
 * 按水位去重 → 缺口（已被服务端有界缓冲淘汰）发一次合成 `resume_gap` 显式告知。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BandiSocket, type WsFrame } from "@/services/ws";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readonly sent: string[] = [];

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.onclose?.();
  }

  open(): void {
    this.onopen?.();
  }

  receive(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent);
  }

  sentFrames(): unknown[] {
    return this.sent.map((raw) => JSON.parse(raw) as unknown);
  }
}

function newSocket(): BandiSocket {
  return new BandiSocket({ seqResume: true, baseDelayMs: 1, heartbeatMs: 0, maxAttempts: 0 });
}

async function latestAfterReconnect(socket: BandiSocket): Promise<FakeWebSocket> {
  socket.connect();
  const first = FakeWebSocket.instances.at(-1) as FakeWebSocket;
  first.open();
  first.onclose?.(); // 触发重连
  await new Promise((resolve) => setTimeout(resolve, 25));
  return FakeWebSocket.instances.at(-1) as FakeWebSocket;
}

describe("BandiSocket · seq/resume 断线补发", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("首次连接不发 resume；记录水位后重连发 resume{last_seq}", async () => {
    const socket = newSocket();
    const second = await latestAfterReconnect(socket);
    expect(FakeWebSocket.instances).toHaveLength(2);
    const first = FakeWebSocket.instances[0];
    expect(first.sentFrames()).not.toContainEqual(expect.objectContaining({ type: "resume" }));

    // 断线前收到 seq 1..3 → 水位 3
    const frames: WsFrame[] = [];
    socket.onFrame((frame) => frames.push(frame));
    second.receive({ type: "message", seq: 1, data: {} });
    second.receive({ type: "message", seq: 2, data: {} });
    second.receive({ type: "message", seq: 3, data: {} });
    expect(frames).toHaveLength(3);

    // 再断线重连 → 必须带水位请求补发
    second.onclose?.();
    await new Promise((resolve) => setTimeout(resolve, 25));
    const third = FakeWebSocket.instances.at(-1) as FakeWebSocket;
    third.open();
    expect(third.sentFrames()).toContainEqual({ type: "resume", last_seq: 3 });
  });

  it("多会话水位分桶：A 会话的水位不吞掉 B 会话的帧（一条连接服务多会话）", async () => {
    const socket = newSocket();
    const ws = await latestAfterReconnect(socket);
    const frames: WsFrame[] = [];
    socket.onFrame((frame) => frames.push(frame));

    // A 会话已处理到 seq=9（水位按作用域分别记）
    ws.receive({ type: "done", seq: 9, scope: "s-a" });
    // B 会话的 seq=1 远低于 A 的水位——必须仍被放行，否则切会话后整段回复被吞掉
    ws.receive({ type: "done", seq: 1, scope: "s-b" });

    expect(frames.filter((frame) => frame.type === "done")).toHaveLength(2);
  });

  it("会话通道：resume 带当前作用域（服务端据此按会话分桶补发）", async () => {
    const socket = newSocket();
    socket.setResumeScope("s-a");
    const ws = await latestAfterReconnect(socket);
    ws.receive({ type: "done", seq: 4, scope: "s-a" });

    ws.onclose?.();
    await new Promise((resolve) => setTimeout(resolve, 25));
    const next = FakeWebSocket.instances.at(-1) as FakeWebSocket;
    next.open();

    expect(next.sentFrames()).toContainEqual({ type: "resume", last_seq: 4, scope: "s-a" });
  });

  it("破坏者：水位以内的重复帧被丢弃（补发重叠不重复消费）", async () => {
    const socket = newSocket();
    const ws = await latestAfterReconnect(socket);
    const frames: WsFrame[] = [];
    socket.onFrame((frame) => frames.push(frame));

    ws.receive({ type: "message", seq: 7, data: { n: 7 } });
    ws.receive({ type: "message", seq: 7, data: { n: 7 } });
    ws.receive({ type: "message", seq: 6, data: { n: 6 } });

    expect(frames).toHaveLength(1);
    expect(frames[0]).toMatchObject({ seq: 7 });
  });

  it("缺口：seq 跳跃发一次合成 resume_gap（每连接一次）", async () => {
    const socket = newSocket();
    const ws = await latestAfterReconnect(socket);
    const frames: WsFrame[] = [];
    socket.onFrame((frame) => frames.push(frame));

    ws.receive({ type: "message", seq: 5, data: {} });
    ws.receive({ type: "message", seq: 9, data: {} });
    ws.receive({ type: "message", seq: 20, data: {} });

    const gaps = frames.filter((frame) => frame.type === "resume_gap");
    expect(gaps).toHaveLength(1);
    expect(gaps[0]).toMatchObject({ from: 6, to: 8 });
    expect(frames.filter((frame) => frame.type === "message")).toHaveLength(3);
  });

  it("resumed 帧抬水位（其后的 live 帧不被误判为缺口）", async () => {
    const socket = newSocket();
    const ws = await latestAfterReconnect(socket);
    const frames: WsFrame[] = [];
    socket.onFrame((frame) => frames.push(frame));

    ws.receive({ type: "message", seq: 4, data: {} });
    ws.receive({ type: "resumed", replayed: 0, last_seq: 40 });
    ws.receive({ type: "message", seq: 41, data: {} });

    expect(frames.filter((frame) => frame.type === "resume_gap")).toHaveLength(0);
    expect(frames.filter((frame) => frame.type === "message")).toHaveLength(2);
  });

  it("无 seq 的帧（pong/端点直发）不受协议门影响", async () => {
    const socket = newSocket();
    const ws = await latestAfterReconnect(socket);
    const frames: WsFrame[] = [];
    socket.onFrame((frame) => frames.push(frame));

    ws.receive({ type: "pong", ts: 1 });
    ws.receive({ type: "error", data: "content required" });

    expect(frames.map((frame) => frame.type)).toEqual(["pong", "error"]);
  });
});
