# AscendForge 架构与状态机

## 1. 进程拓扑

```text
本地主机浏览器
   │ HTTPS (反向代理)
   ▼
┌──────────────────────────── 服务器 ────────────────────────────┐
│  Web/API 进程 (python -m ascendforge serve)                      │
│   - 登录/会话/CSRF/幂等                                          │
│   - 任务配置、控制、状态查询、下载                                │
│   - 不执行 CLI turn、不直接提交                                  │
│                                                                  │
│  SQLite (WAL) + data/tasks + data/secrets                        │
│         ▲                     ▲                                  │
│         │                     │                                  │
│  Worker 进程 (python -m ascendforge worker)                      │
│   - Scheduler：持久化队列、限频、对账、轮询                       │
│   - 不调用模型，不执行候选代码                                    │
│         │                                                        │
│  Codex CLI runner（独立进程，按任务 workspace）                   │
│         └── CANNJudge HTTPS（服务器会话）                         │
└──────────────────────────────────────────────────────────────────┘
```

- Web 与 worker 是两个独立进程；可以分别重启。
- 浏览器关闭/电脑关机不影响 worker。Worker 由 systemd/容器托管。
- 所有持久化状态在 `ASCENDFORGE_DATA_DIR`；容器部署必须挂载持久卷。
- SQLite 使用 WAL、`busy_timeout=30s`，关键写操作用 `BEGIN IMMEDIATE`。

## 2. 模块

| 模块 | 文件 | 职责 |
|---|---|---|
| Web/API | `web.py`, `webui/` | 认证、CSRF、幂等、任务控制、下载、静态 UI |
| Worker | `worker.py` | 轮询、提交、恢复 |
| Scheduler | `scheduler.py` | 同账号队列、间隔/每日/在途/预算、unknown 对账 |
| Task | `tasks.py` | 配置校验、状态机、暂停/恢复/停止语义 |
| Experiments | `experiments.py` | 快照/哈希、结果归一化、best、档案 |
| Platform | `platform/` | 适配器契约、CANNJudge、连接服务 |
| Agent | `agent/` | 改动审计与提交前检查（CLI 执行） |
| Execution | `execution/` | NPU/CANN 能力检测 |
| Security | `security.py` | scrypt、Fernet、会话、CSRF、登录限流 |

## 3. 一次优化循环

1. 用户在 Web 创建任务：服务器解析题目链接 → 下载 ZIP → 保存 problem/template → 任务 `pending`。
2. `start` → `running`，`next_action_at=now`。
3. Worker 只负责提交与轮询；本地 Codex runner 独立创建 attempt：
   - 检查任务状态；
   - 取当前 attempt；没有则创建；
   - 启动/恢复 app-server thread，注入 SKILL.md、TOOLS.md、题目和历史；
   - Codex 在 workspace 内修改源码；
   - 宿主审计改动、生成快照、排队提交。
4. `create_candidate`：校验模板/文件 → 写不可变快照目录 + manifest + SHA-256 → attempt 绑定 snapshot。
5. `submit_official`：仅创建排队记录，attempt → `queued`；Codex 本轮结束。
6. Worker `process_queued_submissions`：
   - `BEGIN IMMEDIATE` 原子检查全局 unknown / 在途 / 间隔 / 每日 / 每题 / 运行时限；
   - 通过后 submission → `submitting`，写入 `sent_at`（**发送前**，失败也计入限频）；
   - 提交前重新读取快照并校验 SHA；
   - 调平台提交：
     - 有 `submissionId` → `judging`；
     - 超时/5xx/无 ID → `unknown`，任务 `needs_review`，冻结新提交；
     - 401 → `needs_login`；403 → `needs_review`；429 → 退避。
7. Worker `poll_due` 按独立节奏查询；最终结果 → 归一化 → attempt `finished` → 更新 best → 评估停止条件。
8. 下一轮 Codex turn 沿用同一 app-server thread，父版本指向 best 或上一版本。
9. `finish_task` / 预算 / 目标 / 停滞触发 `completed`。

## 4. 任务状态机

```text
pending ──start──▶ running ──pause──▶ paused
   │                 │  ▲               │
   │                 │  └──resume───────┘
   │                 ├──401──▶ needs_login ──resume──▶ running
   │                 ├──unknown──▶ needs_review ──resume──▶ running
   │                 ├──target/budget/stall/agent──▶ completed
   │                 ├──fatal──▶ failed
   └──stop──▶ stopped ◀──stop── (running/paused/needs_*)
```

- `completed` 表示“结束”，**不表示“有提升”**；可能没有更好版本。
- `paused` 不再产生新的 CLI turn/提交，但继续收集已在途结果。
- `needs_review` 冻结所有新提交（同账号全局 unknown），直到对账/人工确认。
- `stopped` 不撤销远端已接受的提交；取消尚未发送的 queued 提交。
- 非法转移抛出 `StateError`，Web 返回 409。

## 5. Attempt / Submission 状态

Attempt：`generated`（已创建/可写）→ `local_checking`（可选）→ `queued` → `submitting` → `judging` → `finished`；异常分支 `submission_unknown`、`failed`。

Submission：`queued` → `submitting` → `judging` → `finished`；异常 `unknown`、`failed`。

关键约束：

- 同一 attempt 不能有两个在途提交。
- 同账号任意 unknown 存在时，所有新提交冻结。
- `sent_at` 在 HTTP 请求前写入；失败尝试也计入频率/每日额度；`remote_id` 只在实际受理后写入。
- 远端结果原始 JSON 全量保存；归一化只做状态映射，不臆造耗时单位。

## 6. 快照与提交一致性

```text
data/tasks/<task_id>/snapshots/<snapshot_id>/
  files/<path...>           exact UTF-8 bytes
  manifest.json             snapshot_id, sha256, per-file sha256/bytes
```

- 聚合哈希：对排序后的 `path\0sha256\0size` 行取 SHA-256。
- 快照目录创建后不再修改；attempt 只能绑定一次。
- 提交前 `read_snapshot()` 重新读取并校验每个文件与聚合哈希；不一致直接失败，不发送。
- 提交文件规则：
  - `npu_kernel_dev`：模板所有 editable 文件必填；只读文件不可改（相同内容会被丢弃）；禁止 `judge.asc`/`main.asc`/`data_utils.h`；额外文件只允许根目录 `.asc`/`.h`。
  - 传统工程：`tiling_h` / `tiling_key_h` / `host_cpp` / `kernel_cpp` 四字段；多个候选文件时报错而不是猜。

## 7. 调度与预算

同账号全局：

- `min_submit_interval_sec`：从持久化 `sent_at` 计算，重启不重置。
- `max_inflight`：`submitting/submitted/judging/unknown` 计数。
- `max_submissions_per_day`：按 `Asia/Shanghai` 自然日；发送前计数。
- `max_submissions_per_task`。
- `max_runtime_sec`、`max_stall_rounds`、`max_attempts`、`target_score`。

所有检查与领取在同一个 `BEGIN IMMEDIATE` 事务中完成；崩溃恢复：
- `submitting` 超过租约 → `unknown`（保守：可能已受理）；
- queued 保留，恢复后继续；
- attempt/任务状态不重置；
- 间隔/每日计数来自 DB，不因重启清零。

## 8. Codex CLI 工具

Codex app-server 只暴露三个宿主工具：

```text
read_problem
submit_official
read_official_evaluation
```

`submit_official` 只创建 Scheduler 排队记录；Codex 不能直接访问 CANNJudge，也不能修改提交限额。宿主审计 workspace 改动、生成不可变快照并校验哈希。

## 9. 执行边界

- Codex CLI 在任务 workspace 内直接运行，使用自己的认证和沙箱配置。
- AscendForge 不再内置 Docker/Local executor，也不执行候选代码。
- 无 NPU 时 `candidate_execution=unavailable`，不会伪造设备结果。

## 10. 数据表（SQLite）

`users`, `sessions`, `idempotency`, `credentials`, `app_settings`, `tasks`, `attempts`, `snapshots`, `submissions`, `events`, `agent_messages`, `audit_log`, `cli_runs`, `task_messages`.

凭据只存 `credentials.ciphertext`（Fernet），主密钥在 `secrets/master.key` 或 `ASCENDFORGE_SECRET_KEY`；数据库备份不含明文密钥。

## 11. 恢复语义

| 故障 | 恢复 |
|---|---|
| Web 重启 | worker 不受影响；浏览器重新登录/轮询即可 |
| worker 重启 | 清理过期租约；`submitting` → unknown 对账；queued 继续；不重发已受理提交 |
| 主机重启 | 两个服务由进程管理器拉起；状态从磁盘恢复 |
| 502/网络中断 | 不宣称提交成功；unknown → 对账 |
| 登录过期 | 任务 `needs_login`；重新连接后手动恢复 |
| 唯一对账匹配 | 自动 adopt 远端 ID，继续查询并恢复任务 |
| 多候选/无候选 | 保持 `needs_review`，等待人工 adopt 或 mark_failed |
