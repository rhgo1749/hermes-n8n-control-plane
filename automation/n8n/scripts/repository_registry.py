#!/usr/bin/env python3
"""Read-only discovery of GitHub repositories opted into Hermes management.

Shadow registry: discovers repositories by GitHub topic, derives repository
metadata, and resolves Kanban board association from durable task provenance or,
for a first intake only, an empty canonical live board. It does not mutate GitHub,
n8n, Hermes, Kanban, webhooks, or cron state.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Self
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

GITHUB_API = "https://api.github.com"
DEFAULT_TOPIC = "hermes-agent"
DEFAULT_CHECKOUT_ROOT = Path("/ws/projects")
DEFAULT_KANBAN_BOARDS_ROOT = Path("/home/hermes/.hermes/kanban/boards")
HTTP_TIMEOUT_SECONDS = 30
MAX_PAGES = 20
MAX_GITHUB_RESPONSE_BYTES = 4 * 1024 * 1024
REPOSITORY_IDENTITY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$"
)
BRANCH_IDENTITY = re.compile(
    r"^(?!.*(?:\.\.|//|@\{))[A-Za-z0-9][A-Za-z0-9._/-]{0,254}(?<![./])$"
)
BOARD_IDENTITY = re.compile(r"^[^/\s]{1,255}$")
GITHUB_ISSUE_KEY = re.compile(r"^github:([^:]+/[^:]+):issue:\d+$", re.IGNORECASE)
CONTRACT_CANDIDATES: tuple[str, ...] = (
    "AGENTS.md",
    "AGENTS_PROJECT.md",
    "Docs/AGENTS.md",
    ".agent/REQ_REQUEST_TEMPLATE.md",
    ".agent/PR_REQUEST_TEMPLATE.md",
)


class RegistryError(RuntimeError):
    """Discovery or normalization failed closed."""


def _valid_branch(value: object) -> bool:
    if not isinstance(value, str) or not BRANCH_IDENTITY.fullmatch(value):
        return False
    return not (
        value in {".", ".."}
        or value.startswith(("-", ".", "/"))
        or value.endswith((".", "/", ".lock"))
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(character in value for character in "~^:?*[\\")
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


class BoardRepositoryEvidence(tuple[str, ...]):
    """Repository provenance plus total/non-GitHub task occupancy.

    The tuple base preserves the existing private evidence mapping shape for
    callers that only need the repository identities. The occupancy fields are
    carried alongside it so an unmanaged/manual canonical board cannot be
    mistaken for a truly empty bootstrap board.
    """

    task_count: int
    non_github_task_count: int

    def __new__(
        cls,
        repositories: Iterable[str] = (),
        *,
        task_count: int = 0,
        non_github_task_count: int = 0,
    ) -> Self:
        value = super().__new__(cls, repositories)
        value.task_count = int(task_count)
        value.non_github_task_count = int(non_github_task_count)
        return value


@dataclass(frozen=True)
class RegistryEntry:
    repository: str
    repository_id: int
    default_branch: str
    canonical_slug: str
    display_name: str
    board: str | None
    board_status: str
    checkout: str
    checkout_status: str
    checkout_remote: str | None
    contract_paths: tuple[str, ...]
    ready: bool
    reason: str | None
    # First-intake provisioning intent: set ONLY for a verified checkout whose
    # canonical board does not exist yet (board_status ==
    # "not_found_task_provenance"). The registry stays read-only — it declares
    # the intent; the production intake owns the single idempotent
    # ``hermes kanban boards create <canonical_slug>`` provision. All
    # fail-closed board_status values (canonical_board_conflict,
    # ambiguous_*) keep bootstrap=None.
    bootstrap: dict[str, str] | None


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
        method="GET",
    )
    last_transport_error: Exception | None = None
    for attempt in range(2):
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_GITHUB_RESPONSE_BYTES + 1)
            if len(raw) > MAX_GITHUB_RESPONSE_BYTES:
                raise RegistryError("GitHub API response exceeded size limit")
            return json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
            )
        except HTTPError as exc:
            if allow_not_found and exc.code == 404:
                exc.close()
                return None
            if 500 <= exc.code <= 599 and attempt == 0:
                exc.close()
                continue
            # Do not copy GitHub's response body into diagnostics: a proxy or
            # upstream error can echo an Authorization header or credential URL.
            exc.close()
            if 500 <= exc.code <= 599:
                raise RegistryError(
                    f"GitHub API HTTP {exc.code}: retry_exhausted"
                ) from exc
            raise RegistryError(f"GitHub API HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            last_transport_error = exc
            if attempt == 0:
                continue
            break
        except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise RegistryError("GitHub API returned invalid JSON") from exc

    error_type = type(last_transport_error).__name__ if last_transport_error else "unknown"
    raise RegistryError(f"GitHub API request failed: {error_type}") from last_transport_error


def discover_repositories(
    token: str,
    owner: str,
    topic: str,
    owner_type: str = "personal",
) -> list[dict[str, Any]]:
    """Return all accessible, non-archived owner repos carrying ``topic``."""
    owner = owner.strip()
    topic = topic.strip().lower()
    owner_type = owner_type.strip().casefold()
    if not owner or not topic:
        raise RegistryError("owner and topic must be non-empty")
    if owner_type not in {"personal", "organization"}:
        raise RegistryError("owner_type must be personal or organization")

    query = f"{'org' if owner_type == 'organization' else 'user'}:{owner} topic:{topic}"
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
        full_name_value = repo.get("full_name")
        owner_payload = repo.get("owner")
        repo_owner_value = (
            owner_payload.get("login")
            if isinstance(owner_payload, dict)
            else None
        )
        repo_owner_type = (
            owner_payload.get("type")
            if isinstance(owner_payload, dict)
            else None
        )
        if not isinstance(full_name_value, str) or not isinstance(
            repo_owner_value, str
        ) or not isinstance(repo_owner_type, str):
            continue
        full_name = full_name_value.strip()
        repo_owner = repo_owner_value.strip()
        full_name_owner, separator, _repo_name = full_name.partition("/")
        if (
            not REPOSITORY_IDENTITY.fullmatch(full_name)
            or not separator
            or full_name_owner.casefold() != owner.casefold()
            or repo_owner.casefold() != owner.casefold()
            or repo_owner_type.casefold()
            != ("organization" if owner_type == "organization" else "user")
        ):
            continue
        if type(repo.get("archived")) is not bool or type(repo.get("disabled")) is not bool:
            continue
        if repo.get("archived") is not False or repo.get("disabled") is not False:
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


def _path_has_symlink_component(path: Path) -> bool:
    """Return whether an absolute lexical path traverses a symlink."""
    if not path.is_absolute():
        return True
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            return True
    return False


def _safe_git_environment() -> dict[str, str]:
    env = os.environ.copy()
    for variable in (
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_ASKPASS",
        "GIT_CREDENTIAL_HELPER",
        "GIT_SSH_COMMAND",
        "GIT_PROXY_COMMAND",
    ):
        env.pop(variable, None)
    for variable in tuple(env):
        if variable.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(variable, None)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_TRACE": "0",
            "GIT_TRACE_CURL": "0",
            "GIT_CURL_VERBOSE": "0",
        }
    )
    return env


def _git_origin(checkout: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_safe_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _kanban_board_repository_evidence(
    boards_root: Path,
) -> dict[str, BoardRepositoryEvidence]:
    """Read provenance and occupancy recorded in each live board's tasks."""
    if not boards_root.exists():
        return {}
    if not boards_root.is_dir():
        raise RegistryError(f"Kanban boards root is not a directory: {boards_root}")
    if _path_has_symlink_component(boards_root):
        raise RegistryError(f"Kanban boards root path is unsafe: {boards_root}")

    evidence: dict[str, BoardRepositoryEvidence] = {}
    for db in sorted(boards_root.glob("*/kanban.db")):
        if _path_has_symlink_component(db):
            raise RegistryError(f"Kanban board database path is unsafe: {db}")
        board = db.parent.name
        if board.startswith("_"):
            continue

        repositories: set[str] = set()
        non_github_task_count = 0
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT idempotency_key FROM tasks"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            raise RegistryError(f"could not read Kanban provenance for board {board}: {exc}") from exc

        for (raw_key,) in rows:
            match = GITHUB_ISSUE_KEY.fullmatch(str(raw_key or ""))
            if match:
                repositories.add(match.group(1).casefold())
            else:
                non_github_task_count += 1
        evidence[board] = BoardRepositoryEvidence(
            sorted(repositories),
            task_count=len(rows),
            non_github_task_count=non_github_task_count,
        )
    return evidence


def _board_default_workdir(boards_root: Path, board: str | None) -> Path | None:
    """Return a resolved board's declared workdir without inventing one.

    Board metadata is trusted only for checkout LOCATION after the board itself
    has already been resolved from task provenance/canonical-board policy. The
    repository identity is still verified independently from the checkout's
    ``origin`` remote before an entry can become ready.

    Legacy boards without ``board.json`` (or without ``default_workdir``) keep
    the historical ``checkout_root/canonical_slug`` fallback in ``build_entry``.
    Malformed board metadata fails closed instead of silently selecting a path.
    """
    if board is None:
        return None
    metadata = boards_root / board / "board.json"
    if metadata.is_symlink() or _path_has_symlink_component(metadata):
        raise RegistryError(f"Kanban board metadata path is unsafe: {metadata}")
    if not metadata.exists():
        return None
    if not metadata.is_file():
        raise RegistryError(f"Kanban board metadata is not a file: {metadata}")
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"invalid Kanban board metadata for {board}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegistryError(f"invalid Kanban board metadata for {board}: expected object")
    declared_slug_value = payload.get("slug")
    if declared_slug_value is not None and not isinstance(declared_slug_value, str):
        raise RegistryError(f"invalid Kanban board metadata slug for {board}")
    declared_slug = (
        declared_slug_value
        if isinstance(declared_slug_value, str)
        else ""
    )
    if declared_slug and (
        declared_slug != declared_slug.strip()
        or declared_slug.casefold() != board.casefold()
    ):
        raise RegistryError(
            f"Kanban board metadata slug mismatch: directory={board} metadata={declared_slug}"
        )
    raw_workdir_value = payload.get("default_workdir")
    if raw_workdir_value is None:
        return None
    if not isinstance(raw_workdir_value, str):
        raise RegistryError(
            f"Kanban board default_workdir is invalid for {board}"
        )
    raw_workdir = raw_workdir_value.strip()
    if not raw_workdir:
        return None
    workdir = Path(raw_workdir)
    if not workdir.is_absolute():
        raise RegistryError(
            f"Kanban board default_workdir must be absolute for {board}: {raw_workdir}"
        )
    if _path_has_symlink_component(workdir):
        raise RegistryError(
            f"Kanban board default_workdir path is unsafe for {board}: {raw_workdir}"
        )
    return workdir


def _resolve_board(
    repository: str,
    evidence: Mapping[str, tuple[str, ...] | BoardRepositoryEvidence],
) -> tuple[str | None, str]:
    """Resolve one live board without inventing a repository association.

    Durable ``tasks.idempotency_key`` provenance remains authoritative. The
    only bootstrap exception is a live board whose directory name exactly
    matches the repository canonical slug (case-insensitive) and which has no
    GitHub repository provenance yet. Once the first intake task is written,
    normal task provenance takes over on the next registry snapshot.
    """
    repository_key = repository.casefold()
    exact: list[str] = []
    conflicted: list[str] = []

    for board, raw_evidence in evidence.items():
        board_evidence = (
            raw_evidence
            if isinstance(raw_evidence, BoardRepositoryEvidence)
            else BoardRepositoryEvidence(raw_evidence)
        )
        repositories = tuple(board_evidence)
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
    if len(exact) > 1:
        return None, "ambiguous_multiple_boards"

    repo_name = repository.split("/", 1)[-1]
    canonical_slug = repo_name.casefold()
    canonical_matches = [
        board for board in evidence if board.casefold() == canonical_slug
    ]
    if len(canonical_matches) > 1:
        return None, "ambiguous_canonical_boards"
    if len(canonical_matches) == 1:
        board = canonical_matches[0]
        raw_evidence = evidence[board]
        board_evidence = (
            raw_evidence
            if isinstance(raw_evidence, BoardRepositoryEvidence)
            else BoardRepositoryEvidence(raw_evidence)
        )
        board_repositories = tuple(board_evidence)
        if board_repositories:
            return None, "canonical_board_conflict"
        if board_evidence.task_count:
            return None, "canonical_board_conflict"
        return board, "resolved_empty_canonical_board"

    return None, "not_found_task_provenance"


def build_entry(
    repo: dict[str, Any],
    checkout_root: Path,
    *,
    contract_paths: Iterable[str] = (),
    board: str | None = None,
    board_status: str = "not_found_task_provenance",
    checkout_path: Path | None = None,
    origin_reader: Callable[[Path], str | None] | None = None,
) -> RegistryEntry:
    full_name_value = repo.get("full_name")
    if not isinstance(full_name_value, str):
        raise RegistryError("repository full_name is invalid")
    full_name = full_name_value.strip()
    if (
        full_name != full_name_value
        or not REPOSITORY_IDENTITY.fullmatch(full_name)
    ):
        raise RegistryError(f"repository full_name is invalid: {full_name!r}")
    repository_id_value = repo.get("id")
    if type(repository_id_value) is not int or repository_id_value <= 0:
        raise RegistryError(f"repository id is invalid for {full_name}")
    repository_id = repository_id_value
    default_branch_value = repo.get("default_branch")
    if not isinstance(default_branch_value, str):
        raise RegistryError(f"repository default_branch is invalid for {full_name}")
    default_branch = default_branch_value.strip()
    if default_branch != default_branch_value or not _valid_branch(default_branch):
        raise RegistryError(f"repository default_branch is invalid for {full_name}")

    raw_contracts = tuple(contract_paths)
    if any(not isinstance(path, str) for path in raw_contracts):
        raise RegistryError(f"contract paths are invalid for {full_name}")
    if len(set(raw_contracts)) != len(raw_contracts):
        raise RegistryError(f"duplicate contract paths for {full_name}")
    unknown_contracts = set(raw_contracts) - set(CONTRACT_CANDIDATES)
    if unknown_contracts:
        raise RegistryError(
            f"unknown contract paths for {full_name}: {', '.join(sorted(unknown_contracts))}"
        )
    contract_set = set(raw_contracts)
    contracts = tuple(path for path in CONTRACT_CANDIDATES if path in contract_set)

    repo_name = full_name.split("/", 1)[1]
    slug = repo_name.casefold()
    display_name = repo_name
    checkout = checkout_path if checkout_path is not None else checkout_root / slug
    read_origin = origin_reader or _git_origin
    checkout_safe = (
        checkout.is_absolute()
        and not _path_has_symlink_component(checkout)
        and not checkout.is_symlink()
    )
    origin = read_origin(checkout) if checkout_safe and checkout.is_dir() else None

    if not checkout_safe:
        checkout_status = "unsafe_path"
        ready = False
        reason = "checkout_path_unsafe"
    elif not checkout.exists():
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

    # Provisioning intent for the first-intake path: a verified checkout whose
    # canonical board does not exist yet. The registry stays read-only — it
    # only declares the intent; the production intake owns the single
    # idempotent ``hermes kanban boards create <canonical_slug>`` provision.
    # Fail-closed states (canonical_board_conflict, ambiguous_*) and
    # provenance-resolved boards keep bootstrap=None.
    bootstrap: dict[str, str] | None = None
    if (
        checkout_status == "verified"
        and board is None
        and board_status == "not_found_task_provenance"
    ):
        bootstrap = {"board": slug, "checkout": str(checkout)}

    return RegistryEntry(
        repository=full_name,
        repository_id=repository_id,
        default_branch=default_branch,
        canonical_slug=slug,
        display_name=display_name,
        board=board,
        board_status=board_status,
        checkout=str(checkout),
        checkout_status=checkout_status,
        checkout_remote=origin,
        contract_paths=contracts,
        ready=ready,
        reason=reason,
        bootstrap=bootstrap,
    )


def _fixture_repositories(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"invalid fixture JSON: {path}") from exc
    if isinstance(payload, dict):
        payload = payload.get("items")
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise RegistryError("fixture must contain only repository objects")
    return payload


def _fixture_contract_reader(
    repositories: Iterable[dict[str, Any]],
) -> Callable[[str, str], tuple[str, ...]]:
    by_name: dict[str, tuple[str, ...]] = {}
    for repo in repositories:
        raw_full_name = repo.get("full_name")
        if not isinstance(raw_full_name, str) or raw_full_name != raw_full_name.strip():
            raise RegistryError("fixture full_name must be a canonical string")
        full_name = raw_full_name
        raw = repo.get("contract_paths", [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise RegistryError(f"fixture contract_paths must be a string list for {full_name}")
        if len(set(raw)) != len(raw):
            raise RegistryError(f"fixture contract_paths contains duplicates for {full_name}")
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
    checkout_resolver: Callable[[str, str | None], Path | None] | None = None,
    origin_reader: Callable[[Path], str | None] | None = None,
) -> dict[str, Any]:
    entries: list[RegistryEntry] = []
    for repo in repositories:
        raw_full_name = repo.get("full_name")
        raw_default_branch = repo.get("default_branch")
        if (
            not isinstance(raw_full_name, str)
            or raw_full_name != raw_full_name.strip()
            or not isinstance(raw_default_branch, str)
            or raw_default_branch != raw_default_branch.strip()
            or not _valid_branch(raw_default_branch)
        ):
            raise RegistryError("repository metadata contains invalid identity fields")
        full_name = raw_full_name
        default_branch = raw_default_branch
        contracts = contract_reader(full_name, default_branch)
        board, board_status = board_resolver(full_name)
        if (
            board is not None
            and (
                not isinstance(board, str)
                or board != board.strip()
                or not BOARD_IDENTITY.fullmatch(board)
            )
        ):
            raise RegistryError(f"board identity is invalid for {full_name}")
        if (
            not isinstance(board_status, str)
            or board_status != board_status.strip()
            or not board_status
        ):
            raise RegistryError(f"board status is invalid for {full_name}")
        checkout_path = (
            checkout_resolver(full_name, board)
            if checkout_resolver is not None
            else None
        )
        entries.append(
            build_entry(
                repo,
                checkout_root,
                contract_paths=contracts,
                board=board,
                board_status=board_status,
                checkout_path=checkout_path,
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


def live_registry_snapshot(
    token: str,
    owner: str,
    topic: str,
    checkout_root: Path,
    kanban_root: Path,
    owner_type: str | None = None,
) -> dict[str, Any]:
    """Build the authoritative live registry snapshot without mutating state."""
    if owner_type is None:
        # Preserve the three-argument call shape used by existing integrations
        # and tests; personal-owner mode is the historical default.
        repositories = discover_repositories(token, owner, topic)
    else:
        repositories = discover_repositories(token, owner, topic, owner_type)
    board_evidence = _kanban_board_repository_evidence(kanban_root)

    return registry_snapshot(
        repositories,
        checkout_root,
        contract_reader=lambda repository, branch: _github_contracts(
            token,
            repository,
            branch,
        ),
        board_resolver=lambda repository: _resolve_board(
            repository,
            board_evidence,
        ),
        checkout_resolver=lambda repository, board: _board_default_workdir(
            kanban_root,
            board,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default=os.environ.get("HERMES_GITHUB_OWNER", "rhgo1749"))
    parser.add_argument("--topic", default=os.environ.get("HERMES_GITHUB_TOPIC", DEFAULT_TOPIC))
    parser.add_argument(
        "--owner-type",
        default=os.environ.get("HERMES_GITHUB_OWNER_TYPE", "personal"),
        choices=("personal", "organization"),
    )
    parser.add_argument("--checkout-root", type=Path, default=DEFAULT_CHECKOUT_ROOT)
    parser.add_argument("--kanban-root", type=Path, default=DEFAULT_KANBAN_BOARDS_ROOT)
    parser.add_argument("--fixture-json", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.fixture_json:
            repositories = _fixture_repositories(args.fixture_json)
            contract_reader = _fixture_contract_reader(repositories)
            board_evidence = _kanban_board_repository_evidence(args.kanban_root)
            snapshot = registry_snapshot(
                repositories,
                args.checkout_root,
                contract_reader=contract_reader,
                board_resolver=lambda repository: _resolve_board(
                    repository,
                    board_evidence,
                ),
                checkout_resolver=lambda repository, board: _board_default_workdir(
                    args.kanban_root,
                    board,
                ),
            )
        else:
            snapshot = live_registry_snapshot(
                _github_token(),
                args.owner,
                args.topic,
                args.checkout_root,
                args.kanban_root,
                args.owner_type,
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
