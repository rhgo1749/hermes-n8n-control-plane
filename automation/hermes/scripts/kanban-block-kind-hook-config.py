#!/usr/bin/env python3
"""Render fail-closed H4V3 Kanban lifecycle guard entries into config.yaml.

This helper preserves the existing YAML text and comments instead of loading
and dumping the whole Hermes configuration. It writes a candidate path only;
the deployer decides whether to atomically install it. Re-running it replaces
entries for the same approved guard command and retires the superseded
``kanban-workspace-guard.py`` hook, so deployment is idempotent and cannot
leave two competing workspace creation policies active.

The historical ``kanban-block-kind-guard.py`` command path remains stable for
shell-hook consent. The command is now a small lifecycle wrapper and is
registered for ``kanban_block``, ``kanban_create``, and ``terminal``; it owns
the specialist completion-contract and workspace-binding creation boundaries
without introducing a second allowlist approval.
"""
from __future__ import annotations

import argparse
import os
import shlex
import stat
import tempfile
from pathlib import Path

GUARD_FILENAME = "kanban-block-kind-guard.py"
DEFAULT_HERMES_PYTHON = Path(
    os.environ.get(
        "HERMES_PYTHON_BIN", "/ws/hermes-agent/venv/bin/python3"
    )
)
LEGACY_WORKSPACE_GUARD_FILENAME = "kanban-workspace-guard.py"
DEDUP_GUARD_PATH = "/home/hermes/.hermes/scripts/kanban-dedup-guard.py"
_MATCHERS = ("kanban_block", "kanban_create", "terminal")


def _render_entry_block(command: str, entry_indent: str = "    ") -> list[str]:
    command_text = shlex.join(shlex.split(command))
    field_indent = entry_indent + "  "
    lines: list[str] = []
    for matcher in _MATCHERS:
        lines.extend(
            [
                f"{entry_indent}- matcher: {matcher}\n",
                f"{field_indent}command: {command_text}\n",
                f"{field_indent}timeout: 10\n",
                f"{field_indent}fail_closed: true\n",
            ]
        )
    return lines

def _pre_tool_call_end(lines: list[str], start: int) -> int:
    """End of the ``hooks: pre_tool_call:`` block.

    The range stops at the next *sibling* key that sits at the same two-space
    indentation under ``hooks:`` (for example ``post_tool_call:``) or at the
    next unindented top-level key (for example ``logging:``). A blank line or
    a comment that belongs to the following section does not terminate the
    range.
    """
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if (
            line.startswith("  ")
            and not line.startswith(("    ", "  \t"))
            and line.lstrip()
            and line.lstrip()[0] not in "#-"
        ):
            return index
        if line.strip() and not line.startswith(" "):
            return index
    return len(lines)


def _pre_tool_call_range(lines: list[str]) -> tuple[int, int] | None:
    for index, line in enumerate(lines):
        if line.startswith("  pre_tool_call:"):
            return index, _pre_tool_call_end(lines, index)
    return None


def _entry_ranges(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    starts = [
        index
        for index in range(start + 1, end)
        if lines[index].startswith(("  - ", "    - "))
    ]
    return [
        (entry_start, starts[position + 1] if position + 1 < len(starts) else end)
        for position, entry_start in enumerate(starts)
    ]


def _is_replaced_guard_entry(lines: list[str], start: int, end: int) -> bool:
    block = "".join(lines[start:end])
    return any(
        filename in block
        for filename in (GUARD_FILENAME, LEGACY_WORKSPACE_GUARD_FILENAME)
    )


def render(text: str, command: str) -> str:
    lines = text.splitlines(keepends=True)
    found = _pre_tool_call_range(lines)
    if found is not None:
        start, end = found
        ranges = _entry_ranges(lines, start, end)
        kept: list[str] = []
        cursor = start + 1
        for position, (entry_start, entry_end) in enumerate(ranges):
            kept.extend(lines[cursor:entry_start])
            if not _is_replaced_guard_entry(lines, entry_start, entry_end):
                kept.extend(lines[entry_start:entry_end])
            elif position == len(ranges) - 1:
                # The final entry range includes the blank separator before
                # the next sibling/top-level key. Preserve it while replacing
                # old lifecycle-guard entries.
                suffix: list[str] = []
                for line in reversed(lines[entry_start:entry_end]):
                    if line.strip():
                        break
                    suffix.insert(0, line)
                kept.extend(suffix)
            cursor = entry_end
        kept.extend(lines[cursor:end])
        insertion = len(kept)
        while insertion > 0 and not kept[insertion - 1].strip():
            insertion -= 1
        entry_indent = (
            lines[ranges[0][0]][: len(lines[ranges[0][0]]) - len(lines[ranges[0][0]].lstrip())]
            if ranges
            else "    "
        )
        kept[insertion:insertion] = _render_entry_block(command, entry_indent)
        return "".join(lines[: start + 1] + kept + lines[end:])

    hook_indices = [index for index, line in enumerate(lines) if line == "hooks:\n"]
    if hook_indices:
        index = hook_indices[-1] + 1
        return "".join(
            lines[:index]
            + ["  pre_tool_call:\n"]
            + _render_entry_block(command)
            + lines[index:]
        )

    separator = "" if not text or text.endswith("\n") else "\n"
    return text + separator + "hooks:\n  pre_tool_call:\n" + "".join(_render_entry_block(command))


def _normalize_known_runtime_commands(text: str, command: str) -> str:
    python_bin = shlex.split(command)[0]
    canonical = f"{python_bin} {DEDUP_GUARD_PATH}"
    lines = text.splitlines(keepends=True)
    rendered: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        if not stripped.startswith("command:"):
            rendered.append(line)
            index += 1
            continue

        indent = line[: len(line) - len(stripped)]
        raw_parts = [stripped[len("command:"):].strip()]
        end = index + 1
        while end < len(lines):
            continuation = lines[end]
            continuation_stripped = continuation.lstrip()
            continuation_indent = continuation[: len(continuation) - len(continuation_stripped)]
            if not continuation_stripped.strip() or len(continuation_indent) <= len(indent):
                break
            raw_parts.append(continuation_stripped.strip())
            end += 1

        try:
            parts = shlex.split(" ".join(raw_parts))
        except ValueError:
            rendered.extend(lines[index:end])
            index = end
            continue

        if (
            len(parts) == 2
            and parts[1] == DEDUP_GUARD_PATH
            and parts[0].endswith("python3")
        ):
            newline = "\n" if any(part.endswith("\n") for part in lines[index:end]) else ""
            rendered.append(f"{indent}command: {canonical}{newline}")
        else:
            rendered.extend(lines[index:end])
        index = end

    return "".join(rendered)

def write_candidate(source: Path, destination: Path, command: str) -> None:
    if not source.is_file():
        raise SystemExit(f"config.yaml not found: {source}")
    original = source.read_text(encoding="utf-8")
    rendered = _normalize_known_runtime_commands(render(original, command), command)
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(source.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--guard", type=Path, required=True)
    parser.add_argument(
        "--python",
        dest="python_bin",
        type=Path,
        default=DEFAULT_HERMES_PYTHON,
    )
    args = parser.parse_args()
    command = f"{args.python_bin} {args.guard}"
    write_candidate(args.source, args.destination, command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
