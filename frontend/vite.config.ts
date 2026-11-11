import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import svgr from "vite-plugin-svgr";
import { defineConfig } from "vitest/config";

import { backendLauncher } from "./vite-backend-plugin";

/** 内核（FastAPI sidecar）监听地址——与 ADR-35/42 口径一致，仅回环。 */
const KERNEL_ORIGIN = process.env.BANDI_KERNEL_ORIGIN ?? "http://127.0.0.1:8766";

export default defineConfig(({ command }) => ({
  // 功能：build 用相对 base，使 dist/index.html 里的资源引用可被 file:// 与 sidecar 两种方式加载；
  // dev 保持 "/"，避免影响 HMR 与 /api 代理。
  base: command === "build" ? "./" : "/",
  plugins: [react(), tailwindcss(), svgr(), backendLauncher(KERNEL_ORIGIN)],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      // /api 含 WS 端点（/api/agent/stream 会话流式）——必须开 ws 升级代理
      "/api": { target: KERNEL_ORIGIN, changeOrigin: true, ws: true },
      "/ws": { target: KERNEL_ORIGIN.replace(/^http/, "ws"), ws: true },
      // 就绪探针必须落到内核；否则 Vite SPA fallback 会回 index.html 200 骗过门禁
      "/health": { target: KERNEL_ORIGIN, changeOrigin: true },
      "/ready": { target: KERNEL_ORIGIN, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    css: false,
    // 2026-09-23：交互密集/图谱计算类用例在系统负载高时超 5s 默认值（两次实证干扰全量门禁）——
    // 全局给足余量（正常用例不受影响，仅上限放宽）
    testTimeout: 20000,
  },
}));
