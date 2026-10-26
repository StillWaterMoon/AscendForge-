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


def cmd_codex_run(args) -> int:
    import tempfile

    from .cli_agent.codex import CodexAdapter
    from .experiments import get_attempt, read_snapshot

    settings = _settings(args)
    db = Database(settings.db_path)
    db.init()
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (args.task,))
    if not task:
        print(f"task not found: {args.task}", file=sys.stderr)
        return 1
    attempt_id = task.get("performance_best_attempt_id") or task.get("best_attempt_id")
    if not attempt_id:
        latest = db.query_one(
            "SELECT id FROM attempts WHERE task_id=? AND snapshot_id IS NOT NULL ORDER BY ordinal DESC LIMIT 1",
            (args.task,),
        )
        attempt_id = latest["id"] if latest else None
    attempt = get_attempt(db, attempt_id) if attempt_id else None
    if not attempt or not attempt.get("snapshot_id"):
        print("task has no candidate source snapshot", file=sys.stderr)
        return 1
    bundle = read_snapshot(db, settings, attempt["snapshot_id"])
    root = Path(args.workspace) if args.workspace else Path(tempfile.mkdtemp(prefix="af-codex-"))
    adapter = CodexAdapter(timeout_sec=args.timeout)
    history = db.query(
        "SELECT ordinal,status,hypothesis,normalized_json FROM attempts WHERE task_id=? ORDER BY ordinal DESC LIMIT 12",
        (args.task,),
    )
    task_meta = {
        "task_id": args.task,
        "title": task.get("title"),
        "problem_title": task.get("problem_title"),
        "problem_url": task.get("problem_url"),
        "current_attempt": attempt["id"],
        "attempt_ordinal": attempt["ordinal"],
        "status": task.get("status"),
    }
    workspace = adapter.prepare_workspace(
        root,
        bundle["files"],
        task_meta=task_meta,
        instructions=(
            "This is a local Codex inspection/implementation workspace. "
            "The host will review changed files; this command never submits to CANNJudge.\n"
            f"Recent attempts: {json.dumps([{k: row.get(k) for k in ('ordinal','status','hypothesis')} for row in history], ensure_ascii=False)}\n"
        ),
    )
    prompt = args.prompt or (
        "Read TASK.json and AGENTS.md. Inspect the source files in this workspace and "
        "report a concrete next optimization step. Do not access credentials, the platform database, "
        "or any official judge. Do not modify files unless the operator explicitly asked for changes. "
        "End with a short summary of what you changed or what should change."
    )
    result = adapter.start(
        prompt,
        workspace=workspace,
        output_file=root / "last.txt",
        sandbox=args.sandbox,
        model=args.model,
    )
    changes = adapter.collect_changes(workspace, bundle["files"])
    print(json_dumps({
        "workspace": str(workspace),
        "run_id": result.run_id,
        "session_ref": result.session_ref,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "last_message": result.last_message,
        "usage": result.usage,
        "changed_files": sorted(changes),
        "change_count": len(changes),
        "stderr": result.stderr[-1000:],
        "note": "Local CLI only: changes are NOT saved as a candidate and NOT submitted.",
    }, pretty=True))
    return 0 if result.exit_code == 0 and not result.timed_out else 2


def cmd_codex_loop(args) -> int:
    from .codex_loop import run_codex_agent

    settings = _settings(args)
    result = run_codex_agent(
        settings,
        args.task,
        prompt=args.prompt or "",
        timeout_sec=args.timeout,
        submission_timeout=args.submission_timeout,
        keep_running=args.keep_running,
        lock_token=getattr(args, "lock_token", None),
    )
    print(json_dumps(result, pretty=True))
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
            "cannjudge_connected": bool(db.scalar("SELECT 1 FROM credentials WHERE kind='cannjudge'")),
            "capabilities": {
                "npu": caps.get("npu"),
                "cann": caps.get("cann"),
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

    cli_probe = sub.add_parser("cli-probe", help="探测 Codex / DeepSeek CLI 能力（不启动会话）")
    cli_probe.set_defaults(func=cmd_cli_probe)

    codex_run = sub.add_parser("codex-run", help="在本地工作区运行 Codex（不保存候选、不提交）")
    codex_run.add_argument("--task", required=True)
    codex_run.add_argument("--workspace", help="工作区目录；默认创建临时目录")
    codex_run.add_argument("--prompt", help="覆盖默认的只读检查提示")
    codex_run.add_argument("--model")
    codex_run.add_argument("--sandbox", choices=["read-only", "workspace-write"], default="read-only")
    codex_run.add_argument("--timeout", type=float, default=900)
    codex_run.set_defaults(func=cmd_codex_run)

    codex_loop = sub.add_parser("codex-loop", help="本地 Codex 编辑→快照→评测循环")
    codex_loop.add_argument("--task", required=True)
    codex_loop.add_argument("--prompt", help="额外优化指令")
    codex_loop.add_argument("--timeout", type=float, default=3600, help="单轮 Codex 超时")
    codex_loop.add_argument("--submission-timeout", type=float, default=1200)
    codex_loop.add_argument("--keep-running", action="store_true")
    codex_loop.add_argument("--lock-token", help=argparse.SUPPRESS)
    codex_loop.set_defaults(func=cmd_codex_loop)

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
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
