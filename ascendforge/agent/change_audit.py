"""Program-computed change evidence for a candidate revision.

The model's hypothesis is an intention, not evidence.  Before a candidate is
snapshotted we compare it with its parent source and record what actually
changed, so later prompts cannot mistake a comment edit for a vectorization
implementation.
"""

from __future__ import annotations

import difflib
import hashlib
import re


def strip_cpp_comments(text: str) -> str:
    """Remove C/C++ comments while preserving string and char literals."""
    out: list[str] = []
    index = 0
    state = "code"  # code | line | block | string | char
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                state = "line"
                out.append(" ")
                index += 2
                continue
            if char == "/" and nxt == "*":
                state = "block"
                out.append(" ")
                index += 2
                continue
            out.append(char)
            if char == '"':
                state = "string"
            elif char == "'":
                state = "char"
            index += 1
            continue
        if state == "line":
            if char == "\n":
                state = "code"
                out.append("\n")
            index += 1
            continue
        if state == "block":
            if char == "*" and nxt == "/":
                state = "code"
                index += 2
                continue
            if char == "\n":
                out.append("\n")
            index += 1
            continue
        # string / char literal
        out.append(char)
        if char == "\\" and index + 1 < len(text):
            out.append(nxt)
            index += 2
            continue
        if (state == "string" and char == '"') or (state == "char" and char == "'"):
            state = "code"
        index += 1
    return "".join(out)


def normalize_code(text: str) -> str:
    stripped = strip_cpp_comments(text)
    return "\n".join(line.strip() for line in stripped.splitlines() if line.strip())


def _file_diff(parent: str, current: str, path: str) -> list[str]:
    return list(
        difflib.unified_diff(
            parent.splitlines(),
            current.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )


_VECTOR_CLAIM = re.compile(
    r"(vector|向量|LocalTensor|AscendC::(?:And|Or|Mins|Maxs|Muls|Compare|Select|Cast|Duplicate))",
    re.IGNORECASE,
)
_VECTOR_API = re.compile(
    r"AscendC::(?:And|Or|Not|Mins|Maxs|Muls|Adds|Compare|Select|Cast|Duplicate|Copy)\s*\("
)


def claim_check(hypothesis: str, files: dict[str, str]) -> dict:
    """Flag a claim that names vector operations while the code has none."""
    code = "\n".join(strip_cpp_comments(value) for value in files.values())
    claims_vector = bool(_VECTOR_CLAIM.search(hypothesis or ""))
    has_vector_api = bool(_VECTOR_API.search(code))
    return {
        "claims_vector": claims_vector,
        "has_vector_api": has_vector_api,
        "mismatch": claims_vector and not has_vector_api,
    }


def audit_candidate(parent_files: dict[str, str] | None, files: dict[str, str]) -> dict:
    parent_files = parent_files or {}
    paths = sorted(set(parent_files) | set(files))
    file_rows = []
    diff_lines: list[str] = []
    code_changed = False
    raw_changed = False
    changed_lines = 0
    for path in paths:
        before = parent_files.get(path)
        after = files.get(path)
        if before == after:
            continue
        raw_changed = True
        before_text = before or ""
        after_text = after or ""
        code_same = normalize_code(before_text) == normalize_code(after_text)
        if not code_same:
            code_changed = True
        delta = _file_diff(before_text, after_text, path)
        diff_lines.extend(delta)
        additions = sum(1 for line in delta if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in delta if line.startswith("-") and not line.startswith("---"))
        changed_lines += additions + deletions
        file_rows.append(
            {
                "path": path,
                "added": additions,
                "removed": deletions,
                "code_changed": not code_same,
                "comments_only": code_same,
            }
        )
    diff_text = "\n".join(diff_lines)
    if not raw_changed:
        kind = "no_change"
    elif code_changed:
        kind = "code"
    else:
        kind = "comments_only"
    parent_lines = sum(max(1, len((parent_files.get(path) or "").splitlines())) for path in parent_files)
    return {
        "kind": kind,
        "effective": code_changed,
        "raw_changed": raw_changed,
        "code_changed": code_changed,
        "changed_files": file_rows,
        "changed_lines": changed_lines,
        "parent_lines": parent_lines,
        "rewrite_ratio": round(changed_lines / max(1, parent_lines), 3),
        "diff_sha": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
        "diff_text": diff_text,
    }
