# 容器部署

容器里只跑**后端内核**（FastAPI + WebSocket，`8766`）。前端页面和 Electron 桌面壳仍在本机运行，
通过同一个 API 连过来——桌面壳依赖本机路径、`~/.bandi/` 与 Windows 凭据保护，不适合进容器。

## 文件

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 多阶段构建：builder 装依赖产出 venv，production 只带运行期产物，非 root 运行 |
| `docker-compose.yml` | 生产形态：具名卷存数据、健康检查、失败重启 |
| `docker-compose.dev.yml` | 开发形态：源码只读挂载 + `--reload`，改代码即时生效 |
| `.env.example` | 环境变量样例，复制成 `.env` 后填写 |

## 快速开始

```bash
cp deploy/.env.example deploy/.env
# 至少填 BANDI_API_KEY 与 BANDI_LLM_* 三项
docker compose -f deploy/docker-compose.yml up -d --build
curl http://127.0.0.1:8766/health
```

前端仍在仓库根目录跑：

```bash
cd frontend && pnpm run dev
```

Vite 会通过代理连到 `127.0.0.1:8766`，和本地直跑后端没有区别。

## 开发形态

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up --build
```

`bandi/bandi` 以只读方式挂进容器，`--reload` 监听该目录，改 Python 代码不用重建镜像。

## 数据

会话、记忆、团队、凭据档案都在容器内 `/data`，对应具名卷 `bandi-data`：

```bash
docker compose -f deploy/docker-compose.yml down          # 停服务，保留数据
docker compose -f deploy/docker-compose.yml down -v       # 连数据一起删
```

要复用宿主机已有的 `~/.bandi/`，把 compose 里的 `bandi-data:/data` 换成
`${HOME}/.bandi:/data`（Windows 下写成绝对路径，如 `C:/Users/you/.bandi:/data`）。

## 常见问题

**为什么容器只跑后端？** 桌面壳要拿本机文件系统、系统凭据与本地端口，容器化后这些能力都拿不到；
后端是纯 HTTP 服务，最适合进容器（例如放到常开的小主机上，本机只开前端）。

**能不能只跑容器不开前端？** 可以。后端自带 SPA 托管（`BANDI_SERVE_SPA`），把 `frontend/dist`
打进去即可；默认关闭，走 `pnpm run dev` 的开发链路。
