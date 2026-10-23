# 通用优化闭环：现状、入口与缺口

日期：2026-10-23  
范围：不针对 CopySign 的性能结论，检查“任意算子题从启动到下一轮实验”的通用流程。

## 1. 阶段与责任

| 阶段 | Agent 触发/决定 | 宿主保证 | 当前实现 |
|---|---|---|---|
| 读取任务 | 读取题目、模板、历史、父代码 | 提供真实题目合同、真实源码快照和历史结果 | `read_problem`、`read_history`、`read_attempt_source`；上下文注入 problem/template/experiment |
| 建立基线 | 产出第一份可编译、正确实现 | 完整测试覆盖门槛、不可变快照、错误与结果绑定 | `create_candidate` + scheduler；`note_result` 在第一次 `all_passed` 时固定 experiment baseline/working |
| 提出实验 | 根据证据提出 hypothesis 和验证计划 | 固定 baseline，记录 search space、success criteria、budget | UI/API `POST /experiment`；Agent `start_experiment`；`experiments` 表 |
| 实现猜想 | 基于父版本修改；编译/正确性失败时修复 | 真实 diff、父快照、实际参数、失败诊断；不让注释-only 候选通过 | `change_audit` 拒绝 `NO_EFFECTIVE_CHANGE`；`working_revision` 只在可编译结果后推进 |
| 验证与探索 | 读取真实结果，决定修复、调参或换方向 | 预期测试全覆盖检查；耗时可比性 key；调度器/参数 runner | `normalize_result` 的 `all_passed`/testset_passed；`comparison_key`；`ParameterRunner` 可选 |
| 形成结论 | 判断哪些证据支持猜想 | 不把 hypothesis 当事实；预算/复测未完成标为 incomplete | `finish_experiment` 工具；任务终止时自动关闭 active experiment；conclusion 记录 verified=false |
| 下一轮 | 选择新方向，指定新 baseline | 关闭旧实验；新实验固定 baseline，不继承无关改动 | `start_experiment` 工具；新实验默认 performance best 或 correctness baseline |

## 2. 三个关键边界

### 参数扫描不是通用流程

`ParameterRunner` 是可选执行器，只有 `search_space.configs` 存在时才使用。  
算法替换、向量化、融合、布局变化不要求 tile/depth 表；Agent 可以直接从固定基线创建修订并提交。

### Agent 决定方向，宿主决定事实

宿主没有写死“下一步调 tile”。宿主负责：

- 真实 diff；
- 父版本和 working revision；
- 测试覆盖；
- 性能结果可比性；
- 预算和终止；
- 结论不能超过证据。

Agent 负责提出 hypothesis、选择实验类型和决定是否继续。

### 历史服务下一次决策

上下文提供：

```text
当前 best / correctness baseline / performance best
当前实验 hypothesis、baseline、working、success criteria
父源码和父结果
最近的 observed_change（不是只看 hypothesis）
最近逐测试点反馈
实验预算和 stop_reason
```

完整旧源码通过 `read_attempt_source` 按需读取，不在每次请求中重复全部历史代码。

## 3. 当前已验证的通用场景

以下均为 mock/HTTP 层验证，不代表真实 NPU 效果：

| 场景 | 测试 |
|---|---|
| 第一次正确候选自动成为实验基线 | `test_first_verified_candidate_establishes_baseline` |
| 编译失败后修复，再建立基线 | `test_compile_failure_repair_then_baseline_building_on_another_task` |
| 新实验固定 baseline 并记录计划 | `test_start_experiment_records_fixed_baseline_and_plan` |
| 纯注释变更被拒绝 | `test_change_audit` |
| 慢 Pass 不覆盖 experiment/performance best | `test_slower_passing_candidate_does_not_replace_experiment_best` |
| 预算耗尽关闭实验为 incomplete | `test_budget_completion_closes_experiment_incomplete` |
| 参数枚举、复用、修复、复测、预算 | `test_parameter_runner` |
| 预期测试数不足不冒充 all_passed | `test_normalization` |
| 不同测试集/环境不混排性能 | `test_comparison_key_changes_with_testset_and_environment` |

## 4. 真实运行记录

CopySign 参数实验 #3：

```text
status: abandoned
verified: false
stop_reason: budget_exhausted
retests_missing: C, D
```

四个配置都完成了真实远端评测，但 C/D 的 0.32% 差异触发 5% 复测规则，预算已耗尽，因此结论是“验证未完成”，不是“方向无效”。

## 5. 剩余缺口

1. **决策仍以 Agent + strategy 为主**
   宿主提供阶段提示和约束，但没有一个确定性的“下一步该修复/调参/换方向”决策器。

2. **没有本地编译器/NPU**
   编译错误仍是远端反馈；preflight 只能拦截已知 API 误用。

3. **不同题目的真实验证不足**
   当前真实证据只有 CopySign；其他题目只有 mock 流程验证。

4. **实际模型请求没有逐次归档**
   system/tools 仍可能事后重建。

5. **review interval 与多轮方向切换**
   还没有达到约 10 次候选自动触发复盘的完整策略。

6. **参数 runner 不是通用实验执行器**
   它只覆盖参数化文本绑定；算法型实验仍由 Agent 创建候选。

## 6. 当前状态

```text
总测试：99 passed
CopySign task：paused
experiment #3：abandoned / incomplete
correctness baseline：#24
performance best：#37
```

下一步优先验证“不同题目 + 非参数实验”的完整闭环，而不是继续优化 CopySign。
