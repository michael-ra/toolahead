#!/usr/bin/env python3
"""Reject generated, private, or development-only files in release archives."""

from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


FORBIDDEN_PARTS = {
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tickets",
    ".toolahead",
    "__pycache__",
    "benchmarks",
    "build",
    "demo_project",
    "docs",
    "htmlcov",
    "scripts",
    "tests",
}
FORBIDDEN_SUFFIXES = (
    ".coverage",
    ".events.json",
    ".log",
    ".pyc",
    ".pyo",
    ".tmp",
    ".whl",
)
PRIVATE_PATTERNS = {
    "macOS home path": re.compile(rb"/Users/[^/\s]+/"),
    "macOS temporary path": re.compile(rb"/private/var/folders/"),
    "Windows home path": re.compile(rb"[A-Za-z]:\\\\Users\\\\[^\\\s]+\\\\"),
    "Anthropic API key": re.compile(rb"sk-ant-[A-Za-z0-9_-]{12,}"),
    "OpenAI API key": re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
}


def _logical_parts(name: str) -> tuple[str, ...]:
    parts = PurePosixPath(name).parts
    # sdists wrap files in one versioned top-level directory; wheels do not.
    if len(parts) > 1 and parts[0].lower().startswith("toolahead-") \
            and not parts[0].lower().endswith((".dist-info", ".data")):
        parts = parts[1:]
    return tuple(part.lower() for part in parts)


def _members(path: Path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, archive.read(info)
        return
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for info in archive.getmembers():
                if info.isfile():
                    source = archive.extractfile(info)
                    yield info.name, source.read() if source else b""
        return
    raise ValueError(f"unsupported distribution: {path}")


def check(path: Path) -> list[str]:
    problems: list[str] = []
    for name, data in _members(path):
        parts = _logical_parts(name)
        lowered = name.lower()
        if FORBIDDEN_PARTS.intersection(parts):
            problems.append(f"development/generated path: {name}")
        if parts and parts[-1].startswith("test_"):
            problems.append(f"test module: {name}")
        if lowered.endswith(FORBIDDEN_SUFFIXES):
            problems.append(f"generated file: {name}")
        for label, pattern in PRIVATE_PATTERNS.items():
            if pattern.search(data):
                problems.append(f"{label} in {name}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archives", nargs="+")
    args = parser.parse_args()
    failed = False
    for raw in args.archives:
        path = Path(raw)
        problems = check(path)
        if problems:
            failed = True
            print(f"FAIL {path}")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"OK   {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
