# AscendForge · 昇腾工坊

本地 CLI 驱动的昇腾算子自动优化平台。首个远端评测平台为 **CANNJudge**。

服务器托管 CLI 会话、调度、提交、实验结果和持久化状态；本地主机只需要浏览器。关闭网页、电脑关机或 Web 服务重启都不会丢失任务。

> **当前验证状态（2026-10-22）**
>
> - CANNJudge 的公开题目解析、模板读取、ZIP 工程下载已在本机**真实联网验证**。
> - **真实登录、题目导入、真实提交、远端 ID、结果查询与独立读回已完成**。模板基线曾返回 `Runtime Error`（15/15），**不代表算子通过**。
> - 历史测试任务和证据已清空；当前只保留 admin 账号与 CANNJudge 连接。
> - 本地 Codex CLI 已跑通“工作区修改 → 快照 → 官方提交 → 会话恢复”。
> - 优化循环保护：编译失败的候选不会推进实验 working revision；已知的 `AscendC::And/Or` 标量掩码调用会在 snapshot 前被 preflight 拒绝；实验基线固定为当时 global best。
> - 提交字节与快照逐字节一致；`kernel.asc` 与远端回显一致。
> - 服务器环境（HiDevLab 950 系列）仍在审批中；本机无 NPU，因此 Ascend C 候选本地仍标为 `unavailable/untested`，不会伪造设备结果。
> - 97 个自动化测试通过（mock/HTTP 层）；mock 通过不等于真实 CANNJudge 接入成功。

## 为什么是这个形态

- **单用户、单服务器**：Python 标准库 + SQLite + 独立 worker，部署简单，故障面小。
- **浏览器只是控制面**：开始/暂停/恢复/停止只是写入意图；代码修改、提交和轮询都在服务器侧的本地 CLI 与 worker 完成。
- **凭据隔离**：Codex 使用自己的认证；CANNJudge Cookie 只存在于 AscendForge 的加密凭据存储中。
- **保守的提交语义**：先落盘不可变快照，再提交完全相同的字节；超时/响应丢失进入 `submission_unknown`，先对账，绝不自动重发。
- **诚实的设备状态**：无 NPU 时只运行数据生成器和 CPU 参考实现，候选状态为 `untested`。

## 快速开始

### 1. 环境

- Python **3.11+**
- 唯一运行时依赖：`cryptography`（凭据加密）
- 推荐：Linux 服务器 + 反向代理 HTTPS；Codex CLI 直接使用任务 workspace

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

### 2. 初始化

```bash
export ASCENDFORGE_DATA_DIR=/var/lib/ascendforge
python -m ascendforge init            # 交互创建管理员，并生成数据目录/主密钥
```

也可以跳过 `init`，直接启动 Web 服务，在浏览器首次初始化页面输入控制台打印的一次性 setup token。

### 3. 启动（两个进程）

```bash
# 终端/服务 1：Web/API
python -m ascendforge serve --host 0.0.0.0 --port 8080

# 终端/服务 2：后台优化 worker
python -m ascendforge worker
```

生产环境请用 systemd 或容器编排托管两个进程，**不要让 worker 依赖 SSH 会话**。参考 [deploy/](deploy/)。

### 4. 在浏览器中

1. 打开 `http://<server>:8080`，登录 AscendForge。
2. **连接与设置 → CANNJudge**：输入 CANNJudge 账号和密码，并填写一道用于验证权限的题目链接。官方题面、提交和结果查询仍必须经过该账号。
3. **连接与设置 → CLI 运行器**：当前只启用本机 Codex CLI；它使用自己的认证，不需要 AscendForge 模型 API Key。
4. **新建任务**：粘贴完整 CANNJudge 题目链接；当前任务固定使用本地 CLI（Codex），DeepSeek Harness / Claude Code 为后续预留。
5. **开始**：本地 CLI 执行代码修改，宿主完成快照、排队提交和结果轮询。
6. 任务详情可看源码、远端结果、diff、调度额度、预算；可下载 best 源码、指定版本和完整任务档案。

Windows 开发（本仓库当前开发机）：

```powershell
$env:PYTHONPATH = "D:\AscendForge"
$env:ASCENDFORGE_DATA_DIR = "D:\AscendForge\data"
D:\python\python.exe -m ascendforge serve
D:\python\python.exe -m ascendforge worker
```

## 常用命令

```bash
python -m ascendforge status                    # 任务/worker/能力概览
python -m ascendforge serve                     # Web/API
python -m ascendforge worker                    # 后台优化
python -m ascendforge user list
python -m ascendforge probe --url https://cannjudge.cn/home
```

任务档案下载：任务详情 → “导出档案”。档案包含源码快照、父子关系、生成器、种子、远端结果、原始反馈和 SHA-256；默认排除凭据、会话、日志。

## 目录结构

```text
ascendforge/
  web.py                 HTTP/API/静态 UI（标准库，无前端构建）
  webui/                 index.html / style.css / app.js
  worker.py              独立后台循环
  scheduler.py           持久化队列、限频、预算、对账
  tasks.py               任务状态机与配置校验
  experiments.py         快照、结果归一化、best、档案导出
  security.py            scrypt 密码、Fernet 凭据、会话、CSRF
  platform/
    base.py              平台适配器契约
    cannjudge.py         CANNJudge 实现
    service.py           连接/会话/题目导入
  agent/
    change_audit.py      有效改动审计
    preflight.py         提交前源码检查
  execution/
    capabilities.py      NPU/CANN 诚实检测
  mcp/                   任务级 MCP：题目读取 + 两类提交工具 + 固定测试服务器评测
  cli.py, db.py, states.py
skills/                  CLI 启动 / 逆向评测 skill
tests/                   97 个 unittest
deploy/                  systemd / Docker / Caddy / Nginx
docs/                    架构、能力矩阵、阶段 0 报告、运维、安全、验收

CLI 启动时注入：

- `skills/start-operator-optimization/SKILL.md`
- `skills/start-operator-optimization/TOOLS.md`
- `skills/recover-operator-workload/SKILL.md`（仅逆向评测任务）
- 题目 URL、workspace、模板/空项目说明、可编辑与只读文件、评分目标

实验记录仍保留在 `experiments` 表供任务详情使用。
- 旧数据：已有 attempt 自动归入一个 `legacy` experiment，不伪造历史猜想；下一次新建 attempt 会开启新实验
- 覆盖范围：`all_passed` 要求声明的预期测试点全部完成且通过；只有部分返回时记为 `testset_passed`/`current_testset_only`，不冒充完整通过
- 可比性：性能结果带 `comparison_key`（题目版本、测试点集合、CANN/SoC、时间单位和环境指纹）；不同 key 的耗时不会混入同一个 performance best
- 兼容层：`evaluations` 表、revision 的 config/edit_mode 字段已预留；真实 patch、参数评测和去重仍在下一阶段
- 本地 Codex：`codex-run` 做只读检查；`codex-loop` 通过 Codex app-server 为每个任务建立一个持久 thread，发送真实 `SKILL.md` + `TOOLS.md`，并只暴露 `read_problem`、`submit_official`、`read_official_evaluation` 三个平台工具；Codex 自己读题、改 workspace、提交和轮询结果，不设固定轮数，直到人工暂停；Web 的“继续任务”恢复同一会话
- MCP：任务级 `python -m ascendforge.mcp.server --task-id ... --workspace ...` 提供 `read_problem`、`read_server_evaluation`、`read_official_evaluation`、`submit_server_test`、`submit_official` 五个工具；快照哈希、请求 ID 幂等、官方限频和服务器/官方结果分离见 [MCP 工具](docs/mcp-tools.md)
- 项目模板：新建任务可上传 ZIP；上传后 Codex 从该工程开始，不上传则是空项目、从 0 开始；ZIP 会做路径穿越、文件数、大小和 UTF-8 文本校验，细节见 [项目模板上传](docs/project-template.md)
- 执行模式：只保留本地 CLI（Codex）；创建后自动建会话并注入真实 skill 与工具合同，会话名按“题目名+算子优化”生成（如 `AddN算子优化`）。配置保留 `cli_backend` 字段，DeepSeek Harness / Claude Code 以后接入
- 任务管理：停止/暂停的任务可在任务卡片点“删除”，级联删除 attempts、快照、提交、事件、workspace 和日志；running 或存在未知/发送中提交时拒绝删除，审计日志保留
- 逆向评测数据：任务详情按钮按题目/版本/测试集指纹复用规格；无缓存时启动独立 Codex 逆向 thread/workspace，诊断提交共享 Scheduler 但不影响优化 best；只导入 `specifications.json`/`evidence.md`，不生成 tensor。详见 [逆向评测数据](docs/workload-recovery.md)
- Codex 执行边界：暂停采用软暂停，保留 Codex 进程和 app-server thread，只停止新 turn；继续时在同一会话上恢复。停止仍清理进程树。提交限频、额度、在途、幂等和 unknown 对账仍由宿主强制执行
- 评价目标：`best_attempt_id` 只表示正确性基线；`performance_best_attempt_id` 独立按耗时比较（默认 `max_time`，可通过 `performance_metric` 改为 `avg_time`），没有耗时数据时记为“性能未知”，不会回退成 precision
- 父代码：新 attempt 自动注入父快照完整源码；也可用 `read_attempt_source` 按需读取
- 父评测：同时注入父 attempt 的完整逐测试点结果和原始 `result_json`，不只给聚合值
- 远端反馈：上下文自动包含最近一次逐测试点 `precision_ratio`/耗时；历史工具也返回 `testcase_precision`，不只给聚合最小值

```

## 数据布局

```text
$ASCENDFORGE_DATA_DIR/
  ascendforge.db          SQLite（WAL）
  secrets/master.key      Fernet 主密钥（务必单独备份）
  secrets/setup.token     首次初始化令牌
  tasks/<task_id>/
    problem/package.zip   原始题目工程包
    problem/project/      解压后的模板
    snapshots/<id>/files/ 不可变源码快照（含 manifest.json）
  logs/ascendforge.log    轮转日志
  tmp/                    导出/下载临时文件
```

## 关键语义

| 场景 | 行为 |
|---|---|
| 网页关闭 / 断网 | worker 继续；重连后从服务端拉取完整状态 |
| Web 服务重启 | worker 不受影响；状态全在 SQLite/磁盘 |
| worker 崩溃 | 重启后 `submitting` 超租约的提交标记 `unknown`，先对账 |
| 提交超时/无 submissionId | `submission_unknown` + `needs_review`，冻结新提交，等待对账 |
| 暂停 | 禁止新的 CLI turn 和提交；已发出的提交继续收集结果 |
| 停止 | 不撤销远端已接受提交；取消尚未发送的排队提交 |
| 401/403 | 进入 `needs_login`/`needs_review`，不盲目重试 |
| 429 | 遵守 `Retry-After`，有上限退避 |
| 无 NPU | 数据/参考结果可跑；候选 `unavailable/untested` |
| 自定义远端测试 | 未核实，禁用 |
| best 更新 | 仅远端“通过且指标可比且不退化”的新版本；失败/编译错误不覆盖 |
| 重放请求 | `X-Request-Id` 幂等键 + 状态机；开始/暂停/恢复/停止幂等 |

## 安全边界

- 密码 scrypt（每用户盐）；会话令牌只在 DB 存 SHA-256。
- CANNJudge Cookie 用 Fernet 加密，密钥在数据库之外；浏览器只看到掩码。
- Codex 在任务 workspace 内修改代码；AscendForge 不再内置 Docker/Local executor。
- 导出档案默认排除凭据/会话/日志。
- 远程访问必须经 HTTPS 反向代理，并设置 `ASCENDFORGE_SECURE_COOKIES=true`。

详见 [docs/security.md](docs/security.md)。

## 测试

```bash
PYTHONPATH=. python -m unittest discover -s tests -t . -v
```

覆盖：快照不可变与篡改检测、提交限频/每日额度/在途上限、并发原子领取、重启后限频保持、未知提交冻结与对账、人工恢复、CSRF/幂等、凭据掩码、档案排除密钥、CANNJudge 提交载荷校验、任务状态机与 skills 加载、Codex 会话暂停/继续、CLI 运行器预留、提交 401/403/429/unknown 异常分支。

## 尚未验证 / 需要你完成

1. **真实可通过的算子实现**：阶段 1 已用模板基线打通“提交—结果”链路，但基线是模板，返回 `Runtime Error`；还需要真实实现并拿到 `Pass`。
2. **服务器**：HiDevLab 950 审批下来后，部署 `serve` + `worker`，确认 CANN/NPU 能力；无 NPU 的开发机不能替代。
3. **HTTPS/域名**：远程访问前配置反向代理与 `ASCENDFORGE_SECURE_COOKIES=true`。

## 文档

- [架构与状态机](docs/architecture.md)
- [部署与运维](docs/operations.md)
- [安全模型](docs/security.md)
- [项目模板](docs/project-template.md)
- [逆向评测数据](docs/workload-recovery.md)
- [MCP 工具](docs/mcp-tools.md)
