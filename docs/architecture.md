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
│   - 不执行模型调用、不执行生成代码、不直接提交                     │
│                                                                  │
│  SQLite (WAL) + data/tasks + data/secrets                        │
│         ▲                     ▲                                  │
│         │                     │                                  │
│  Worker 进程 (python -m ascendforge worker)                      │
│   - Agent Runtime：上下文 → 模型 → 动作校验 → 工具               │
│   - Scheduler：持久化队列、限频、预算、对账、轮询                 │
│   - 隔离执行器：Docker(推荐) / 本地子进程                         │
│         │                                                        │
│         ├── 模型 API（只做推理）                                  │
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
| Worker | `worker.py` | 轮询、提交、Agent 单步、恢复 |
| Scheduler | `scheduler.py` | 同账号队列、间隔/每日/在途/预算、unknown 对账 |
| Task | `tasks.py` | 配置校验、状态机、暂停/恢复/停止语义 |
| Experiments | `experiments.py` | 快照/哈希、结果归一化、best、档案 |
| Platform | `platform/` | 适配器契约、CANNJudge、连接服务 |
| Agent | `agent/` | 协议、Provider、Runtime、工具、Prompt/Skill |
| Execution | `execution/` | Docker/本地执行器、能力检测 |
| Security | `security.py` | scrypt、Fernet、会话、CSRF、登录限流 |

## 3. 一次优化循环

1. 用户在 Web 创建任务：服务器解析题目链接 → 下载 ZIP → 保存 problem/template → 任务 `pending`。
2. `start` → `running`，`next_action_at=now`。
3. Worker 选取到期任务，获取租约，`AgentRuntime.step()`：
   - 检查任务状态与模型预算；
   - 取当前 attempt；没有则创建；
   - 组装上下文（题目合同、模板、历史、best、能力、预算）；
   - 调模型；
   - 只用宿主白名单工具执行动作；
   - 持久化动作、工具结果、用量。
4. `create_candidate`：校验模板/文件 → 写不可变快照目录 + manifest + SHA-256 → attempt 绑定 snapshot。
5. `request_submission`：仅创建排队记录，attempt → `queued`；模型结束本步。
6. Worker `process_queued_submissions`：
   - `BEGIN IMMEDIATE` 原子检查全局 unknown / 在途 / 间隔 / 每日 / 每题 / 运行时限；
   - 通过后 submission → `submitting`，写入 `sent_at`（**发送前**，失败也计入限频）；
   - 提交前重新读取快照并校验 SHA；
   - 调平台提交：
     - 有 `submissionId` → `judging`；
     - 超时/5xx/无 ID → `unknown`，任务 `needs_review`，冻结新提交；
     - 401 → `needs_login`；403 → `needs_review`；429 → 退避。
7. Worker `poll_due` 按独立节奏查询；最终结果 → 归一化 → attempt `finished` → 更新 best → 评估停止条件。
8. 下一轮 `AgentRuntime.step()` 用 result 历史创建新 attempt，父版本指向 best 或上一版本。
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
- `paused` 不再产生新的模型调用/执行/提交，但继续收集已在途结果。
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
- `max_model_calls`、`max_tokens`、`max_cost`（可选；未知价格不算 0）、`max_runtime_sec`、`max_stall_rounds`、`max_attempts`、`target_score`。

所有检查与领取在同一个 `BEGIN IMMEDIATE` 事务中完成；崩溃恢复：
- `submitting` 超过租约 → `unknown`（保守：可能已受理）；
- queued 保留，恢复后继续；
- attempt/任务状态不重置；
- 间隔/每日计数来自 DB，不因重启清零。

## 8. Agent 工具协议

模型输出结构化动作；宿主校验 `protocol_version/action_id/task_id/attempt_id/tool_name/arguments`、任务归属、状态和参数 schema 后执行。模型没有 shell/网络/凭据。

工具：`read_problem`、`read_history`、`create_candidate`、`create_test_generator`、`run_local_validation`、`request_submission`、`query_result`、`finish_task`。

`request_submission` 只入队；模型不能直接访问 CANNJudge，也不能修改预算。

## 9. 隔离执行

- `docker`：`--network none`、内存/CPU/PID 限制、`--cap-drop ALL`、`no-new-privileges`、临时 bind mount；推荐服务器。
- `local`：临时目录、最小环境、超时、输出上限；POSIX 下设置 rlimit。**不能强制禁网**，能力页明确告警；服务器不建议。
- 生成代码看不到数据库、`secrets/`、会话、API key；工作目录在 `data/tmp`，执行后删除。
- 无 NPU 时 `candidate_execution=unavailable`，本地验证只证明生成器/参考实现可执行，不证明 Ascend C 正确。

## 10. 数据表（SQLite）

`users`, `sessions`, `idempotency`, `credentials`, `app_settings`, `tasks`, `attempts`, `snapshots`, `generators`, `submissions`, `model_calls`, `events`, `agent_messages`, `audit_log`.

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
