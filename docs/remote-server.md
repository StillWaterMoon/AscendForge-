# 远程服务器通道

## 目标

Codex 仍然运行在 AscendForge 宿主机上，但可以通过一条受控通道在远端服务器执行命令：

```text
本地 Codex
  → remote_exec 动态工具
  → 本地 RemoteBridge 进程
  → SSH
  → 远端工作目录
```

远端不需要安装 Codex，也不需要安装 AscendForge。远端只需要 SSH、一个可写的远程目录和可用的 shell/工具链。

## 连接入口

登录后进入 **连接与设置**，在 **远程服务器** 卡片中填写。

如果官方 NPU 环境给的是“SSH 直连”弹窗，直接把完整命令粘贴到：

```text
官方 SSH 连接命令
连接密码
```

点“解析并填充”。例如：

```text
ssh -J jt_xxx:jump_password@113.47.8.48:2234 root@179.62.58.131
```

会被拆成目标机、跳板机、跳板机密码，连接密码填到目标机密码。

也可以手动填写：

- 主机
- SSH 端口
- 用户名
- 认证方式：SSH key / 密码
- 私钥路径
- 远程工作目录
- 密码（仅密码认证，留空保持旧值）
- 跳板机（可选）：主机、端口、用户名、认证方式、私钥、密码

跳板机对应 OpenSSH 的：

```bash
ssh -J jump_user@jump_host:jump_port root@target
```

应用内不解析 `user:password@host:port` 这种字符串；跳板机密码填在单独的“跳板机密码”字段。

例如：

```bash
ssh -J jump_user:jump_password@113.47.8.48:2234 root@179.62.58.131
```

网页填写：

```text
主机                179.62.58.131
SSH 端口            22
用户名              root
认证方式            按目标机实际的 key/密码
跳板机主机          113.47.8.48
跳板机端口          2234
跳板机用户名        jump_user
跳板机认证          密码
跳板机密码          jump_password
远程工作目录        /tmp/ascendforge
```

“测试连接”只做 SSH 握手和 `true`；“连接”启动常驻 `remote-bridge` 进程并保持 SSH 连接。

## remote_exec

连接成功且任务启动/恢复时，Codex 会额外获得：

```text
remote_exec(command, timeout?)
```

调用时宿主：

1. 读取当前本地 workspace 源码；
2. 通过桥删除并重建远端 `<remote_root>/<task_id>`；
3. 上传源码；
4. 在远端目录执行 shell 命令；
5. 返回 `exit_code/stdout/stderr`。

因此 Codex 可以用它做：

```bash
cmake -S . -B build && cmake --build build
./build/...
npu-smi info
python3 tools/check.py
```

## 边界

- 三个官方平台工具仍然是 `read_problem`、`submit_official`、`read_official_evaluation`。
- `remote_exec` 是执行通道，不是第四个平台工具。
- 官方提交仍由本地宿主 Scheduler 完成。
- Codex 看不到 SSH 密码或私钥内容，也不能直接访问本地数据库。
- 远程 workspace 每次调用都从当前本地 workspace 同步；远端生成的文件不会自动回传。
- 密码用 SecretBox 加密保存；桥令牌只保存在本地数据库，绑定 `127.0.0.1`。
- 当前 SSH host key 使用 `AutoAddPolicy`；生产环境应改为固定 known_hosts 校验。

## 限制

这不是“远程 Codex”。Codex 的模型、thread、文件编辑和平台工具都在本地；远程只负责命令执行。

如果以后要在远端运行 Codex，需要把整个 AscendForge + Codex 部署到远端，而不是使用本通道。
