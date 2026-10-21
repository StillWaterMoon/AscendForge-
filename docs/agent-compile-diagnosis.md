# Agent 编译错误诊断（CopySign，2026-10-21）

任务：`task_1a15cec970375a9f2`（CopySign / `6ad45f04193e507d8e54f48d`）
状态：已暂停，避免继续消耗提交额度。

## 1. 实际提交记录

| attempt | remote id | 结果 | 关键错误 |
|---|---|---|---|
| 1（基线） | `6ad84d04193e507d8e4a0502` | Runtime Error | 模板 `run_kernel` 为空：`ProfileError: invocation launched no task` |
| 8 | `6ad87071193e507d8e5ac0d9` | Compile Error | `no member named 'Ands' in namespace 'AscendC'`；`CeilDiv` host/device 不匹配；`is_tensorTrait_v<unsigned char>` 失败 |
| 9 | `6ad8719b193e507d8e5b4566` | Compile Error | `reinterpret_cast<GlobalTensor<int8_t>>(x)` 非法；`CeilDivI` 是 `__aicore__` 却被 host `run_kernel` 调用 |
| 17 | `6ad872ff193e507d8e5be59d` | Compile Error | `CeilDivHost` 是 host 函数，却被 `__global__` kernel 调用 |
| 18 | `6ad8742c193e507d8e5c8ce6` | Wrong Answer | **已编译通过**，但 15/15 全部错误，precision_ratio 约 0.03–0.51 |

所以不是“所有提交都编译错误”：前三次是编译错误，最近一次已经编译通过，但正确性不通过。

## 2. 编译错误的具体原因

### 2.1 `AscendC::Ands` 不存在

```text
kernel.asc:78:18: error: no member named 'Ands' in namespace 'AscendC';
did you mean 'Acos'?
```

模型按通用 Ascend C 经验猜了一个按位与 API，但当前 CANN 版本没有 `AscendC::Ands`（编译器提示的 `Acos` 只是字符串相似，不是替代）。同批还出现：

```text
only support TensorTrait type Tensor!
type 'unsigned char' cannot be used prior to '::'
```

说明 int8 走的 TensorTrait/接口与模型假设不一致。**不能靠猜 API；必须使用当前 CANN 头文件实际提供的接口。**

### 2.2 把 `GM_ADDR` 强转成 `GlobalTensor`

```text
error: reinterpret_cast from '__gm__ uint8_t *' to 'AscendC::GlobalTensor<int8_t>' is not allowed
```

`GlobalTensor<T>` 是 Ascend C 的包装类型，不是指针，不能：

```cpp
reinterpret_cast<AscendC::GlobalTensor<int8_t>>(x)
```

正确做法：

```cpp
AscendC::GlobalTensor<int8_t> gx;
gx.SetGlobalBuffer((__gm__ int8_t*)x, n);
```

attempt 8 本来用对了 `SetGlobalBuffer`，attempt 9 全量重写时又退回了 `reinterpret_cast`——这是“整文件重写”导致的回归。

### 2.3 host/device 函数属性不匹配

先后出现两个方向：

```text
// attempt 9
error: no matching function for call to 'CeilDivI'
note: candidate function not viable: call to [aicore] function from [host] function

// attempt 17
error: no matching function for call to 'CeilDivHost'
note: candidate function not viable: call to [host] function from __global__ [aicore] function
```

- `run_kernel` 是 **host** 函数；
- `__global__ __vector__` kernel 是 **device/aicore** 函数；
- 被 kernel 调用的 helper 必须是 `__aicore__`，或者在 kernel 内直接写算术；
- 被 `run_kernel` 调用的 helper 必须是 host 函数。
- 模型把 helper 的 `__aicore__` 标反了，或者在同一文件里混用。

### 2.4 为什么每次只修一个错误

模型每轮都是**重写整个 `kernel.asc`**，不是最小修补；它看到一条编译错误后修这一条，同时可能改坏上一轮已经正确的部分。再加上本机没有 CANN/NPU，无法在提交前本地编译，只能靠远端编译器一次暴露一批错误。

## 3. 最近一次 Wrong Answer 的原因

attempt 18 已编译成功，但 15/15 全错。它不是编译问题，而是实现问题：

1. **只支持 int8**。题目合同明确允许 `int8/int16/int32/float16/float32`，样例恰好是 int8；候选把所有元素都当 1 字节 int8 处理，其他 dtype 全部错误。
2. **按元素数当字节数**。`n` 是元素数；int16/32/fp16/fp32 每元素 2/4 字节，候选只复制 `n` 字节。
3. **按 32B 向上取整后越界读写**。`nAligned` 可能大于真实分配长度，尾部 `DataCopy` 读到缓冲区外。
4. **int8 标量 UB 读写路径**。`GetValue/SetValue` 与 `DataCopy` 组合在当前编译/运行环境下可能不稳定；15 个用例的 precision 呈 0.03/0.12/0.25/0.5 分布，说明不是单一符号位错误，而是数据布局/切分/尾部处理问题。
5. **没有本地/CPU 参考验证**。本机没有 CANN，`run.sh cpu` 也跑不了，所以它没有在提交前发现这些问题。

## 4. 已确认的系统性问题

- 没有本地 CANN 编译环境时，Agent 的每次“编译修复”都要消耗一次远端提交额度。
- 当前模型倾向于猜 API，而不是只使用模板注释中列出的接口。
- 当前 Agent 反馈只给到编译错误/测试结果，没有“最小修改约束”和“编译前静态检查”。
- best 没有更新（没有 Pass），任务已暂停；不要再盲目重试。

## 5. 建议的修复顺序

1. **先暂停自动提交**（已做）。
2. 给 Agent 增加“编译前静态闸门”：
   - 禁止 `reinterpret_cast<...GlobalTensor...>`；
   - 禁止使用未验证的 `AscendC::Ands` 等 API；
   - 检查 `__global__`/`run_kernel` 调用的 helper 属性；
   - 检查 dtype 是否覆盖题目合同。
3. 给 Agent 增加“只修当前错误、最小 diff”的提示，禁止无必要整文件重写。
4. 更好的是在 HiDevLab 950/CANN 环境先跑 `run.sh cpu/npu` 编译，再把通过编译的文件提交，避免远程编译试错。
5. 正确性优先：先写一个 dtype 全覆盖、边界安全、哪怕性能一般的实现拿到 Pass，再优化性能。
6. 恢复任务前把 `max_stall_rounds`、`max_attempts` 控制在用户可接受的预算内。

## 6. 当前状态

- 任务：`paused`，没有新的在途提交。
- CANNJudge 账号：仍连接。
- 最近真实提交：`6ad8742c193e507d8e5c8ce6`（Wrong Answer，已保存原始结果）。
- 没有 best。
