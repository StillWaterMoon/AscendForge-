# 验收清单与当前状态

日期：2026-10-21  
测试命令：`PYTHONPATH=. python -m unittest discover -s tests -t . -v` → **112 passed**

状态含义：

- **PASS (mock/HTTP)**：自动化测试覆盖，使用 FakePlatform/MockProvider，非真实 CANNJudge。
- **PASS (unit)**：纯逻辑/存储/安全测试。
- **IMPLEMENTED, LIVE UNVERIFIED**：代码与界面存在，但没有真实账号/服务器/模型，不能标为验收通过。
- **PENDING**：依赖外部条件。

## 1. 核心闭环

| 验收项 | 状态 | 证据 / 说明 |
|---|---|---|
| 登录使用已有真实账号，会话失效可恢复 | **LIVE VERIFIED (登录/题目权限)** | 用户已在 Web 连接账号；服务器保存加密 Cookie + user_id，题目解析/模板读取成功（`verified_at` 存在）；密码未落库。401 恢复路径仍待真实验证 |
| 至少一次真实提交绑定源码快照、哈希、远端 ID | **PASS (LIVE, 2026-10-21)** | CopySign 基线：snapshot `3aa9af10...b377b95`，request_hash 一致，remote id `6ad84d04193e507d8e4a0502`；平台回显 `kernel.asc` SHA-256 一致 |
| 无模型时的真实提交流程 | **PASS (LIVE)** | 任务详情“提交基线（手动）”→ 真实提交 → `manual_baseline_finished`；不需要模型 |
| 真实评测结果可查询、保存、展示 | **PASS (LIVE)** | 平台返回 `Runtime Error`，15/15 测试点；原始响应保存，独立 `GET /api/submissions/{id}?userId=...` 读回一致 |
| Agent 可由用户 API 驱动，工具由宿主管理 | **PASS (HTTP mock provider)** | `test_provider_http`：真实 HTTP 请求 OpenAI 兼容端点，验证 JSON 动作、原生 tool_calls、tools 400 降级、Runtime 创建快照并入队；真实模型 key 仍未测 |
| 提交异常分支（401/403/429/unknown） | **PASS (unit)** | `test_submission_errors`：401→needs_login、403→needs_review、429 退避且计入额度、unknown→冻结 |
| 自动迭代至少两轮，保留父子关系和全部结果 | PASS (mock/HTTP) | `test_runtime_loop`：v1→10、v2→12、v3 失败；父子/best 正确 |
| 同账号跨任务限频，重启后仍生效 | PASS (unit) | `test_claim_send_and_interval_persist_across_restart`、`test_interval_is_global_across_tasks` |
| 响应丢失不盲目重复，支持对账/人工恢复 | PASS (unit) | unknown 冻结、唯一 adopt、歧义 needs_review、mark_failed |
| 预算、暂停、停止、退避由调度器强制执行 | PASS (unit) | 每日/在途/间隔/预算；paused 不入队新的发送；stop 取消 queued |
| 新版本失败/退化不覆盖 best | PASS (unit) | `test_best_only_replaced_by_comparable_improvement` |
| 数据由 Agent 生成，参考实现与校验可独立核对 | PASS (unit) | `test_local_validation`：生成器/参考实现在隔离执行器跑通；候选仍为 unavailable |
| 无 NPU 和不支持自定义远端测试明确展示 | PASS (unit) + UI | 能力检测、`candidate_execution=unavailable`、`custom_tests=false` |
| 凭据不进模型上下文/实验文件，生成代码隔离 | PASS (unit) | 加密、掩码、环境清洗、档案扫描无 key |
| 最终报告区分真实/mock/未验证 | PASS | 本文 + README + phase0 |

## 2. 服务器与 Web

| 验收项 | 状态 | 证据 / 说明 |
|---|---|---|
| 独立服务器托管 Web、后端、worker；本地仅浏览器 | IMPLEMENTED, DEPLOY PENDING | 两进程 + systemd/Docker 文件；HiDevLab 未审批 |
| Web 可配置、建任务、控制、查看结果，无需本地 CLI | PASS (HTTP) / LIVE PENDING | `test_web` 覆盖 setup/login/CSRF/任务控制/模型掩码/档案/best 下载；真实数据待账号 |
| 关闭网页/电脑关机后服务器任务继续 | IMPLEMENTED, NOT LIVE TESTED | worker 独立进程；未在真实服务器做断电/关机演练 |
| Web 与 worker 独立运行，重启不重复执行 | IMPLEMENTED, NOT LIVE TESTED | 状态全持久化；worker 恢复逻辑有单测；未做真实进程重启演练 |
| 浏览器写操作防重复，凭据仅服务器保存 | PASS (HTTP) | CSRF 403；X-Request-Id replay；key 掩码 |
| best/指定版本/任务档案可下载，排除凭据 | PASS (unit/HTTP) | 档案测试扫描无 key；best/attempt 下载 HTTP 测试通过 |
| 数据库与实验文件持久化，服务器备份/恢复演练 | PASS (unit) | 备份/校验/恢复/篡改检测；真实服务器卷未演练 |

## 3. 安全与诚实性

| 验收项 | 状态 |
|---|---|
| 密码/会话/API key/Cookie 不进入提示词、快照、普通日志 | PASS (unit + code review) |
| 生成代码限制时间/内存/输出/路径；Docker 禁网 | PASS (unit, local); Docker 未实跑 |
| 不伪造 NPU 结果；CPU 参考 ≠ 算子通过 | PASS (code/UI) |
| 不猜测平台端点；404 端点已替换 | PASS | latest 404 已记录并改用前端端点 |
| 验证码/二次验证交用户，不绕过 | PASS (design) |
| 未知提交不自动重发 | PASS (unit) |
| 导出不含凭据/会话/日志 | PASS (unit) |
| 备份默认不含主密钥 | PASS (unit) |

## 4. 依赖外部条件、当前无法验收

1. **真实 Pass（可通过的算子实现）**：阶段 1 提交的是模板基线，返回 `Runtime Error`；还没有 `Pass`，也没有 best。
2. **真实模型 Agent 自动优化**：模型 API 尚未配置；需要 base_url/key/model，并完成至少两轮真实迭代。
3. **HiDevLab 950**：审批、部署、网络出口、Docker/CANN/NPU 能力。
4. **模型网关兼容性**：不同中转站对 tools/JSON/计费字段的兼容性。
6. **HTTPS 域名**：反向代理与证书。
7. **浏览器视觉/交互**：已用 headless Chrome 实际登录演示实例并截图验证登录页、任务面板、任务详情、attempt 源码/diff、平台连接、模型设置；未做跨浏览器/移动端/无障碍回归。
8. **进程故障演练**：真实服务器上的 kill -9、重启 web/worker、断网、重复请求。

## 5. 阶段 1 实测记录（2026-10-21）

阶段 1 的真实闭环已用 CopySign 模板基线完成：登录/会话 → 导入题目 → 快照 → 调度提交 → remote id → 结果查询 → 原始结果保存 → 独立读回。详细证据见 [stage1-report.md](stage1-report.md) 和 [evidence JSON](evidence/stage1-2026-10-21.json)。基线结果为 `Runtime Error`（0/15），因此下一步是真实实现/Agent 优化，而不是重复验证提交通道。

### 后续复验步骤

1. 在服务器启动 web + worker，浏览器登录 AscendForge。
2. 平台连接：输入 CANNJudge 账号密码 + Add 题目链接；确认返回账号 masked、题目标题、模板文件数。
3. 新建任务（先设 1 次提交、300s 间隔、预算小），点击开始。
4. 在任务详情确认 attempt 快照 SHA、排队、发送、remote id。
5. 在 CANNJudge 网页核对提交内容与快照一致、结果与系统展示一致。
6. 下载任务档案，校验 `SHA256SUMS`；确认不含 key/Cookie。
7. 暂停 → 重启 worker → 恢复，确认没有重复提交；制造一次 unknown（可用测试环境）并演练对账。
8. 关闭浏览器/电脑，等待一轮结果，重新打开确认状态更新。
9. 记录：真实通过项、仅 mock 项、未验证项；不要把 mock 结果写成真实接入。
