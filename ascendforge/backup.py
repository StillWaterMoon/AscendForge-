"""Server-side backup, verification and restore.

A task archive is *not* a disaster-recovery backup: credentials, the master key
and global settings are deliberately handled separately.  A full backup contains
the SQLite database and every referenced experiment file; the master key is only
included when explicitly requested, because a backup plus key is equivalent to
all credentials in plaintext.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Settings
from .db import Database
from .util import canonical_json, json_dumps, now, sha256_file

BACKUP_SCHEMA = "ascendforge-backup/1"


class BackupError(RuntimeError):
    pass


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def create_backup(
    settings: Settings,
    db: Database,
    *,
    include_key: bool = False,
    include_problem_packages: bool = True,
) -> dict:
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    directory = settings.backups_dir / f"ascendforge-{_stamp()}"
    suffix = 1
    while directory.exists():
        directory = settings.backups_dir / f"ascendforge-{_stamp()}-{suffix}"
        suffix += 1
    directory.mkdir(parents=True)
    files: list[dict] = []

    def add_file(path: Path, rel: str) -> None:
        if not path.is_file():
            return
        files.append({"path": rel, "sha256": sha256_file(path), "bytes": path.stat().st_size})

    db_target = directory / "ascendforge.db"
    db.backup_to(db_target)
    add_file(db_target, "ascendforge.db")

    if settings.tasks_dir.is_dir():
        target_tasks = directory / "tasks"
        for source in sorted(settings.tasks_dir.rglob("*")):
            if not source.is_file():
                continue
            rel = source.relative_to(settings.data_dir).as_posix()
            if not include_problem_packages and rel.endswith("problem/package.zip"):
                continue
            destination = directory / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            add_file(destination, rel)

    key_included = False
    if include_key and settings.key_file.is_file():
        destination = directory / "secrets" / "master.key"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings.key_file, destination)
        if os.name == "posix":
            destination.chmod(0o600)
        add_file(destination, "secrets/master.key")
        key_included = True

    manifest = {
        "schema": BACKUP_SCHEMA,
        "product": "AscendForge",
        "created_at": now(),
        "database": "ascendforge.db",
        "files": files,
        "master_key_included": key_included,
        "credentials": "encrypted inside the database; decryptable only with the master key",
        "notes": [
            "Without secrets/master.key the encrypted model key and CANNJudge session cannot be restored.",
            "Restore with: python -m ascendforge restore <this-directory> --data-dir <target>",
            "Verify with: python -m ascendforge verify-backup <this-directory>",
        ],
    }
    (directory / "manifest.json").write_text(json_dumps(manifest, pretty=True), encoding="utf-8")
    sums = "\n".join(f"{item['sha256']}  {item['path']}" for item in sorted(files, key=lambda x: x["path"]))
    (directory / "SHA256SUMS").write_text(sums + "\n", encoding="utf-8")
    return {
        "path": str(directory),
        "files": len(files),
        "bytes": sum(item["bytes"] for item in files),
        "master_key_included": key_included,
        "created_at": manifest["created_at"],
    }


def verify_backup(directory: str | Path) -> dict:
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise BackupError("manifest.json missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != BACKUP_SCHEMA:
        raise BackupError("unsupported backup schema")
    problems: list[str] = []
    checked = 0
    for item in manifest.get("files", []):
        path = directory / item["path"]
        if not path.is_file():
            problems.append(f"missing: {item['path']}")
            continue
        if sha256_file(path) != item["sha256"]:
            problems.append(f"hash mismatch: {item['path']}")
        checked += 1
    database = directory / manifest.get("database", "ascendforge.db")
    integrity = None
    if database.is_file():
        try:
            conn = sqlite3.connect(str(database))
            try:
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                conn.close()
            if integrity != "ok":
                problems.append(f"sqlite integrity_check: {integrity}")
        except sqlite3.Error as exc:
            problems.append(f"sqlite open failed: {exc}")
    else:
        problems.append("database missing")
    return {
        "ok": not problems,
        "checked_files": checked,
        "problems": problems,
        "sqlite_integrity": integrity,
        "master_key_included": bool(manifest.get("master_key_included")),
        "created_at": manifest.get("created_at"),
    }


def restore_backup(directory: str | Path, target_data_dir: str | Path, *, force: bool = False) -> dict:
    directory = Path(directory)
    target = Path(target_data_dir)
    verification = verify_backup(directory)
    if not verification["ok"]:
        raise BackupError("backup verification failed: " + "; ".join(verification["problems"][:10]))
    target.mkdir(parents=True, exist_ok=True)
    if (target / "ascendforge.db").exists() and not force:
        raise BackupError("target already contains a database; use --force to overwrite")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    restored = 0
    for item in manifest.get("files", []):
        source = directory / item["path"]
        if item["path"] == "secrets/master.key" and (target / "secrets" / "master.key").exists() and not force:
            continue
        destination = target / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        restored += 1
    return {
        "restored_files": restored,
        "target": str(target),
        "master_key_included": verification["master_key_included"],
        "note": "Start the web server and worker after restore; in-flight submissions are reconciled, not resent.",
    }


def list_backups(settings: Settings) -> list[dict]:
    if not settings.backups_dir.is_dir():
        return []
    items = []
    for directory in sorted(settings.backups_dir.iterdir(), reverse=True):
        manifest = directory / "manifest.json"
        if not directory.is_dir() or not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            size = sum(item.get("bytes", 0) for item in data.get("files", []))
        except Exception:
            continue
        items.append(
            {
                "name": directory.name,
                "created_at": data.get("created_at"),
                "files": len(data.get("files", [])),
                "bytes": size,
                "master_key_included": bool(data.get("master_key_included")),
            }
        )
    return items
