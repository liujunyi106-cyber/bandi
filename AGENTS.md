# AGENTS.md — Bandi Agent 开发计划

构建**个人多 Agent 数字员工组织（组织向）**框架（Python 3.11+）→ 桌面产品（React 18 + shadcn/ui）。产品主线 = 个人/小团队搭建并管理 AI 员工组织（角色/SOP/skills/模型 + 项目制）；本体 5 实体降级为**知识组织层**。

**当前状态（2026-11-06）**：P5 前端轨道主线 ✅（Day135-144 + 137b-3/137c/137d/137e + 资源中心 + FE-05 SOP 编辑器）；**2026-10-31 拍板：Day145/146（FE-07/FE-08）与 P5-5c 剩余项不做（原型已删）、Day144 后台定时任务工具全部放行、Sonner toast 移顶部居中**；**2026-10-31 收口：P0-1~P0-6 全部完成（并行三线 hunk 拆批提交、verify 9/9、前端 104 files/961 tests、openapi 170 paths）**。**P5-5b ✅（2026-11-01 并行会话交付）→ 下一 = P5-6（Electron 打包，Day147-151）→ P6（插件轨道，ADR-38）**；**2026-11-01 全量审计 + P1 清收：P0/P1 已修（project_root/保留 id/员工免审批/allow_session/席位 owner/记忆 scope/审批窗口/桌面 API key/CSP/就绪门）；剩余 P5-6 sidecar 见 交付文档收集/2026-11-01-全量Bug与安全审计-交付文档.md**。**进度唯一真相 = `学习计划与执行日志.md` 日志表**；TierB / SD / CT / 并发工程 / 记忆工程 / 审计收口等历史批次与债务明细见日志表与 `docs/技术债务清单.md`，本行不再展开。**⚠️ 并行工作区改动常在同树，提交按「并行工作区与提交纪律」精确列文件。**

**2026-11-06 发布批（本笔）**：0.2.3 桌面修复收口（线程级 token、员工身份头统一、断线不再自动结算、记忆问候过滤、群聊/DM 路由、CSP 字体）；真机验收通过；GitHub Release `v0.2.3` 已发布（4 资产，`_release-public` 发布提交 `9d1e746`，源码补齐后 `main=52b09d0`）；0.2.3→0.2.4 自动更新下载验证通过，0.2.4 Release/tag 已删除、本机回 0.2.3；清理约 1.5GB 构建产物与临时文件。**本地开发仓 git 已修复**：旧历史 164 个对象不可恢复，已以发布基线 `9d1e746` + 当前工作区重建干净历史 `ed1ac9c`（备份 `Desktop/bandi-git-backup-20261106`，`git fsck` 0 错误）。详见 `交付文档收集/2026-11-06-0.2.3发布与自动更新验证-交付文档.md`。

**2026-10-30 批（本笔收尾）**：Day144 会话化定时任务 + 资源中心修复 + RBAC 删除/时区口径 + Agent 长任务目标治理 P0/P1/P2 + **FE-05 SOP 可视化编辑器**（含新建 SOP、节点 ID 重命名、复制节点、保存升 patch、出边列表删除连线）；全量 `verify.ps1` **9/9 PASSED**、前端 **101 files / 954 tests**、build/lint/i18n 通过、FE-05 真机 18/18；按后端 / 资源中心+SOP / 团队+看板 / 文档四批提交。

**2026-10-31 范围拍板批（本笔）**：Day145（Dashboard 全景 FE-07）/ Day146（Traces + 质量门控 FE-08）/ P5-5c 剩余项（DistillPage、技能市场三态、应用中心 FE-13、资源市场半步 FE-14）**全部拍板不做**，原型 HTML 物理删除、排期/差距/设计规划/交付索引同步；Day144 后台定时任务**工具白名单取消**（助手侧 `_BackgroundInteraction` + 员工侧 SeatExecutor 交互通道统一自动放行 `ask_permission`/`ask_confirmation`，`ask_input` 仍空；风险：后台可无人工执行写/命令，路径沙箱与权限档仍生效）；FE-05 Sonner toast 全局移至顶部居中（避免遮挡 SOP 编辑器右上按钮与底部输入区）；保留 P5-5b（知识库前端 FE-09 + 资产中心 FE-12）待做。

## 全局纪律（必读 ⚠️）

纪律按执行链路排列共 13 项。**违反任何一项将导致交付文档不合格。**

| 优先级 | 含义 | 违反后处理 |

|------|------|-----------|

| 🔴 P0 | 不可违反，破了=项目质量不可逆 | 立即停下来，补完再继续 |

| 🟡 P1 | 必须遵守，特殊情况可补 | 记入交付文档"未完成项"，当天解决 |

| 🟢 P2 | 遵守优先，允许延迟 | 记入交付文档"未完成项"，下个 Day 补 |

### 1. 阅读纪律（Read Discipline）🔴 P0

进入每个 Day 前必须完成阅读链，**不准在未完成阅读链的情况下直接写代码**：

- 任务确认：读 `学习计划与执行日志.md` 当日条目

- 架构对齐：读 `docs/架构设计细节.md` 涉及 §节

- 差距/债检查：读 `docs/Harness工程差距分析与改进.md` §五 P0 清单 + `docs/技术债务清单.md` 涉及项

- 源码通读：用 Read 工具读目标模块全部现有源文件（不准跳读）

### 2. Skills 使用纪律（Skills Discipline）🔴 P0

每个 session 开始按场景加载 skills，**不准未加载就开工**：编程=`brainstorming`+`implement`+`karpathy-guidelines`；重构/Bug=`systematic-debugging`+`code-review`；测试=`test-driven-development`；规划=`planning-with-files`；PRD=`pm-skills`；前端 UI=`finesse-skill`+`better-icons`。

- **前端 UI 专项（强制，2026-09-21 起；2026-09-22 换用 finesse-skill 发行包）**：任何前端/UI 实现（`frontend/`、Electron 壳、页面/组件/样式/图表/图标）开工前**必须先加载 `finesse-skill` + `better-icons` 两个 skill**，未加载即开工 = §2 P0 违规。技能位置：用户级 `~/.workbuddy/skills/{finesse-skill,better-icons}`（源：`D:\浏览器下载\skills集合\关于ui设计师的skills\` + `finesse-skill` 发行仓库 v0.10.0）；项目侧 `.opencode/skills/finesse-skill/`（顶层 SKILL.md 为 openCode 加载入口，嵌套 `skills/finesse-ui/` 为发行仓库原貌）。用法——`finesse-skill` 定「register（brand / product / commerce）+ 三档（SOUL / SPECTACLE / DENSITY）+ premium substrate + cheapness blacklist + preflight」，交付前跑 `node scripts/detect.mjs <file>`；`better-icons` 供 Iconify 200+ 图标库检索取 SVG（`npx better-icons search <q>` / `npx better-icons get <prefix:name>`）。
- **前端参考文件（强制）**：前端实现**必读** `docs/前端设计规划.md`（前端参考设计总纲：技术栈 / 色板 token / 字体 / 图标规范 / 功能分区矩阵 / 交互与状态规范 / 落地顺序）+ **`docs/前端组件与交互规范.md`**（控件级细则：按钮变体 / 圆形图标钮悬浮填充 / @提及色 / 胶囊控件；两者冲突以规范文件为准）；配色与视觉母版 = `docs/staffdeck-ui-参考.md`（注意头部 SD1 改版注记）；决策依据 = `docs/ADR.md` ADR-35/36/42。

### 3. 外部参考依赖纪律（External Reference Discipline）🟡 P1

实现新模块前**不准从零闭门造方案**：按 ADR-16 优先级查参考源（LeAgent⭐1 → pi⭐2 → ~~DataLink~~→ OpenOPC⭐4 → cc-haha⭐5 → StaffDeck⭐6 → YooDesk 叙事）；交付文档 §3 记录参考了哪个源的哪个文件、借鉴了什么；不参考须写明理由。

### 4. 编码纪律（Coding Discipline）🔴 P0

- 每个 Day 在**独立 session** 完成；同一 session 产出多个 Day，每完成一个必须先走收工流程（§10）

- Session 被打断恢复时，先读日志表 + 最新交付文档确认进度

- 编码前 git working tree clean；修改已有代码前先 Read 完整文件；一次只改一个模块（≤3 个无关模块）

### 5. 代码质量执行纪律（Code Quality Discipline）🔴 P0

- **Commit 门禁**：verify.ps1 不通过 → 不准提交

- 零屎山：无注释代码、无魔法数字、无 >3 层缩进；DRY（重复 >3 行提取）；删除优先；公开函数必有类型标注

- 注释只写「当前功能是什么」（`# 功能：中文描述`），不留修复历史/旧实现对比注释

- 修改后必须跑全部已有测试回归

### 5a. 注释规范（Comment Discipline）🔴 P0

**注释只写「当前功能是什么」**（`# 功能：中文描述`），一条注释一行功能说明，**禁止**以下内容（2026-10-06 用户拍板，全库已清理 1240 行）：

- **内部工作流痕迹**：编号（`P0-1`/`C-2`/`K-2`/`CT-06`/`HB-P2`/`FM-96`/`AUD3-7`/`MEM-1`/`T4`/`E4`/`S8` 等）、审计/审查/复核/终审/清收标记、日期（`2026-09-29` 等）、`（用户拍板）`/`（真机反馈）`/`（实测）`类来源标注；
- **参考源对照叙述**：`对齐 pi/opencode`、`（pi 同值）`、`同构`/`同值`/`参考源` 等比较说明——写「功能本身」，不写「跟谁对齐」；
- **括号尾巴**：任何以 `（…）` 收尾的补充说明（修复历史、旧实现对比、「伪接线」「死配置」类批判语）。

新写注释 = 一句话说清这段代码**在做什么**（可含关键约束如「头 60%/尾 40%」这类**事实参数**）；**追溯信息一律进交付文档/git log，不进代码**。测试契约同理（不得断言源码中存在特定注释串）。改动后必跑全量回归。

### 6. 测试纪律（Test Discipline）🟡 P1

不准先实现后补测试（TDD 优先）；单测用 MockProvider；每模块 ≥3 用例；**测试断言「行为结果/最终产物」而非「代码存在」**。

### 6a. 功能验证纪律（Feature Verification Discipline）🟡 P1

**完整功能必须当场端到端验证**：走生产主链路（真实 store）+ 断言最终产物（LLM 请求内容级/写入可读回）+ 三问自检（生产主链路吗？断言最终产物吗？真实输入中文/空/超长/多轮还能通吗？）。接线类 bug 单测抓不到，交付文档 §4 必须含端到端验证描述（含红灯证据）。

### 6b. 契约测试纪律（Contract Test Discipline）🔴 P0

**新增/修改关键模块行为必须同步补 `tests/test_contracts/` 契约**（测承诺行为不变式，不测实现）；缺失契约先补契约再改代码。完成定义（DoD）每项修复/新功能必须全部勾选：

1. TDD 红→绿（契约先于/同步于实现，禁止先修后补）

2. 契约测试覆盖

3. 破坏者视角用例（至少一个"怎么绕过它"）

4. Windows 实测

5. 真实数据形态（旧库/中文/特殊字符）

6. 失败模式清单对照（`交付文档收集/失败模式清单逐条核对-2026-09-28.md`；原 `docs/失败模式清单.md` 已于 2026-09-28 删除，全文迁入该报告「附」节）

7. verify 6/6

8. 交付文档 + 技术债清单

9. **同族清单（FM-8）**：修 bug 必跑 `fm8-sweep.ps1 -Pattern <被修 API>`，同族实例逐一标注 已修/已登记/不适用——禁止只修点名实例不查同族

10. **契约族级覆盖**：契约钉**行为族**非单路径——列出全部生产调用路径并逐条有契约（参数化覆盖全部成员）；`fm8-sweep -ReverseSync` 反核对

11. **接线完整性（FM-1）**：新增符号必跑 `wiring-check.ps1 -Symbol/-AssemblyPoint` 确认生产调用点与全装配点参数透传——**零生产调用/装配缺参 = 没接线，禁止交付**

12. **端到端跑通**：生产主链路真实执行 + 断言最终产物（§6a）

- 契约钉行为不钉实现（禁 grep 源码断言"代码存在"）

- 审计方法论（破坏者视角）：问「怎么绕过/什么输入崩/跨入口一致吗」，验证"打破"非"存在"，报告必须含破坏性验证证据

- 新失败模式回填 `交付文档收集/失败模式清单逐条核对-2026-09-28.md`（「附：原《失败模式清单》全文归档」节）

### 7. 提交前审查纪律（Pre-Commit Review Discipline）🔴 P0

`git commit` 前逐行审查 diff：无调试代码（print/pdb）、无 key/路径泄漏、无无关文件（pyc/venv/缓存）、无注释代码块。**不准提交自己不理解或未逐行审查的代码。**

### 8. 文档同步纪律（Doc Sync Discipline）🟢 P2

代码变更涉及文档必须同步：配置字段→`config.yaml`+架构 §十八-C；接口/行为→架构对应 §节；依赖→`pyproject.toml`；架构决策偏离→`docs/ADR.md` 新条目；仓库规范变动→本文件。

### 8a. 交付文档纪律（Delivery Doc Discipline）🔴 P0

改代码后立即在 `交付文档收集/` 写变更记录：文件路径行号、改了什么与为什么、变更类型（新增/修复/重构/测试/配置）。跳过 = 🔴 P0，AI 须点出并阻止提交。

该目录**只在本地留存**（已在 `.gitignore` 排除）：不进仓库、不推远端，任何提交/推送清单都不得包含它。

### 9. 飞书同步纪律（Feishu Sync Discipline）🟡 P1

收工前完成：① 用 `lark-cli base +record-batch-update` 手动更新「每日任务」状态/备注，并按需写反馈表（原 `scripts/complete-day.ps1` 已删除）② 如需调整后续计划同步任务表。

### 10. 收工纪律（Ship Discipline）🟡 P1

每个 Day 结束按序执行，**不可调换或跳过**：① verify.ps1 ② 纪律自查（§1-§13 逐项）③ 写 `交付文档收集/Day{N}-交付文档.md` ④ 飞书同步 ⑤ 精确 stage 本任务文件后 commit（引用 Day；**禁用盲目 `git add -A`**，见「并行工作区与提交纪律」）。**不准在交付文档未就绪时 commit。**

### 11. Session 边界纪律（Session Boundary Discipline）🔴 P0

跨 Day（"继续下一个 Day"）→ 先收工（§10）再启动新 Day；session 中断恢复 → 先读日志表+交付文档确认进度再编码；同 session 多 Day → 每个 Day 分别走收工流程。

### 12. 人类拦截纪律（Human Override Discipline）🔴 P0

用户跳过/忽略纪律时：① 识别跳过意图（"别读了直接写"/"先跳过去"等）② 主动点出被跳过纪律编号与后果 ③ 用户二次确认后记录到交付文档"未完成项"再继续 ④ **不可静默妥协**。用户说"开始 Day N"→ 先输出启动卡片（§工作流）再编码。

### 13. 文档底座纪律（Doc Base Discipline）🔴 P0

唯一底座 = `docs/架构设计细节.md`（§十八 Phase 范围 + §十九 目录树）。其他文档描述范围/目录须标 `@base: docs/架构设计细节.md §...`；`学习计划与执行日志.md` §2 计划表不设模块/测试列（表头标注指向 §十八）；`技术债务清单.md` 只记 issue+状态。

### 纪律自查清单（收工 Step ②逐项检查）

| 自查 | 优先级 | 未完成处理 |

|------|--------|-----------|

| §1 读当日架构+源码 / §2 加载 skills / §3 查参考源 | P0/P0/P1 | 补做再继续/补进交付文档 |

| §4 独立 session + git clean / §5 verify 过 + 无 TODO | P0 | 修复到通过 |

| §6 测试已写 / §6a 端到端验证已做 | P1 | 当天补 |

| §6b 契约 + DoD 12 项全过 | P0 | 补完再提交 |

| §7 diff 逐行审查 / §8a 交付文档已写 | P0 | 审查完/补写再提交 |

| §9 飞书同步 / §10 交付文档+日志表更新 | P1 | 当天补 |

| §11 session 边界干净 / §12 无静默接受跳过 / §13 底座同步 | P0/P0/P2 | 处理完再继续 |

## 关键文档（阅读顺序）

AGENTS.md（根） → 学习计划与执行日志.md → D:\个人知识图谱\index.md

| 文档 | 用途 |

|------|------|

| `学习计划与执行日志.md` | 逐天任务分解 + Phase 里程碑 + 日志表（叙事风）——**进度唯一真相** |

| `docs/架构设计细节.md` | 9 层实现级架构（L0-L8）+ 目录树（唯一底座） |

| `docs/ADR.md` | 架构决策及理由（41 项） |

| `docs/技术债务清单.md` | 未修复项追踪；`docs/待补齐差距清单-P4后.md` P4 后差距 |

| `docs/PRD-Bandi.md` / `docs/测试与评测标准.md` | 需求范围 / 测试标准 |

| `功能验证纪律.md` | 端到端验证方法与接线 bug 案例 |

| `交付文档收集/失败模式清单逐条核对-2026-09-28.md`（原 `docs/失败模式清单.md`，2026-09-28 删除并入） / `docs/待测试调优登记.md` | 失败模式回填 / 取舍登记 |

| **`docs/前端设计规划.md`** | **前端参考设计文件**（Day135 起前端实施唯一依据）+ `docs/staffdeck-ui-参考.md` 视觉母版；**控件级细则 = `docs/前端组件与交互规范.md`**（按钮变体 / 圆钮悬浮填充 / @提及 / 胶囊，冲突以规范文件为准） |

| `交付文档收集/AGENTS.md` | 交付文档索引（每份交付文档写入后同步登记） |

## 工作流规则

### Day 启动流程（门禁 ⚠️）

**Step -1** 跑 `verify-start.ps1`（git clean / 上 Day 交付文档存在）。

**Step 0** AI 输出启动卡片（§1 阅读链/§2 skills/§3 参考源/§4 git clean 逐项打勾），**不准在卡片未全确认前编码**；用户确认或跳过（跳过记入交付文档"未完成项"）后进入编码。

### 每日交付前验证

```powershell

.\bandi\scripts\verify.ps1                  # ruff + import + config + 目录树 + pytest（commit 门禁；失败详情见「关键陷阱」）

.\bandi\scripts\fm8-sweep.ps1 -Pattern X    # DoD-9 同族清单（修 bug 必跑）

.\bandi\scripts\wiring-check.ps1 -Symbol X  # DoD-11 接线完整性（新功能必跑）；-StubScan 查 stub

```

验证通过 + 交付文档就绪后：`git add <本任务文件显式列表> && git commit -m "feat/fix/docs: {块} 英文描述"`（**禁用盲目 `git add -A`**，见「并行工作区与提交纪律」）

### 每日交付文档（`交付文档收集/Day{N}-交付文档.md`）

当日核心模块/功能 · 代码变更明细（文件+行号+类型）· 参考源借鉴 · 架构对齐（偏离注明原因）· 验证记录（verify 副本）· commit hash · 未完成项与阻塞点 · **下一个 Day 恢复点**（起始模块/必读文档/必加载 skills，中断恢复据此直接启动）。

## 代码规范（Phase 1 生效）

- 格式化 ruff（line-length=100）；类型全标注；测试 pytest + MockProvider；命名 PascalCase/snake_case/UPPER_SNAKE

- 错误继承 `BandiError`；工具异常不阻断 Agent 循环；导入分组（stdlib→三方→本地）

- Commit `feat/fix/docs/refactor: 英文描述`（行 ≤72）；简洁优先（3 行不用 50 行），功能入口 `# 功能：中文描述`

## 关键命令

```powershell
# 全部从仓库根执行；venv 在 bandi\.venv（不是根目录）
.\bandi\scripts\verify.ps1                  # 交付前验证（ruff+import+config+目录树+pytest -n 4，全量 ~8min）
.\bandi\.venv\Scripts\python.exe -m pytest bandi\tests -q -n 4 -m "not heavy"                          # 快轨（排除重型：子进程/外部工具/网络/浏览器）
.\bandi\.venv\Scripts\python.exe -m pytest bandi\tests\test_tools\test_tools.py -q                     # 单文件
.\bandi\.venv\Scripts\python.exe -m pytest bandi\tests\test_contracts\test_contract_x.py::TestY::test_z -q  # 单用例
git add <本任务文件显式列表>; git commit -m "feat: 英文描述"   # 禁用盲目 git add -A（见「并行工作区与提交纪律」）
git log --oneline -5                         # 看历史
```

## 外部依赖

- 知识库：`D:\个人知识图谱\`（接入点 schema\CLAUDE.md）

- 参考源码（ADR-16）：`D:\源码\各Agent源码\{LeAgent, pi, openopc, cc-haha, ...}`（详见 §3）

- 生产级参考：`D:\Program Files\Tencent\Marvis\`

## 注意

- `p1/p2/p3.json`、`sync_batch.json` 为飞书 payload（gitignored）

- 测试用 MockProvider 不调真实 API（真实测试 DeepSeek 全套约 ¥1-2）；前端构建需 node/pnpm

- `.opencode/skills/` 132 个项目级 skills，`in-progress/`/`deprecated/` 按需启用

## Git 仓库信息

- **仓库位置**：`深度学习与实践计划/` 根目录（**不是** `bandi/`）；bandi 下禁止 `git init`

- **禁止** 在根目录之外的子目录独立建仓；`bandi/` 有自身历史（已在根仓内合并演进，日常 `git -C bandi log` 查看）

## 并行工作区与提交纪律（高频陷阱 ⚠️）

本仓库常有**并行工作区在途改动**（另一会话/任务修改的若干文件未提交，`git status` 出现大量 M/??）。规则：

- **禁止盲目 `git add -A`**：提交前先 `git status --porcelain` 核对；只 `git add <本任务文件显式列表>`

- 本任务文件与并行改动**混在同一文件**时：用 `git apply --cached <仅含本任务 hunk 的 patch>` 只暂存自己的 hunk（本仓库先例：`cli/app.py`、`agent/factory.py`），未分离时禁止整文件 add

- 并行改动**不提交、不回滚、不代修**；verify 若因并行文件失败（ruff/格式/pytest），记录「失败项 100% 归属并行文件」后照常提交本任务（先例多起）

- 长任务开始时先 `git status` 记基线，提交前再核对一次

## 关键陷阱（本仓库实证）

- **PowerShell 内联写文档会吞反引号**：`` `f `` 被解析为 form feed（历史事故：`file_edit.py` 在文档中变 `\x0cile_edit.py`，FM-38）。**写含反引号的文档/代码一律用 Write/Edit 工具或 Python 脚本文件**，不要用 `python -c "..."` 内联中文 + 反引号

- **符号级存在性检查用 Grep 工具**（findstr 曾漏检：`format_lines_with_numbers` 被判零引用后删除，域回归才发现测试类仍直接调用）

- **verify.ps1 会吞 pytest 失败详情**：失败时手动 `bandi\.venv\Scripts\python.exe -m pytest -q --tb=line`（workdir=bandi）定位；PowerShell `*>` 重定向默认 UTF-16，读取需用 `utf-16`

- **测试隔离**（FM-62 家族）：默认路径组件（`Path.home()`/`os.path.expanduser`）必须 tmp + env 双保险；端点直调测试需重置依赖单例，否则写穿真实 `~/.bandi/`

