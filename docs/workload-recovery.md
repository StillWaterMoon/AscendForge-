# 逆向评测数据（输入规格恢复）

## 目标与边界

从题面、Pass 基线、模板、已有日志和允许的官方诊断反馈恢复**输入规格**：

- dtype、rank、shape、axis、属性、元素数
- 每个测试点 Codex 确认的状态

不恢复原始 tensor 数值，不生成大规模 tensor，不执行数据生成器，不做性能刷分。
诊断被平台明确拒绝时记录原因，不通过换编码绕过。

逆向必须以优化任务真实产生的 **Pass 提交**为基座；没有 Pass 时不允许启动。

## 任务卡流程

1. 先让优化任务实际跑出一个 **Pass 提交**。没有 Pass 时“逆向评测数据”按钮禁用，后端也会拒绝启动。
2. 打开优化任务详情，点击 **逆向评测数据**。
3. 后端先按指纹查缓存：
   - `verified`：直接展示，不新建会话、不提交。
   - 已有运行中/暂停中的逆向任务：关联并展示进度。
   - 只有 `partial`：展示已有结果；再次点击或“重新验证/重新探测”启动新 revision。
4. 没有记录时创建独立逆向任务和 Codex thread，并把最新 Pass 提交的快照复制为隐藏任务的基座。

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

它与优化任务的 workspace、attempt、best 完全分开。创建时读取优化任务的 Pass 提交快照，在隐藏任务中生成一个只读基座 attempt，再从该快照初始化逆向 workspace；不是从空模板重新开始。

`specifications.json`、`.ascendforge/skills/`、`evidence.md` 等恢复产物不会作为源码提交。

诊断提交通过同一个 Scheduler，因此共享官方账号的冷却、每日额度、在途限制和 unknown 对账；提交 attempts 标记 `diagnostic=1`，不会更新优化任务的 best。

## Skill

启动时不整份注入 skill。完整技能目录会复制到：

```text
<workspace>/.ascendforge/skills/oj-feedback-reverse-engineering/
<workspace>/.ascendforge/TOOLS.md
```

启动消息只给路径，并要求 Codex 自己读取 `SKILL.md`、按需读取 `references/`；模板代码直接从 workspace 读取。

Codex 只使用：

```text
read_problem(url)
submit_official(...)
read_official_evaluation(submission_id)
```

工作区 `AGENTS.md` 明确允许通过 `submit_official` 发起诊断提交；它只禁止绕过宿主直接调用 CANNJudge。诊断提交仍受任务暂停状态和共享 Scheduler 限制。

恢复 session 的第一步必须是 `read_problem(url)`；宿主会在 `submit_official` 处拒绝“尚未读题”的诊断提交。诊断 attempt/submission 只存在于隐藏的 recovery task，不会出现在优化任务的尝试/提交列表里。

诊断不使用 `print/stdout`。先得到 Pass 基线，再用 `if/else` 条件制造可控 WA（例如某 dtype 直接不计算）；规模可用条件余数探针分别取得 `N mod m` 后用 CRT 合并，或使用受控错误比例/radix 编码。通道、分母和取整语义未校准前不解读比例。

## 结构化导入

Codex 只写一个最小 `specifications.json`，不要求 `evidence.md` 或长篇报告。示意：

```json
{
  "schema": "ascendforge-workload-spec/1",
  "confirmed": true,
  "testcases": [
    {
      "testcase_id": "case-1",
      "inputs": [{"name": "x", "dtype": "float16", "shape": [2, 3], "numel": 6}],
      "outputs": [{"name": "y", "dtype": "float16", "shape": [2, 3]}],
      "attributes": {"axis": 1},
      "derived": {}
    }
  ]
}
```

`confirmed: true` 表示 Codex 已确认这张表；宿主自动记录来源任务、诊断 submission/attempt/snapshot 和导入时间。

宿主会校验：

- testcase_id 与当前题目/版本
- shape 只能是非负整数；numel 与 shape 乘积一致
- 声明 confirmed 时字段必须完整；未知字段仍用 null
- 声明完整但缺少已知测试点、或存在未知项时，状态降为 `partial`
- 工作区出现超过 1 MiB 的 tensor 文件时拒绝导入

不需要 `evidence.md`，也不需要为每个字段手写 evidence_refs。

状态：

```text
verified  Codex confirmed 且字段/测试点覆盖完整
partial   有部分输入规格或存在 unknown/inferred
unknown   没有可用输入规格
```

`JSON 格式正确` 不等于字段完整；`confirmed` 只表示 Codex 的确认，不伪造官方测量值。

## 状态

```text
pending / running / paused / external_blocked / partial / completed / failed
```

Codex 正常退出不等于完成：没有导入 `specifications.json` 时记为 `failed`；导入只有部分证据时记为 `partial`。

## 暂停与继续

- 暂停：停止 Codex runner 进程树，释放 thread writer；保留隐藏任务、workspace 和 Codex thread 引用。
- 继续：恢复原 thread/workspace；未决提交先查询原 ID，不重复发送。
- 恢复失败不自动新建空会话。
- 同一 `spec_key` 通过数据库部分唯一索引保证最多一个活跃逆向任务。
