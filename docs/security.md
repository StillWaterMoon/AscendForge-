# 安全模型

## 1. 资产

| 资产 | 位置 | 风险 |
|---|---|---|
| AscendForge 管理员密码 | scrypt hash in SQLite | 账号接管 |
| AscendForge 会话 | HttpOnly cookie；DB 只存 token SHA-256 | 会话劫持 |
| CANNJudge 会话 | `credentials`（Fernet 密文）；主密钥在 DB 外 | 账号冒用/数据泄露 |
| CANNJudge 账号会话 | 加密 Cookie + user id；不存密码 | 冒名提交 |
| 题目/源码/评测反馈 | `data/tasks` | 知识产权/竞赛规则 |
| 主密钥 | `secrets/master.key` 或 env | 解密全部凭据 |
| 任务档案 | `data/tmp`（临时） | 源码 + 加密凭据 |

## 2. 控制措施

### 访问与认证

- 首次初始化使用一次性 setup token（控制台打印），成功后不再可用。
- 密码 scrypt（N=2^14, r=8, p=1，16 字节随机盐，32 字节输出）。
- 会话 token 32 字节随机；Cookie `HttpOnly; SameSite=Lax; Path=/`；HTTPS 时加 `Secure`。
- 会话默认 12 小时；登录节流（每 IP 10 次/5 分钟，进程内）。
- 所有写操作要求 `X-CSRF-Token`（绑定服务器会话）并校验同源 `Origin`。
- `X-Request-Id` 幂等键防重复执行；开始/暂停/恢复/停止本身也幂等。
- 安全响应头：CSP、`X-Frame-Options: DENY`、`nosniff`、`Referrer-Policy`、`Permissions-Policy`。

### 凭据

- CANNJudge Cookie 使用 Fernet（AES-128-CBC + HMAC）加密。
- 主密钥 32 字节 urlsafe-base64；文件权限 600；也可以由 `ASCENDFORGE_SECRET_KEY` 注入。
- 浏览器只读取掩码；任何 API 都不返回明文 key/Cookie。
- 密码不持久化；CANNJudge 登录只在服务器内存中短暂处理。
- 快照、任务档案、普通日志均不含凭据。
- 日志过滤器对 `api_key/password/token/authorization/cookie/Bearer/sk-...` 做保守脱敏；不记录请求体。

### CLI 与工具

- 当前只支持本地 Codex CLI；Codex 使用自己的认证，不读取 AscendForge 模型 key。
- Codex app-server 只暴露 `read_problem`、`submit_official`、`read_official_evaluation` 三个宿主工具。
- `submit_official` 只创建 Scheduler 排队记录；Codex 不能直接访问 CANNJudge，也不能修改提交限额。
- AscendForge 不保存 Codex 的隐藏思维链；只保存会话引用、turn 事件、快照和提交结果。
- **执行隔离由本地 Codex 的 sandbox 配置决定，AscendForge 不再提供 Docker/Local executor，也不做额外的 OS 级隔离。**

### 快照与提交

### 输入与文件

- 所有路径做 `safe_relpath` 校验：拒绝绝对路径、盘符、`..`、反斜杠、NUL。
- ZIP 解压拒绝路径越界、符号链接、超大解压体积；单包默认上限 64 MB。
- 快照限制 256 文件 / 12 MiB；候选单文件 2 MiB。
- 提交前重新读取快照并校验每个文件 SHA-256 与聚合哈希。
- 文件内容按 UTF-8 处理；提交字节与快照完全一致。

### 导出

- 任务档案只包含该任务的行和文件；显式排除凭据、会话、日志。
- 下载经过登录 + CSRF/会话校验；文件名做安全化处理。

## 3. 威胁模型

| 威胁 | 缓解 |
|---|---|
| Codex 越权修改宿主文件 | 依赖 Codex 自身 sandbox；AscendForge 只接收允许文件快照并做路径校验 |
| Codex 直接访问 CANNJudge | 不注入平台 Cookie；只能通过 `submit_official` 入队 |
| 恶意题目 ZIP 路径穿越 | 拒绝 `..`/绝对路径/符号链接/超大解压 |
| 篡改快照后提交 | 提交前逐文件 + 聚合哈希复核，失败不发送 |
| 重复提交/额度竞争 | 持久化队列 + `BEGIN IMMEDIATE` + 幂等键 |
| 提交响应丢失后重复 | unknown 状态 + 冻结 + 对账；不自动重发 |
| 会话/CSRF 攻击 | HttpOnly/SameSite/Secure + CSRF token + Origin 校验 |
| 日志泄露 | 脱敏 + 不记录请求体；日志轮转 |

## 4. 已知限制

1. 单进程登录节流，多 Web 实例/重启后计数不共享；公网部署应加代理层限流。
2. 没有 MFA、硬件密钥、密码找回；单管理员模型。
3. 没有自动密钥轮换；轮换主密钥需要重新加密凭据（当前需手工导出/重新连接）。
4. SQLite 单写者；高并发多用户不是目标。
5. Codex CLI 的沙箱/网络权限由 Codex 配置决定；AscendForge 不提供额外的 OS 级隔离。
6. 平台限频、验证码、二次验证、自动提交规则未完全核实；系统按保守策略运行，遇到验证码交用户完成。
8. 任务档案不包含实际生成数据字节（默认），不能仅凭种子保证跨环境逐字节复现。
9. 日志可能包含 Codex 输出/错误信息；不要在其中放入密钥。
10. `setup token` 会打印到控制台；初始化完成后应视为失效，但文件仍保留在 `secrets/`，注意权限。

## 5. 事件响应

1. 立即 `pause` 所有任务，必要时 `stop`。
2. 断开 CANNJudge 会话；撤销 AscendForge 所有会话（改密码会撤销）。
3. 检查 `events`/`audit_log`/`submission` 记录；确认是否有 unknown/异常提交。
4. 如主密钥泄露：生成新密钥并重新连接 CANNJudge。
5. 保留日志与证据，修补入口（HTTPS、防火墙、密码、依赖）。
