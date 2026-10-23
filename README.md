# AscendForge · 昇腾工坊

Agent 驱动的昇腾算子自动优化平台。首个远端评测平台为 **CANNJudge**。

服务器托管优化循环、模型调用、调度、实验存储和提交；本地主机只需要浏览器。关闭网页、电脑关机或 Web 服务重启都不会丢失任务，后台 worker 会从持久化状态恢复。

> **当前验证状态（2026-10-22）**
>
> - CANNJudge 的公开题目解析、模板读取、ZIP 工程下载已在本机**真实联网验证**。
> - **真实登录、题目导入、真实提交、远端 ID、结果查询与独立读回已完成**。模板基线曾返回 `Runtime Error`（15/15），**不代表算子通过**。
> - **2026-10-22 Agent 在 CopySign 取得真实 `15/15 Pass`**：attempt 24，remote id `6ad99b643ff2fef7e806c2f4`，`precision_ratio=1.0`，best 已写入；任务以 `agent_finished:target_reached` 正常结束。
> - 早期迭代保留用于诊断：3 次编译错误 + 1 次 `Wrong Answer`。详见 [Agent 编译错误诊断](docs/agent-compile-diagnosis.md)。
> - 优化循环保护：编译失败的候选不会推进实验 working revision；已知的 `AscendC::And/Or` 标量掩码调用会在 snapshot 前被 preflight 拒绝；实验基线固定为当时 global best。
> - 提交字节与快照逐字节一致；`kernel.asc` 与远端回显一致。
> - 服务器环境（HiDevLab 950 系列）仍在审批中；本机无 NPU，因此 Ascend C 候选本地仍标为 `unavailable/untested`，不会伪造设备结果。
> - 99 个自动化测试通过（mock/HTTP 层）；mock 通过不等于真实 CANNJudge 接入成功。
> - 没有模型 API 时，可在任务详情点击 **“提交基线（手动）”**：只提交一次模板可编辑文件，用来验证真实登录/提交/结果链路，不经过模型。

## 界面预览

本机 headless Chrome 实际截图（演示数据；非真实 CANNJudge 提交）。

2026-10-22 视觉刷新：暖白纸感浅色主题、任务卡片、逐测试点结果。

品牌标识：

![AscendForge logo](docs/screenshots/logo-preview.png)

![任务面板](docs/screenshots/ui-refresh-dashboard.png)

![任务详情](docs/screenshots/ui-refresh-task.png)

![逐测试点结果](docs/screenshots/ui-refresh-testcases.png)

旧版截图：

![旧版任务面板](docs/screenshots/dashboard.png)

![旧版尝试记录与源码](docs/screenshots/attempt-source.png)

![旧版版本 diff](docs/screenshots/attempt-diff.png)

## 为什么是这个形态

- **单用户、单服务器**：Python 标准库 + SQLite + 独立 worker，部署简单，故障面小。
- **浏览器只是控制面**：开始/暂停/恢复/停止只是写入意图；模型调用、提交、轮询都在服务器 worker 里。
- **凭据与模型上下文隔离**：模型只看到题目、源码和历史；API key、CANNJudge Cookie 只存在于加密凭据存储中。
- **保守的提交语义**：先落盘不可变快照，再提交完全相同的字节；超时/响应丢失进入 `submission_unknown`，先对账，绝不自动重发。
- **诚实的设备状态**：无 NPU 时只运行数据生成器和 CPU 参考实现，候选状态为 `untested`。

## 快速开始

### 1. 环境

- Python **3.11+**
- 唯一运行时依赖：`cryptography`（凭据加密）
- 推荐：Linux 服务器 + Docker（用于隔离执行生成代码）+ 反向代理 HTTPS

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
2. **平台连接**：输入用户提供的 CANNJudge 账号和密码，并填写一道用于验证权限的题目链接。登录在服务器完成；密码不落库，只保存加密的会话 Cookie 与用户 ID。若账号无法访问该题目，会明确返回“账号无法连接 judge”。
3. **模型与设置**：填写中转站/模型 `base_url`、`api_key`、模型名。可测试连接。
4. **新建任务**：粘贴完整 CANNJudge 题目链接，设置预算。
5. **开始**：worker 会创建快照、调用模型、排队提交、轮询结果并迭代。
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
python -m ascendforge backup                    # 服务器备份（默认不含主密钥）
python -m ascendforge verify-backup <dir>       # 备份完整性检查
python -m ascendforge restore <dir> --force     # 恢复（会覆盖目标数据目录）
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
    protocol.py          版本化工具协议 + JSON Schema 校验
    providers.py         OpenAI 兼容 Provider + Mock
    runtime.py           单步 Agent Runtime
    tools.py             宿主工具执行
    strategy.py          根据最新结果自动派生当前阶段策略
    prompts.py, skills.py
  execution/
    executor.py          Docker / 本地子进程隔离执行
    capabilities.py      NPU/CANN/Docker 诚实检测
  backup.py, cli.py, db.py, states.py
skills/                  四个 SKILL.md（通用优化 + Ascend 平台 + 测试生成 + 工作台规范）
tests/                   99 个 unittest
deploy/                  systemd / Docker / Caddy / Nginx
docs/                    架构、能力矩阵、阶段 0 报告、运维、安全、验收

提示词分层：

- 通用优化方法：`skills/optimize-operator/SKILL.md`
- Ascend/CANNJudge 约束：`skills/optimize-ascend-operator/SKILL.md`
- 测试生成：`skills/generate-operator-tests/SKILL.md`
- 长期约束：任务配置里的 `strategy_prompt`，在新建任务时填写
- 人工留言：任务详情“给 Agent 留言”，写入 `task_messages`，下一次 attempt 作为 `Operator messages` 注入
- 当前阶段：`active_strategy` 根据最新远端结果和 best 自动派生（compile → correctness → performance），随任务进展变化
- 实验/修订：`experiments` 固定 `baseline_attempt_id`、`working_attempt_id`、`hypothesis`、`search_space`、`success_criteria` 和实验预算；attempt 通过 `experiment_id` 归属实验；新实验从 performance best / correctness baseline 开始
- 旧数据：已有 attempt 自动归入一个 `legacy` experiment，不伪造历史猜想；下一次新建 attempt 会开启新实验
- 覆盖范围：`all_passed` 要求声明的预期测试点全部完成且通过；只有部分返回时记为 `testset_passed`/`current_testset_only`，不冒充完整通过
- 可比性：性能结果带 `comparison_key`（题目版本、测试点集合、CANN/SoC、时间单位和环境指纹）；不同 key 的耗时不会混入同一个 performance best
- 兼容层：`evaluations` 表、revision 的 config/edit_mode 字段已预留；真实 patch、参数评测和去重仍在下一阶段
- 参数实验：`parameter-run` 是宿主侧确定性 runner；配置枚举、固定基线、参数化源码快照、结果复用、失败重试、复测和预算耗尽均有记录；`--simulate` 不产生远端提交，真实模式通过 scheduler 提交并等待结果
- 通用闭环：阶段、责任边界、已验证场景和剩余缺口见 [通用优化闭环](docs/general-optimization-loop.md)；参数扫描是可选执行器，不是所有实验的固定形式
- 评价目标：`best_attempt_id` 只表示正确性基线；`performance_best_attempt_id` 独立按耗时比较（默认 `max_time`，可通过 `performance_metric` 改为 `avg_time`），没有耗时数据时记为“性能未知”，不会回退成 precision
- 运行预算：`max_tokens=0` / `max_model_calls=0` 表示不限；提交上限、每日上限、尝试轮数、运行时长等仍由宿主强制执行，无限 Token 不等于无限提交
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
  backups/
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
| 暂停 | 禁止新模型调用/执行/提交；已发出的提交继续收集结果 |
| 停止 | 不撤销远端已接受提交；取消尚未发送的排队提交 |
| 401/403 | 进入 `needs_login`/`needs_review`，不盲目重试 |
| 429 | 遵守 `Retry-After`，有上限退避 |
| 无 NPU | 数据/参考结果可跑；候选 `unavailable/untested` |
| 自定义远端测试 | 未核实，禁用 |
| best 更新 | 仅远端“通过且指标可比且不退化”的新版本；失败/编译错误不覆盖 |
| 重放请求 | `X-Request-Id` 幂等键 + 状态机；开始/暂停/恢复/停止幂等 |

## 安全边界

- 密码 scrypt（每用户盐）；会话令牌只在 DB 存 SHA-256。
- 模型 key 与 CANNJudge Cookie 用 Fernet 加密，密钥在数据库之外；浏览器只看到掩码。
- 生成代码在独立子进程或 Docker 中执行：临时工作目录、最小环境、输出/时间/内存限制；Docker 模式禁网。**本地子进程回退不能强制禁网**，能力页会明确提示，服务器推荐 Docker。
- 导出档案默认排除凭据/会话/日志；备份默认不含主密钥。
- 远程访问必须经 HTTPS 反向代理，并设置 `ASCENDFORGE_SECURE_COOKIES=true`。

详见 [docs/security.md](docs/security.md)。

## 测试

```bash
PYTHONPATH=. python -m unittest discover -s tests -t . -v
```

覆盖：快照不可变与篡改检测、提交限频/每日额度/在途上限、并发原子领取、重启后限频保持、未知提交冻结与对账、人工恢复、两轮 mock 优化与失败不覆盖 best、CSRF/幂等、凭据掩码、档案排除密钥、备份校验/恢复、隔离执行器超时/输出上限/环境清洗、CANNJudge 提交载荷校验、生成器/参考实现隔离执行、任务状态机与 skills 加载、真实 HTTP OpenAI 兼容 Provider（JSON 动作、原生 tool_calls、tools 降级）、提交 401/403/429/unknown 异常分支、手动基线与继续自动优化。

## 尚未验证 / 需要你完成

1. **真实可通过的算子实现**：阶段 1 已用模板基线打通“提交—结果”链路，但基线是模板，返回 `Runtime Error`；还需要真实实现并拿到 `Pass`。
2. **真实模型 Agent 两轮优化**：需要你提供中转站 base_url、key、模型名；不同网关对 `tools` 字段、JSON 输出和计费字段的兼容性需要实测。
3. **服务器**：HiDevLab 950 审批下来后，部署 `serve` + `worker`，确认 Docker/CANN/NPU 能力；无 NPU 的开发机不能替代。
4. **HTTPS/域名**：远程访问前配置反向代理与 `ASCENDFORGE_SECURE_COOKIES=true`。

## 文档

- [架构与状态机](docs/architecture.md)
- [CANNJudge 能力矩阵与证据](docs/capability-matrix.md)
- [阶段 0 真实探测报告](docs/phase0-report.md)
- [阶段 1 真实提交报告](docs/stage1-report.md)
- [Agent 编译错误诊断](docs/agent-compile-diagnosis.md)
- [阶段 1 证据 JSON](docs/evidence/stage1-2026-10-21.json)
- [部署与运维](docs/operations.md)
- [安全模型](docs/security.md)
- [验收清单](docs/acceptance.md)
- [Agent 工具协议](docs/protocol.md)
