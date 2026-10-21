"""Explicit skill loading.

Skills live in ``<repo>/skills/<name>/SKILL.md``.  They are *not* expected to be
discovered automatically by any chat client: the host loads the requested
bodies and injects them into the system prompt for the relevant stage.  The
loaded names and SHA-256 hashes are stored with the attempt for reproducibility.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


class SkillError(RuntimeError):
    pass


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path
    sha256: str

    def public(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "sha256": self.sha256,
            "path": str(self.path),
        }


def _parse_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    end = None
    for index in range(1, len(lines)):
        line = lines[index].strip()
        if line == "---":
            end = index
            break
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"').strip("'")
    if end is None:
        return {}, text
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


class SkillRegistry:
    def __init__(self, skills_root: Path):
        self.root = Path(skills_root)

    def list(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        items = []
        for directory in sorted(self.root.iterdir()):
            path = directory / "SKILL.md"
            if path.is_file():
                try:
                    items.append(self.load(directory.name).public())
                except SkillError:
                    continue
        return items

    def load(self, name: str) -> Skill:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name or ""):
            raise SkillError("invalid skill name")
        path = self.root / name / "SKILL.md"
        if not path.is_file():
            raise SkillError(f"skill not found: {name}")
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_front_matter(text)
        declared = meta.get("name") or name
        if declared != name:
            raise SkillError(f"skill front matter name mismatch: {declared} != {name}")
        if not body.strip():
            raise SkillError(f"skill body is empty: {name}")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return Skill(
            name=name,
            description=meta.get("description", ""),
            body=body.strip(),
            path=path,
            sha256=digest,
        )

    def load_many(self, names: list[str]) -> list[Skill]:
        return [self.load(name) for name in names]

    def prompt_block(self, names: list[str]) -> tuple[str, list[dict]]:
        skills = self.load_many(names)
        parts = []
        for skill in skills:
            parts.append(
                f"### Skill: {skill.name}\n"
                f"(sha256 {skill.sha256})\n\n"
                f"{skill.body}"
            )
        return "\n\n---\n\n".join(parts), [skill.public() for skill in skills]
