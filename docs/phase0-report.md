# 阶段 0 报告：环境与平台可行性核实

日期：2026-10-21（Asia/Shanghai）  
执行环境：当前开发机（Windows，Python 3.12，无 NPU），不是 HiDevLab 服务器。

## 1. 结论摘要

| 项目 | 结论 |
|---|---|
| AscendForge 自身闭环 | 已实现并测试（123 个 unittest） |
| CANNJudge 公开题目/模板/ZIP | **真实联网验证通过** |
| CANNJudge 登录 | **真实登录已由用户在 Web 完成**（账号已连接，题目权限已验证，密码未保存） |
| CANNJudge 真实提交/结果 | **已完成（2026-10-21）**：CopySign 模板基线 remote id `6ad84d04193e507d8e4a0502`，返回 `Runtime Error`（0/15）；提交字节/快照一致，结果可独立读回 |
| NPU / CANN | 开发机不可用；HiDevLab 950 申请审批中 |
| 数据生成/CPU 参考 | 已实现隔离执行；候选 Ascend C 永远标记 untested（无 NPU） |
| 自定义远端测试 | 未核实；禁用 |
| 服务器部署 | 代码与部署文件已准备；未在 HiDevLab 上实际部署 |

**阶段 1 的提交通道已验收；但不代表算子通过。** 基线是模板，平台返回 `Runtime Error`；真实 `Pass`、best 和 Agent 自动优化仍未完成。详见 [阶段 1 报告](stage1-report.md)。

## 2. 实际执行的验证

### 2.1 首页与公开 API

```text
GET https://cannjudge.cn/home
→ 200, 433 bytes, title CANNJudge

GET /api/groups/public
→ 200, _id=69c8ffcea2bcbfa3591f481e

GET /api/groups/name/hc_cann_codelabs
→ 200, _id=6aa12edac76b321ca6605317

GET /api/contests/group/6aa12edac76b321ca6605317
→ 200, 1 contest: 6aa272292d3dd2c5ae20807a (name=cann)

GET /api/problems/contest/6aa272292d3dd2c5ae20807a
→ 200, 1 problem: 6aa29c152d3dd2c5ae3379f1 (name=addv2, title=add)
```

### 2.2 题目、模板、工程包

```text
GET /api/problems/6aa29c152d3dd2c5ae3379f1
→ 200, 2899 bytes, code_template=npu_kernel_dev

GET /api/problems/6aa29c152d3dd2c5ae3379f1/template
→ 200, 16983 bytes, data.files:
   CMakeLists.txt (read-only)
   data_utils.h (read-only)
   kernel.asc (editable, 1077 chars)
   main.asc (read-only)
   run.sh (read-only)
   scripts/Add.py, scripts/gen_data.py, scripts/verify_result.py (read-only)

GET /api/problems/6aa29c152d3dd2c5ae3379f1/package
→ 200, application/zip, 16357 bytes
  第一次探测 sha256=74fb5013c0418d2c334feacef90c247c61fa4349ad5f785bc8af27168d864e3b
  第二次探测 sha256=4c4b6a8033d224c8dc3f50c3eb1ffd72cb9c515ed9f347263ec4dc1199571761
  解压 8 个文件成功，拒绝路径越界/符号链接
```

> 两次下载的 ZIP 字节哈希不同（平台重新打包/ZIP 时间戳），所以 AscendForge
> 以**本地导入时实际保存的包**计算哈希，不假设远端包字节稳定；提交快照的哈希
> 只针对本地源码字节。

### 2.3 登录/提交端点存在性

不使用真实凭据，只检查协议：

```text
GET /api/users/captcha
→ 200, captchaId + SVG

POST /api/users/login {}
→ 400 {"message":"password required"}

POST /api/submissions/submit {}
→ 400 {"message":"userId and problemId required"}

GET /api/submissions/does-not-exist
→ 400 {"message":"invalid submission id"}
```

### 2.4 发现的偏差

1. 公开资料示例链接为 `/hc_cann_codelabs/cann/add`，但当前 API 的题目 canonical name 是 **`addv2`**，标题是 `add`。`/api/problems/name/add` 返回 404。
   - 处理：先按名查；404 时读取 `/api/problems/contest/{id}`，只在 `name/canonical_name/title` **唯一精确匹配**时采用；否则报错并列出可用题目，不猜测。
2. 公开资料中的 `GET /api/submissions/problem/{id}/latest` 实测 **404 Cannot GET**。
   - 处理：改用前端实际使用的：
     - `GET /api/submissions/user/{userId}/problem/{problemId}`
     - `GET /api/submissions/user/{userId}?withCount=1&limit=50&order=desc`
3. 模板响应外层是 `{code,msg,data:{files}}`，已兼容。
4. 前端提交结果查询使用 `GET /api/submissions/{id}?userId=...`，已加入。

## 3. 架构与关键选择

- **Python 3.11+ + SQLite + 独立 worker**：单用户、单服务器，避免引入外部 DB/队列；WAL + `BEGIN IMMEDIATE` 实现原子额度。
- **标准库 HTTP + 原生 JS**：无前端构建、无 CDN、无 Node 运行时依赖；浏览器只轮询 JSON。
- **快照先于提交**：聚合 SHA-256 + 每文件 hash；发送前重新校验，避免可变工作目录。
- **凭据分离**：Fernet（密钥在 DB 外）；模型上下文/档案/日志不含 key/Cookie。
- **保守提交**：超时/5xx/无 ID → `submission_unknown`，冻结同账号新提交，先对账；不自动重发。
- **诚实设备状态**：无 NPU 时 `candidate_execution=unavailable`；CPU 参考通过 ≠ 算子通过。
- **显式 Skill 注入**：`skills/*/SKILL.md` 由宿主读取并注入，不依赖客户端自动发现。

## 4. 已实现并验证的能力

- 任务/attempt/submission 状态机；非法转移拒绝。
- 同账号最小间隔、每日额度、在途上限、每题上限；重启后计数保留；跨任务间隔。
- 并发领取同一提交只成功一次。
- unknown 冻结、唯一对账 adopt、歧义保持人工审查、人工 mark_failed。
- 两轮 mock 优化：快照 → 提交 → 结果 → 新 attempt（父版本）→ best 提升；第三轮失败不覆盖 best。
- 内容哈希/篡改检测；导出档案默认排除凭据，可校验 SHA256SUMS。
- Web：首次 setup、登录、CSRF、幂等 replay、任务控制、模型配置掩码、best/attempt/档案下载。
- 备份：创建、SHA-256 校验、SQLite integrity_check、恢复、篡改检测；默认不含主密钥。
- 隔离执行器：环境清洗、超时、输出上限、路径越界拒绝；生成器/参考实现端到端跑通。
- CANNJudge 提交载荷：核函数多文件、只读保护、禁止文件、额外文件限制、传统四字段。

## 5. 未验证 / 阻塞

| 阻塞 | 需要的操作 |
|---|---|
| 真实 CANNJudge 登录 | **已完成**：账号已在 Web 连接并验证题目权限 |
| 真实提交与结果 | **已完成**：CopySign 基线真实提交，remote id + 原始结果 + 独立读回；结果为 Runtime Error，非 Pass |
| HiDevLab 950 服务器 | 审批通过后部署、确认网络出口、Docker/CANN/NPU |
| 模型 API | 用户提供 base_url/key/model；需实测网关兼容性 |
| HTTPS/域名 | 配置反向代理；设置 `ASCENDFORGE_SECURE_COOKIES=true` |
| 平台限频规则 | 真实账号下观察；默认 300s/1 在途/20 每日 |
| 浏览器视觉回归 | 已完成首轮：headless Chrome 实际登录演示实例，截图验证登录页、任务面板、任务详情、attempt 源码/diff、平台、设置；未做跨浏览器/移动端 |

## 6. 下一步（按阶段）

1. **阶段 1（已完成）**：Web 登录 → 导入 CopySign → 手动基线提交 → remote id/结果 → 提交字节与快照一致。基线 `Runtime Error`，未通过。
2. **阶段 2**：在真实账号下验证 429、401、超时、重启；用两个任务验证跨任务间隔。
3. **阶段 3**：接入用户模型，完成至少两轮真实“生成/修改 → 提交 → 反馈 → 再修改”。
4. **阶段 4**：在 950 服务器上确认 NPU/CANN；否则保持 untested，只验证生成器/参考。
5. **阶段 5**：服务器部署、跨机器浏览器验证、关闭网页/重启 web/重启 worker、备份恢复演练。

## 7. 复现实验命令

```bash
# 公开探测（不登录）
python -m ascendforge probe --url https://cannjudge.cn/home

# 真实题目解析/模板/ZIP（匿名）
PYTHONPATH=. python scripts/cannjudge_probe.py \
  --problem-url https://cannjudge.cn/hc_cann_codelabs/cann/add \
  --download-package --probe-auth-endpoints

# 自动化测试
PYTHONPATH=. python -m unittest discover -s tests -t . -v
```
