"""Prompt assembly with strict context bounds.

The prompt contains the problem contract, template, experiment history and
current budget.  It never contains API keys, cookies, passwords or raw session
data.  Hidden model reasoning is not requested or stored; actions and short
summaries are enough for reproducibility.
"""

from __future__ import annotations

import json
from typing import Any

from ..util import json_dumps, truncate_text

MAX_PROBLEM_CHARS = 24_000
MAX_TEMPLATE_CHARS = 48_000
MAX_HISTORY_CHARS = 16_000


def system_prompt(skill_block: str) -> str:
    return f"""You are the AscendForge (昇腾工坊) optimization agent, operating on a private server for CANNJudge Ascend operator tasks.

## Non-negotiable host rules
1. You do not have filesystem, shell, browser or network access. Every effect goes through one of the structured tools below, executed and validated by the host.
2. Never ask for or reveal CANNJudge passwords/cookies, model API keys, environment variables or session files. They are not in your context and must not be requested.
3. Never submit to CANNJudge directly. Only call `request_submission`; the host scheduler enforces frequency, daily quotas, in-flight limits, budgets and reconciliation.
4. Never fabricate compile results, testcase results, timings, scores or a device run. If no NPU is available, an Ascend C candidate is `unavailable/untested`; a CPU reference pass is not an operator pass.
5. Only claim improvement from a measured, comparable remote result. A failed or degenerate candidate must not replace the best version.
6. Preserve the problem contract: interface signature, dtype, shape, layout, tolerance and allowed values come from the imported template. Do not invent missing constraints.
7. Do not hard-code evaluation answers, exploit the judge, or weaken validation to gain score.
8. Do not store or reveal private chain-of-thought. Give a short, testable hypothesis in the `hypothesis` field and a brief action `summary`.

## Action protocol (protocol_version "1")
Return exactly one JSON object per turn, or use native tool calling when available:

```json
{{"protocol_version":"1","action_id":"act-001","task_id":"<task>","attempt_id":"<attempt>","tool_name":"read_problem","arguments":{{}},"summary":"brief reason"}}
```

Allowed tools:
- `read_problem`: read the imported contract/template.
- `read_history`: previous attempts and remote outcomes.
- `create_candidate`: save an immutable candidate snapshot (files + hypothesis).
- `create_test_generator`: store generator + independent reference sources (does not execute).
- `run_local_validation`: run generator/reference in the isolated executor; candidate stays untested without an NPU.
- `request_submission`: enqueue the current snapshot for scheduled remote evaluation.
- `query_result`: read stored submission state (no network).
- `finish_task`: finish with a truthful reason and summary.

After `request_submission`, stop working on this attempt: the scheduler sends it, polls the remote result, and starts the next attempt with feedback. If a result is not yet available, do not invent one.

## Loaded skills (explicitly injected by the host)
{skill_block}
"""


def _brief_problem(problem: dict) -> str:
    if not isinstance(problem, dict):
        return "(missing problem)"
    allowed = {
        "name",
        "title",
        "code_template",
        "description",
        "content",
        "input",
        "output",
        "constraints",
        "tags",
        "difficulty",
        "score",
        "time_limit",
        "memory_limit",
        "_id",
    }
    data = {k: v for k, v in problem.items() if k in allowed}
    text = json_dumps(data, pretty=True)
    return truncate_text(text, MAX_PROBLEM_CHARS)


def _template_files(template: dict, *, include_contents: bool = True) -> str:
    files = []
    if isinstance(template, dict):
        raw = template.get("files")
        if isinstance(raw, dict):
            raw = [{"path": k, "content": v, "editable": True} for k, v in raw.items()]
        files = raw or []
    chunks = []
    used = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        path = item.get("path") or item.get("name")
        editable = bool(item.get("editable"))
        content = item.get("content") if isinstance(item.get("content"), str) else ""
        header = f"### {path} ({'editable' if editable else 'read-only'}, {len(content)} chars)\n"
        body = content if include_contents and editable else "(content not shown)"
        piece = header + body + "\n"
        if used + len(piece) > MAX_TEMPLATE_CHARS:
            chunks.append(f"... template truncated after {used} chars ...")
            break
        chunks.append(piece)
        used += len(piece)
    return "\n".join(chunks) if chunks else "(no template files)"


def _history(attempts: list[dict], *, current_id: str | None) -> str:
    if not attempts:
        return "(no previous attempts)"
    lines = []
    for attempt in attempts[:20]:
        marker = "*" if attempt.get("id") == current_id else " "
        normalized = attempt.get("normalized") or {}
        snapshot = (attempt.get("snapshot_id") or "-")[:18]
        lines.append(
            f"{marker} #{attempt.get('ordinal')} {attempt.get('id')} status={attempt.get('status')} "
            f"parent={attempt.get('parent_attempt_id') or '-'} snapshot={snapshot} "
            f"outcome={normalized.get('outcome') or '-'} "
            f"pass={normalized.get('testcase_passed', '-')}/{normalized.get('testcase_total', '-')} "
            f"score={normalized.get('score', '-')} precision={normalized.get('precision_ratio', '-')} "
            f"hypothesis={(attempt.get('hypothesis') or '')[:160]!r}"
        )
        if attempt.get("error_message"):
            lines.append(f"    error: {str(attempt['error_message'])[:200]}")
    text = "\n".join(lines)
    return truncate_text(text, MAX_HISTORY_CHARS)


def context_prompt(
    *,
    task: dict,
    attempt: dict,
    attempts: list[dict],
    best_files: dict[str, str] | None,
    capabilities: dict | None,
) -> str:
    config = task.get("config") or {}
    problem = task.get("problem") or {}
    template = task.get("template") or {}
    best_text = "(no best version yet)"
    if best_files:
        chunks = []
        used = 0
        for path, content in sorted(best_files.items()):
            piece = f"### {path}\n{content}\n"
            if used + len(piece) > 20_000:
                chunks.append("... best source truncated ...")
                break
            chunks.append(piece)
            used += len(piece)
        best_text = "\n".join(chunks)
    current_state = {
        "task_status": task.get("status"),
        "attempt_status": attempt.get("status"),
        "snapshot_id": attempt.get("snapshot_id"),
        "budget": config,
        "local_best": task.get("local_best"),
        "capabilities": capabilities,
    }
    return f"""# Current task

task_id: {task.get('id')}
attempt_id: {attempt.get('id')} (ordinal {attempt.get('ordinal')})
problem_title: {task.get('problem_title')}
problem_url: {task.get('problem_url')}
code_template: {problem.get('code_template')}

## Problem contract (imported from CANNJudge)
{_brief_problem(problem)}

## Template files
{_template_files(template)}

## Experiment history (newest first)
{_history(attempts, current_id=attempt.get('id'))}

## Current best source (remote best only; may be empty)
{best_text}

## Current state / budget
```json
{json_dumps(current_state, pretty=True)}
```

Proceed with one action. If the current attempt already has a candidate snapshot, either request its submission or revise it before submitting. Do not submit twice for the same attempt.
"""


def result_excerpt(normalized: dict | None) -> str:
    if not normalized:
        return "(no result)"
    data = {
        "outcome": normalized.get("outcome"),
        "raw_status": normalized.get("raw_status"),
        "testcase_passed": normalized.get("testcase_passed"),
        "testcase_total": normalized.get("testcase_total"),
        "score": normalized.get("score"),
        "precision_ratio": normalized.get("precision_ratio"),
        "message": normalized.get("message"),
    }
    return json.dumps(data, ensure_ascii=False)
