"""AscendForge command line (optional management helper; the Web UI is primary)."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .backup import BackupError, create_backup, list_backups, restore_backup, verify_backup
from .config import Settings
from .db import Database
from .execution.capabilities import detect_capabilities
from .logging_setup import configure_logging
from .security import CredentialStore, SecretBox, hash_password, load_master_key
from .util import json_dumps, new_id, now, sha256_file


def _settings(args) -> Settings:
    if getattr(args, "data_dir", None):
        os.environ["ASCENDFORGE_DATA_DIR"] = str(Path(args.data_dir).expanduser())
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.logs_dir)
    return settings


def _prompt_password(label: str = "密码") -> str:
    first = getpass.getpass(f"{label}（至少 10 位）: ")
    second = getpass.getpass(f"重复{label}: ")
    if first != second:
        raise SystemExit("两次输入不一致")
    return first


def cmd_init(args) -> int:
    settings = _settings(args)
    db = Database(settings.db_path)
    db.init()
    box = SecretBox(load_master_key(settings.key_file, os.environ.get("ASCENDFORGE_SECRET_KEY", "")))
    credentials = CredentialStore(db, box)
    count = int(db.scalar("SELECT COUNT(*) FROM users") or 0)
    if count and not args.force:
        print(f"已存在 {count} 个用户；使用 --force 创建另一个管理员。")
        return 0
    username = args.username or input("管理员用户名 [admin]: ").strip() or "admin"
    password = args.password or _prompt_password()
    try:
        hashed = hash_password(password)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    db.execute(
        "INSERT INTO users(id, username, password_hash, created_at) VALUES(?, ?, ?, ?)",
        (new_id("user"), username, hashed, now()),
    )
    print(f"已初始化数据目录：{settings.data_dir}")
    print(f"已创建管理员：{username}")
    print(f"数据库：{settings.db_path}")
    print(f"主密钥：{settings.key_file}（请单独备份并保护）")
    return 0


def cmd_serve(args) -> int:
    settings = _settings(args)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.secure_cookies:
        settings.secure_cookies = True
    from .web import serve

    return serve(settings)


def cmd_worker(args) -> int:
    settings = _settings(args)
    from .worker import Worker

    worker = Worker(settings)
    worker.run_forever()
    return 0


def cmd_parameter_run(args) -> int:
    settings = _settings(args)
    from .parameter_runner import (
        ParameterRunner,
        SchedulerEvaluator,
        SimulatedParameterEvaluator,
    )
    from .web import App

    app = App(settings)
    task = app.tasks.get(args.task)
    experiment = (
        app.db.query_one("SELECT * FROM experiments WHERE id=? AND task_id=?", (args.experiment, args.task))
        if args.experiment
        else app.db.query_one(
            "SELECT * FROM experiments WHERE task_id=? AND status='active' ORDER BY ordinal DESC LIMIT 1",
            (args.task,),
        )
    )
    if not experiment:
        print("没有 active experiment", file=sys.stderr)
        return 1
    if args.resume:
        if task["status"] == "paused":
            app.tasks.resume(args.task)
        elif task["status"] == "pending":
            app.tasks.start(args.task)
        elif task["status"] != "running":
            print(f"任务状态 {task['status']} 不能恢复", file=sys.stderr)
            return 1
    if app.tasks.get(args.task)["status"] != "running":
        print("参数 runner 需要任务处于 running；使用 --resume", file=sys.stderr)
        return 1
    config = dict(task.get("config") or {})
    config["parameter_runner_active"] = True
    app.db.execute(
        "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
        (json_dumps(config), now(), args.task),
    )
    evaluator = SimulatedParameterEvaluator() if args.simulate else SchedulerEvaluator(
        app.db, app.scheduler, args.task, timeout_sec=args.timeout
    )
    try:
        result = ParameterRunner(
            app.db, settings, args.task, experiment["id"], evaluator
        ).run()
    finally:
        config.pop("parameter_runner_active", None)
        app.db.execute(
            "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
            (json_dumps(config), now(), args.task),
        )
        if not args.keep_running and app.tasks.get(args.task)["status"] == "running":
            try:
                app.tasks.pause(args.task, reason="parameter_runner_finished")
            except Exception:
                pass
    print(json_dumps(result, pretty=True))
    return 0 if result.get("status") == "completed" else 2


def cmd_backup(args) -> int:
    settings = _settings(args)
    db = Database(settings.db_path)
    db.init()
    result = create_backup(
        settings,
        db,
        include_key=args.include_key,
        include_problem_packages=not args.no_packages,
    )
    print(json_dumps(result, pretty=True))
    return 0


def cmd_verify_backup(args) -> int:
    result = verify_backup(args.path)
    print(json_dumps(result, pretty=True))
    return 0 if result["ok"] else 2


def cmd_restore(args) -> int:
    settings = _settings(args)
    result = restore_backup(args.path, settings.data_dir, force=args.force)
    print(json_dumps(result, pretty=True))
    print("提示：恢复后请检查主密钥是否匹配，并同时启动 web 与 worker。")
    return 0


def cmd_user(args) -> int:
    settings = _settings(args)
    db = Database(settings.db_path)
    db.init()
    if args.user_action == "list":
        rows = db.query("SELECT id, username, created_at, disabled FROM users ORDER BY created_at")
        print(json_dumps(rows, pretty=True))
        return 0
    if args.user_action == "create":
        username = args.username or input("用户名: ").strip()
        password = args.password or _prompt_password()
        if not username:
            print("用户名不能为空", file=sys.stderr)
            return 1
        try:
            hashed = hash_password(password)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        try:
            db.execute(
                "INSERT INTO users(id, username, password_hash, created_at) VALUES(?, ?, ?, ?)",
                (new_id("user"), username, hashed, now()),
            )
        except Exception as exc:
            print(f"创建失败：{exc}", file=sys.stderr)
            return 1
        print(f"已创建用户：{username}")
        return 0
    if args.user_action == "passwd":
        username = args.username or input("用户名: ").strip()
        row = db.query_one("SELECT id FROM users WHERE username=?", (username,))
        if not row:
            print("用户不存在", file=sys.stderr)
            return 1
        password = args.password or _prompt_password()
        try:
            hashed = hash_password(password)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, row["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        print(f"已更新 {username} 的密码并撤销其登录会话")
        return 0
    print("未知用户操作", file=sys.stderr)
    return 1


def cmd_cli_probe(args) -> int:
    from .cli_agent.probe import probe_all

    print(json_dumps(probe_all(), pretty=True))
    return 0


def cmd_status(args) -> int:
    settings = _settings(args)
    db = Database(settings.db_path)
    db.init()
    counts = {row["status"]: row["count"] for row in db.query("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status")}
    heartbeat = db.setting_get("worker_heartbeat", None)
    caps = detect_capabilities(settings)
    print(json_dumps(
        {
            "version": __version__,
            "data_dir": str(settings.data_dir),
            "database": str(settings.db_path),
            "tasks": counts,
            "worker": heartbeat,
            "model_configured": bool(db.scalar("SELECT 1 FROM credentials WHERE kind='model'")),
            "cannjudge_connected": bool(db.scalar("SELECT 1 FROM credentials WHERE kind='cannjudge'")),
            "capabilities": {
                "npu": caps.get("npu"),
                "cann": caps.get("cann"),
                "docker": caps.get("docker"),
                "executor": caps.get("executor"),
                "candidate_execution": caps.get("candidate_execution"),
            },
        },
        pretty=True,
    ))
    return 0


def cmd_probe(args) -> int:
    """Safe public reachability probe; never logs credentials."""
    url = args.url
    request = urllib.request.Request(url, headers={"User-Agent": f"AscendForge/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(200_000)
            status = response.status
            headers = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        body = exc.read(2000)
        status = exc.code
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
    except Exception as exc:
        print(json_dumps({"url": url, "reachable": False, "error": str(exc)}, pretty=True))
        return 1
    import hashlib

    title = ""
    text = body.decode("utf-8", errors="replace")
    if "<title>" in text.lower():
        try:
            title = text.lower().split("<title>", 1)[1].split("</title>", 1)[0]
        except IndexError:
            title = ""
    print(json_dumps(
        {
            "url": url,
            "reachable": True,
            "status": status,
            "content_type": headers.get("content-type"),
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "title": title[:200],
            "note": "公开页面探测，不代表登录、题目权限或提交接口已验证。",
        },
        pretty=True,
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ascendforge", description="AscendForge（昇腾工坊）管理工具；日常操作请使用 Web 面板。")
    parser.add_argument("--data-dir", help="覆盖 ASCENDFORGE_DATA_DIR")
    parser.add_argument("--version", action="version", version=f"AscendForge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化数据目录并创建管理员")
    init.add_argument("--username")
    init.add_argument("--password")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    serve = sub.add_parser("serve", help="启动 Web/API 服务（不包含 worker）")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--secure-cookies", action="store_true")
    serve.set_defaults(func=cmd_serve)
    web = sub.add_parser("web", help="serve 的别名")
    web.add_argument("--host")
    web.add_argument("--port", type=int)
    web.add_argument("--secure-cookies", action="store_true")
    web.set_defaults(func=cmd_serve)

    worker = sub.add_parser("worker", help="启动后台优化 worker")
    worker.set_defaults(func=cmd_worker)

    parameter = sub.add_parser("parameter-run", help="运行确定性参数实验 runner")
    parameter.add_argument("--task", required=True)
    parameter.add_argument("--experiment", help="默认使用 active experiment")
    parameter.add_argument("--simulate", action="store_true", help="使用模拟评测，不提交远端")
    parameter.add_argument("--resume", action="store_true", help="运行前恢复 paused 任务")
    parameter.add_argument("--keep-running", action="store_true", help="结束后不暂停任务")
    parameter.add_argument("--timeout", type=float, default=1800, help="每个配置等待远端结果的秒数")
    parameter.set_defaults(func=cmd_parameter_run)

    cli_probe = sub.add_parser("cli-probe", help="探测 Codex / DeepSeek CLI 能力（不启动会话）")
    cli_probe.set_defaults(func=cmd_cli_probe)

    backup = sub.add_parser("backup", help="创建服务器备份")
    backup.add_argument("--include-key", action="store_true", help="包含主密钥（高风险）")
    backup.add_argument("--no-packages", action="store_true", help="排除题目 ZIP 包")
    backup.set_defaults(func=cmd_backup)

    verify = sub.add_parser("verify-backup", help="校验备份完整性")
    verify.add_argument("path")
    verify.set_defaults(func=cmd_verify_backup)

    restore = sub.add_parser("restore", help="从备份恢复")
    restore.add_argument("path")
    restore.add_argument("--force", action="store_true")
    restore.set_defaults(func=cmd_restore)

    user = sub.add_parser("user", help="用户管理")
    user_sub = user.add_subparsers(dest="user_action", required=True)
    user_list = user_sub.add_parser("list", help="列出用户")
    user_list.set_defaults(func=cmd_user)
    user_create = user_sub.add_parser("create", help="创建用户")
    user_create.add_argument("--username")
    user_create.add_argument("--password")
    user_create.set_defaults(func=cmd_user)
    user_passwd = user_sub.add_parser("passwd", help="重置密码")
    user_passwd.add_argument("--username")
    user_passwd.add_argument("--password")
    user_passwd.set_defaults(func=cmd_user)

    status = sub.add_parser("status", help="查看服务器状态")
    status.set_defaults(func=cmd_status)

    probe = sub.add_parser("probe", help="探测公开页面可达性（不涉及登录）")
    probe.add_argument("--url", default="https://cannjudge.cn/home")
    probe.set_defaults(func=cmd_probe)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130
    except (BackupError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
