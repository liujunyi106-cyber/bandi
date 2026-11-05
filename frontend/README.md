# Bandi 前端（`frontend/`）

> **@base docs/前端设计规划.md**（前端实施唯一依据）/ **docs/架构设计细节.md §十九**（目录树）/ **docs/ADR.md** ADR-35 / 36 / 42

React 18 + TypeScript + Vite + Tailwind CSS v4 + shadcn/ui 单页壳，Electron 壳承载（ADR-42）。

## 命令

```bash
pnpm install           # 装依赖（锁文件 pnpm-lock.yaml；Electron 二进制走镜像：ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/）
pnpm run dev           # 仅前端 dev server（127.0.0.1:5173，/api 与 /ws 代理到内核 8766）
pnpm run dev:api       # 仅内核（bandi/.venv 起 uvicorn）
pnpm run dev:desktop   # 三进程：vite + uvicorn + Electron 窗口（开发主入口）
pnpm run build         # tsc -b && vite build（产物 dist/）
pnpm run test          # vitest run
pnpm run lint          # eslint
pnpm run format        # prettier --write
pnpm run i18n:check    # zh/en 文案 key 漂移校验
pnpm run electron:compile  # Electron 主进程 / preload 编译（tsc → CJS，产物 electron/*.js）
```

> **桌面打包（`electron-builder`）属 P5-6（Day147-151）**：Day135 只落 `electron/` 壳骨架 + `electron-builder.yml`
> 配置，安装包构建时再补 `electron-builder` 依赖（其依赖树庞大，提前安装会显著拖慢 install）。

## 目录

```
frontend/
├── electron/          Electron 主进程壳（main.ts / preload.ts，编译产物 main.js 同目录，见 .gitignore）
├── scripts/           check-i18n.cjs（i18n key 漂移门禁）
└── src/
    ├── app/           nav-registry（左栏导航注册表，P6 插件可注入）
    ├── components/
    │   ├── layout/    AppShell / AppSidebar / AppHeader / PagePlaceholder
    │   ├── ui/        shadcn/ui 基础组件（CLI 生成，勿手改）
    │   └── chat|graph|org|knowledge/   业务域组件（随对应 Day 落）
    ├── hooks/         use-kernel-socket 等
    ├── i18n/          zh.json（基准）/ en.json / 查表实现
    ├── icons/         业务图标单一路径库（自绘，禁复制 StaffDeck path——AGPL 红线）
    ├── lib/           cn() 等工具
    ├── pages/         路由页（chat / team / work / knowledge / skills / automation / assets / settings）
    ├── plugins/       插件宿主 SDK 契约（P6）
    ├── services/      api.ts（同源 fetch + 统一错误体）/ ws.ts（断线重连 + 事件订阅）
    ├── slots/         可插槽容器注册表（P6 前端插件挂载点）
    ├── stores/        zustand（UI 全局态）
    ├── theme/         主题辅助（员工 tone / 危险级）
    └── types/         跨模块共享类型
```

## 约定

- **视觉 token 一律走 `src/index.css`**（Tailwind v4 `@theme`），色板照 `docs/前端设计规划.md` §3（SD1 冷白灰）；**不复刻任何参考源界面样式**。
- **仅 light 主题**：`dark:` 变体绑到永不出现的 `.dark` 类（§3.4）。
- **单页壳共享一条 WS**（`services/ws.ts` 模块级单例），导航切换不重连（§8）。
- 前端实现前**必加载 `finesse-ui` + `better-icons` 两个 skill**（根 AGENTS.md §2 前端 UI 专项）。
