#!/usr/bin/env python3
"""Fake hermes CLI for board-identity-migration tests.

Implements the subset of the Hermes CLI the migration tool invokes:
``kanban boards list/create/rename/rm`` plus board-scoped task
``create/complete/archive`` with the core idempotency rule (a non-archived
task with the same idempotency key is returned instead of a duplicate).
The boards root comes from ``FAKE_KANBAN_BOARDS_ROOT``.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def _boards_root() -> Path:
    root = Path(os.environ["FAKE_KANBAN_BOARDS_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    return root


def _board_dir(slug: str) -> Path:
    return _boards_root() / slug


def _connect(slug: str) -> sqlite3.Connection:
    return sqlite3.connect(_board_dir(slug) / "kanban.db")


def _meta(slug: str) -> dict:
    path = _board_dir(slug) / "board.json"
    if not path.is_file():
        return {"slug": slug}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_meta(slug: str, meta: dict) -> None:
    _board_dir(slug).mkdir(parents=True, exist_ok=True)
    meta["slug"] = slug
    meta.setdefault("archived", False)
    (_board_dir(slug) / "board.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def _ensure_db(slug: str) -> None:
    if not _board_dir(slug).is_dir():
        _board_dir(slug).mkdir(parents=True, exist_ok=True)
    db = _board_dir(slug) / "kanban.db"
    if not db.is_file():
        con = _connect(slug)
        con.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
            "status TEXT, idempotency_key TEXT)"
        )
        con.commit()
        con.close()


def _boards_list(args: list[str]) -> int:
    root = _boards_root()
    items = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            meta = _meta(child.name)
            if meta.get("archived"):
                continue
            items.append({"slug": child.name, "name": meta.get("name", child.name)})
    if "--json" in args:
        print(json.dumps(items))
    else:
        for item in items:
            print(item["slug"])
    return 0


def _boards_create(args: list[str]) -> int:
    slug = args[0]
    rest = args[1:]
    name = None
    workdir = None
    i = 0
    while i < len(rest):
        if rest[i] == "--name":
            name = rest[i + 1]; i += 2
        elif rest[i] == "--description":
            i += 2
        elif rest[i] == "--default-workdir":
            workdir = rest[i + 1]; i += 2
        else:
            i += 1
    _ensure_db(slug)
    meta = _meta(slug)
    meta["name"] = name or slug
    meta["default_workdir"] = workdir or ""
    meta["archived"] = False
    _write_meta(slug, meta)
    return 0


def _boards_rename(args: list[str]) -> int:
    slug, name = args[0], args[1]
    if not _board_dir(slug).is_dir():
        print(f"fake-hermes: board {slug} does not exist", file=sys.stderr)
        return 2
    meta = _meta(slug)
    meta["name"] = name
    meta["archived"] = False
    _write_meta(slug, meta)
    return 0


def _boards_rm(args: list[str]) -> int:
    slug = args[0]
    hard = "--delete" in args
    bdir = _board_dir(slug)
    if not bdir.is_dir():
        print(f"fake-hermes: board {slug} does not exist", file=sys.stderr)
        return 2
    if hard:
        shutil.rmtree(bdir)
        return 0
    arch = _boards_root() / "_archived"
    arch.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    target = arch / f"{slug}-{ts}"
    suffix = 1
    while target.exists():
        target = arch / f"{slug}-{ts}-{suffix}"
        suffix += 1
    bdir.rename(target)
    return 0


def _task_create(args: list[str], board: str) -> int:
    title = args[0]
    rest = args[1:]
    key = None
    initial = "todo"
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--body", "--assignee", "--created-by"):
            i += 2
        elif arg == "--idempotency-key":
            key = rest[i + 1]; i += 2
        elif arg == "--initial-status":
            initial = rest[i + 1]; i += 2
        elif arg == "--json":
            i += 1
        else:
            i += 2
    _ensure_db(board)
    con = _connect(board)
    if key is not None:
        row = con.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived'",
            (key,),
        ).fetchone()
        if row:
            print(json.dumps({"id": row[0], "status": row[1], "created_at": 0}))
            con.close()
            return 0
    count = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    new_id = f"t_fake{count + 1:03d}"
    con.execute(
        "INSERT INTO tasks (id, title, status, idempotency_key) VALUES (?,?,?,?)",
        (new_id, title, initial, key),
    )
    con.commit()
    print(json.dumps({"id": new_id, "status": initial, "created_at": int(time.time())}))
    con.close()
    return 0


def _task_complete(args: list[str], board: str) -> int:
    _ensure_db(board)
    con = _connect(board)
    for tid in [a for a in args if not a.startswith("--")]:
        con.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    con.commit()
    con.close()
    return 0


def _task_archive(args: list[str], board: str) -> int:
    _ensure_db(board)
    con = _connect(board)
    if "--rm" in args:
        ids = args[args.index("--rm") + 1:]
        for tid in ids:
            con.execute("DELETE FROM tasks WHERE id=?", (tid,))
    else:
        for tid in [a for a in args if not a.startswith("--")]:
            con.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
    con.commit()
    con.close()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] != "kanban":
        print("fake-hermes: only 'kanban' is supported", file=sys.stderr)
        return 2
    args = args[1:]
    board = None
    if args and args[0] == "--board":
        board = args[1]
        args = args[2:]
    cmd, rest = args[0], args[1:]
    if cmd == "boards":
        sub, sub_args = rest[0], rest[1:]
        if sub == "list":
            return _boards_list(sub_args)
        if sub == "create":
            return _boards_create(sub_args)
        if sub == "rename":
            return _boards_rename(sub_args)
        if sub in ("rm", "remove"):
            return _boards_rm(sub_args)
        print(f"fake-hermes: unsupported boards subcommand {sub}", file=sys.stderr)
        return 2
    if board is None:
        print("fake-hermes: task commands require --board", file=sys.stderr)
        return 2
    if cmd == "create":
        return _task_create(rest, board)
    if cmd == "complete":
        return _task_complete(rest, board)
    if cmd == "archive":
        return _task_archive(rest, board)
    print(f"fake-hermes: unsupported command {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
