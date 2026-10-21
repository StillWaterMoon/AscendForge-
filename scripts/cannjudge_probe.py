#!/usr/bin/env python3
"""Safe CANNJudge integration probe.

Checks public problem/template/package access without credentials and (optionally)
verifies that the login/submit endpoints exist by sending deliberately empty
payloads.  It never logs credentials, cookies or response bodies beyond short
error messages.

Usage:
    python scripts/cannjudge_probe.py --problem-url https://cannjudge.cn/hc_cann_codelabs/cann/add
    python scripts/cannjudge_probe.py --probe-auth-endpoints --output evidence.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ascendforge.platform.cannjudge import CannjudgeAdapter  # noqa: E402
from ascendforge.util import now  # noqa: E402

DEFAULT_PROBLEM = "https://cannjudge.cn/hc_cann_codelabs/cann/add"


def probe_auth_endpoints(base_url: str) -> list[dict]:
    results = []
    checks = [
        ("GET", "/api/users/captcha", None),
        ("POST", "/api/users/login", b"{}"),
        ("POST", "/api/submissions/submit", b"{}"),
        ("GET", "/api/submissions/does-not-exist", None),
    ]
    for method, path, body in checks:
        request = urllib.request.Request(
            base_url + path,
            data=body,
            headers={
                "User-Agent": "AscendForge-probe/0.1",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = response.read(500)
                results.append(
                    {
                        "method": method,
                        "path": path,
                        "status": response.status,
                        "content_type": response.headers.get("content-type"),
                        "note": "endpoint response (body not retained for captcha)",
                    }
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", errors="replace")
            results.append(
                {
                    "method": method,
                    "path": path,
                    "status": exc.code,
                    "error": detail[:200],
                }
            )
        except Exception as exc:
            results.append({"method": method, "path": path, "error": str(exc)[:200]})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe CANNJudge public integration surface")
    parser.add_argument("--base-url", default="https://cannjudge.cn")
    parser.add_argument("--problem-url", default=DEFAULT_PROBLEM)
    parser.add_argument("--download-package", action="store_true")
    parser.add_argument("--probe-auth-endpoints", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    evidence: dict = {
        "schema": "ascendforge-cannjudge-probe/1",
        "probed_at": now(),
        "base_url": args.base_url,
        "problem_url": args.problem_url,
        "checks": [],
    }
    adapter = CannjudgeAdapter(base_url=args.base_url, timeout=30)
    try:
        homepage = urllib.request.Request(
            args.base_url + "/home", headers={"User-Agent": "AscendForge-probe/0.1"}
        )
        with urllib.request.urlopen(homepage, timeout=20) as response:
            body = response.read(10_000)
        evidence["checks"].append(
            {"step": "homepage", "ok": True, "status": response.status, "bytes": len(body)}
        )
    except Exception as exc:
        evidence["checks"].append({"step": "homepage", "ok": False, "error": str(exc)[:300]})

    try:
        problem = adapter.resolve_problem(args.problem_url)
        evidence["problem"] = {
            key: problem.get(key)
            for key in ("_id", "name", "canonical_name", "title", "contest_id", "code_template")
        }
        evidence["checks"].append({"step": "resolve_problem", "ok": True, "problem_id": problem.get("_id")})
    except Exception as exc:
        evidence["checks"].append({"step": "resolve_problem", "ok": False, "error": str(exc)[:500]})
        problem = None

    if problem:
        try:
            template = adapter.get_template(str(problem["_id"]))
            files = CannjudgeAdapter._template_files(template)
            evidence["template"] = {
                "file_count": len(files),
                "editable": [item["path"] for item in files if item["editable"]],
                "readonly": [item["path"] for item in files if not item["editable"]],
            }
            evidence["checks"].append({"step": "get_template", "ok": True, "files": len(files)})
        except Exception as exc:
            evidence["checks"].append({"step": "get_template", "ok": False, "error": str(exc)[:500]})
        if args.download_package:
            try:
                target = Path(tempfile.mkdtemp(prefix="cannjudge-package-"))
                package = adapter.download_package(str(problem["_id"]), target)
                evidence["package"] = {
                    "bytes": package["bytes"],
                    "sha256": package["sha256"],
                    "files": len(package["files"]),
                }
                evidence["checks"].append({"step": "download_package", "ok": True})
            except Exception as exc:
                evidence["checks"].append({"step": "download_package", "ok": False, "error": str(exc)[:500]})

    if args.probe_auth_endpoints:
        evidence["auth_endpoint_checks"] = probe_auth_endpoints(args.base_url)

    evidence["notes"] = [
        "Public problem/template/package checks do not authenticate.",
        "Login/submit success is NOT verified by this probe; use the Web UI with a real account.",
        "No password, cookie or API key is read or written by this script.",
    ]
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if all(item.get("ok") for item in evidence["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
