"""Derive the current optimization phase from measured task state.

The owner-authored strategy is a stable contract/goal.  This module produces the
short-lived phase strategy that must change as the task moves from compilation
to correctness to performance.
"""

from __future__ import annotations

from typing import Any


def _normalized(attempt: dict) -> dict:
    value = attempt.get("normalized")
    if isinstance(value, dict):
        return value
    value = attempt.get("normalized_json")
    if isinstance(value, dict):
        return value
    return {}


def _latest_with_result(attempts: list[dict]) -> dict | None:
    for attempt in attempts:
        normalized = _normalized(attempt)
        if normalized.get("outcome") or normalized.get("testcases"):
            return attempt
    return None


def derive_active_strategy(task: dict, attempts: list[dict]) -> dict:
    """Return a phase description that is safe to inject into the prompt."""
    latest = _latest_with_result(attempts or [])
    best_id = task.get("best_attempt_id")
    best = next((a for a in (attempts or []) if a.get("id") == best_id), None)
    if best_id:
        best_ordinal = best.get("ordinal") if best else "?"
        text = (
            f"当前阶段：性能优化。已有通过验证的 best（attempt #{best_ordinal}）。\n"
            "保持完整正确性、接口和 dtype 覆盖；先测当前 best 基线，再每轮只改一个主要因素"
            "（分块、搬运、并行、同步、融合等）。性能退化或不可比的结果不能覆盖 best；"
            "只有真实、可比的测量才更新 best。"
        )
        return {
            "phase": "performance",
            "text": text,
            "based_on_attempt": best_id,
            "latest_attempt": latest.get("ordinal") if latest else None,
        }

    if not latest:
        return {
            "phase": "baseline",
            "text": (
                "当前阶段：建立基线。还没有可用的远端结果。先读取题目合同、模板和父版本，"
                "产出一个可编译、接口完整、覆盖题目 dtype 的最小实现；不要先做性能优化。"
            ),
            "based_on_attempt": None,
            "latest_attempt": None,
        }

    normalized = _normalized(latest)
    outcome = str(normalized.get("outcome") or "unknown")
    ordinal = latest.get("ordinal")
    passed = normalized.get("testcase_passed")
    total = normalized.get("testcase_total")
    precision = normalized.get("precision_ratio")
    if outcome == "compile_error":
        text = (
            f"当前阶段：编译修复。最新 attempt #{ordinal} 是编译错误。"
            "先修根因，保留父代码中已经正确的部分，不做无关性能改动；"
            "只使用当前 CANN 版本真实存在、模板或头文件确认过的 API。"
        )
        phase = "compile"
    elif outcome == "runtime_error":
        text = (
            f"当前阶段：运行修复。最新 attempt #{ordinal} 是运行错误。"
            "先检查启动参数、接口、边界、内存/同步和运行时错误信息；"
            "不要在没有正确运行前做性能优化。"
        )
        phase = "runtime"
    elif outcome in ("failed", "timeout", "system_error"):
        text = (
            f"当前阶段：正确性修复。最新 attempt #{ordinal} 结果为 {outcome}，"
            f"通过 {passed}/{total}，precision_ratio={precision}。"
            "按逐测试点的 dtype/shape/比例模式定位语义、宽度、尾部或边界问题；"
            "先修复正确性，不要先调性能。"
        )
        phase = "correctness"
    else:
        text = (
            f"当前阶段：确认结果。最新 attempt #{ordinal} 的结果还不明确（{outcome}）。"
            "先读取真实逐测试点结果并确认阶段，不要猜测通过或提升。"
        )
        phase = "verify"
    return {
        "phase": phase,
        "text": text,
        "based_on_attempt": latest.get("id"),
        "latest_attempt": ordinal,
    }
