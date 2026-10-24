# CLI Agent 迁移：实施前能力核查

日期：2026-10-24  
范围：只核查本机/服务器当前安装情况与现有代码入口；未启动真实 CLI 会话，未产生模型调用或官方提交。

## 1. 结论

```text
Codex CLI：已安装、已登录，可作为首个候选后端
DeepSeek CLI：未安装，不能实现或验证第二个后端
旧 API 优化循环：仍在使用
CLI Supervisor / 会话运行记录：尚未实现
平台工具 MCP/HTTP 桥：尚未实现
累计预算：仍然存在，尚未按 CLI 模式解除
```

因此当前只能判定为：

```text
Codex 后端：capability available，但未做端到端 CLI 接管
DeepSeek 后端：blocked（缺少可执行文件）
```

## 2. Codex CLI

- 可执行文件：`C:\Users\JF\AppData\Roaming\npm\codex.cmd`
- 版本：`codex-cli 0.163.0`
- 登录：`Logged in using ChatGPT`
- 非交互运行：`codex exec`
- JSON 事件：`--json`
- 会话恢复：`codex exec resume <session_id> [prompt]`
- 工作目录：`-C <dir>`
- 沙箱：`--sandbox read-only|workspace-write|danger-full-access`
- 最后消息文件：`--output-last-message`
- MCP：`codex mcp` 命令存在；已有内部 MCP 条目，但**没有 AscendForge 工具桥**

`codex doctor` 报告：

- 登录有效
- 存在可选 MCP 配置问题
- Windows Defender / Dev Drive 警告
- 3 个旧 rollout 状态问题

这些不影响“CLI 能启动”，但会影响长期会话恢复和稳定性，部署时需要处理。

## 3. DeepSeek CLI

未在 PATH 找到：

```text
deepseek
deepseek-cli
deepseek.cmd
deepseek.exe
dscli
```

全局 npm 只有：

```text
@openai/codex@0.163.0
@anthropic-ai/claude-code@2.1.96
```

因此不能把 Codex 的参数模板套到 DeepSeek CLI 上，也不能声称第二个后端已接入。

需要用户提供/安装真实 DeepSeek CLI，并确认：

- 可执行文件与版本
- 非交互模式
- 会话恢复
- JSON/结构化事件
- MCP 或命令桥能力
- 服务器服务用户的登录状态

本机已登录不等于服务器服务用户已登录；不得复制或展示凭据。

## 4. 现有旧循环入口

| 位置 | 现状 |
|---|---|
| `agent/runtime.py` | 逐 attempt 组装 prompt，直接 `provider.complete()` |
| `agent/runtime.py:152` | `agent_max_steps` 硬上限 |
| `scheduler.py:187-204` | attempt/model/token/runtime/cost 累计预算 |
| `scheduler.py:841-854` | 达到预算后自动结束任务 |
| `tasks.py:52-72` | 默认预算字段 |
| `webui/app.js` | 预算填写与“预算耗尽”文案 |
| `parameter_runner.py` | 实验修订/提交/复测预算 |

CLI 模式不能只改 UI；必须同时绕过这些后端判断，并保留单次超时、限频、幂等和 unknown 对账。

## 5. 需要新增的基础设施

1. **CLI Adapter**
   - `probe_capabilities`
   - `start`
   - `resume`
   - `observe`
   - `interrupt`
   - `terminate`

2. **Supervisor**
   - 任务租约 / epoch
   - 同一任务单执行者
   - 运行记录：provider、CLI 版本、session_ref、run_id、工作目录、进程标识、退出原因
   - 暂停/恢复/停止时拒绝旧会话迟到写入

3. **平台工具桥**
   - 题目、源码、历史、候选、diff、评测、提交、实验结果
   - 优先 MCP；若 CLI 不支持 MCP，使用其实际支持的 HTTP/命令桥
   - 工具身份必须是任务级、可校验，不能只靠提示词

4. **CLI 模式开关**
   - `execution_mode = api | cli`
   - CLI 模式跳过累计预算停止
   - 旧 API 模式隔离保留，用于回滚
   - 同一任务不能同时运行两个执行循环

## 6. Local Codex adapter smoke

`ascendforge codex-run --task <task_id>` now:

1. reads the task's performance-best (or correctness/latest) snapshot;
2. prepares a workspace with `TASK.json`, `AGENTS.md`, source files and a short recent-attempt list;
3. runs `codex exec --json` with `read-only` sandbox by default;
4. parses `thread_id`, `agent_message`, `turn.completed` usage;
5. reports changed files, but **does not save a candidate and does not submit**.

Actual local smoke against `task_1a16bdf5f935daf82`:

```text
exit_code = 0
session_ref = present
change_count = 0
usage.input_tokens = 69289
usage.output_tokens = 715
last_message = float32 tiled vector experiment proposal
```

It modified nothing and correctly remained read-only. This proves local Codex workspace execution and event parsing, not platform tool takeover.

## 7. 已完成的最小代码

新增：

```text
ascendforge/cli_agent/probe.py
ascendforge/cli_agent/__init__.py
tests/test_cli_agent_probe.py
```

提供：

```bash
python -m ascendforge cli-probe
```

该命令只做：

- 查找可执行文件
- 读取版本
- 检查登录状态
- 解析 `exec` / `resume` / JSON / sandbox 能力

不会：

- 启动 Codex 会话
- 调用模型
- 读取或输出 auth token
- 提交任何任务

## 8. 下一步顺序

1. Codex Adapter 已具备本地 workspace 启动、事件解析和只读 smoke；下一步持久化 `cli_runs` / `session_ref`。
2. 增加 host 侧“收集变更 → 保存不可变快照 → 排队评测”的闭环，而不是让 Codex 直接提交。
3. 建立平台工具桥，先用 mock 工具验证读任务、保存候选、查结果。
4. 用 Codex 做一次受控的写入候选验证（read-only 以外的 sandbox）。
5. 实现 CLI 模式的预算绕过，保留单次执行保护。
6. 接入 Web 后端选择与运行记录。
7. DeepSeek Harness 暂不实现，等确认可编程入口。
