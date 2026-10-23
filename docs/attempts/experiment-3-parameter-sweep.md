# 实验 #3：CopySign tile / depth 参数拆解

## 结论

**验证未完成。**

四个配置都完成了当次评测并 15/15 Pass，但实验的成功标准包含：

```text
repeat_if_within_pct = 5
```

C 与 D 的 max_time 只差约 0.32%，属于需要复测的范围。实验预算 `max_revisions=4` 已被四个配置用完，C/D 复测没有执行。

因此：

```text
status = incomplete
verified = false
stop_reason = budget_exhausted
retests_missing = ["C", "D"]
```

不能把这次结果写成“参数组合已经稳定验证”。

## 实验定义

```text
experiment: exp_1a167d46239d96dfc
baseline / working: attempt #37
fixed implementation: #37 的标量计算路径，不引入新 Ascend API
```

| 配置 | tile | depth | 角色 |
|---|---:|---:|---|
| A | 8192 | 1 | 原配置 |
| B | 32768 | 1 | 只改 tile |
| C | 8192 | 2 | 只改 depth |
| D | 32768 | 2 | #37 组合配置 |

## 真实结果

| 配置 | attempt | 结果 | max_time | avg_time | 相对 D max | 证据 |
|---|---:|---|---:|---:|---:|---|
| A | #39 | 15/15 Pass | 39174.56 | 7960.40 | +20.87% | remote `6adb1db41dac5d235bcf3f48` |
| B | #40 | 15/15 Pass | 39068.84 | 7934.94 | +20.55% | remote `6adb1ee11dac5d235bcff062` |
| C | #41 | 15/15 Pass | 32512.61 | 7125.44 | +0.32% | remote `6adb200c1dac5d235bd0a25c` |
| D | #42 | 15/15 Pass | 32408.59 | 7100.42 | 0.00% | reused #37 `6adacf033ff2fef7e8967629` |

D 与 baseline #37 相同，因此复用了 #37 的测量结果，没有重复提交。

## 观测

- depth 从 1 到 2，在 tile=8192 时 max_time 从 39174.56 降到 32512.61，约 **-17.0%**。
- 在 depth=2 时，把 tile 从 8192 提到 32768，max_time 只从 32512.61 降到 32408.59，约 **-0.32%**。
- 在当前实现和当前 15 点测试集上，**depth 的关联性远大于 tile**。
- 这只是耗时关联，不是 profiling；不能据此宣称 MTE2/vector/MTE3 已经实际重叠。
- 所有结果只覆盖当前评测集，不能外推到任意 shape。

## 复测缺口

按 `repeat_if_within_pct=5`：

```text
best max_time = 32408.59 (D)
C max_time    = 32512.61
difference    = 0.32% <= 5%
```

C 和 D 都需要复测；实验预算只够四个主配置，没有余量执行复测。

## 当前状态

```text
experiment #3: abandoned
conclusion: incomplete / verified=false
stop_reason: budget_exhausted
experiment_best: D (#42)
task: paused
```

`experiment_best=D` 只表示当前测量中的最好配置，不表示实验已完整验证。

## 下一步

应新建一个只做复测的实验，例如：

```text
baseline: #37
configs: C 和 D 各复测一次
success_criteria.repeat_if_within_pct: 5
budget.max_revisions: 2
```

复测通过后才可以把本次参数拆解写成“已验证”；否则保留“未完成”。
