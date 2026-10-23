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
from .strategy import derive_active_strategy

MAX_PROBLEM_CHARS = 24_000
MAX_TEMPLATE_CHARS = 48_000
MAX_HISTORY_CHARS = 16_000
MAX_PARENT_SOURCE_CHARS = 200_000
MAX_PARENT_RESULT_CHARS = 120_000


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
9. Start every non-first candidate from the provided parent source. Make a minimal, reviewable patch; preserve unchanged files. Do not rebuild from the template or from a summary unless this is the first candidate or the parent source is unavailable.
10. Do not invent Ascend C APIs. Use only APIs present in the template comments, CANN headers, or verified examples. A static check passing is not a compile; a compile is not correctness.

## Action protocol (protocol_version "1")
Return exactly one JSON object per turn, or use native tool calling when available:

```json
{{"protocol_version":"1","action_id":"act-001","task_id":"<task>","attempt_id":"<attempt>","tool_name":"read_problem","arguments":{{}},"summary":"brief reason"}}
```

Allowed tools:
- `read_problem`: read the imported contract/template.
- `read_history`: previous attempts and remote outcomes (metadata only).
- `read_attempt_source`: read a previous attempt's full source (default: parent); use it for a minimal patch, never rebuild from summaries.
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
        "desc",
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


def _observed_change_text(attempt: dict) -> str:
    raw = attempt.get("changed_files_json")
    if not raw:
        return ""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ""
    files = data.get("changed_files") or []
    names = ", ".join(str(item.get("path")) for item in files[:4])
    claim = data.get("claim") or {}
    suffix = " claim_mismatch=true" if claim.get("mismatch") else ""
    return (
        f"observed_change={data.get('kind') or 'unknown'} files=[{names}] "
        f"changed_lines={data.get('changed_lines')}{suffix}"
    )


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
        observed = _observed_change_text(attempt)
        if observed:
            lines.append("    " + observed)
        if attempt.get("error_message"):
            lines.append(f"    error: {str(attempt['error_message'])[:200]}")
    text = "\n".join(lines)
    return truncate_text(text, MAX_HISTORY_CHARS)


def _testcase_digest(attempts: list[dict]) -> str:
    """Render per-testcase feedback, not just the aggregate minimum.

    CANNJudge exposes ``precision_ratio`` per testcase and often no expected
    value.  Repeated ratios across dtype groups are a useful signal, so the
    Agent should see them directly instead of having to guess from one number.
    """
    for item in attempts:
        normalized = item.get("normalized") or {}
        cases = normalized.get("testcases") or []
        if not cases:
            continue
        ordinal = item.get("ordinal")
        status = normalized.get("raw_status") or normalized.get("outcome") or "unknown"
        passed = normalized.get("testcase_passed")
        total = normalized.get("testcase_total")
        score = normalized.get("score")
        precision = normalized.get("precision_ratio")
        lines = [
            f"attempt #{ordinal} — {status}; passed={passed}/{total}; score={score}; "
            f"precision_ratio(min across cases)={precision}",
        ]
        shown = cases[:30]
        ratios = []
        for case in shown:
            index = int(case.get("index", 0)) + 1
            ratio = case.get("precision_ratio")
            time_value = case.get("time")
            raw = case.get("raw") if isinstance(case.get("raw"), dict) else {}
            best_time = raw.get("best_time")
            ratios.append(
                f"#{index} status={case.get('status')} precision={ratio} "
                f"time={time_value} best_time={best_time}"
            )
        lines.append("per-testcase: " + " | ".join(ratios))
        messages = [
            str(case.get("message"))
            for case in shown
            if case.get("message")
        ]
        if messages:
            lines.append("testcase message: " + messages[0][:1000])
        lines.append(
            "Note: precision_ratio is a platform field, not a confirmed error fraction. "
            "Compare testcases by dtype/shape; repeated ratios across a fixed group "
            "may indicate element-width, stride, tail or per-dtype coverage bugs."
        )
        return "\n".join(lines)
    return "(no per-testcase remote feedback yet)"


def _parent_result_text(
    parent_attempt: dict | None,
    parent_result: dict | None,
    parent_raw: dict | None,
) -> str:
    if not parent_attempt:
        return "(no parent attempt)"
    ordinal = parent_attempt.get("ordinal")
    if not parent_result and not parent_raw:
        return (
            f"attempt #{ordinal} has no final remote result yet; "
            "use its hypothesis and source, and do not invent testcase data."
        )
    normalized = parent_result or {}
    lines = [
        f"attempt #{ordinal} full remote evaluation",
        f"status={normalized.get('raw_status') or normalized.get('outcome') or 'unknown'}",
        f"passed={normalized.get('testcase_passed')}/{normalized.get('testcase_total')} "
        f"score={normalized.get('score')} precision_ratio(min)={normalized.get('precision_ratio')}",
    ]
    observed = _observed_change_text(parent_attempt)
    if observed:
        lines.append(observed)
    if normalized.get("message"):
        lines.append("message: " + str(normalized["message"]))
    cases = normalized.get("testcases") or []
    for case in cases:
        raw = case.get("raw") if isinstance(case.get("raw"), dict) else {}
        lines.append(
            f"#{int(case.get('index', 0)) + 1} "
            f"status={case.get('status')} passed={case.get('passed')} "
            f"precision_ratio={case.get('precision_ratio')} "
            f"time={case.get('time')} best_time={raw.get('best_time')} "
            f"message={case.get('message')}"
        )
    payload = parent_raw if parent_raw is not None else normalized
    raw_text = json_dumps(payload, pretty=True)
    lines.append("full_raw_json:")
    lines.append(truncate_text(raw_text, MAX_PARENT_RESULT_CHARS))
    return "\n".join(lines)


def _operator_messages(task: dict) -> str:
    messages = task.get("messages") or []
    if not messages:
        return "(no operator messages yet)"
    lines = []
    for item in messages[-10:]:
        content = truncate_text(str(item.get("content") or ""), 2000)
        lines.append(
            f"- {item.get('author') or 'operator'} "
            f"({item.get('role') or 'operator'}): {content}"
        )
    return "\n".join(lines)


def context_prompt(
    *,
    task: dict,
    attempt: dict,
    attempts: list[dict],
    best_files: dict[str, str] | None,
    parent_files: dict[str, str] | None = None,
    parent_attempt: dict | None = None,
    parent_result: dict | None = None,
    parent_raw: dict | None = None,
    experiment: dict | None = None,
    capabilities: dict | None = None,
) -> str:
    config = task.get("config") or {}
    problem = task.get("problem") or {}
    template = task.get("template") or {}
    strategy_text = str(config.get("strategy_prompt") or "").strip()
    if strategy_text:
        strategy_text = (
            "Treat this as problem-specific goals/constraints and hypotheses, not as a "
            "replacement for correctness or the imported contract:\n\n"
            + truncate_text(strategy_text, 8000)
        )
    else:
        strategy_text = "(none; follow the generic optimize-operator skill and the imported contract)"
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
    parent_text = "(no parent candidate; this is the first candidate and may use the template)"
    if parent_files:
        chunks = []
        used = 0
        header = ""
        if parent_attempt:
            header = (
                f"attempt #{parent_attempt.get('ordinal')} "
                f"(id {parent_attempt.get('id')}, parent of this attempt)\n"
                f"hypothesis: {parent_attempt.get('hypothesis') or ''}\n\n"
            )
        for path, content in sorted(parent_files.items()):
            piece = f"### {path}\n{content}\n"
            if used + len(piece) > MAX_PARENT_SOURCE_CHARS:
                chunks.append("... parent source exceeds the host context cap ...")
                break
            chunks.append(piece)
            used += len(piece)
        parent_text = header + "\n".join(chunks)
    active_strategy = derive_active_strategy(task, attempts)
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

## Current experiment
experiment_id: {(experiment or {}).get('id') or '(none)'}
status: {(experiment or {}).get('status') or '(not started)'}
ordinal: {(experiment or {}).get('ordinal') or '—'}
hypothesis: {(experiment or {}).get('hypothesis') or '(not recorded)'}
baseline_attempt: {(experiment or {}).get('baseline_attempt_id') or '(none)'}
working_attempt: {(experiment or {}).get('working_attempt_id') or '(none)'}
experiment_best: {(experiment or {}).get('experiment_best_id') or '(none)'}
global_best: {task.get('best_attempt_id') or '(none)'}
baseline_perf: {(experiment or {}).get('baseline_perf') or '(not measured)'}
global_best_perf: {(experiment or {}).get('global_best_perf') or '(not measured)'}
performance_best: {(experiment or {}).get('performance_best') or '(unknown)'}

A revision belongs to the current experiment. Start from the working revision
when continuing this experiment; a new experiment starts from the global best.
The baseline stays fixed for the lifetime of the experiment.

## Experiment history (newest first)
{_history(attempts, current_id=attempt.get('id'))}

## Latest remote feedback (per testcase)
{_testcase_digest(attempts)}

## Parent candidate source (start from this; minimal diff)
{parent_text}

## Parent evaluation result (full; use this with the parent source)
{_parent_result_text(parent_attempt, parent_result, parent_raw)}

## Operator messages (human → Agent; newest last)
{_operator_messages(task)}
New requirements here are authoritative for the next attempt. Treat them as
task guidance, not as credentials; acknowledge them through the next
hypothesis/summary rather than inventing an immediate reply.

## Problem-specific strategy (owner constraints; stable)
{strategy_text}

## Active strategy (auto-derived from current phase; changes with results)
phase: {active_strategy.get('phase')}
{active_strategy.get('text')}

This phase instruction is authoritative for the current stage. The stable
owner constraints above remain authoritative for goals, limits and
non-negotiables; if they mention an outdated phase, this section wins.

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
