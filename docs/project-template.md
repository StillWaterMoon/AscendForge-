# 项目模板上传

新建任务时可以选择上传一个 ZIP 项目模板，也可以不传。

## 上传

- 位置：新建任务弹窗“项目模板 ZIP（可选）”
- 上传成功后得到临时 `upload_id`
- 创建任务时把 ZIP 解压到该任务目录，并生成 `template_json`
- Codex 启动时使用这份模板初始化 workspace
- 上传 ZIP 只保留在任务目录，Codex 之后可以在 workspace 中继续修改

## 不上传

不选 ZIP 时：

```text
template_json = {"files": [], "source": "empty"}
config.project_source = "empty"
```

Codex 从空目录开始，需要自己创建源码、构建脚本和入口文件。

## ZIP 限制

- 只接受 ZIP；
- 默认上限由 `ASCENDFORGE_MAX_PACKAGE_MB` 控制（默认 64 MiB）；
- 解压后最大 64 MiB；
- 最多 500 个文件；
- 拒绝绝对路径、`../` 路径和符号链接；
- 文件必须是 UTF-8 文本；
- 如果所有文件位于同一个顶层目录，会自动去掉该目录；
- `__MACOSX/` 和 `.DS_Store` 会被忽略；
- 上传模板中的 `main.asc`、`CMakeLists.txt`、`run.sh`、`sample.py`、
  `reference.py`、`README.md`、`requirements.txt` 固定为只读，不进入
  官方提交载荷；
- 只有根目录的 `kernel.asc`、其他 `.asc` 和 `.h` 作为可编辑提交文件。

解压失败会返回明确错误，不会创建半成品任务。

## 任务配置

```text
config.project_source             upload | empty
config.project_template_sha256    上传 ZIP 的 SHA-256
config.project_template_archive   任务目录中的 ZIP 相对路径
config.project_template_files     导入文件数
```

## 与 Codex 的关系

`codex_loop` 读取 `template_json` 初始化 workspace：

- 有上传：Codex 从用户工程开始；
- 无上传：workspace 只有 `TASK.json` / `AGENTS.md`，源码从 0 开始。

用户上传模板后，平台不再提供 MCP `read_problem_file` 工具；读取草稿由
Codex 自己的文件能力完成。
