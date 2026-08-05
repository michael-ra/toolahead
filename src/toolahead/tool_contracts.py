"""Stable ToolAhead tool contracts shared by prefetch workers and MCP calls.

The important invariant is that a cold execution and a cache replay are
rendered by the same functions.  The model therefore sees the same text,
structured fields, exit semantics, path rules, and truncation behavior on both
paths.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_READ_LIMIT = 2_000
MAX_READ_LIMIT = 10_000
DEFAULT_SEARCH_LIMIT = 200
MAX_SEARCH_LIMIT = 2_000
SKIP_DIRS = {".git", "__pycache__", "node_modules"}
SHELL_CONTROL = {";", "&&", "||", "|", "&", "(", ")", ">", ">>", "<", "<<"}


class ToolContractError(ValueError):
    """A user-visible, deterministic tool input or workspace error."""


def shell_words(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return command.split()


def replayable_command(command: str) -> bool:
    """Whether a command is safe to speculate in a disposable checkout.

    This intentionally recognizes a narrow family of test and lint commands.
    Exact project opt-in is a second, separate requirement for replay.
    """

    if not isinstance(command, str) or not command.strip():
        return False
    if "\n" in command or "`" in command or "$(" in command:
        return False
    words = shell_words(command)
    if not words or any(word in SHELL_CONTROL for word in words):
        return False
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words.pop(0)
    if not words:
        return False
    executable = os.path.basename(words[0]).lower()
    if executable in {"pytest", "py.test", "ruff", "eslint", "tsc", "mypy",
                      "jest", "vitest"}:
        return True
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        return len(words) >= 3 and words[1] == "-m" and words[2] in {
            "unittest", "pytest"}
    if executable == "npm":
        return words[1:2] == ["test"] or words[1:3] == ["run", "test"]
    if executable == "yarn":
        return words[1:2] == ["test"]
    if executable in {"go", "cargo", "make"}:
        return words[1:2] == ["test"]
    return False


@dataclass(frozen=True)
class ToolOutcome:
    """Raw result cached by ToolAhead before agent-specific rendering."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0

    @property
    def combined(self) -> str:
        return self.stdout + self.stderr

    def as_json(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "ToolOutcome":
        return cls(
            stdout=str(value.get("stdout") or ""),
            stderr=str(value.get("stderr") or ""),
            exit_code=int(value.get("exit_code") or 0),
        )


def _positive_int(value: Any, *, default: int, maximum: int,
                  name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ToolContractError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolContractError(f"{name} must be an integer") from exc
    if parsed < 1 or parsed > maximum:
        raise ToolContractError(f"{name} must be between 1 and {maximum}")
    return parsed


def resolve_path(workspace: str, raw_path: str, *, must_exist: bool = True) -> str:
    """Resolve a path and reject lexical and symlink escapes from workspace."""

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolContractError("path must be a non-empty string")
    root = os.path.realpath(os.path.abspath(workspace))
    candidate = raw_path if os.path.isabs(raw_path) else os.path.join(root, raw_path)
    candidate = os.path.realpath(os.path.abspath(candidate))
    try:
        contained = os.path.commonpath((root, candidate)) == root
    except ValueError:
        contained = False
    if not contained:
        raise ToolContractError("path is outside the configured workspace")
    if must_exist and not os.path.exists(candidate):
        raise ToolContractError(f"path does not exist: {display_path(workspace, candidate)}")
    return candidate


def display_path(workspace: str, path: str) -> str:
    """Return a stable slash-separated project-relative display path."""

    root = os.path.realpath(os.path.abspath(workspace))
    absolute = os.path.realpath(os.path.abspath(path))
    try:
        if os.path.commonpath((root, absolute)) == root:
            value = os.path.relpath(absolute, root)
            return "." if value == "." else value.replace(os.sep, "/")
    except ValueError:
        pass
    return absolute.replace(os.sep, "/")


def normalize_read_input(value: dict[str, Any]) -> dict[str, Any]:
    """Accept familiar Read aliases and return the canonical MCP shape."""

    if not isinstance(value, dict):
        raise ToolContractError("arguments must be an object")
    path = value.get("file_path", value.get("path"))
    if not isinstance(path, str) or not path.strip():
        raise ToolContractError("file_path is required")
    offset = _positive_int(value.get("offset"), default=1,
                           maximum=2_147_483_647, name="offset")
    limit = _positive_int(value.get("limit"), default=DEFAULT_READ_LIMIT,
                          maximum=MAX_READ_LIMIT, name="limit")
    return {"file_path": path, "offset": offset, "limit": limit}


def normalize_search_input(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize a ripgrep-style search request without changing its regex."""

    if not isinstance(value, dict):
        raise ToolContractError("arguments must be an object")
    # ``pattern`` is canonical. Codex occasionally emits the otherwise
    # familiar search schema with ``query`` despite the advertised MCP field,
    # so accept it as a narrow compatibility alias.
    pattern = value.get("pattern", value.get("query"))
    if not isinstance(pattern, str) or pattern == "":
        raise ToolContractError("pattern is required")
    path = value.get("path", ".")
    if not isinstance(path, str) or not path.strip():
        raise ToolContractError("path must be a non-empty string")
    glob = value.get("glob")
    if glob is None:
        globs: list[str] = []
    elif isinstance(glob, str):
        globs = [glob]
    elif isinstance(glob, list) and all(isinstance(item, str) for item in glob):
        globs = list(glob)
    else:
        raise ToolContractError("glob must be a string or an array of strings")
    output_mode = value.get("output_mode", "content")
    if output_mode not in ("content", "files_with_matches", "count"):
        raise ToolContractError(
            "output_mode must be content, files_with_matches, or count")
    head_limit = _positive_int(
        value.get("head_limit", value.get("limit")),
        default=DEFAULT_SEARCH_LIMIT, maximum=MAX_SEARCH_LIMIT,
        name="head_limit")
    offset = _positive_int(value.get("offset"), default=1,
                           maximum=2_147_483_647, name="offset")
    case_insensitive = bool(value.get("case_insensitive", value.get("-i", False)))
    multiline = bool(value.get("multiline", False))
    return {
        "pattern": pattern,
        "path": path,
        "glob": globs,
        "output_mode": output_mode,
        "head_limit": head_limit,
        "offset": offset,
        "case_insensitive": case_insensitive,
        "multiline": multiline,
    }


def normalize_list_input(value: dict[str, Any]) -> dict[str, Any]:
    """Canonical Glob-like file listing contract."""

    if not isinstance(value, dict):
        raise ToolContractError("arguments must be an object")
    pattern = value.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ToolContractError("pattern is required")
    path = value.get("path", ".")
    if not isinstance(path, str) or not path.strip():
        raise ToolContractError("path must be a non-empty string")
    limit = _positive_int(value.get("limit"), default=DEFAULT_SEARCH_LIMIT,
                          maximum=MAX_SEARCH_LIMIT, name="limit")
    return {"pattern": pattern, "path": path, "limit": limit}


def normalize_run_input(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolContractError("arguments must be an object")
    command = value.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolContractError("command is required")
    result: dict[str, Any] = {"command": command.strip()}
    description = value.get("description")
    if isinstance(description, str) and description.strip():
        result["description"] = description.strip()
    return result


def normalize_edit_input(value: dict[str, Any]) -> dict[str, Any]:
    """Canonical Claude-like exact replacement contract."""

    if not isinstance(value, dict):
        raise ToolContractError("arguments must be an object")
    path = value.get("file_path", value.get("path"))
    old = value.get("old_string", value.get("old"))
    new = value.get("new_string", value.get("new"))
    if not isinstance(path, str) or not path.strip():
        raise ToolContractError("file_path is required")
    if not isinstance(old, str) or not old:
        raise ToolContractError("old_string must be a non-empty string")
    if not isinstance(new, str):
        raise ToolContractError("new_string must be a string")
    return {
        "file_path": path,
        "old_string": old,
        "new_string": new,
        "replace_all": bool(value.get("replace_all", False)),
    }


def normalize_write_input(value: dict[str, Any]) -> dict[str, Any]:
    """Canonical Write-like whole-file contract."""

    if not isinstance(value, dict):
        raise ToolContractError("arguments must be an object")
    path = value.get("file_path", value.get("path"))
    content = value.get("content")
    if not isinstance(path, str) or not path.strip():
        raise ToolContractError("file_path is required")
    if not isinstance(content, str):
        raise ToolContractError("content must be a string")
    return {"file_path": path, "content": content}


def read_file(workspace: str, arguments: dict[str, Any]) -> ToolOutcome:
    args = normalize_read_input(arguments)
    path = resolve_path(workspace, args["file_path"])
    if not os.path.isfile(path):
        raise ToolContractError(
            f"not a regular file: {display_path(workspace, path)}")
    data = Path(path).read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolContractError(
            f"binary files are not supported: {display_path(workspace, path)}")
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = args["offset"] - 1
    selected = lines[start:start + args["limit"]]
    rendered = "".join(
        f"{number:6}\t{line}\n"
        for number, line in enumerate(selected, start=args["offset"])
    )
    return ToolOutcome(stdout=rendered)


def edit_file(workspace: str, arguments: dict[str, Any]) -> ToolOutcome:
    """Apply an exact requested edit; edits are never prefetched or replayed."""

    args = normalize_edit_input(arguments)
    path = resolve_path(workspace, args["file_path"])
    if not os.path.isfile(path):
        raise ToolContractError(
            f"not a regular file: {display_path(workspace, path)}")
    data = Path(path).read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolContractError(
            f"binary files are not supported: {display_path(workspace, path)}")
    text = data.decode("utf-8", errors="strict")
    occurrences = text.count(args["old_string"])
    if occurrences == 0:
        raise ToolContractError("old_string was not found in the file")
    if occurrences > 1 and not args["replace_all"]:
        raise ToolContractError(
            f"old_string occurs {occurrences} times; provide more context or set replace_all")
    count = -1 if args["replace_all"] else 1
    updated = text.replace(args["old_string"], args["new_string"], count)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    return ToolOutcome(stdout=f"Updated {display_path(workspace, path)}.\n")


def write_file(workspace: str, arguments: dict[str, Any]) -> ToolOutcome:
    """Create or overwrite one UTF-8 file inside the workspace."""

    args = normalize_write_input(arguments)
    path = resolve_path(workspace, args["file_path"], must_exist=False)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise ToolContractError(
            f"parent directory does not exist: {display_path(workspace, parent)}")
    if os.path.isdir(path):
        raise ToolContractError(
            f"path is a directory: {display_path(workspace, path)}")
    existed = os.path.exists(path)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(args["content"])
    action = "Updated" if existed else "Created"
    return ToolOutcome(stdout=f"{action} {display_path(workspace, path)}.\n")


def _glob_matches(relative: str, globs: list[str]) -> bool:
    if not globs:
        return True
    included = [item for item in globs if not item.startswith("!")]
    excluded = [item[1:] for item in globs if item.startswith("!")]
    if included and not any(fnmatch.fnmatch(relative, item) or
                            fnmatch.fnmatch(os.path.basename(relative), item)
                            for item in included):
        return False
    return not any(fnmatch.fnmatch(relative, item) or
                   fnmatch.fnmatch(os.path.basename(relative), item)
                   for item in excluded)


def _internal_runtime_path(relative: str) -> bool:
    """Hide ToolAhead's installed implementation from workspace tools."""

    value = relative.replace(os.sep, "/")
    return (value == ".toolahead" or value.startswith(".toolahead/") or
            value == ".codex/hooks/toolahead" or
            value.startswith(".codex/hooks/toolahead/"))


def _iter_search_files(workspace: str, target: str, globs: list[str]):
    if os.path.isfile(target):
        relative = display_path(workspace, target)
        if not _internal_runtime_path(relative) and _glob_matches(relative, globs):
            yield target
        return
    for directory, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = os.path.join(directory, name)
            try:
                # A file symlink inside the tree must not turn a read-only
                # search into an escape from the configured workspace.
                resolve_path(workspace, path)
            except ToolContractError:
                continue
            relative = display_path(workspace, path)
            if not _internal_runtime_path(relative) and _glob_matches(relative, globs):
                yield path


def _search_with_python(workspace: str, args: dict[str, Any], target: str) -> list[str]:
    flags = re.IGNORECASE if args["case_insensitive"] else 0
    if args["multiline"]:
        flags |= re.MULTILINE | re.DOTALL
    try:
        regex = re.compile(args["pattern"], flags)
    except re.error as exc:
        raise ToolContractError(f"invalid regular expression: {exc}") from exc
    rows: list[str] = []
    for path in _iter_search_files(workspace, target, args["glob"]):
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        relative = display_path(workspace, path)
        matches: list[tuple[int, str]] = []
        if args["multiline"]:
            for match in regex.finditer(text):
                line_number = text.count("\n", 0, match.start()) + 1
                line = text.splitlines()[line_number - 1] if text.splitlines() else ""
                matches.append((line_number, line))
        else:
            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append((line_number, line))
        if not matches:
            continue
        if args["output_mode"] == "files_with_matches":
            rows.append(relative)
        elif args["output_mode"] == "count":
            rows.append(f"{relative}:{len(matches)}")
        else:
            rows.extend(f"{relative}:{number}:{line}" for number, line in matches)
    return rows


def _search_with_rg(workspace: str, args: dict[str, Any], target: str) -> list[str]:
    command = ["rg", "--color", "never", "--no-heading", "--sort", "path"]
    if args["case_insensitive"]:
        command.append("--ignore-case")
    if args["multiline"]:
        command.extend(("--multiline", "--multiline-dotall"))
    if args["output_mode"] == "files_with_matches":
        command.append("--files-with-matches")
    elif args["output_mode"] == "count":
        command.append("--count")
    else:
        command.append("--line-number")
    for item in args["glob"]:
        command.extend(("--glob", item))
    command.extend(("--", args["pattern"], target))
    process = subprocess.run(command, cwd=workspace, capture_output=True, text=True)
    if process.returncode not in (0, 1):
        message = (process.stderr or process.stdout).strip()
        raise ToolContractError(message or f"search failed with exit code {process.returncode}")
    rows = process.stdout.splitlines()
    normalized = []
    for row in rows:
        normalized.append(row[2:] if row.startswith("./") else row)
    return normalized


def search_files(workspace: str, arguments: dict[str, Any]) -> ToolOutcome:
    args = normalize_search_input(arguments)
    target = resolve_path(workspace, args["path"])
    relative_target = display_path(workspace, target)
    rows = (_search_with_rg(workspace, args, relative_target)
            if shutil.which("rg") else
            _search_with_python(workspace, args, target))
    start = args["offset"] - 1
    selected = rows[start:start + args["head_limit"]]
    return ToolOutcome(stdout="".join(f"{row}\n" for row in selected))


def _glob_matches_pattern(relative: str, pattern: str) -> bool:
    relative = relative.replace(os.sep, "/")
    pattern = pattern.replace(os.sep, "/")
    return (fnmatch.fnmatch(relative, pattern) or
            (pattern.startswith("**/") and
             fnmatch.fnmatch(relative, pattern[3:])))


def list_files(workspace: str, arguments: dict[str, Any]) -> ToolOutcome:
    """List matching files with deterministic project-relative paths."""

    args = normalize_list_input(arguments)
    target = resolve_path(workspace, args["path"])
    rows: list[str] = []
    if os.path.isfile(target):
        relative = display_path(workspace, target)
        if _glob_matches_pattern(relative, args["pattern"]):
            rows.append(relative)
    else:
        for path in _iter_search_files(workspace, target, []):
            relative = display_path(workspace, path)
            if _glob_matches_pattern(relative, args["pattern"]):
                rows.append(relative)
    rows.sort()
    return ToolOutcome(
        stdout="".join(f"{row}\n" for row in rows[:args["limit"]]))


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def _stop_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Terminate a command and every child it spawned, then collect output."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=0.5)
    except (ProcessLookupError, subprocess.TimeoutExpired) as first_error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired as final_error:
            # A detached grandchild can keep inherited stdout/stderr pipe FDs
            # open even after the original process group is gone. Never let
            # cancellation pin a ToolAhead worker forever.
            stdout = final_error.stdout if final_error.stdout is not None \
                else getattr(first_error, "stdout", "")
            stderr = final_error.stderr if final_error.stderr is not None \
                else getattr(first_error, "stderr", "")
            for stream in (process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
    return _text(stdout), _text(stderr)


def run_command(workspace: str, arguments: dict[str, Any], *,
                timeout: float = 120.0,
                cancel_event: threading.Event | None = None) -> ToolOutcome:
    args = normalize_run_input(arguments)
    process = subprocess.Popen(
        args["command"], cwd=workspace, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    deadline = time.monotonic() + max(0.1, timeout)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            stdout, stderr = _stop_process_group(process)
            stderr += "\ntoolahead: superseded by a newer workspace mutation\n"
            return ToolOutcome(stdout, stderr, 130)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout, stderr = _stop_process_group(process)
            stderr += f"\ntoolahead: command timed out after {timeout:.1f}s\n"
            return ToolOutcome(stdout, stderr, 124)
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            return ToolOutcome(stdout or "", stderr or "",
                               int(process.returncode or 0))
        except subprocess.TimeoutExpired:
            continue


def execute(tool: str, workspace: str, arguments: dict[str, Any], *,
            command_timeout: float = 120.0,
            cancel_event: threading.Event | None = None) -> ToolOutcome:
    if tool == "read":
        return read_file(workspace, arguments)
    if tool == "grep":
        return search_files(workspace, arguments)
    if tool == "glob":
        return list_files(workspace, arguments)
    if tool == "edit":
        return edit_file(workspace, arguments)
    if tool == "write":
        return write_file(workspace, arguments)
    if tool == "bash":
        return run_command(workspace, arguments, timeout=command_timeout,
                           cancel_event=cancel_event)
    raise ToolContractError(f"unsupported tool: {tool}")


def visible_text(tool: str, outcome: ToolOutcome) -> str:
    """Model-visible text. Cache hits and misses both pass through here."""

    if tool != "bash":
        return outcome.combined
    body = outcome.combined
    if outcome.exit_code:
        separator = "" if not body or body.endswith("\n") else "\n"
        body += f"{separator}Process exited with code {outcome.exit_code}.\n"
    return body


def structured_result(tool: str, arguments: dict[str, Any],
                      outcome: ToolOutcome) -> dict[str, Any]:
    result: dict[str, Any] = {
        "stdout": outcome.stdout,
        "stderr": outcome.stderr,
        "exit_code": outcome.exit_code,
    }
    if tool == "read":
        args = normalize_read_input(arguments)
        result.update({"file_path": args["file_path"], "offset": args["offset"],
                       "limit": args["limit"]})
    elif tool == "grep":
        args = normalize_search_input(arguments)
        result.update({"pattern": args["pattern"], "path": args["path"],
                       "output_mode": args["output_mode"]})
    elif tool == "glob":
        args = normalize_list_input(arguments)
        result.update({"pattern": args["pattern"], "path": args["path"],
                       "limit": args["limit"]})
    elif tool == "bash":
        result["command"] = normalize_run_input(arguments)["command"]
    elif tool == "edit":
        args = normalize_edit_input(arguments)
        result.update({"file_path": args["file_path"],
                       "replace_all": args["replace_all"]})
    elif tool == "write":
        result["file_path"] = normalize_write_input(arguments)["file_path"]
    return result
