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


def parse_ssh_connect_command(command: str, connection_password: str) -> dict:
    """Parse the provider's one-line SSH command + separate connection password.

    Expected shape:
        ssh -J user:password@jump:port root@target

    The connection password is the target account password; the password inside
    ``-J`` belongs to the jump host.  The command is only parsed in memory; it is
    not persisted or logged.
    """
    import shlex

    try:
        tokens = shlex.split(str(command or ""))
    except ValueError as exc:
        raise RemoteError(f"SSH 命令无法解析：{exc}") from None
    if not tokens or tokens[0].lower() != "ssh":
        raise RemoteError("连接命令必须以 ssh 开头")
    jump_spec = None
    target_port = None
    target_token = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-J":
            if index + 1 >= len(tokens):
                raise RemoteError("-J 后面缺少跳板机")
            jump_spec = tokens[index + 1]
            index += 2
            continue
        if token == "-p":
            if index + 1 >= len(tokens):
                raise RemoteError("-p 后面缺少端口")
            try:
                target_port = int(tokens[index + 1])
            except ValueError:
                raise RemoteError("目标端口必须是数字") from None
            index += 2
            continue
        if token in ("-o", "-i", "-l", "-F"):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        target_token = token
        index += 1
    if not target_token:
        raise RemoteError("命令里没有目标服务器")
    if not connection_password:
        raise RemoteError("连接密码不能为空")

    def split_host_port(value: str) -> tuple[str, int | None]:
        if value.startswith("["):
            # IPv6 form [addr]:port
            end = value.find("]")
            if end < 0:
                return value, None
            host = value[1:end]
            rest = value[end + 1 :]
            if rest.startswith(":") and rest[1:].isdigit():
                return host, int(rest[1:])
            return host, None
        if value.count(":") == 1:
            host, maybe_port = value.rsplit(":", 1)
            if maybe_port.isdigit():
                return host, int(maybe_port)
        return value, None

    def split_user(value: str) -> tuple[str | None, str]:
        if "@" in value:
            return tuple(value.rsplit("@", 1))  # type: ignore[return-value]
        return None, value

    jump_user = None
    jump_host = jump_port = None
    if jump_spec:
        jump_user, jump_hostport = split_user(jump_spec)
        # The provider uses a username token of the form
        # ``jt_xxx:<token>``.  It is ONE username, not user:password.
        # OpenSSH passes the colon through to the jump host verbatim.
        jump_host, jump_port = split_host_port(jump_hostport)
        if not jump_host or not jump_user:
            raise RemoteError("跳板机格式应为 user@host:port")
        if jump_port is None:
            jump_port = 22

    target_user, target_hostport = split_user(target_token)
    target_host, embedded_port = split_host_port(target_hostport)
    if not target_host:
        raise RemoteError("目标服务器地址为空")
    if embedded_port is not None:
        target_port = embedded_port
    if target_port is None:
        target_port = 22
    if not 1 <= target_port <= 65535:
        raise RemoteError("目标端口必须在 1..65535")
    if not target_user:
        target_user = "root"
    return {
        "host": target_host,
        "port": target_port,
        "username": target_user,
        "auth_type": "password",
        "password": connection_password,
        "remote_root": "/tmp/ascendforge",
        "jump_host": jump_host,
        "jump_port": jump_port,
        "jump_username": jump_user,
        "jump_auth_type": "none" if jump_host else None,
        "jump_key_path": None,
        "jump_password": None,
    }


def close_ssh_client(client) -> None:
    if not client:
        return
    jump = getattr(client, "_jump_client", None)
    try:
        client.close()
    except Exception:
        pass
    if jump:
        try:
            jump.close()
        except Exception:
            pass


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
        "jump_host": row.get("jump_host"),
        "jump_port": row.get("jump_port"),
        "jump_username": row.get("jump_username"),
        "jump_auth_type": row.get("jump_auth_type"),
        "jump_key_path": row.get("jump_key_path"),
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
        jump_host = str(payload.get("jump_host") or "").strip() or None
        jump_username = str(payload.get("jump_username") or "").strip() or None
        jump_auth_type = str(payload.get("jump_auth_type") or "password").strip()
        jump_key_path = str(payload.get("jump_key_path") or "").strip() or None
        try:
            jump_port = int(payload.get("jump_port") or 22) if jump_host else None
        except (TypeError, ValueError):
            raise RemoteError("跳板机端口必须是数字") from None
        if jump_host:
            if not jump_username:
                raise RemoteError("跳板机用户名不能为空")
            if jump_port is None or not 1 <= jump_port <= 65535:
                raise RemoteError("跳板机端口必须在 1..65535")
            if jump_auth_type not in ("none", "key", "password"):
                raise RemoteError("跳板机认证方式只能是 none、key 或 password")
            if jump_auth_type == "key" and not jump_key_path:
                raise RemoteError("跳板机 key 认证需要填写私钥路径")
        password = payload.get("password")
        passphrase = payload.get("passphrase")
        jump_password = payload.get("jump_password")
        jump_passphrase = payload.get("jump_passphrase")
        secret = self.secret()
        if isinstance(password, str) and password:
            secret["password"] = password
        if isinstance(passphrase, str) and passphrase:
            secret["passphrase"] = passphrase
        if isinstance(jump_password, str) and jump_password:
            secret["jump_password"] = jump_password
        if isinstance(jump_passphrase, str) and jump_passphrase:
            secret["jump_passphrase"] = jump_passphrase
        if auth_type == "password" and not secret.get("password"):
            raise RemoteError("password 认证需要填写密码")
        if auth_type == "key" and not key_path:
            raise RemoteError("key 认证需要填写 key_path")
        if jump_host and jump_auth_type == "password" and not secret.get("jump_password"):
            raise RemoteError("跳板机密码认证需要填写密码")
        if secret:
            CredentialStore(self.db, self.box).put(REMOTE_SECRET_KIND, secret)
        current = now()
        self.db.execute(
            "INSERT INTO remote_servers(id, host, port, username, auth_type, key_path, "
            "jump_host, jump_port, jump_username, jump_auth_type, jump_key_path, "
            "remote_root, status, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'disconnected', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET host=excluded.host, port=excluded.port, "
            "username=excluded.username, auth_type=excluded.auth_type, key_path=excluded.key_path, "
            "jump_host=excluded.jump_host, jump_port=excluded.jump_port, "
            "jump_username=excluded.jump_username, jump_auth_type=excluded.jump_auth_type, "
            "jump_key_path=excluded.jump_key_path, "
            "remote_root=excluded.remote_root, status='disconnected', pid=NULL, bridge_port=NULL, "
            "bridge_token=NULL, last_error=NULL, updated_at=excluded.updated_at",
            (
                REMOTE_ID, host, port, username, auth_type, key_path,
                jump_host, jump_port, jump_username, jump_auth_type, jump_key_path,
                remote_root, current, current,
            ),
        )
        return self.public()

    def connect_client(self, *, timeout: float = 15.0):
        row = self.get()
        if not row:
            raise RemoteError("尚未配置远程服务器")
        secret = self.secret()
        paramiko = _paramiko()

        def password_client(
            host: str,
            port: int,
            username: str,
            password: str,
            sock=None,
        ):
            """Password or keyboard-interactive auth.

            Some jump gateways expose the one-time credential only through
            keyboard-interactive auth and close the transport after a plain
            password attempt.  Try keyboard-interactive first; if the server
            advertises that it is unsupported, fall back to password on the
            same transport.
            """
            raw_socket = sock
            if raw_socket is None:
                raw_socket = socket.create_connection((host, int(port)), timeout=timeout)
            transport = paramiko.Transport(raw_socket)
            try:
                transport.start_client(timeout=timeout)
                transport.set_keepalive(30)

                def answer(title, instructions, prompts):
                    return [password for _ in prompts]

                try:
                    transport.auth_interactive_dumb(username, answer)
                except Exception as interactive_exc:
                    if not transport.is_active():
                        raise RemoteError(
                            "服务器在认证阶段关闭了 SSH 会话；密码可能已过期，或需要重新获取连接命令"
                        ) from interactive_exc
                    try:
                        transport.auth_password(username, password)
                    except Exception as password_exc:
                        raise RemoteError(
                            "密码和 keyboard-interactive 认证均失败；请确认跳板机/目标机密码，或重新获取连接命令"
                        ) from password_exc
                if not transport.is_authenticated():
                    raise RemoteError("SSH 认证未完成")
                client = paramiko.SSHClient()
                client._transport = transport
                return client
            except Exception:
                try:
                    transport.close()
                except Exception:
                    pass
                raise

        def none_client(host: str, port: int, username: str, sock=None):
            """Provider jump token is carried in the username; no password auth."""
            raw_socket = sock
            if raw_socket is None:
                raw_socket = socket.create_connection((host, int(port)), timeout=timeout)
            transport = paramiko.Transport(raw_socket)
            try:
                transport.start_client(timeout=timeout)
                transport.set_keepalive(30)
                allowed = transport.auth_none(username)
                if not transport.is_authenticated():
                    raise RemoteError(
                        f"跳板机用户名 token 认证失败；允许的认证方式：{allowed or '未知'}"
                    )
                client = paramiko.SSHClient()
                client._transport = transport
                return client
            except Exception:
                try:
                    transport.close()
                except Exception:
                    pass
                raise

        def make_client(
            host: str,
            port: int,
            username: str,
            auth_type: str,
            key_path: str | None,
            password: str | None,
            passphrase: str | None,
            sock=None,
        ):
            if auth_type == "none":
                return none_client(host, port, username, sock=sock)
            if auth_type == "password":
                return password_client(host, port, username, password or "", sock=sock)
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs: dict[str, Any] = {
                "hostname": host,
                "port": int(port),
                "username": username,
                "timeout": timeout,
                "banner_timeout": timeout,
                "auth_timeout": timeout,
                "allow_agent": True,
                "look_for_keys": True,
                "key_filename": key_path,
            }
            if sock is not None:
                kwargs["sock"] = sock
            if passphrase:
                kwargs["passphrase"] = passphrase
            client.connect(**kwargs)
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(30)
            return client

        jump_client = None
        channel = None
        if row.get("jump_host"):
            try:
                jump_client = make_client(
                    row["jump_host"],
                    int(row["jump_port"]),
                    row["jump_username"],
                    row["jump_auth_type"],
                    row.get("jump_key_path"),
                    secret.get("jump_password"),
                    secret.get("jump_passphrase"),
                )
                transport = jump_client.get_transport()
                if transport is None:
                    raise RemoteError("跳板机 SSH transport 不可用")
                channel = transport.open_channel(
                    "direct-tcpip",
                    (row["host"], int(row["port"])),
                    ("127.0.0.1", 0),
                )
            except Exception as exc:
                if jump_client:
                    close_ssh_client(jump_client)
                if "Administratively prohibited" in str(exc):
                    raise RemoteError(
                        "跳板机拒绝端口转发：官方连接 token 已过期或没有转发权限；请重新获取 SSH 直连命令"
                    ) from exc
                if isinstance(exc, RemoteError):
                    raise RemoteError(f"跳板机连接失败：{exc}") from exc
                raise RemoteError(f"跳板机连接失败：{type(exc).__name__}: {exc}") from exc
        try:
            client = make_client(
                row["host"],
                int(row["port"]),
                row["username"],
                row["auth_type"],
                row.get("key_path"),
                secret.get("password"),
                secret.get("passphrase"),
                sock=channel,
            )
        except Exception as exc:
            if jump_client:
                close_ssh_client(jump_client)
            if isinstance(exc, RemoteError):
                raise RemoteError(f"目标机连接失败：{exc}") from exc
            raise RemoteError(f"目标机连接失败：{type(exc).__name__}: {exc}") from exc
        client._jump_client = jump_client
        client._jump_channel = channel
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
            close_ssh_client(self.client)
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
