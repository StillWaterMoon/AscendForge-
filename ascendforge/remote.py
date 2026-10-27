"""Persistent SSH bridge for local Codex remote execution.

Codex still runs on the AscendForge host.  When a remote server is connected,
the local Codex process gets an additional ``remote_exec`` tool.  That tool
talks to this host-side bridge over loopback; the bridge owns the SSH
connection and never exposes SSH credentials to Codex or the browser.

The bridge deliberately executes arbitrary shell commands requested by the
authenticated local host process.  It is a workspace execution channel, not a
general-purpose public SSH proxy.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .security import CredentialStore, SecretBox
from .util import new_id, now

REMOTE_ID = "default"
REMOTE_SECRET_KIND = "remote-server"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_SYNC_BYTES = 32 * 1024 * 1024
MAX_OUTPUT = 512 * 1024
WORKSPACE_SKIP = {
    "TASK.json",
    "AGENTS.md",
    "codex-tools.json",
    "codex-prompt.txt",
    "last.txt",
    "specifications.json",
    "evidence.md",
}


class RemoteError(RuntimeError):
    pass


def _paramiko():
    try:
        import paramiko  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency is optional at runtime
        raise RemoteError("paramiko is not installed; install requirements.txt") from exc
    return paramiko


def _safe_task_dir(root: str, task_id: str) -> str:
    if not TASK_ID_RE.match(task_id):
        raise RemoteError("invalid task id")
    return str(Path(root) / task_id)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(int(pid)) in result.stdout and "No tasks" not in result.stdout
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _public(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "host": row["host"],
        "port": row["port"],
        "username": row["username"],
        "auth_type": row["auth_type"],
        "key_path": row.get("key_path"),
        "remote_root": row["remote_root"],
        "status": row["status"],
        "pid": row.get("pid"),
        "last_error": row.get("last_error"),
        "last_seen": row.get("last_seen"),
    }


class RemoteStore:
    def __init__(self, db: Database, box: SecretBox):
        self.db = db
        self.box = box

    def get(self) -> dict | None:
        return self.db.query_one("SELECT * FROM remote_servers WHERE id=?", (REMOTE_ID,))

    def public(self) -> dict:
        row = self.get()
        if not row:
            return {"status": "disconnected", "configured": False}
        data = _public(row) or {}
        if data.get("status") == "connected" and not _pid_alive(data.get("pid")):
            self.db.execute(
                "UPDATE remote_servers SET status='disconnected', pid=NULL, bridge_port=NULL, "
                "bridge_token=NULL, updated_at=? WHERE id=?",
                (now(), REMOTE_ID),
            )
            data["status"] = "disconnected"
            data["pid"] = None
        data["configured"] = True
        return data

    def secret(self) -> dict:
        return CredentialStore(self.db, self.box).get(REMOTE_SECRET_KIND) or {}

    def save(self, payload: dict) -> dict:
        host = str(payload.get("host") or "").strip()
        username = str(payload.get("username") or "").strip()
        auth_type = str(payload.get("auth_type") or "key").strip()
        remote_root = str(payload.get("remote_root") or "/tmp/ascendforge").strip()
        try:
            port = int(payload.get("port") or 22)
        except (TypeError, ValueError):
            raise RemoteError("端口必须是数字") from None
        if not 1 <= port <= 65535:
            raise RemoteError("端口必须在 1..65535")
        if not host or not username:
            raise RemoteError("host 和 username 不能为空")
        if auth_type not in ("key", "password"):
            raise RemoteError("auth_type 只能是 key 或 password")
        if not remote_root.startswith("/") or remote_root == "/":
            raise RemoteError("remote_root 必须是绝对路径，且不能是根目录")
        key_path = str(payload.get("key_path") or "").strip() or None
        password = payload.get("password")
        passphrase = payload.get("passphrase")
        secret = self.secret()
        if isinstance(password, str) and password:
            secret["password"] = password
        if isinstance(passphrase, str) and passphrase:
            secret["passphrase"] = passphrase
        if auth_type == "password" and not secret.get("password"):
            raise RemoteError("password 认证需要填写密码")
        if auth_type == "key" and not key_path:
            raise RemoteError("key 认证需要填写 key_path")
        if secret:
            CredentialStore(self.db, self.box).put(REMOTE_SECRET_KIND, secret)
        current = now()
        self.db.execute(
            "INSERT INTO remote_servers(id, host, port, username, auth_type, key_path, "
            "remote_root, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'disconnected', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET host=excluded.host, port=excluded.port, "
            "username=excluded.username, auth_type=excluded.auth_type, key_path=excluded.key_path, "
            "remote_root=excluded.remote_root, status='disconnected', pid=NULL, bridge_port=NULL, "
            "bridge_token=NULL, last_error=NULL, updated_at=excluded.updated_at",
            (REMOTE_ID, host, port, username, auth_type, key_path, remote_root, current, current),
        )
        return self.public()

    def connect_client(self, *, timeout: float = 15.0):
        row = self.get()
        if not row:
            raise RemoteError("尚未配置远程服务器")
        secret = self.secret()
        paramiko = _paramiko()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = {
            "hostname": row["host"],
            "port": int(row["port"]),
            "username": row["username"],
            "timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": timeout,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if row["auth_type"] == "password":
            kwargs["password"] = secret.get("password") or ""
        else:
            kwargs["key_filename"] = row.get("key_path")
            if secret.get("passphrase"):
                kwargs["passphrase"] = secret["passphrase"]
        client.connect(**kwargs)
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        return client

    def set_status(self, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        self.db.execute(
            f"UPDATE remote_servers SET {assignments} WHERE id=?",
            (*fields.values(), REMOTE_ID),
        )


class RemoteBridge:
    """Long-lived local bridge owning one SSH connection."""

    def __init__(self, settings: Settings, db: Database, box: SecretBox):
        self.settings = settings
        self.db = db
        self.store = RemoteStore(db, box)
        self.client = None
        self.server: socket.socket | None = None
        self.token = ""
        self.port = 0
        self.lock = threading.Lock()
        self.stopping = threading.Event()

    def start(self) -> None:
        self.client = self.store.connect_client()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.server.settimeout(1.0)
        self.port = int(self.server.getsockname()[1])
        self.token = new_id("remotetoken")
        self.store.set_status(
            status="connected",
            bridge_port=self.port,
            bridge_token=self.token,
            pid=os.getpid(),
            last_error=None,
            last_seen=now(),
        )

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self.stopping.is_set():
                try:
                    conn, _ = self.server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            self.close()

    def close(self) -> None:
        self.stopping.set()
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
            self.server = None
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        try:
            self.store.set_status(
                status="disconnected",
                pid=None,
                bridge_port=None,
                bridge_token=None,
                last_seen=now(),
            )
        except Exception:
            pass

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(30)
            file = conn.makefile("rwb")
            raw = file.readline(64 * 1024 * 1024)
            if not raw:
                return
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict) or request.get("token") != self.token:
                self._reply(file, {"ok": False, "error": "invalid bridge token"})
                return
            action = str(request.get("action") or "")
            if action == "ping":
                self._reply(file, {"ok": True, "pid": os.getpid()})
            elif action == "exec":
                with self.lock:
                    result = self._exec(request)
                self._reply(file, result)
            else:
                self._reply(file, {"ok": False, "error": f"unknown action: {action}"})
        except Exception as exc:
            try:
                self._reply(conn.makefile("rwb"), {"ok": False, "error": str(exc)[:1000]})
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _reply(file, payload: dict) -> None:
        file.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        file.flush()

    def _exec(self, request: dict) -> dict:
        task_id = str(request.get("task_id") or "")
        command = str(request.get("command") or "")
        if not command.strip():
            return {"ok": False, "error": "command 不能为空"}
        remote_dir = _safe_task_dir(str(self.store.get()["remote_root"]), task_id)
        try:
            timeout = max(1, min(int(request.get("timeout") or 600), 3600))
        except (TypeError, ValueError):
            timeout = 600
        files = request.get("files")
        if isinstance(files, dict):
            self._upload(remote_dir, files)
        quoted = shlex.quote(remote_dir)
        wrapped = f"cd {quoted} && {command}"
        assert self.client is not None
        stdin, stdout, stderr = self.client.exec_command(wrapped, timeout=timeout, get_pty=False)
        try:
            stdin.close()
        except Exception:
            pass
        out = stdout.read(MAX_OUTPUT + 1).decode("utf-8", errors="replace")
        err = stderr.read(MAX_OUTPUT + 1).decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        self.store.set_status(last_seen=now())
        return {
            "ok": True,
            "exit_code": code,
            "stdout": out[:MAX_OUTPUT],
            "stderr": err[:MAX_OUTPUT],
            "truncated": len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT,
            "remote_cwd": remote_dir,
        }

    def _upload(self, remote_dir: str, files: dict) -> None:
        cleaned: dict[str, str] = {}
        total = 0
        for path, content in files.items():
            if not isinstance(path, str) or not isinstance(content, str):
                continue
            rel = path.replace("\\", "/").lstrip("/")
            if not rel or ".." in rel.split("/"):
                continue
            total += len(content.encode("utf-8"))
            if total > MAX_SYNC_BYTES:
                raise RemoteError("workspace 同步超过 32 MiB")
            cleaned[rel] = content
        assert self.client is not None
        quoted = shlex.quote(remote_dir)
        _, stdout, stderr = self.client.exec_command(
            f"rm -rf {quoted} && mkdir -p {quoted}", timeout=30, get_pty=False
        )
        stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", errors="replace")
        if err.strip():
            raise RemoteError(f"remote workspace prepare failed: {err[:500]}")
        sftp = self.client.open_sftp()
        try:
            for rel, content in cleaned.items():
                target = f"{remote_dir}/{rel}"
                parent = target.rsplit("/", 1)[0]
                if not parent.startswith(remote_dir):
                    continue
                self._mkdirs(sftp, parent)
                with sftp.file(target, "wb") as handle:
                    handle.write(content.encode("utf-8"))
        finally:
            sftp.close()

    @staticmethod
    def _mkdirs(sftp, path: str) -> None:
        parts = path.strip("/").split("/")
        current = ""
        for part in parts:
            current += "/" + part
            try:
                sftp.stat(current)
            except OSError:
                try:
                    sftp.mkdir(current)
                except OSError:
                    pass


class RemoteBridgeClient:
    def __init__(self, db: Database):
        self.db = db

    @classmethod
    def available(cls, db: Database) -> bool:
        row = db.query_one("SELECT status, pid FROM remote_servers WHERE id=?", (REMOTE_ID,))
        return bool(row and row["status"] == "connected" and _pid_alive(row.get("pid")))

    def request(self, payload: dict, *, timeout: float = 120.0) -> dict:
        row = self.db.query_one("SELECT * FROM remote_servers WHERE id=?", (REMOTE_ID,))
        if not row or row["status"] != "connected" or not row.get("bridge_port"):
            raise RemoteError("远程服务器未连接")
        token = row.get("bridge_token") or ""
        request = {"token": token, **payload}
        try:
            with socket.create_connection(("127.0.0.1", int(row["bridge_port"])), timeout=10) as conn:
                conn.settimeout(timeout)
                file = conn.makefile("rwb")
                file.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
                file.flush()
                raw = file.readline(8 * 1024 * 1024)
        except OSError as exc:
            raise RemoteError(f"远程执行桥不可达: {exc}") from exc
        if not raw:
            raise RemoteError("远程执行桥没有返回结果")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise RemoteError("远程执行桥返回格式错误")
        return result

    def exec_workspace(self, workspace: Path, task_id: str, command: str, timeout: int = 600) -> dict:
        files = self._read_workspace(workspace)
        return self.request(
            {
                "action": "exec",
                "task_id": task_id,
                "command": command,
                "timeout": timeout,
                "files": files,
            },
            timeout=timeout + 30,
        )

    @staticmethod
    def _read_workspace(workspace: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in sorted(Path(workspace).rglob("*")):
            if not path.is_file():
                continue
            if path.name in WORKSPACE_SKIP or ".ascendforge" in path.parts:
                continue
            rel = str(path.relative_to(workspace)).replace("\\", "/")
            try:
                result[rel] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        return result


def start_bridge_process(settings: Settings) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(settings.root)
    env["ASCENDFORGE_DATA_DIR"] = str(settings.data_dir)
    log_path = settings.logs_dir / "remote-bridge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")
    command = [sys.executable, "-m", "ascendforge", "remote-bridge"]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(settings.root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=flags,
        )
    finally:
        handle.close()
    return process


def stop_bridge_process(pid: int | None) -> None:
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.kill(int(pid), 15)
    except Exception:
        pass
