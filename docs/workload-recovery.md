# 逆向评测数据（输入规格恢复）

## 目标与边界

从题面、模板、已有日志和允许的官方诊断反馈恢复**输入规格**：

- dtype、rank、shape、axis、属性、元素数
- 每个测试点的证据状态与证据引用

不恢复原始 tensor 数值，不生成大规模 tensor，不执行数据生成器，不做性能刷分。
诊断被平台明确拒绝时记录原因，不通过换编码绕过。

## 任务卡流程

1. 打开优化任务详情，点击 **逆向评测数据**。
2. 后端先按指纹查缓存：
   - `verified`：直接展示，不新建会话、不提交。
   - 已有运行中/暂停中的逆向任务：关联并展示进度。
   - 只有 `partial`：展示已有结果；再次点击或“重新验证/重新探测”启动新 revision。
3. 没有记录时创建独立逆向任务和 Codex thread。

## 指纹与缓存

`spec_key` 由以下字段计算：

```text
platform
scope（题目 URL 所属作用域）
problem_id
problem_version（平台未提供时为 null）
testset_key（测试点 ID 集合；未提供时为 null）
```

题目版本或测试集变化会产生新的 `spec_key`。旧记录保留在 `workload_specs`，不覆盖；同一 `spec_key` 的重新探测使用递增 revision。

## 独立会话

逆向任务使用隐藏的 recovery task：

```text
data/cli_workspaces/<recovery_task_id>/workspace
Codex thread: <题目>逆向评测数据
```

它与优化任务的 workspace、attempt、best 完全分开。诊断提交通过同一个 Scheduler，因此共享官方账号的冷却、每日额度、在途限制和 unknown 对账；提交 attempts 标记 `diagnostic=1`，不会更新优化任务的 best。

## Skill

启动时注入：

```text
skills/recover-operator-workload/SKILL.md
skills/start-operator-optimization/TOOLS.md
```

Codex 只使用：

```text
read_problem(url)
submit_official(...)
read_official_evaluation(submission_id)
```

## 结构化导入

Codex 在 workspace 写出：

```text
specifications.json
evidence.md
```

示意：

```json
{
  "schema": "ascendforge-workload-spec/1",
  "status": "verified",
  "testcases": [
    {
      "testcase_id": "case-1",
      "inputs": [{"name": "x", "dtype": "float16", "shape": [2, 3], "numel": 6}],
      "outputs": [{"name": "y", "dtype": "float16", "shape": [2, 3]}],
      "attributes": {"axis": 1},
      "derived": {},
      "evidence_status": "measured",
      "evidence_refs": [{"submission_id": "..."}]
    }
  ]
}
```

宿主会校验：

- testcase_id 与当前题目/版本
- shape 只能是非负整数；numel 与 shape 乘积一致
- evidence_refs 必须对应本逆向任务真实 submission/attempt/snapshot
- `measured` 没有有效证据会降级为 `inferred`
- 声明完整但缺少已知测试点、或存在未知项时，状态降为 `partial`
- 工作区出现超过 1 MiB 的 tensor 文件时拒绝导入

`JSON 格式正确` 不等于 `规格已验证`。状态只由证据计算：

```text
verified  全部测试点 measured、证据有效、覆盖已知测试集
partial   有部分输入规格或存在 inferred/unknown
unknown   没有可用输入规格
```

## 状态

```text
pending / running / paused / external_blocked / partial / completed / failed
```

Codex 正常退出不等于完成：没有导入 `specifications.json` 时记为 `failed`；导入只有部分证据时记为 `partial`。

## 暂停与继续

- 暂停：阻止新的诊断提交，保留隐藏任务、workspace 和 Codex thread。
- 继续：恢复原 thread/workspace；未决提交先查询原 ID，不重复发送。
- 恢复失败不自动新建空会话。
- 同一 `spec_key` 通过数据库部分唯一索引保证最多一个活跃逆向任务。
