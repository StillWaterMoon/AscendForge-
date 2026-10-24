# AscendForge MCP 工具

本目录实现 Codex/其他 MCP 客户端使用的任务级工具。设计目标是：

- 只提供**读取**和**提交**，不提供代码编辑工具；Codex 自己在草稿目录里编辑；
- 每次提交都保存不可变源码快照，结果通过唯一 ID 查询；
- 提交成功只表示已接收，不表示编译/正确性通过；
- 服务器测试与官方 OJ 测试严格区分；
- 不把 CANNJudge 凭据、数据库或任意文件系统路径暴露给模型。

代码位置：

```text
ascendforge/mcp/contracts.py    工具定义、参数校验
ascendforge/mcp/functions.py    六个 tool function 的实现
ascendforge/mcp/server_eval.py  固定测试集 NPU 服务器评测后端
ascendforge/mcp/server.py       stdio MCP server
tests/test_mcp.py               工具和协议测试
```

## 启动 MCP server

每个任务使用一个 server 进程，并绑定该任务的草稿目录：

```bash
python -m ascendforge.mcp.server \
  --task-id task_xxx \
  --workspace /path/to/task/workspace
```

也可用环境变量覆盖数据目录：

```bash
ASCENDFORGE_DATA_DIR=/data/ascendforge \
python -m ascendforge.mcp.server --task-id task_xxx
```

服务通过 stdin/stdout 使用 JSON-RPC 2.0；日志只写 stderr。

Codex 配置示例（每个任务使用自己的 task id / workspace）：

```toml
[mcp_servers.ascendforge]
command = "D:\\python\\python.exe"
args = [
  "-m", "ascendforge.mcp.server",
  "--task-id", "task_xxx",
  "--workspace", "D:\\AscendForge\\data\\cli_workspaces\\task_xxx\\workspace",
]
env = { PYTHONPATH = "D:\\AscendForge", ASCENDFORGE_DATA_DIR = "D:\\AscendForge\\data" }
```

## 六个工具

| 类别 | 工具 | 作用 | 返回唯一 ID |
|---|---|---|---|
| 读取 | `read_problem` | 题面、接口/精度、文件目录、可修改范围 | — |
| 读取 | `read_problem_file` | 读取 `kernel.asc` 及按需文件 | — |
| 读取 | `read_server_evaluation` | 查询服务器固定测试结果 | 用 `evaluation_id` |
| 读取 | `read_official_evaluation` | 查询官方 OJ 提交结果 | 用 `submission_id` |
| 提交 | `submit_server_test` | 提交服务器固定测试 | 返回 `evaluation_id` |
| 提交 | `submit_official` | 提交 CANNJudge | 返回 `submission_id` |

### 1. `read_problem`

输入题目 URL：

```json
{
  "url": "https://cannjudge.cn/public/vector/addn"
}
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `url` | string | 是 | CANNJudge 题目页面 URL |

CANNJudge 页面是 SPA，正文由公开 API 返回。函数按 URL 定位题目并读取
完整 `desc`，不截断。

**返回：只返回完整题面 Markdown 字符串，不再返回题目元数据、测试点
列表、文件范围或 JSON 包装。**

MCP 的 `content[0].text` 就是题面原文，例如：

```text
# AddN算子

## 一、赛题背景

AddN 是多路输入逐元素累加算子...
```

### 2. `read_problem_file`

参数：

```json
{
  "path": "kernel.asc",
  "source": "workspace",
  "attempt_id": null
}
```

- `path` 必须是题目模板或当前草稿中的相对路径，禁止 `../` 和绝对路径；
- `source`：
  - `workspace`：当前草稿；
  - `snapshot`：指定/最近一次候选快照；
  - `template`：原始题目模板；
  - 省略时按文件逐个回退：workspace → 最近快照 → template；
- 单文件最多 512 KiB；
- **返回文件原文，不再包 JSON、hash、editable 等元数据**；
- 文件不存在或路径非法时，MCP 返回 `isError=true`。

典型读取：

```json
{"path": "main.asc"}
{"path": "reference.py"}
{"path": "CMakeLists.txt"}
{"path": "run.sh"}
{"path": "kernel.asc", "source": "snapshot", "attempt_id": "att_xxx"}
```

MCP 返回：

```text
// readonly main
```

### 3. `read_server_evaluation`

参数：

```json
{"evaluation_id": "eval_...", "include_logs": true}
```

返回：

```json
{
  "evaluation_id": "eval_...",
  "source_sha256": "...",
  "snapshot_id": "snap_...",
  "status": "queued|running|completed|failed|not_configured",
  "coverage": "fixed task test suite: run.sh + generated/verified test data",
  "compile": {"ok": true, "exit_code": 0, "note": "..."},
  "correctness": {"ok": true, "note": "..."},
  "timing": {"duration_ms": 1234, "method": "process wall clock; ..."},
  "logs": {"stdout_tail": "...", "stderr_tail": "..."},
  "error": null,
  "official": false
}
```

服务器测试使用题目自带的 `run.sh` 固定测试入口：编译、生成固定测试数据、运行 kernel、用 `sample.py verify` 检查。它**不是**官方测试结果，覆盖范围以 `coverage` 为准。

### 4. `read_official_evaluation`

参数：

```json
{"submission_id": "sub_...", "include_raw": false}
```

返回：

```json
{
  "submission_id": "sub_...",
  "attempt_id": "att_...",
  "snapshot_id": "snap_...",
  "source_sha256": "...",
  "state": "queued|submitting|submitted|judging|finished|failed|unknown",
  "remote_id": "...",
  "error_code": "COMPILE_ERROR",
  "error_message": "...",
  "testcases": [],
  "score": null,
  "normalized": {},
  "official": true
}
```

`source_sha256` 来自提交时绑定的快照；用它可以确认结果属于哪份代码。

### 5. `submit_server_test`

参数：

```json
{"request_id": "optional-client-request-id"}
```

返回（立即返回，不等待评测完成）：

```json
{
  "evaluation_id": "eval_...",
  "status": "queued",
  "source_sha256": "...",
  "snapshot_id": "snap_...",
  "coverage": "...",
  "submitted": true,
  "note": "accepted only; query read_server_evaluation for the result"
}
```

- 读取当前草稿源码，保存 `kind=server-test` 的不可变快照；
- 同一个 `request_id` 重试返回同一个 `evaluation_id`；
- 默认测试集是固定的，暂不开放自定义测试数据参数；
- 不消耗官方 OJ 提交额度。

服务器评测后端配置（在部署端提供）：

```text
ASCENDFORGE_SERVER_EVAL_ENABLED=1
ASCENDFORGE_SERVER_EVAL_COMMAND=bash run.sh npu
ASCENDFORGE_SERVER_EVAL_TIMEOUT=900
```

只在配置了 CANN/NPU、测试数据、可信参考实现和调用入口的机器上启用。命令在提交快照的临时副本中执行；未启用时返回 `not_configured`，不会伪造设备结果。

### 6. `submit_official`

参数：

```json
{
  "request_id": "optional-client-request-id",
  "parent_attempt_id": "optional-attempt-id"
}
```

返回：

```json
{
  "submission_id": "sub_...",
  "attempt_id": "att_...",
  "snapshot_id": "snap_...",
  "source_sha256": "...",
  "state": "queued",
  "remote_id": null,
  "reused": false,
  "submitted": true,
  "note": "accepted/queued only; query read_official_evaluation for the result"
}
```

- 从当前草稿创建 attempt + 不可变候选快照；
- 通过现有 `Scheduler.request_submission` 排队，**不直接访问 CANNJudge**；
- 提交频率、每日/每任务额度、在途上限、unknown 对账仍由宿主执行；
- 同一 `request_id` 重试不会重复创建 submission；
- 返回 `queued` 只表示已接收，必须用 `read_official_evaluation` 查询。

## 快照与源码哈希

`submit_server_test` 和 `submit_official` 都返回：

```text
snapshot_id
source_sha256
```

CLI 可以继续修改草稿；后续查询仍按 ID 对应到当时的快照，不会因为草稿变化而混淆结果。

## MCP 协议示例

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_problem","arguments":{"url":"https://cannjudge.cn/public/vector/addn"}}}
```

`tools/call` 成功返回：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{...JSON result...}"}],
    "isError": false
  }
}
```

工具错误也以 MCP 结果返回，`isError=true`，便于 CLI 读取错误并继续修复。

## Python function-call 层

不经过 MCP 也可以直接调用同一实现：

```python
from ascendforge.config import Settings
from ascendforge.db import Database
from ascendforge.mcp.functions import MCPToolFunctions

settings = Settings.from_env()
db = Database(settings.db_path)
db.init()
tools = MCPToolFunctions(settings, db, "task_xxx", workspace="/path/to/draft")

problem = tools.call("read_problem", {"url": "https://cannjudge.cn/public/vector/addn"})
kernel = tools.call("read_problem_file", {"path": "kernel.asc"})
submitted = tools.call("submit_official", {"request_id": "round-42"})
result = tools.call("read_official_evaluation", {"submission_id": submitted["submission_id"]})
```

## 安全边界

- MCP server 绑定一个 task id，不跨任务读取；
- 文件读取受题目模板/草稿路径白名单限制；
- 不返回凭据、cookie、API key；
- 官方提交只走 scheduler，不直接调用 CANNJudge；
- 服务器测试命令由部署方配置，模型不能传入任意 shell 命令；
- 源码快照使用现有不可变 snapshot 存储，并校验 hash。

## 尚未完成

- Codex 启动时通过 MCP 配置自动挂载本 server；
- 服务器测试的 SSH/远程 worker 调度（当前是配置命令 + 本机/已配置环境执行）；
- 自定义测试数据参数（第二版）；
- Web 任务页选择 API/Codex 模式（下一步）。
