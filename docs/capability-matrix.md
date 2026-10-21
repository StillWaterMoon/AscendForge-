# CANNJudge 能力矩阵与证据

> 最后更新：2026-10-21（Asia/Shanghai）
>
> 状态定义：
>
> - **live-verified**：本部署实际发出请求并得到可复现结果。
> - **documented**：公开接入资料/前端代码明确写出，但本部署未成功执行。
> - **unverified**：没有可靠证据，按不支持处理。
> - **not applicable**：当前版本不提供。

## 1. 总表

| 能力 | 状态 | 证据 / 说明 |
|---|---|---|
| 首页可达 | live-verified | `GET https://cannjudge.cn/home` → 200，433 bytes，title `CANNJudge` |
| 公开小组 | live-verified | `GET /api/groups/public` → 200，`_id=69c8ffcea2bcbfa3591f481e` |
| 按组名取小组 | live-verified | `GET /api/groups/name/hc_cann_codelabs` → 200，`_id=6aa12edac76b321ca6605317` |
| 小组比赛列表 | live-verified | `GET /api/contests/group/{group_id}` → 200，1 个比赛 `cann` (`6aa272292d3dd2c5ae20807a`) |
| 比赛题目列表 | live-verified | `GET /api/problems/contest/{contest_id}` → 200，题目 `addv2` / title `add` |
| 按名查题 | live-verified（失败分支） | `GET /api/problems/name/add?contestId=...` → 404；回退到比赛题目列表，按 `title/canonical_name/name` 唯一匹配 |
| 题目详情 | live-verified | `GET /api/problems/{id}` → 200，包含 `code_template=npu_kernel_dev` |
| 模板文件 | live-verified | `GET /api/problems/{id}/template` → 200，`data.files` 8 个，`kernel.asc` editable |
| 工程 ZIP | live-verified | `GET /api/problems/{id}/package` → 200 `application/zip`，16357 bytes；两次下载哈希不同（平台重新打包），本地导入包单独记哈希 |
| 图形验证码 | live-verified（端点） | `GET /api/users/captcha` → 200，`captchaId` + SVG |
| 登录端点 | live-verified（存在性） | `POST /api/users/login {}` → 400 `password required`；字段来自公开资料：`account/loginType/password`（email 兼容字段 `email`） |
| 登录成功 | **unverified** | 无真实账号；尚未取得 `_id` + 会话 Cookie |
| 提交端点 | live-verified（存在性） | `POST /api/submissions/submit {}` → 400 `userId and problemId required` |
| 提交成功 / 返回 ID | **unverified** | 未用真实账号提交；不能声称闭环已验收 |
| 查询单个提交 | live-verified（存在性） | `GET /api/submissions/does-not-exist` → 400 `invalid submission id`；前端使用 `?userId=` |
| 查询最终结果 | **unverified** | 未取得真实 submission id |
| 账号题目提交列表 | documented（前端验证） | 前端 `me/submissions.js`：`GET /api/submissions/user/{userId}?withCount=1&...`；题目页：`GET /api/submissions/user/{userId}/problem/{problemId}` |
| 全站提交列表 | documented（前端验证） | `GET /api/submissions/global/list?withCount=1&skip=&limit=&q=` |
| 旧文档的 latest 端点 | **live-404** | 公开资料写的 `/api/submissions/problem/{id}/latest` 实测 404 `Cannot GET`，不要使用 |
| 排行榜 | documented | `/api/submissions/contest/{contestId}/stats` 等前端路径；未在真实账号下验证 |
| 自定义测试数据上传 | **unverified** | 没有发现可靠接口；系统禁用，不申请 |
| 提交幂等 | **unverified** | 平台未提供幂等键证据；系统按“超时即 unknown，先对账”处理 |
| 自动提交规则/限频 | **unverified** | 只按保守配置限频；需在真实账号下观察 429/规则 |

## 2. 已验证的题目（Add）

2026-10-21 匿名访问：

```text
problem_id   = 6aa29c152d3dd2c5ae3379f1
name         = addv2
title        = add
contest_id   = 6aa272292d3dd2c5ae20807a
group        = hc_cann_codelabs
code_template= npu_kernel_dev
template     = 8 files (kernel.asc editable; others read-only)
package      = 16357 bytes; hash varies per download (observed 74fb5013... and 4c4b6a80...)
```

这证明“题目导入、模板读取、ZIP 下载”真实可用；**不证明**登录、提交、评测结果可用。

## 3. 接口清单（实现使用）

| 操作 | 方法与路径 | 状态 |
|---|---|---|
| 验证码 | `GET /api/users/captcha` | live endpoint |
| 登录 | `POST /api/users/login` | documented + endpoint live |
| 组 | `GET /api/groups/public`, `/api/groups/name/{name}` | live |
| 比赛 | `GET /api/contests/group/{groupId}` | live |
| 题目列表 | `GET /api/problems/contest/{contestId}` | live |
| 按名查题 | `GET /api/problems/name/{name}?contestId=&userId=` | live (404 fallback) |
| 题目详情 | `GET /api/problems/{id}` | live |
| 模板 | `GET /api/problems/{id}/template?userId=` | live |
| 工程包 | `GET /api/problems/{id}/package?userId=` | live |
| 提交 | `POST /api/submissions/submit` `{problemId,userId,files[]}` 或四字段 | documented + endpoint live |
| 查询 | `GET /api/submissions/{id}?userId=` | endpoint live |
| 账号题目提交 | `GET /api/submissions/user/{userId}/problem/{problemId}` | frontend-verified |
| 账号提交列表 | `GET /api/submissions/user/{userId}?withCount=1&limit=&order=desc` | frontend-verified |

## 4. 提交载荷

`npu_kernel_dev`：

```json
{
  "problemId": "...",
  "userId": "...",
  "files": [
    {"path": "kernel.asc", "content": "..."},
    {"path": "helper.h", "content": "..."}
  ]
}
```

传统工程：`problemId`, `userId`, `tiling_h`, `tiling_key_h`, `host_cpp`, `kernel_cpp`。

## 5. 尚存风险

1. 登录可能需要图形验证码/二次验证；系统提供验证码流程，但不绕过。
2. 账号可能只能访问部分小组/比赛；导入时 403 会明确返回“账号无法连接 judge”。
3. 平台限频、每日额度、自动提交规则未核实；默认 300s 间隔、同账号 1 个在途，均可调高但不得低于平台限制。
4. 平台前端是 SPA，接口可能继续变化；已避免使用实测 404 的端点。
5. 自定义测试能力未核实，系统不会上传数据。
6. 服务器（HiDevLab 950）网络出口与授权环境未验证；本报告的探测来自当前开发机。
