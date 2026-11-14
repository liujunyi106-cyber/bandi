/**
 * 功能：Electron 预加载脚本（@base docs/ADR.md ADR-42）。
 * 安全基线：contextIsolation 开、nodeIntegration 关、sandbox 开——渲染进程只能拿到这里白名单暴露的能力。
 * 除只读运行信息外，只额外开放「启动阶段订阅」一条只读通道（启动占位页显示真实进度用）。
 */
import { contextBridge, ipcRenderer } from "electron";

/** 功能：启动阶段广播通道名（与主进程共用同一常量）。 */
const BOOT_STAGE_CHANNEL = "bandi:boot-stage";

/** 功能：启动阶段订阅——返回取消订阅函数，组件卸载时调用。 */
function onBootStage(listener: (stage: string) => void): () => void {
  const handler = (_event: unknown, stage: string): void => {
    listener(stage);
  };
  ipcRenderer.on(BOOT_STAGE_CHANNEL, handler);
  return () => {
    ipcRenderer.removeListener(BOOT_STAGE_CHANNEL, handler);
  };
}

const desktopApi = {
  /** 平台名（渲染侧做静态分支用） */
  platform: process.platform,
  /** 只读版本信息（用于页面诊断/版本提示） */
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
  /** 本地内核 API key（未配置时为空，渲染层不发鉴权头）。 */
  apiKey: process.env.BANDI_API_KEY ?? "",
  /** 订阅启动阶段（spawn → listening → probing → ready） */
  onBootStage,
} as const;

contextBridge.exposeInMainWorld("bandi", desktopApi);

export type BandiDesktopApi = typeof desktopApi;
