# Codex 三工具

版本：2.1；日期：2026-10-25。

本文件只说明 Codex 当前实际可用的三个工具。工具通过 AscendForge 的
Codex app-server dynamic tools 注册，schema 以实际注册为准。

## 范围

- Codex 使用自己的文件工具读取和修改 workspace。
- 宿主绑定任务、workspace、官方账号和提交白名单，Codex 不能切换。
- 不提供读取模板、服务器测试、代码编辑或结果分页工具。
- 只有下面三个工具。

## 1. `read_problem`

读取官方题目完整 Markdown 题面。

### 输入

```json
{
  "url": "https://cannjudge.cn/..."
}
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | string | 是 | 当前题目的官方 URL |

### 返回

完整题面 Markdown 字符串，不截断、不摘要。

### 失败

URL 无法解析、题目不存在或页面没有题面时，工具返回错误；不会把登录页
当作题面。

## 2. `submit_official`

冻结当前 workspace 的允许源码，创建 attempt 和不可变快照，通过宿主的
Scheduler 排队官方提交，立即返回 `submission_id`。

### 输入

```json
{
  "request_id": "可选幂等键",
  "hypothesis": "可选猜想",
  "parent_attempt_id": "可选父 attempt"
}
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `request_id` | string | 否 | 幂等键；同一值重试返回原 submission |
| `hypothesis` | string | 否 | 本轮猜想，写入 attempt |
| `parent_attempt_id` | string | 否 | 指定父 attempt；默认当前工作版本 |

### 行为

```text
读取 workspace 允许的源码
→ 校验源码变化和 preflight
→ 创建 attempt
→ 保存不可变快照
→ 排队官方提交
→ 返回 submission_id
```

只提交用户模板中标记为可编辑的文件。`main.asc`、`CMakeLists.txt`、
`run.sh` 等只读文件不进入官方载荷。

不会等待远端评测完成。

### 返回

```json
{
  "submission_id": "sub_xxx",
  "attempt_id": "att_xxx",
  "snapshot_id": "snap_xxx",
  "source_sha256": "abc...",
  "state": "queued",
  "remote_id": null,
  "reused": false,
  "submitted": true,
  "note": "accepted/queued only; poll read_official_evaluation"
}
```

`submitted=true` 只表示已接收/已排队，不代表编译或正确性通过。

当前版本不支持同源码复测；没有有效源码变化时提交会失败。

## 3. `read_official_evaluation`

按本平台 `submission_id` 查询官方评测。

### 输入

```json
{
  "submission_id": "sub_xxx"
}
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `submission_id` | string | 是 | `submit_official` 返回的本平台 ID，不是 `remote_id` |

### 返回

```json
{
  "submission_id": "sub_xxx",
  "attempt_id": "att_xxx",
  "snapshot_id": "snap_xxx",
  "source_sha256": "abc...",
  "state": "queued",
  "remote_id": null,
  "error_code": null,
  "error_message": null,
  "sent_at": null,
  "poll_count": 0,
  "normalized": {},
  "compile": null,
  "correctness": null,
  "performance": null,
  "score": null,
  "testcases": [],
  "official": true
}
```

`state`：

```text
queued
submitting
submitted
judging
finished
failed
unknown
```

`finished` 只表示流程结束，不等于 Pass。编译错误和 Wrong Answer 是有效
评测结果，不是工具故障。

`source_sha256` 对应该次提交冻结的快照。后续修改 workspace 不会改变已
返回的结果对应源码。

## 宿主边界

- Codex 不直接访问 CANNJudge 凭据。
- 提交必须经过宿主 Scheduler，保留提交限频、在途限制、幂等和 unknown
  对账。
- Codex 暂停/停止后不再发起新提交。
- 已提交的官方评测仍可用 `read_official_evaluation` 查询。

## 启动信息

首次会话会提供：

```text
任务 ID
workspace 绝对路径
题目 URL
上传模板位置，或“空项目”说明
允许提交的文件
只读文件
评分指标和目标
```

模板由用户上传，宿主解压进 workspace。Codex 用自身的文件工具读取和
修改，不通过平台工具读取源码。
