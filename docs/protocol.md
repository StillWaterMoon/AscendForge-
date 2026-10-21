# Agent 工具协议 v1

## 1. 设计原则

- 模型只输出**一个结构化动作**（或原生 tool call）；宿主校验并执行。
- 模型没有 shell、网络、文件系统、CANNJudge 凭据。
- 工具返回结构化 `ok/data/error`；错误可重试性明确。
- 动作、工具结果、短假设/摘要持久化；不要求/不保存隐藏思维链。
- `request_submission` 只入队；调度器拥有最终发送权。

## 2. 动作信封

```json
{
  "protocol_version": "1",
  "action_id": "action-001",
  "task_id": "task-...",
  "attempt_id": "att-...",
  "tool_name": "create_candidate",
  "arguments": {},
  "summary": "short reason"
}
```

校验：

- `protocol_version` 必须是 `1`。
- `task_id`/`attempt_id` 若提供，必须与当前任务/attempt 一致。
- `tool_name` 必须在白名单。
- `arguments` 必须通过对应 JSON Schema。
- `action_id` 缺失时宿主生成；重复 action_id 不保证幂等（工具自身有状态机保护）。

原生 tool calling 的 `function.name/arguments` 会被转换为同一信封。

## 3. 工具

### `read_problem`

参数：`{"include_contents": true}`

返回题目元数据、模板文件（editable 内容）、只读文件列表、禁止文件、说明。

### `read_history`

参数：`{"limit": 20}`

返回最近 attempts（状态、父版本、快照摘要、结果、提交状态、本地校验）、逐测试点 `testcase_precision`（index/status/precision_ratio/time/best_time/message）和 best。只返回摘要，不返回源码。

### `read_attempt_source`

参数：`{"attempt_id": "att-..."}`；省略时读取当前 attempt 的父版本（没有父版本时读取 best）。

返回该 attempt 的不可变快照、完整文件内容、SHA-256 和 hypothesis。用于在父代码上做最小修改，而不是从模板重写。

### `create_candidate`

参数：

```json
{
  "files": [{"path": "kernel.asc", "content": "..."}],
  "hypothesis": "baseline / tune tiling",
  "parent_attempt_id": "att-..."
}
```

- 当前 attempt 只能绑定一次快照；第二次调用报 `SNAPSHOT_EXISTS`。
- 默认父版本：显式 `parent_attempt_id` → 当前 attempt 的父版本 → 任务 best。
- `npu_kernel_dev`：缺失的 editable 文件优先从父快照补齐，其次从原始模板补齐；父版本的其他 helper 文件也会保留；只读文件不可修改；额外文件只允许根目录 `.asc/.h`；禁止 `judge.asc`/`main.asc`/`data_utils.h`。
- 传统工程：校验四字段文件。
- 快照前运行静态 preflight，拒绝 `reinterpret_cast<GlobalTensor<...>>`、`AscendC::Ands` 等已知不可用写法；dtype 覆盖不足给出 warning。
- 成功返回 `snapshot_id`、聚合 SHA-256、文件清单、`parent_attempt_id`、`filled_from_parent`、`filled_from_template`、`preflight`。

### `create_test_generator`

参数：

```json
{
  "generator_source": "...",
  "reference_source": "...",
  "seed": 1234,
  "spec": {"shape": [256,256], "dtype": "float16", "range": [-1,1]},
  "validation": {"atol": 1e-3, "rtol": 1e-3, "nan": "forbid"}
}
```

- 只保存不可变快照，不执行。
- 生成器/参考实现分别保存，记录 SHA-256、种子、规格、规则。

### `run_local_validation`

参数：`{"generator_id": "gen-..."}`

宿主用隔离执行器运行 `harness.py`：

1. `python generator.py --seed <seed> --out out`
   - 必须写 `out/manifest.json`。
2. `python reference.py --seed <seed> --manifest out/manifest.json --out out/reference`
   - 必须写 `out/reference/reference.json`。

结果：

```json
{
  "status": "reference_ok|generator_failed|reference_failed|timeout|executor_unavailable",
  "generator_ok": true,
  "reference_ok": true,
  "candidate_executed": false,
  "candidate_status": "unavailable",
  "seed": 1234,
  "spec": {},
  "rules": {},
  "backend": "docker|local",
  "network_enforced": true,
  "artifacts": [{"path": "out/...", "sha256": "...", "bytes": 123}],
  "limitations": ["..."]
}
```

- 无 NPU 时 `candidate_executed=false`，候选为 `unavailable/untested`。
- CPU 参考通过不代表 Ascend C 通过。
- 生成器/参考代码与候选代码都在同一隔离工作目录执行；Docker 模式禁网。

### `request_submission`

参数：`{}`

- 当前 attempt 必须有快照、状态为 `generated/local_checking`。
- 每个 attempt 只能有一个在途提交。
- 同账号有 unknown 时拒绝。
- 成功只创建 `queued` 记录；宿主调度器决定何时发送。

### `query_result`

参数：`{"submission_id": "..."}（可选）`

只读宿主数据库；不发起远端请求。返回 submission 状态、remote id、归一化结果、原始响应。

### `finish_task`

参数：

```json
{"reason": "target_reached|budget|no_improvement|blocked|user_request|other", "summary": "..."}
```

结束任务；不承诺有提升。已提交/在途的远端结果仍会由 worker 收集。

## 4. 工具返回

成功：

```json
{"ok": true, "data": {...}}
```

失败：

```json
{"ok": false, "error": {"code": "BAD_ARGUMENTS", "message": "...", "retryable": true}}
```

常见错误码：

`UNKNOWN_TOOL`, `BAD_ARGUMENTS`, `TASK_MISMATCH`, `ATTEMPT_MISMATCH`, `TASK_NOT_RUNNING`, `SNAPSHOT_EXISTS`, `NO_SNAPSHOT`, `MISSING_EDITABLE_FILES`, `READONLY_MODIFIED`, `FORBIDDEN_FILE`, `EXTRA_FILE_REJECTED`, `DUPLICATE_SUBMISSION`, `SUBMISSION_UNKNOWN`, `ACCOUNT_CANNOT_CONNECT_JUDGE`, `EXECUTOR_UNAVAILABLE`。

## 5. 模型 Provider 兼容

- 默认 OpenAI 兼容 `POST {base_url}/v1/chat/completions`（若 base_url 以 `/v1` 结尾则直接拼 `/chat/completions`）。
- 支持原生 tool calling；网关拒绝 `tools` 字段时自动降级一次为 JSON 动作协议（不会重复受理结果）。
- 用量读取 `usage.prompt_tokens/completion_tokens`；价格按每 1K token 配置；缺少价格时费用为 unknown，不能当 0。
- 401/403 停止并标记需要审查；429/网络错误有上限退避。

## 6. 数据最小化

提示词包含：题目合同、模板、历史结果、best 源码、预算、能力。**不包含**：API key、Cookie、密码、环境变量、数据库路径、会话文件。
