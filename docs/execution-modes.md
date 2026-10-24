# 执行模式

新建任务时必须选择一种执行模式，两种模式的字段和行为不同。

## LLM API

- 使用设置页保存的模型 API（OpenAI 兼容接口）
- 服务器 AgentRuntime 负责每轮提示词、工具调用和上下文
- 任务创建后不会自动启动，需要在任务详情点击“开始”
- 显示 API 预算、模型调用、Token、费用、运行时限等配置
- 使用题目导入的平台模板（如果有）

## 本地 Codex

- 使用本机已登录的 Codex CLI
- 每个任务一个 Codex app-server thread
- 创建任务后自动启动 Codex 会话
- 可选上传项目模板 ZIP：
  - 上传：Codex 从该工程开始
  - 不传：空项目，从 0 开始
- 可填写 Codex 初始指令
- 首条会话消息发送占位 skill：

```text
HELLO！ HELLO
```

- 不设置固定轮数；只有用户手动暂停/停止才结束
- 不显示 API 模型/Token/费用配置

## 自动启动链路

```text
POST /api/tasks
→ 创建任务
→ execution_mode=codex
→ 启动 codex-loop 子进程
→ 创建 Codex app-server thread
→ 发送占位 skill + 任务上下文
```

如果 Codex 启动失败，任务仍会创建，响应里带有 `codex_start_error`，
可以在任务详情手动点击“Codex 运行”重试。

## MCP 配置的含义

MCP 是 Codex 连接平台工具的一种协议。把 MCP server 配置挂到 Codex 会话，
意思是让 Codex 启动时执行类似：

```bash
python -m ascendforge.mcp.server \
  --task-id <task_id> \
  --workspace <workspace>
```

然后 Codex 通过 MCP 的 `tools/list` / `tools/call` 调用平台的
`read_problem`、`submit_server_test`、`submit_official` 等工具。

当前 Codex 会话使用的是宿主注入的 app-server 动态工具，不是外部 MCP
配置。两者工具语义相同，但“挂 MCP 配置”需要后续把该 server 注册到 Codex
的 MCP 配置中，并按任务传入 task id/workspace。
