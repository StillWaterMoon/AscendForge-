# 验收清单与当前状态

日期：2026-10-21  
测试命令：`PYTHONPATH=. python -m unittest discover -s tests -t . -v` → **55 passed**

状态含义：

- **PASS (mock/HTTP)**：自动化测试覆盖，使用 FakePlatform/MockProvider，非真实 CANNJudge。
- **PASS (unit)**：纯逻辑/存储/安全测试。
- **IMPLEMENTED, LIVE UNVERIFIED**：代码与界面存在，但没有真实账号/服务器/模型，不能标为验收通过。
- **PENDING**：依赖外部条件。

## 1. 核心闭环

| 验收项 | 状态 | 证据 / 说明 |
|---|---|---|
| 登录使用已有真实账号，会话失效可恢复 | IMPLEMENTED, LIVE UNVERIFIED | Web 平台连接页支持账号密码 + 可选验证码；401 → `needs_login`；未真实登录 |
| 至少一次真实提交绑定源码快照、哈希、远端 ID | IMPLEMENTED, LIVE UNVERIFIED | 快照/哈希/提交载荷单测；真实提交待账号 |
| 真实评测结果可查询、保存、展示 | IMPLEMENTED, LIVE UNVERIFIED | 查询端点存在性已验证；真实 submission id 待提交 |
| Agent 可由用户 API 驱动，工具由宿主管理 | IMPLEMENTED, MOCK TESTED | MockProvider 两轮闭环；OpenAI 兼容 Provider 未用真实 key 测 |
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

1. **真实 CANNJudge 登录**：需要用户在 Web 输入账号密码（不要在聊天中发送密码）。
2. **真实提交**：需要账号对目标题目有提交权限；平台可能要求验证码/二次验证。
3. **真实结果**：需要一次被受理的 submission id。
4. **HiDevLab 950**：审批、部署、网络出口、Docker/CANN/NPU 能力。
5. **模型 API**：base_url/key/model；不同中转站对 tools/JSON/计费字段的兼容性。
6. **HTTPS 域名**：反向代理与证书。
7. **浏览器视觉/交互**：当前仅 HTTP 层测试；尝试用本机 headless Chrome/Edge 截图被运行环境沙箱拒绝（mojo/crashpad 访问被拒），未完成视觉回归。
8. **进程故障演练**：真实服务器上的 kill -9、重启 web/worker、断网、重复请求。

## 5. 建议的阶段 1 验收步骤

1. 在服务器启动 web + worker，浏览器登录 AscendForge。
2. 平台连接：输入 CANNJudge 账号密码 + Add 题目链接；确认返回账号 masked、题目标题、模板文件数。
3. 新建任务（先设 1 次提交、300s 间隔、预算小），点击开始。
4. 在任务详情确认 attempt 快照 SHA、排队、发送、remote id。
5. 在 CANNJudge 网页核对提交内容与快照一致、结果与系统展示一致。
6. 下载任务档案，校验 `SHA256SUMS`；确认不含 key/Cookie。
7. 暂停 → 重启 worker → 恢复，确认没有重复提交；制造一次 unknown（可用测试环境）并演练对账。
8. 关闭浏览器/电脑，等待一轮结果，重新打开确认状态更新。
9. 记录：真实通过项、仅 mock 项、未验证项；不要把 mock 结果写成真实接入。
