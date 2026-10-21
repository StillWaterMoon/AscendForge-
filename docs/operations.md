# 部署与运维

## 1. 服务器要求

- Linux x86_64/ARM64，Python 3.11+
- 持久磁盘：数据库、题目包、源码快照、生成数据、备份
- 可选但推荐：Docker Engine（隔离执行生成代码）
- 可选：NPU + CANN（没有也能运行，只是候选 untested）
- 反向代理/证书：Caddy 或 Nginx（远程访问必须 HTTPS）
- 进程管理：systemd 或容器编排；**不要**用 SSH 会话前台运行

单用户规模建议：2 vCPU / 4 GB RAM / 40 GB 磁盘起；题目包和快照通常很小，生成数据上限受执行器限制。

## 2. 安装（systemd，推荐）

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin ascendforge
sudo mkdir -p /opt/ascendforge /var/lib/ascendforge /etc/ascendforge
sudo chown -R ascendforge:ascendforge /opt/ascendforge /var/lib/ascendforge

sudo -u ascendforge git clone <repo> /opt/ascendforge
cd /opt/ascendforge
sudo -u ascendforge python3 -m venv .venv
sudo -u ascendforge .venv/bin/pip install -r requirements.txt
```

复制环境文件：

```bash
sudo cp deploy/ascendforge.env.example /etc/ascendforge/ascendforge.env
sudo editor /etc/ascendforge/ascendforge.env
sudo chmod 600 /etc/ascendforge/ascendforge.env
```

至少设置：

```dotenv
ASCENDFORGE_DATA_DIR=/var/lib/ascendforge
ASCENDFORGE_HOST=127.0.0.1
ASCENDFORGE_PORT=8080
ASCENDFORGE_SECURE_COOKIES=true
ASCENDFORGE_EXECUTOR=docker
ASCENDFORGE_EXECUTOR_IMAGE=python:3.12-slim
```

初始化管理员：

```bash
sudo -u ascendforge env $(grep -v '^#' /etc/ascendforge/ascendforge.env | xargs) \
  /opt/ascendforge/.venv/bin/python -m ascendforge init
```

> 也可以不运行 `init`，首次打开网页时使用控制台打印的一次性 setup token 初始化。两种方式二选一。

安装服务：

```bash
sudo cp deploy/systemd/ascendforge-web.service /etc/systemd/system/
sudo cp deploy/systemd/ascendforge-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ascendforge-web ascendforge-worker
sudo systemctl status ascendforge-web ascendforge-worker
```

如果 worker 使用 Docker 执行器，把 `ascendforge` 加入 docker 组（或使用 rootless Docker），并确认 `docker info` 对该用户可用：

```bash
sudo usermod -aG docker ascendforge
sudo systemctl restart ascendforge-worker
sudo -u ascendforge docker info
```

> Docker socket 等价于宿主机 root 权限。只应在受控服务器上这样做；不要把它暴露给 Web 进程。

## 3. HTTPS 反向代理

### Caddy

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo editor /etc/caddy/Caddyfile   # 改域名/邮箱
sudo systemctl reload caddy
```

Caddy 自动申请证书。Web 服务只监听 `127.0.0.1:8080`，不要直接暴露公网。

### Nginx

参考 `deploy/nginx.conf`，使用 `proxy_pass http://127.0.0.1:8080;`，并转发 `Host`、`X-Forwarded-*`。AscendForge 不信任 `X-Forwarded-For` 做安全判断。

### 直连局域网

若确实要在私网直接访问，至少：

```dotenv
ASCENDFORGE_HOST=0.0.0.0
ASCENDFORGE_SECURE_COOKIES=false
```

并通过防火墙限制来源。**公网必须 HTTPS**。

## 4. 容器部署

```bash
cp .env.example .env
# 编辑 .env，至少改 SECRET_KEY/SETUP_TOKEN/ADMIN_PASSWORD
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f
```

- `web` 与 `worker` 共享 `ascendforge-data` 卷；删除容器不会删除卷。
- compose 默认 `ASCENDFORGE_EXECUTOR=local`，因为容器内再调 Docker 需要挂载 socket，风险较高。
- 生产建议：**Web/worker 用 systemd 跑在宿主机，worker 用 Docker 执行器**；或者单独一台执行节点。
- 如果坚持在容器内用 Docker 执行器，必须显式挂载 `/var/run/docker.sock` 并接受其 root 等价权限；本项目默认不这么做。

## 5. 备份与恢复

### 5.1 任务档案（用户级）

Web 任务详情 → “导出档案”。包含：

- 任务、attempt、父子关系、配置、预算
- 不可变源码快照 + 每文件 SHA-256
- 生成器/参考实现快照、种子、规格
- 远端提交记录、原始结果、归一化结果
- 事件、模型用量
- `SHA256SUMS`

默认排除：模型 key、CANNJudge Cookie、AscendForge 会话、日志。大体积生成数据默认不包含。

### 5.2 服务器备份（灾备）

```bash
sudo -u ascendforge env $(grep -v '^#' /etc/ascendforge/ascendforge.env | xargs) \
  /opt/ascendforge/.venv/bin/python -m ascendforge backup
```

输出目录：`$DATA_DIR/backups/ascendforge-YYYYmmdd-HHMMSS/`，包含：

- `ascendforge.db`（SQLite backup API，包含 WAL 内容）
- `tasks/`（题目包、快照、生成器）
- `manifest.json`（每文件 SHA-256、SQLite 路径、是否含主密钥）
- `SHA256SUMS`

默认**不包含** `secrets/master.key`。没有主密钥，备份里的模型 key/CANNJudge 会话无法解密。

校验：

```bash
python -m ascendforge verify-backup /var/lib/ascendforge/backups/ascendforge-...
```

恢复：

```bash
sudo systemctl stop ascendforge-web ascendforge-worker
python -m ascendforge restore /path/to/backup --data-dir /var/lib/ascendforge --force
sudo systemctl start ascendforge-web ascendforge-worker
```

恢复后：

1. 确认 `secrets/master.key` 与备份匹配；不匹配时模型/CANNJudge 凭据不可用，需要重新连接。
2. 打开任务详情，检查 unknown 提交；**不会重新发送**，需对账/人工确认。
3. 运行 `verify-backup` 和一次登录/任务创建演练。

> 备份是敏感数据：即使不含主密钥，仍包含源码、题目和评测反馈。请加密存储并限制访问。

## 6. 升级

```bash
sudo systemctl stop ascendforge-worker ascendforge-web
cd /opt/ascendforge
sudo -u ascendforge git pull
sudo -u ascendforge .venv/bin/pip install -r requirements.txt
sudo systemctl start ascendforge-web ascendforge-worker
```

- 数据库 schema 目前是 v1；升级前先备份。
- 升级不会自动重发在途提交；worker 启动会先恢复/对账。
- 回滚：停止服务、恢复备份、切回旧代码。

## 7. 日志与监控

- 日志：`$DATA_DIR/logs/ascendforge.log`，10 MB 轮转，保留 5 份。
- 健康检查：`GET /api/health`（无需登录）。
- Worker 心跳：`GET /api/worker`，或任务详情页右上角状态。
- 关键事件：任务详情“事件”时间线（创建、快照、排队、发送、受理、结果、best、错误、暂停/恢复）。
- 建议监控：
  - web/worker systemd active
  - `/api/health` 200
  - worker 心跳 < 30s
  - 磁盘剩余空间
  - `submission_unknown` / `needs_review` 数量
  - 429/401/5xx 事件

## 8. 故障排查

| 现象 | 检查 |
|---|---|
| 页面能开但任务不动 | `systemctl status ascendforge-worker`；`/api/worker` 心跳 |
| “账号无法连接 judge” | 先在浏览器登录 CANNJudge；确认账号可访问该题目；检查 403/网络 |
| 登录 401 | 会话过期；重新连接平台，任务会 `needs_login`，恢复前先对账 |
| 一直 `needs_review` | 有 unknown 提交；在“提交与反馈”对账/人工确认 |
| 提交被限频 | 查看任务详情的“下一次提交”；不要绕过调度器 |
| 403 提交 | 题目/小组权限或比赛关闭；平台拒绝，不自动重试 |
| 模型 401/429 | 检查中转站 key/额度；任务进入 needs_review/退避 |
| 执行器不可用 | `ASCENDFORGE_EXECUTOR=docker` 时检查 Docker、镜像、权限；或临时 local（弱隔离） |
| 无 NPU | 正常；候选会标 `unavailable/untested`，不是错误 |
| 备份恢复后凭据失效 | 主密钥未包含或与备份不匹配；重新连接平台/模型 |
| 端口冲突 | `ss -lntp`；改 `ASCENDFORGE_PORT` |

## 9. 安全运营

- 立即修改初始密码；不要多人共享管理员。
- 定期轮换模型 key；CANNJudge 会话过期后重新登录。
- 不要把 `data/` 放进公开静态目录或 git。
- 备份与主密钥分开保存；含密钥的备份按明文凭据管理。
- 只开放 443；SSH 限制来源；保持系统与依赖更新。
- 发现异常提交/费用：暂停任务、断开平台、轮换 key、检查事件与审计日志。
