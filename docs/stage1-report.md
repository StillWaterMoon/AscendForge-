# 阶段 1 报告：真实提交闭环

日期：2026-10-21（Asia/Shanghai）
执行环境：本机 AscendForge 实例（`data/`），CANNJudge 真实账号会话，无 NPU/CANN。
证据文件：[stage1-2026-10-21.json](evidence/stage1-2026-10-21.json)

> 后续进展（2026-10-22）：Agent 在 attempt 24 取得真实 `15/15 Pass`（remote id `6ad99b643ff2fef7e806c2f4`，`precision_ratio=1.0`），best 已写入。本文保留的是 2026-10-21 的模板基线历史，不代表当前最终状态。

## 1. 结论

**阶段 1 的真实“登录 → 导入题目 → 保存快照 → 提交 → 取得远端 ID → 查询最终结果 → 保存原始反馈”闭环已跑通。**

但本次提交的是题目模板的可编辑文件，不是可工作的 CopySign 实现。平台返回：

```text
status: Runtime Error
visible testcases: 0/15 passed
message: [Evaluation check failed] profiling and API validation:
         ProfileError: invocation launched no task
```

因此：

- **提交流程**：验收通过
- **算子正确性/性能**：未通过；没有任何 best，不能声称 CopySign 已实现
- **真实通过（Pass）**：仍待完成

## 2. 题目与任务

| 项目 | 值 |
|---|---|
| 平台 | CANNJudge |
| 题目 | CopySign（`copysign`） |
| 题目 ID | `6ad45f04193e507d8e54f48d` |
| 比赛 | `ladder_2026_jingzhou` |
| 题目链接 | `https://cannjudge.cn/ccpc/ladder_2026_jingzhou/copysign` |
| 工程类型 | `npu_kernel_dev` |
| 任务 ID | `task_1a15cec970375a9f2` |
| 任务终态 | `completed` / `manual_baseline_finished` |
| best | 无（失败结果不能成为 best） |

## 3. 提交与快照证据

| 项目 | 值 |
|---|---|
| snapshot ID | `snap_1a15cecce557c21e9` |
| 快照聚合 SHA-256 | `3aa9af10794b8e0a8fe7f085f2b02039b208b0dee64c15837c87cd618b377b95` |
| submission.request_hash | 同上，**一致** |
| 文件数 | 1 |
| 文件 | `kernel.asc`，1288 bytes |
| `kernel.asc` SHA-256 | `022d2dd0990f99c2cc76ffb194cdaec52bb841f8f1787190dca23f8f96f8641f` |
| submission ID | `sub_1a15cecd288284152` |
| 远端 remote ID | `6ad84d04193e507d8e4a0502` |
| 发送时间 | 2026-10-21 13:26:27（+08） |
| 结果时间 | 2026-10-21 13:27:08（+08） |

平台结果中的 `files` 回显了完整工程（8 个文件：模板 7 个只读 + 提交的 `kernel.asc`）。仅比较提交文件：

```text
platform echo kernel.asc SHA-256 = 022d2dd0...f8641f
snapshot       kernel.asc SHA-256 = 022d2dd0...f8641f
match = true
```

说明实际提交字节与服务器快照一致。

## 4. 结果

- 远端状态：`Runtime Error`
- 可见测试点：15
- 通过：0
- `precision_ratio`: 0
- `score`: null
- `valid`: true
- `theory_score`: 0
- 典型测试点消息：

```text
[Evaluation check failed] profiling and API validation:
ProfileError: invocation launched no task
```

15 个测试点均返回同类 Runtime Error。说明平台已实际接收并评测该提交，而不是网络/接口失败。

独立读回：

```text
GET /api/submissions/6ad84d04193e507d8e4a0502?userId=...
→ status=Runtime Error, result_count=15
```

与数据库保存的原始响应一致。

## 5. 事件链

```text
task_created
problem_imported
attempt_created (manual)
candidate_snapshot (sha 3aa9af10...)
baseline_created
task_running
submission_queued
submission_sending
submission_accepted (remote_id 6ad84d04193e507d8e4a0502)
submission_finished (runtime_error, 0/15)
task_completed (manual_baseline_finished)
```

## 6. 这次运行还暴露/修复的问题

1. **没有模型时点“开始”**：第一个任务（`task_1a15cea...`）进入 `needs_review`，错误码曾是错误的 `SUBMISSION_UNKNOWN`。已修复为按真实原因记录（`MODEL_NOT_CONFIGURED`），并新增不需要模型的“提交基线（手动）”路径。
2. **基线任务不应继续调用模型**：基线成功后任务自动 `completed`（`manual_baseline_finished`），不会因模型未配置再次进入 `needs_review`。
3. **手动基线不能重复**：同一任务只允许一次基线提交，防止误消耗额度。
4. **提交字节一致性**：`request_hash` 现在取自快照 SHA-256，而不是快照 ID；本次核对为一致。

## 7. 验证边界

| 项目 | 状态 |
|---|---|
| 真实登录/会话保存 | 已验证（用户 Web 登录；密码未保存） |
| 题目导入/模板/工程包 | 已验证 |
| 不可变快照 + SHA-256 | 已验证 |
| 真实提交 + remote ID | 已验证 |
| 真实结果查询/保存/独立读回 | 已验证 |
| 真实 `Pass` | **未验证**（基线是模板） |
| Agent 自动两轮优化 | **未验证**（模型未配置） |
| NPU 本地执行 | 不可用（无 NPU/CANN） |
| 自定义远端测试 | 未核实，禁用 |
| 429/401/重启/对账的真实演练 | 未验证（阶段 2） |

## 8. 下一步

1. 配置真实模型 API（base_url/key/model），先做连接测试。
2. 新建 CopySign 任务，让 Agent 读取题目合同、模板和本次 Runtime Error 反馈，生成真正的 `kernel.asc`。
3. 提交并读取反馈；目标是至少拿到 `Pass`，再讨论 best/性能。
4. 阶段 2：真实账号下验证 401、429、提交超时、unknown 对账、worker 重启与跨任务限频。
5. 阶段 3：完成至少两轮真实“生成/修改 → 提交 → 反馈 → 再修改”。
