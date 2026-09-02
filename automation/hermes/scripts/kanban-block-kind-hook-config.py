#!/usr/bin/env python3
"""Render the Kanban block-kind shell-hook entries into config.yaml.

This helper preserves the existing YAML text and comments instead of loading
and dumping the whole Hermes configuration.  It writes a candidate path only;
the deployer decides whether to atomically install it.  Re-running it replaces
only entries for the same guard, so deployment is idempotent.
"""
from __future__ import annotations

import argparse
import os
import shlex
import stat
import tempfile
from pathlib import Path

GUARD_FILENAME = "kanban-block-kind-guard.py"
_MATCHERS = ("kanban_block", "terminal")


def _render_entry_block(command: str) -> list[str]:
    command_text = shlex.join(shlex.split(command))
    lines: list[str] = []
    for matcher in _MATCHERS:
        lines.extend(
            [
                f"    - matcher: {matcher}\n",
                f"      command: {command_text}\n",
                "      timeout: 10\n",
                "      fail_closed: true\n",
            ]
        )
    return lines


def _pre_tool_call_end(lines: list[str], start: int) -> int:
    """End of the ``hooks: pre_tool_call:`` block.

    The range stops at the next *sibling* key that sits at the same two-space
    indentation under ``hooks:`` (for example ``post_tool_call:``) or at the
    next unindented top-level key (for example ``logging:``).  A blank line or
    a comment that belongs to the following section does not terminate the
    range.  Terminating only at the next top-level key made a valid config with
    a ``post_tool_call`` sibling swallow that sibling (and its entries) into
    the ``pre_tool_call`` range, so the guard entries were appended at the end
    of a range that already contained the sibling — and the YAML parser then
    attached the new entries to ``post_tool_call`` instead of ``pre_tool_call``.
    """
    for index in range(start + 1, len(lines)):
        line = lines[index]
        # A two-space-indented key that is NOT the deeper four-space entry
        # form (``    - ``) is a sibling hook key and terminates the range.
        if line.startswith("  ") and not line.startswith(("    ", "  \t")) and line.lstrip() and line.lstrip()[0] not in "#-":
            return index
        # An unindented top-level key also terminates the range.
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
        if lines[index].startswith("    - ")
    ]
    return [
        (entry_start, starts[position + 1] if position + 1 < len(starts) else end)
        for position, entry_start in enumerate(starts)
    ]


def _is_guard_entry(lines: list[str], start: int, end: int) -> bool:
    block = "".join(lines[start:end])
    return any(f"matcher: {matcher}" in block for matcher in _MATCHERS) and GUARD_FILENAME in block


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
            if not _is_guard_entry(lines, entry_start, entry_end):
                kept.extend(lines[entry_start:entry_end])
            elif position == len(ranges) - 1:
                # The final entry range includes the blank separator before
                # the next top-level key.  Keep that separator when replacing
                # an existing guard, otherwise a second render drifts.
                suffix: list[str] = []
                for line in reversed(lines[entry_start:entry_end]):
                    if line.strip():
                        break
                    suffix.insert(0, line)
                kept.extend(suffix)
            cursor = entry_end
        kept.extend(lines[cursor:end])
        # Insert at the end of the existing pre_tool_call list.  A final blank
        # line is retained as-is; the next top-level key remains untouched.
        insertion = len(kept)
        while insertion > 0 and not kept[insertion - 1].strip():
            insertion -= 1
        kept[insertion:insertion] = _render_entry_block(command)
        return "".join(lines[: start + 1] + kept + lines[end:])

    hook_indices = [index for index, line in enumerate(lines) if line == "hooks:\n"]
    if hook_indices:
        index = hook_indices[-1] + 1
        return "".join(lines[:index] + ["  pre_tool_call:\n"] + _render_entry_block(command) + lines[index:])

    separator = "" if not text or text.endswith("\n") else "\n"
    return text + separator + "hooks:\n  pre_tool_call:\n" + "".join(_render_entry_block(command))


def write_candidate(source: Path, destination: Path, command: str) -> None:
    if not source.is_file():
        raise SystemExit(f"config.yaml not found: {source}")
    original = source.read_text(encoding="utf-8")
    rendered = render(original, command)
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
    args = parser.parse_args()
    command = f"python3 {args.guard}"
    write_candidate(args.source, args.destination, command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
