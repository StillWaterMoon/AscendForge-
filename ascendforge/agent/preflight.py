"""Cheap host-side checks before a candidate is snapshotted or submitted.

This is not a compiler.  It only rejects a few failure modes that have already
been observed against the live CANNJudge toolchain, so the Agent does not burn a
real submission on a known-bad construct.  The remote compiler remains the
authority.
"""

from __future__ import annotations

import re
from typing import Any

_RULES = (
    (
        "GLOBALTENSOR_REINTERPRET_CAST",
        re.compile(r"reinterpret_cast\s*<\s*(?:AscendC::)?GlobalTensor\s*<"),
        "GlobalTensor is a wrapper type, not a pointer. Construct it with "
        "SetGlobalBuffer((__gm__ T*)ptr, n) instead of reinterpret_cast.",
    ),
    (
        "ASCENDC_ANDS_UNAVAILABLE",
        re.compile(r"\bAscendC::Ands\s*\("),
        "AscendC::Ands is not present in the tested CANN version. Use a supported "
        "API or scalar/bit operations appropriate to the dtype.",
    ),
)


def _call_arguments(text: str, start: int) -> list[str]:
    depth = 0
    args = []
    current = []
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
            if depth == 1:
                continue
        elif char == ")":
            depth -= 1
            if depth == 0:
                args.append("".join(current).strip())
                return args
        elif char == "," and depth == 1:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    return []


def _looks_scalar(expr: str) -> bool:
    value = expr.strip()
    if not value:
        return False
    if re.search(r"\b0x[0-9a-fA-F]+\b", value):
        return True
    if re.search(r"^\([^)]*\)\s*[-+]?\d", value):
        return True
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?[uUlLfF]*", value):
        return True
    return False


def _scalar_vector_operand_errors(path: str, content: str) -> list[dict[str, str]]:
    errors = []
    pattern = re.compile(r"\bAscendC::(And|Or)\s*\(")
    for match in pattern.finditer(content):
        args = _call_arguments(content, match.end() - 1)
        if len(args) != 4:
            continue
        if _looks_scalar(args[2]):
            line = content.count("\n", 0, match.start()) + 1
            errors.append(
                {
                    "code": "ASCENDC_AND_OR_SCALAR_OPERAND",
                    "path": path,
                    "line": line,
                    "message": (
                        "AscendC::And/Or has no (dst, src, scalar, count) overload in the "
                        "tested CANN version. Use three LocalTensor operands, or keep the "
                        "parent's scalar path; do not pass a constant mask."
                    ),
                }
            )
    return errors


def preflight_candidate(files: dict[str, str], problem: dict[str, Any] | None = None) -> dict:
    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    for path, content in sorted(files.items()):
        if not path.endswith((".asc", ".h", ".hpp", ".cpp", ".cc", ".c")):
            continue
        for code, pattern, message in _RULES:
            match = pattern.search(content)
            if match:
                line = content.count("\n", 0, match.start()) + 1
                errors.append({"code": code, "path": path, "line": line, "message": message})
        errors.extend(_scalar_vector_operand_errors(path, content))

    text = "\n".join(files.values())
    dtype_tokens = {
        "int8": "int8",
        "int16": "int16",
        "int32": "int32",
        "float16": "float16",
        "float32": "float32",
        "half": "float16",
        "bfloat16": "bfloat16",
    }
    present = sorted({name for token, name in dtype_tokens.items() if token in text.lower()})
    desc = str((problem or {}).get("desc") or (problem or {}).get("description") or "").lower()
    problem_dtypes = sorted(
        {name for token, name in dtype_tokens.items() if token in desc}
    )
    if len(problem_dtypes) > 1 and len(present) == 1:
        warnings.append(
            "The problem mentions multiple dtypes "
            f"({', '.join(problem_dtypes)}) but this candidate only mentions {present[0]}. "
            "Verify dtype/shape coverage before submitting."
        )
    if len(problem_dtypes) > 1 and not any(
        token in text.lower() for token in ("dtype", "tensorgroupinfo", "tensorinfo")
    ):
        warnings.append(
            "The problem mentions multiple dtypes but the candidate does not branch on "
            "the TensorInfo dtype. A single-type implementation may fail other testcases."
        )

    return {"ok": not errors, "errors": errors, "warnings": warnings}
