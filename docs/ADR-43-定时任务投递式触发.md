# ADR-43：定时任务改为「投递式」触发

## 背景

Bandi 现在的定时任务到期后**直接调用 `run_agent_turn`**（headless 路径）：

```python
# bandi/api/app.py  _headless_runner
outcome = await run_agent_turn(
    user_input=background_prompt,
    mode="execute",
    session_id=session_id,
    user_interaction=_BackgroundInteraction(),   # 模拟交互
    ...
)
```

这条路径**独立于用户正常发消息的链路**，有自己的失败面：

1. `_BackgroundInteraction` 是模拟交互，任何需要真实 UI 的环节都只能 fail-open
2. 失败时 Agent 把异常包装成一句普通回复返回，调度器无法区分成败
   （真机：`last_error=None`、`last_output=模型服务暂时不可用`，事后无法复盘）
3. 任务与用户的会话历史**天然分离**，用户看不到"这个自动化刚才做了什么"

## 参考源

DSH（DeepSeek Harness）的 `dsh-schedule`：

```javascript
"Host timer over stored tasks; Session activation is a delivery operation."
"delivery commits only when ctx.sessions.flush() reports that a message is durable"
"Dispatch failures are logged; refused admission does not retry automatically."
```

核心：**定时器不执行任务，只把一条消息投递到会话投递箱（inbox）**。
真正的执行由会话侧按正常回合推进——与用户手动发消息**完全同一条路径**。

配套设计：
- `deliveryReceipt`（`deliveredAt` / `scheduledAt` / `messageId`）随任务持久化
- `deliveryHistory` 保留窗口（天数 + 条数上限）
- 投递失败**只记日志、不自动重试**（拒绝 admission 重试会风暴）

## 决策

采纳「投递式」，分两步落地。

### 第一步（本 ADR 范围）：投递 + 消费队列

**新增 durable 投递表**（与 `scheduled_tasks` 同库）：

```
task_deliveries
  delivery_id   TEXT PRIMARY KEY
  task_id       TEXT NOT NULL
  session_id    TEXT NOT NULL
  prompt        TEXT NOT NULL
  scheduled_at  TEXT NOT NULL   -- 原本该触发的时刻
  delivered_at  TEXT NOT NULL   -- 实际投递时刻
  status        TEXT NOT NULL   -- pending / consumed / failed
  consumed_at   TEXT
  error         TEXT
```

**调度器改动**：`_fire` 不再调 runner，改为写一行 `pending` 投递，
然后立即 `_advance(task)` 推进窗口（投递成功≠执行成功，窗口不因执行失败而卡住）。

**消费侧**：新增 `DeliveryConsumer`，由主循环在**会话空闲时**取 `pending` 投递，
走 `run_agent_turn` 的**同一入口**（与用户发消息同路径），执行完把投递置 `consumed`。

### 第二步（后续）：deliveryHistory 保留窗口

`deliveryHistory` 天数/条数上限、前端展示投递历史。本 ADR 只落表结构与状态机。

## 不采纳的方案

**A. 维持 headless 直调**
已证伪：失败不可见、与用户链路分叉。且真机出现过「任务跑了但用户完全无感」。

**B. 投递失败自动重试**
DSH 明确「refused admission does not retry automatically」。
重试会放大故障：投递是**幂等写**，失败通常是库不可用，重试只会加剧。

## 影响面

| 文件 | 改动 |
|---|---|
| `scheduler/store.py` | 新增 `task_deliveries` 表 + `enqueue_delivery` / `claim_pending_deliveries` / `mark_delivery` |
| `scheduler/scheduler.py` | `_fire` 改投递；保留 `runner` 注入作为兼容路径 |
| `scheduler/consumer.py` | 新增投递消费者 |
| `api/app.py` | 装配消费者；`_headless_runner` 保留供 `trigger` 手动调用 |

## 验收

1. 到期任务产生 `pending` 投递行，`next_run_at` 已推进
2. 消费者取到投递后走 `run_agent_turn`，成功置 `consumed`
3. 失败置 `failed` + `error`，**不自动重试**
4. 同一投递不会被两个消费者重复执行（`claim` 原子）
