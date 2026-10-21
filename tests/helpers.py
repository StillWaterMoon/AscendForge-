"""Test fixtures: temporary instance, fake platform, seeded tasks."""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

from ascendforge.agent.providers import ModelConfig, MockProvider
from ascendforge.agent.runtime import AgentRuntime
from ascendforge.agent.skills import SkillRegistry
from ascendforge.config import Settings
from ascendforge.db import Database
from ascendforge.experiments import add_event, attach_snapshot, create_attempt, create_snapshot
from ascendforge.platform.base import PlatformError
from ascendforge.scheduler import Scheduler
from ascendforge.security import CredentialStore, SecretBox, load_master_key
from ascendforge.tasks import TaskService, normalize_config
from ascendforge.util import canonical_json, new_id, now

REPO = Path(__file__).resolve().parents[1]

PROBLEM = {
    "_id": "problem-1",
    "name": "Add",
    "code_template": "npu_kernel_dev",
    "description": "Compute y = x1 + x2 with float32.",
    "constraints": "1 <= n <= 1024",
}
TEMPLATE = {
    "files": [
        {"path": "kernel.asc", "content": "// template kernel\n", "editable": True},
        {"path": "main.asc", "content": "// readonly main\n", "editable": False},
        {"path": "CMakeLists.txt", "content": "# readonly build\n", "editable": False},
    ]
}


class FakePlatform:
    def __init__(self):
        self.user_id = "judge-user-1"
        self.submitted: list[dict] = []
        self.results: dict[str, dict] = {}
        self.counter = 0
        self.default_result = {
            "status": "Pass",
            "result": [{"testcase_status": "Pass", "precision_ratio": 0.99, "time": 1.25}],
            "score": 10,
        }
        self.resolve_error: PlatformError | None = None

    def adapter(self):
        return self

    def account_user_id(self):
        return self.user_id

    def resolve_and_download(self, problem_url: str, task_dir: Path) -> dict:
        if self.resolve_error:
            raise self.resolve_error
        problem_dir = Path(task_dir) / "problem"
        project = problem_dir / "project"
        project.mkdir(parents=True, exist_ok=True)
        (project / "kernel.asc").write_text("// template kernel\n", encoding="utf-8")
        archive = problem_dir / "package.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("kernel.asc", "// template kernel\n")
            zf.writestr("main.asc", "// readonly main\n")
        return {
            "problem": dict(PROBLEM),
            "template": json.loads(json.dumps(TEMPLATE)),
            "package": {
                "archive": str(archive),
                "project_dir": str(project),
                "bytes": archive.stat().st_size,
                "files": [{"path": "kernel.asc", "bytes": 18, "sha256": "x"}],
                "sha256": "testpackage",
            },
            "adapter": self,
        }

    def submit(self, problem: dict, template: dict, files: dict[str, str]) -> dict:
        self.counter += 1
        remote = f"remote-{self.counter}"
        self.submitted.append(
            {
                "remote": remote,
                "problem_id": problem.get("_id"),
                "files": dict(files),
                "at": now(),
            }
        )
        self.results[remote] = json.loads(json.dumps(self.default_result))
        return {"submission_id": remote, "raw": {"data": {"submissionId": remote}}}

    def get_submission(self, submission_id: str) -> dict:
        return json.loads(json.dumps(self.results[submission_id]))

    def find_recent(self, problem_id: str, user_id: str, since: float) -> list[dict]:
        return []


def make_settings(tmp: Path) -> Settings:
    data = tmp / "data"
    settings = Settings(
        root=REPO,
        data_dir=data,
        db_path=data / "ascendforge.db",
        key_file=data / "secrets" / "master.key",
        setup_token="test-setup-token",
        admin_user="admin",
        admin_password="",
        executor="local",
        worker_poll_seconds=0.2,
        lease_seconds=60,
    )
    settings.ensure_dirs()
    return settings


def seed_task(env, *, status: str = "pending", config: dict | None = None, title: str = "Test task") -> dict:
    task_id = new_id("task")
    directory = env.settings.tasks_dir / task_id
    problem_dir = directory / "problem"
    problem_dir.mkdir(parents=True, exist_ok=True)
    archive = problem_dir / "package.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("kernel.asc", "// template kernel\n")
    normalized = normalize_config(config or {})
    current = now()
    env.db.execute(
        "INSERT INTO tasks(id, title, platform, problem_id, problem_url, problem_title, problem_json, "
        "template_json, package_rel, status, config_json, created_at, updated_at, next_action_at, started_at) "
        "VALUES(?, ?, 'cannjudge', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            title,
            PROBLEM["_id"],
            "https://cannjudge.cn/hc_cann_codelabs/cann/add",
            PROBLEM["name"],
            canonical_json(PROBLEM),
            canonical_json(TEMPLATE),
            str(archive.relative_to(env.settings.data_dir)).replace("\\", "/"),
            status,
            canonical_json(normalized),
            current,
            current,
            current,
            current if status in ("running", "paused", "needs_login", "needs_review") else None,
        ),
    )
    add_event(env.db, task_id, None, "task_seeded", {})
    return env.tasks.get(task_id)


def seed_attempt(env, task: dict, *, parent: str | None = None, source: str = "// candidate", status: str = "generated") -> dict:
    attempt = create_attempt(env.db, task["id"], parent_attempt_id=parent)
    attach_snapshot(
        env.db,
        env.settings,
        task_id=task["id"],
        attempt_id=attempt["id"],
        files={"kernel.asc": source},
    )
    if status != "generated":
        env.db.execute("UPDATE attempts SET status=? WHERE id=?", (status, attempt["id"]))
    return env.db.query_one("SELECT * FROM attempts WHERE id=?", (attempt["id"],))


def make_env(tmp: Path, *, provider=None, script: list | None = None):
    settings = make_settings(tmp)
    db = Database(settings.db_path)
    db.init()
    box = SecretBox(load_master_key(settings.key_file, ""))
    credentials = CredentialStore(db, box)
    platform = FakePlatform()
    tasks = TaskService(db, settings, platform)
    scheduler = Scheduler(db, settings, platform, tasks)
    model_payload = {
        "provider": "mock",
        "base_url": "https://mock.invalid/v1",
        "api_key": "test-key",
        "model": "mock-model",
        "supports_tool_calls": False,
        "max_tokens": 512,
        "temperature": 0.1,
        "timeout": 10,
        "price_prompt_per_1k": 0.001,
        "price_completion_per_1k": 0.002,
    }
    credentials.put("model", model_payload)
    if provider is None and script is not None:
        config = ModelConfig.from_payload(model_payload)
        provider = MockProvider(config, script=script)
    factory = (lambda cfg: provider) if provider is not None else None
    runtime = AgentRuntime(
        db,
        settings,
        platform,
        tasks,
        scheduler,
        credentials,
        skills=SkillRegistry(settings.root / "skills"),
        provider_factory=factory,
    )
    return SimpleNamespace(
        settings=settings,
        db=db,
        box=box,
        credentials=credentials,
        platform=platform,
        tasks=tasks,
        scheduler=scheduler,
        runtime=runtime,
        tmp=tmp,
    )
