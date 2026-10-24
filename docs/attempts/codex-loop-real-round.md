# 本地 Codex 真实闭环：第一轮

## 结果

任务：`task_1a16bdf5f935daf82`（CopySign）  
父版本：`att_1a16bf1e019f6c5ea`（performance best #5）  
Codex 候选：`att_1a16ce57489121185`（#8）  
快照：`snap_1a16ce94160eba302`  
远端提交：`sub_1a16ce941be46f642` / `6adc64ad1dac5d235b9a9287`  
远端结果：**15/15 Pass**

| 指标 | 父版本 #5 | Codex #8 | 变化 |
|---|---:|---:|---:|
| max_time | 75146.59 | 77046.21 | +2.53% |
| avg_time | 19261.32 | 16651.02 | **-13.55%** |

因为当前性能目标是 `max_time`，`performance_best` 仍为 #5；但平均值明显下降。  
这不是“方向无效”：max 退化、avg 改善，需要后续更细的测量或重新定义目标。

## Codex 做了什么

真实 diff 不是注释变化，而是把 float16 路径改成每 32 位字处理两个元素：

```text
copy_sign_bits<0x80008000u, 2>
```

并增加了最后一个奇数元素的标量尾处理。其他 dtype 路径保留。

关键代码位置：

```text
kernel.asc
  template<uint32_t SIGN, uint32_t LANES>
  __gm__ uint32_t* xp/sp/yp
  for (i = first / LANES; i < last / LANES; ++i)
  if (LANES == 2 && (last & 1)) { ... uint16 tail ... }
```

## 执行证据

- Codex session：`01a16ce5-8078-7a42-bc30-f66b9f210b7d`
- CLI run：`clirun_1a16ce9412f38edff`
- Codex usage：
  - input_tokens: 372057
  - cached_input_tokens: 350208
  - output_tokens: 7540
- 修改文件：`kernel.asc`
- 宿主保存了不可变 snapshot
- 官方提交由 worker 发出，Codex 没有直接提交

## 说明

Codex 的 last message 还声称“Host simulation passed 953 cases”。  
这句话不是 CANNJudge 证据，也不是 NPU profiling；本次可确认的事实只有：

```text
15/15 远端测试点通过
snapshot 与提交关联
max/avg 时间已记录
```

不能把 Codex 自述的 953 cases 或“halving loop iterations”当作实测结论。

## 状态

```text
task: paused
execution_mode: codex
codex_runner_active: false
codex_runner_status: finished
```

下一轮可以从 #8 继续修复/优化，也可以从 #5 重新选方向；必须明确选择并记录原因。
