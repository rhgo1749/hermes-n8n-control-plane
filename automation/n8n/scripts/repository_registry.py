#!/usr/bin/env python3
"""Read-only discovery of GitHub repositories opted into Hermes management.

Shadow registry: discovers repositories by GitHub topic, derives repository
metadata, and resolves existing Kanban board association from durable task
provenance. It does not mutate GitHub, n8n, Hermes, Kanban, webhooks, or cron
state.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

GITHUB_API = "https://api.github.com"
DEFAULT_TOPIC = "hermes-agent"
DEFAULT_CHECKOUT_ROOT = Path("/ws/projects")
DEFAULT_KANBAN_BOARDS_ROOT = Path("/home/hermes/.hermes/kanban/boards")
HTTP_TIMEOUT_SECONDS = 30
MAX_PAGES = 20
GITHUB_ISSUE_KEY = re.compile(r"^github:([^:]+/[^:]+):issue:\d+$", re.IGNORECASE)
CONTRACT_CANDIDATES: tuple[str, ...] = (
    "AGENTS.md",
    "AGENTS_PROJECT.md",
    "Docs/AGENTS.md",
    ".agent/PR_REQUEST_TEMPLATE.md",
)


class RegistryError(RuntimeError):
    """Discovery or normalization failed closed."""


@dataclass(frozen=True)
class RegistryEntry:
    repository: str
    repository_id: int
    default_branch: str
    canonical_slug: str
    board: str | None
    board_status: str
    checkout: str
    checkout_status: str
    checkout_remote: str | None
    contract_paths: tuple[str, ...]
    ready: bool
    reason: str | None


def _github_token() -> str:
    token = (os.environ.get("HERMES_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        raise RegistryError("GitHub token is required (HERMES_GITHUB_TOKEN or GITHUB_TOKEN)")
    return token


def _github_json(
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    allow_not_found: bool = False,
) -> Any:
    query = f"?{urlencode(params)}" if params else ""
    req = Request(
        f"{GITHUB_API}{path}{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-n8n-control-plane/repository-registry",
        },
    )
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except HTTPError as exc:
        if allow_not_found and exc.code == 404:
            return None
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RegistryError(f"GitHub API HTTP {exc.code}: {body}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RegistryError(f"GitHub API request failed: {exc}") from exc


def discover_repositories(token: str, owner: str, topic: str) -> list[dict[str, Any]]:
    """Return all accessible, non-archived owner repos carrying ``topic``."""
    owner = owner.strip()
    topic = topic.strip().lower()
    if not owner or not topic:
        raise RegistryError("owner and topic must be non-empty")

    query = f"user:{owner} topic:{topic}"
    found: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        payload = _github_json(
            token,
            "/search/repositories",
            {"q": query, "per_page": 100, "page": page, "sort": "full_name", "order": "asc"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise RegistryError("GitHub repository search returned an unexpected payload")
        items = [item for item in payload["items"] if isinstance(item, dict)]
        found.extend(items)
        if len(items) < 100:
            break
    else:
        raise RegistryError(f"repository search exceeded {MAX_PAGES} pages")

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for repo in found:
        full_name = str(repo.get("full_name") or "").strip()
        repo_owner = str((repo.get("owner") or {}).get("login") or "").strip()
        if not full_name or repo_owner.casefold() != owner.casefold():
            continue
        if bool(repo.get("archived")):
            continue
        key = full_name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(repo)
    return sorted(result, key=lambda item: str(item.get("full_name") or "").casefold())


def _github_contracts(
    token: str,
    repository: str,
    default_branch: str,
    *,
    fetch_json: Callable[..., Any] | None = None,
) -> tuple[str, ...]:
    """Detect contract files from the repository's authoritative default branch."""
    fetch = fetch_json or _github_json
    repository_path = quote(repository, safe="/")
    found: list[str] = []
    for candidate in CONTRACT_CANDIDATES:
        candidate_path = quote(candidate, safe="/")
        payload = fetch(
            token,
            f"/repos/{repository_path}/contents/{candidate_path}",
            {"ref": default_branch},
            allow_not_found=True,
        )
        if payload is None:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise RegistryError(
                f"contract candidate is not a file on {repository}@{default_branch}: {candidate}"
            )
        found.append(candidate)
    return tuple(found)


def _normalise_remote(value: str) -> str:
    """Normalize common GitHub HTTPS/SSH/git remotes to ``owner/repo``."""
    raw = value.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    prefixes = (
        "https://github.com/",
        "http://github.com/",
        "ssh://git@github.com/",
        "git://github.com/",
        "git@github.com:",
    )
    for prefix in prefixes:
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    return raw.strip("/")


def _git_origin(checkout: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _kanban_board_repository_evidence(boards_root: Path) -> dict[str, tuple[str, ...]]:
    """Read GitHub repository identities recorded in each live board's task keys."""
    if not boards_root.exists():
        return {}
    if not boards_root.is_dir():
        raise RegistryError(f"Kanban boards root is not a directory: {boards_root}")

    evidence: dict[str, tuple[str, ...]] = {}
    for db in sorted(boards_root.glob("*/kanban.db")):
        board = db.parent.name
        if board.startswith("_"):
            continue

        repositories: set[str] = set()
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT idempotency_key FROM tasks WHERE idempotency_key IS NOT NULL"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            raise RegistryError(f"could not read Kanban provenance for board {board}: {exc}") from exc

        for (raw_key,) in rows:
            match = GITHUB_ISSUE_KEY.match(str(raw_key or ""))
            if match:
                repositories.add(match.group(1).casefold())
        evidence[board] = tuple(sorted(repositories))
    return evidence


def _resolve_board(
    repository: str,
    evidence: dict[str, tuple[str, ...]],
) -> tuple[str | None, str]:
    """Resolve one existing board from durable GitHub issue task provenance."""
    repository_key = repository.casefold()
    exact: list[str] = []
    conflicted: list[str] = []

    for board, repositories in evidence.items():
        if repository_key not in repositories:
            continue
        if len(repositories) == 1:
            exact.append(board)
        else:
            conflicted.append(board)

    if conflicted:
        return None, "ambiguous_task_provenance"
    if len(exact) == 1:
        return exact[0], "resolved_task_provenance"
    if not exact:
        return None, "not_found_task_provenance"
    return None, "ambiguous_multiple_boards"


def build_entry(
    repo: dict[str, Any],
    checkout_root: Path,
    *,
    contract_paths: Iterable[str] = (),
    board: str | None = None,
    board_status: str = "not_found_task_provenance",
    origin_reader: Callable[[Path], str | None] | None = None,
) -> RegistryEntry:
    full_name = str(repo.get("full_name") or "").strip()
    if "/" not in full_name:
        raise RegistryError(f"repository full_name is invalid: {full_name!r}")
    try:
        repository_id = int(repo["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistryError(f"repository id is invalid for {full_name}") from exc
    default_branch = str(repo.get("default_branch") or "").strip()
    if not default_branch:
        raise RegistryError(f"repository default_branch is missing for {full_name}")

    unknown_contracts = set(contract_paths) - set(CONTRACT_CANDIDATES)
    if unknown_contracts:
        raise RegistryError(
            f"unknown contract paths for {full_name}: {', '.join(sorted(unknown_contracts))}"
        )
    contract_set = set(contract_paths)
    contracts = tuple(path for path in CONTRACT_CANDIDATES if path in contract_set)

    repo_name = full_name.split("/", 1)[1]
    slug = repo_name.casefold()
    checkout = checkout_root / slug
    read_origin = origin_reader or _git_origin
    origin = read_origin(checkout) if checkout.is_dir() else None

    if not checkout.exists():
        checkout_status = "missing"
        ready = False
        reason = "checkout_missing"
    elif not checkout.is_dir():
        checkout_status = "not_directory"
        ready = False
        reason = "checkout_not_directory"
    elif origin is None:
        checkout_status = "origin_unavailable"
        ready = False
        reason = "checkout_origin_unavailable"
    elif _normalise_remote(origin).casefold() != full_name.casefold():
        checkout_status = "remote_mismatch"
        ready = False
        reason = "checkout_remote_mismatch"
    else:
        checkout_status = "verified"
        if board is None:
            ready = False
            reason = f"board_{board_status}"
        else:
            ready = True
            reason = None

    return RegistryEntry(
        repository=full_name,
        repository_id=repository_id,
        default_branch=default_branch,
        canonical_slug=slug,
        board=board,
        board_status=board_status,
        checkout=str(checkout),
        checkout_status=checkout_status,
        checkout_remote=origin,
        contract_paths=contracts,
        ready=ready,
        reason=reason,
    )


def _fixture_repositories(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"could not read fixture: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("items")
    if not isinstance(payload, list):
        raise RegistryError("fixture must be a repository list or search payload with items")
    return [item for item in payload if isinstance(item, dict)]


def _fixture_contract_reader(
    repositories: Iterable[dict[str, Any]],
) -> Callable[[str, str], tuple[str, ...]]:
    by_name: dict[str, tuple[str, ...]] = {}
    for repo in repositories:
        full_name = str(repo.get("full_name") or "").strip()
        raw = repo.get("contract_paths", [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise RegistryError(f"fixture contract_paths must be a string list for {full_name}")
        unknown = set(raw) - set(CONTRACT_CANDIDATES)
        if unknown:
            raise RegistryError(
                f"fixture contains unknown contract paths for {full_name}: "
                f"{', '.join(sorted(unknown))}"
            )
        by_name[full_name.casefold()] = tuple(path for path in CONTRACT_CANDIDATES if path in raw)

    def _read(repository: str, default_branch: str) -> tuple[str, ...]:
        del default_branch
        return by_name.get(repository.casefold(), ())

    return _read


def registry_snapshot(
    repositories: Iterable[dict[str, Any]],
    checkout_root: Path,
    *,
    contract_reader: Callable[[str, str], tuple[str, ...]],
    board_resolver: Callable[[str], tuple[str | None, str]],
    origin_reader: Callable[[Path], str | None] | None = None,
) -> dict[str, Any]:
    entries: list[RegistryEntry] = []
    for repo in repositories:
        full_name = str(repo.get("full_name") or "").strip()
        default_branch = str(repo.get("default_branch") or "").strip()
        contracts = contract_reader(full_name, default_branch)
        board, board_status = board_resolver(full_name)
        entries.append(
            build_entry(
                repo,
                checkout_root,
                contract_paths=contracts,
                board=board,
                board_status=board_status,
                origin_reader=origin_reader,
            )
        )
    entries.sort(key=lambda item: item.repository.casefold())
    return {
        "schema_version": 2,
        "mode": "shadow",
        "board_authority": "tasks.idempotency_key",
        "contract_candidates": list(CONTRACT_CANDIDATES),
        "repositories": [
            {**asdict(entry), "contract_paths": list(entry.contract_paths)} for entry in entries
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default=os.environ.get("HERMES_GITHUB_OWNER", "rhgo1749"))
    parser.add_argument("--topic", default=os.environ.get("HERMES_GITHUB_TOPIC", DEFAULT_TOPIC))
    parser.add_argument("--checkout-root", type=Path, default=DEFAULT_CHECKOUT_ROOT)
    parser.add_argument("--kanban-root", type=Path, default=DEFAULT_KANBAN_BOARDS_ROOT)
    parser.add_argument("--fixture-json", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.fixture_json:
            repositories = _fixture_repositories(args.fixture_json)
            contract_reader = _fixture_contract_reader(repositories)
        else:
            token = _github_token()
            repositories = discover_repositories(token, args.owner, args.topic)
            contract_reader = lambda repository, branch: _github_contracts(
                token, repository, branch
            )

        board_evidence = _kanban_board_repository_evidence(args.kanban_root)
        board_resolver = lambda repository: _resolve_board(repository, board_evidence)
        snapshot = registry_snapshot(
            repositories,
            args.checkout_root,
            contract_reader=contract_reader,
            board_resolver=board_resolver,
        )
        text = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
    except (RegistryError, OSError) as exc:
        print(f"repository-registry: ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
