#!/usr/bin/env python3
"""Dynamic backend-aware worker resource admission for Hermes Kanban.

This overlay keeps the existing ``kanban.worker_resources`` shape but adds an
optional ``backend`` selector.  The important deployment use case is:

    kanban:
      worker_resources:
        local-serial-llm:
          capacity: 1
          backend: local
          assignees:
            - "kanban-main"
            - "kanban-developer"

An assignee only matches ``backend: local`` while that profile's *current*
model config resolves to a local endpoint.  The same profile can therefore use
Codex/cloud in parallel today and automatically join the one-slot local pool
after its model config is switched to llama.cpp later.

The module also installs the same admission gate on Hermes core READY/REVIEW
claims.  The gate lives at the claim boundary rather than in spawn_fn: while a
short host-wide resource lock is held, the existing cross-board worker count is
checked and the core claim is made.  A successful claim itself becomes the
durable in-flight reservation (RUNNING with no PID yet), so the lock can be
released before the subprocess spawn without opening a race.

No Hermes source files are modified; all changes are runtime monkeypatches and
are idempotent.
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import io
import ipaddress
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, cast
from urllib.parse import urlparse

logger = logging.getLogger("h4v3.resource_scheduler")

_LOCAL_PROVIDER_HINTS = frozenset({
    "llamacpp",
    "llama-cpp",
    "llama.cpp",
    "ollama",
    "lmstudio",
    "vllm",
    "sglang",
    "local",
})
_LOCAL_HOST_ALIASES = frozenset({
    "localhost",
    "0.0.0.0",
    "::1",
    "host.docker.internal",
})


@dataclass(frozen=True)
class ProfileBackend:
    profile: str
    provider: str
    model: str
    base_url: str
    kind: str  # local | cloud | unknown


def _normalized_provider(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalized_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _host_from_url(value: str) -> str:
    text = _normalized_url(value)
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"http://{text}")
    return (parsed.hostname or "").strip().lower().rstrip(".")


def _host_is_local(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in _LOCAL_HOST_ALIASES or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local")
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_unspecified
    )


def _base_url_is_local(base_url: str) -> bool:
    return _host_is_local(_host_from_url(base_url))


def _profile_dir(profile_name: str) -> Path:
    """Resolve one profile without mutating process-global HERMES_HOME."""
    from hermes_cli import profiles as profiles_mod
    from hermes_constants import get_default_hermes_root

    name = profiles_mod.normalize_profile_name(profile_name)
    if name == "default":
        return Path(get_default_hermes_root())
    return Path(profiles_mod.get_profile_dir(name))


def _model_config_for_profile(profile_name: str) -> Mapping[str, Any]:
    from hermes_cli.config import read_user_config_raw

    path = _profile_dir(profile_name) / "config.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"profile config missing: {path}")
    cfg = read_user_config_raw(path)
    if not isinstance(cfg, Mapping):
        raise ValueError(f"profile config is not a mapping: {path}")
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        return {"default": model_cfg}
    if not isinstance(model_cfg, Mapping):
        return {}
    value = dict(model_cfg)

    # Hermes accepts model.default as a nested {provider, model} object.
    default = value.get("default")
    if isinstance(default, Mapping):
        nested = dict(default)
        if not value.get("provider") and nested.get("provider"):
            value["provider"] = nested.get("provider")
        if not value.get("model") and nested.get("model"):
            value["model"] = nested.get("model")
        value["default"] = nested.get("model") or nested.get("default") or ""

    return value


def resolve_profile_backend(profile_name: str) -> ProfileBackend:
    """Classify the profile's current primary model route.

    Explicit local/private ``model.base_url`` wins over the provider label.
    This deliberately handles an OpenAI-compatible provider pointed at a
    local llama.cpp endpoint.  When no URL is configured, known local provider
    aliases are treated as local; known cloud provider labels are cloud.
    An unreadable/ambiguous config is ``unknown``.
    """
    profile = str(profile_name or "").strip()
    if not profile:
        return ProfileBackend("", "", "", "", "unknown")
    try:
        model_cfg = _model_config_for_profile(profile)
    except Exception as exc:
        logger.warning(
            "resource admission: cannot resolve backend for %s: %s",
            profile,
            exc,
        )
        return ProfileBackend(profile, "", "", "", "unknown")

    provider = _normalized_provider(model_cfg.get("provider"))
    model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    base_url = _normalized_url(model_cfg.get("base_url"))

    if base_url:
        kind = "local" if _base_url_is_local(base_url) else "cloud"
    elif provider in _LOCAL_PROVIDER_HINTS:
        kind = "local"
    elif provider:
        kind = "cloud"
    else:
        kind = "unknown"

    return ProfileBackend(profile, provider, model, base_url, kind)


def _backend_matches(selector: str, assignee: str) -> bool:
    selector = str(selector or "any").strip().lower() or "any"
    if selector in {"any", "*"}:
        return True
    backend = resolve_profile_backend(assignee)
    if selector == "local":
        # Fail closed: if a policy explicitly protects a scarce local resource
        # and the profile config becomes unreadable, protect the slot rather
        # than accidentally admitting several workers.
        return backend.kind in {"local", "unknown"}
    if selector == "cloud":
        return backend.kind == "cloud"
    raise ValueError(f"unsupported worker resource backend selector: {selector!r}")


def install_dynamic_resource_policy(admission_module: Any) -> None:
    """Make the existing edge admission module backend-aware.

    Existing policies without ``backend`` retain byte-for-byte semantics:
    assignee glob match alone decides membership.
    """
    original_policies = getattr(admission_module, "_resource_policies", None)
    if not callable(original_policies):
        raise RuntimeError("admission module has no _resource_policies")
    if getattr(original_policies, "_dynamic_backend_installed", False):
        return

    @dataclass(frozen=True)
    class DynamicWorkerResource:
        name: str
        capacity: int
        assignees: tuple[str, ...]
        stale_worker_grace_seconds: float
        backend: str = "any"

        def matches(self, assignee: str) -> bool:
            name = str(assignee or "").strip()
            if not name:
                return False
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in self.assignees):
                return False
            return _backend_matches(self.backend, name)

    def dynamic_policies(cfg: Mapping[str, Any]) -> tuple[Any, ...]:
        # Let the existing parser own validation/defaults for all legacy fields.
        parsed = cast(tuple[Any, ...], original_policies(cfg))
        raw = cfg.get(getattr(admission_module, "RESOURCE_CONFIG_KEY", "worker_resources"))
        raw = raw if isinstance(raw, Mapping) else {}
        out: list[Any] = []
        for policy in parsed:
            raw_spec = raw.get(policy.name, {})
            backend = "any"
            if isinstance(raw_spec, Mapping):
                backend = str(raw_spec.get("backend") or "any").strip().lower() or "any"
            if backend not in {"any", "*", "local", "cloud"}:
                error_cls = getattr(admission_module, "ResourceAdmissionError", ValueError)
                raise error_cls(
                    f"worker resource {policy.name!r} backend must be "
                    "'any', 'local', or 'cloud'"
                )
            out.append(
                DynamicWorkerResource(
                    name=policy.name,
                    capacity=int(policy.capacity),
                    assignees=tuple(policy.assignees),
                    stale_worker_grace_seconds=float(policy.stale_worker_grace_seconds),
                    backend=backend,
                )
            )
        return tuple(out)

    dynamic_policies._dynamic_backend_installed = True  # type: ignore[attr-defined]
    dynamic_policies._dynamic_backend_original = original_policies  # type: ignore[attr-defined]
    admission_module._resource_policies = dynamic_policies


def _all_board_db_paths(kanban_db: Any, board: str) -> tuple[Path, ...]:
    """Return default + named board DB paths for the shared Hermes root."""
    current = Path(kanban_db.kanban_db_path(board=board)).expanduser().resolve()
    try:
        default = Path(kanban_db.kanban_db_path(board="default")).expanduser().resolve()
    except Exception:
        default = current

    candidates: list[Path] = []
    if default.is_file():
        candidates.append(default)
    try:
        root = Path(kanban_db.boards_root()).expanduser().resolve()
        candidates.extend(sorted(root.glob("*/kanban.db")))
    except Exception:
        # Older Hermes builds: derive the named-board root from the default DB.
        candidates.extend(sorted((default.parent / "kanban" / "boards").glob("*/kanban.db")))
    if current.is_file():
        candidates.append(current)
    return tuple(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))


@contextlib.contextmanager
def _host_resource_lock(kanban_db: Any, board: str, resource_name: str):
    """One host-wide lock shared by normal dispatcher and edge rework lane."""
    try:
        import fcntl
    except ImportError:
        yield False
        return

    try:
        root = Path(kanban_db.kanban_home()).expanduser().resolve()
    except Exception:
        paths = _all_board_db_paths(kanban_db, board)
        if not paths:
            yield False
            return
        root = paths[0].parent

    lock_dir = root / "kanban" / ".resource-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(resource_name.encode("utf-8")).hexdigest()[:20]
    lock_path = lock_dir / f"{digest}.lock"
    with open(lock_path, "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def install_cross_board_helpers(admission_module: Any) -> None:
    """Share correct default+named-board counting and lock location."""
    admission_module._board_db_paths = _all_board_db_paths
    admission_module._resource_lock = _host_resource_lock


def _connection_db_path(conn: sqlite3.Connection) -> Optional[Path]:
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        # sqlite Row or tuple: seq, name, file
        try:
            name = row["name"]
            file_name = row["file"]
        except (TypeError, KeyError, IndexError):
            name = row[1]
            file_name = row[2]
        if name == "main" and file_name:
            try:
                return Path(str(file_name)).expanduser().resolve()
            except OSError:
                return Path(str(file_name)).expanduser()
    return None


def _board_for_connection(kanban_db: Any, conn: sqlite3.Connection) -> str:
    current = _connection_db_path(conn)
    if current is None:
        try:
            return str(kanban_db.get_current_board())
        except Exception:
            return "default"

    try:
        default = Path(kanban_db.kanban_db_path(board="default")).expanduser().resolve()
        if current == default:
            return "default"
    except Exception:
        pass

    try:
        for meta in kanban_db.list_boards(include_archived=False):
            slug = str(meta.get("slug") or "").strip()
            if not slug:
                continue
            try:
                candidate = Path(
                    kanban_db.kanban_db_path(board=slug)
                ).expanduser().resolve()
            except Exception:
                continue
            if candidate == current:
                return slug
    except Exception:
        pass

    # Standard layout fallback: .../kanban/boards/<slug>/kanban.db
    if current.name == "kanban.db" and current.parent.parent.name == "boards":
        return current.parent.name
    return "default"


def _load_kanban_cfg() -> Mapping[str, Any]:
    from hermes_cli.config import load_config

    cfg = load_config() or {}
    if not isinstance(cfg, Mapping):
        return {}
    kcfg = cfg.get("kanban") or {}
    return kcfg if isinstance(kcfg, Mapping) else {}


def _row_assignee(
    conn: sqlite3.Connection,
    task_id: str,
    cfg: Mapping[str, Any],
) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        value = row["assignee"]
    except (TypeError, KeyError, IndexError):
        value = row[0]
    assignee = str(value or "").strip()
    if assignee:
        return assignee
    default_assignee = str(cfg.get("default_assignee") or "").strip()
    return default_assignee or None


_last_resource_diagnostics: list[dict[str, Any]] = []


def _diagnostic_limit(admission_module: Any) -> int:
    try:
        return max(1, int(getattr(admission_module, "RESOURCE_OUTCOME_LIMIT", 128)))
    except (TypeError, ValueError):
        return 128


def _profile_exists(profile_name: str) -> Optional[bool]:
    """Return profile existence, or ``None`` when the probe is unavailable."""
    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        return None
    try:
        return bool(profile_exists(profile_name))
    except Exception:
        return False


def _pending_rows(
    conn: sqlite3.Connection,
    statuses: tuple[str, ...],
) -> list[sqlite3.Row]:
    """Read unclaimed dispatch candidates with a legacy-schema fallback."""
    placeholders = ", ".join("?" for _ in statuses)
    try:
        return conn.execute(
            "SELECT id, assignee FROM tasks "
            f"WHERE status IN ({placeholders}) AND claim_lock IS NULL "
            "ORDER BY priority DESC, created_at ASC",
            statuses,
        ).fetchall()
    except sqlite3.OperationalError:
        # Small isolated fixtures and older installations may not have the
        # optional claim_lock/ordering columns yet.  The live schema takes the
        # first path; the fallback remains read-only and conservative.
        try:
            return conn.execute(
                "SELECT id, assignee FROM tasks "
                f"WHERE status IN ({placeholders}) ORDER BY id ASC",
                statuses,
            ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"could not inspect resource candidates: {type(exc).__name__}: {exc}"
            ) from exc


def _classify_pending_resources(
    kanban_db: Any,
    admission_module: Any,
    conn: sqlite3.Connection,
    cfg: Mapping[str, Any],
    statuses: tuple[str, ...],
    *,
    virtual_reservations: Optional[dict[str, int]] = None,
) -> Optional[tuple[bool, list[dict[str, Any]]]]:
    """Classify pending rows without claiming them.

    ``None`` means the configured overlay has no applicable resource (or the
    profile probe is unavailable), so callers must preserve the core's legacy
    result.  Otherwise the first item says whether health should keep treating
    the queue as pending and the second contains bounded deferral diagnostics.
    When ``virtual_reservations`` is provided, each available candidate
    consumes one same-tick virtual slot so dry-run classification mirrors the
    claim-to-spawn reservation window without mutating the database.
    """
    raw = cfg.get(getattr(admission_module, "RESOURCE_CONFIG_KEY", "worker_resources"))
    if raw in (None, {}):
        return None

    rows = _pending_rows(conn, statuses)
    if not rows:
        return None
    board = _board_for_connection(kanban_db, conn)
    active_by_resource: dict[str, list[dict[str, Any]]] = {}
    diagnostics: list[dict[str, Any]] = []
    matched_resource = False
    spawnable = False
    health_failure = False

    for row in rows:
        assignee = str(row["assignee"] or "").strip()
        if not assignee:
            continue
        exists = _profile_exists(assignee)
        if exists is None:
            # Do not change the core's degraded-install fallback when profile
            # discovery itself is unavailable.
            return None
        if not exists:
            continue
        lane = str(statuses[0]) if len(statuses) == 1 else "dispatch"
        try:
            resource = admission_module.resource_for_assignee(cfg, assignee)
        except Exception as exc:
            matched_resource = True
            health_failure = True
            diagnostics.append({
                "task_id": str(row["id"]),
                "lane": lane,
                "reason": "resource_config_invalid",
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        if resource is None:
            spawnable = True
            continue

        matched_resource = True
        if (
            virtual_reservations is not None
            and resource.name in virtual_reservations
        ):
            active_count = int(virtual_reservations[resource.name])
        else:
            active = active_by_resource.get(resource.name)
            if active is None:
                try:
                    active = admission_module._active_resource_workers(
                        kanban_db, board, resource
                    )
                except Exception as exc:
                    health_failure = True
                    diagnostics.append({
                        "task_id": str(row["id"]),
                        "lane": lane,
                        "reason": "resource_admission_failed",
                        "resource_group": resource.name,
                        "resource_capacity": int(resource.capacity),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    continue
                active_by_resource[resource.name] = active
            active_count = len(active)
            if virtual_reservations is not None:
                virtual_reservations[resource.name] = active_count
        if active_count >= int(resource.capacity):
            diagnostics.append({
                "task_id": str(row["id"]),
                "lane": lane,
                "reason": "resource_busy",
                "resource_group": resource.name,
                "resource_active": active_count,
                "resource_capacity": int(resource.capacity),
            })
        else:
            spawnable = True
            if virtual_reservations is not None:
                virtual_reservations[resource.name] = active_count + 1

    if not matched_resource:
        return None
    # A resource failure must remain visible to the existing dispatcher health
    # signal.  Only an all-capacity-wait queue may intentionally report false.
    return (
        spawnable or health_failure,
        diagnostics[:_diagnostic_limit(admission_module)],
    )


def _record_resource_diagnostics(
    admission_module: Any,
    diagnostics: list[dict[str, Any]],
    *,
    board: Optional[str] = None,
) -> None:
    recorder = getattr(admission_module, "record_resource_admission_outcome", None)
    if not callable(recorder):
        return
    for item in diagnostics:
        try:
            recorder(
                task_id=item.get("task_id"),
                board=board if board is not None else item.get("board"),
                resource_group=item.get("resource_group"),
                reason=item.get("reason", "resource_admission"),
                lane=item.get("lane"),
                resource_active=item.get("resource_active"),
                resource_capacity=item.get("resource_capacity"),
            )
        except Exception:
            # Telemetry must never change admission behavior.
            continue


def _dispatch_resource_diagnostics(
    kanban_db: Any,
    admission_module: Any,
    conn: sqlite3.Connection,
    cfg: Mapping[str, Any],
    *,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    virtual_reservations = {} if dry_run else None
    statuses_to_check = [("ready",)]
    if bool(cfg.get("review_dispatch", True)):
        statuses_to_check.append(("review",))
    for statuses in statuses_to_check:
        classified = _classify_pending_resources(
            kanban_db,
            admission_module,
            conn,
            cfg,
            statuses,
            virtual_reservations=virtual_reservations,
        )
        if classified is not None:
            diagnostics.extend(classified[1])
    # A row can be visible through more than one compatibility query only when
    # a caller supplies an unusual fixture; keep the diagnostic stream stable.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in diagnostics:
        key = (
            item.get("task_id"),
            item.get("lane"),
            item.get("reason"),
            item.get("resource_group"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:_diagnostic_limit(admission_module)]


def _safe_dispatch_resource_diagnostics(
    kanban_db: Any,
    admission_module: Any,
    conn: sqlite3.Connection,
    cfg: Mapping[str, Any],
    *,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    try:
        return _dispatch_resource_diagnostics(
            kanban_db,
            admission_module,
            conn,
            cfg,
            dry_run=dry_run,
        )
    except Exception as exc:
        return [{
            "task_id": None,
            "lane": "dispatch",
            "reason": "resource_admission_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }]


def _spawned_task_ids(result: Any) -> set[str]:
    spawned = getattr(result, "spawned", None)
    if not isinstance(spawned, (list, tuple)):
        return set()
    task_ids: set[str] = set()
    for item in spawned:
        if isinstance(item, (tuple, list)) and item:
            task_id = item[0]
        elif isinstance(item, Mapping):
            task_id = item.get("task_id")
        else:
            task_id = None
        if task_id is not None:
            task_ids.add(str(task_id))
    return task_ids


def _spawned_candidates(result: Any) -> list[tuple[str, str]]:
    """Return core's ordered dry-run candidates without changing their order."""
    spawned = getattr(result, "spawned", None)
    if not isinstance(spawned, (list, tuple)):
        return []
    candidates: list[tuple[str, str]] = []
    for item in spawned:
        if isinstance(item, (tuple, list)) and item:
            task_id = item[0]
            assignee = item[1] if len(item) > 1 else ""
        elif isinstance(item, Mapping):
            task_id = item.get("task_id")
            assignee = item.get("assignee")
        else:
            continue
        if task_id is None:
            continue
        candidates.append((str(task_id), str(assignee or "").strip()))
    return candidates


def _dry_run_resource_diagnostics(
    kanban_db: Any,
    admission_module: Any,
    conn: sqlite3.Connection,
    cfg: Mapping[str, Any],
    result: Any,
) -> list[dict[str, Any]]:
    """Replay resources after core has selected its authoritative candidates.

    Core owns lane order, spawn caps, review reservation, profile caps, the
    respawn guard, and default-assignee resolution. This helper therefore
    consumes only the ordered ``result.spawned`` entries from that completed
    core dry-run. The second pass emits capacity telemetry for other pending
    rows sharing a resource already consumed by those candidates; it never
    changes the candidate list and cannot preempt a core scheduling decision.
    """
    raw = cfg.get(getattr(admission_module, "RESOURCE_CONFIG_KEY", "worker_resources"))
    if raw in (None, {}):
        return []
    candidates = _spawned_candidates(result)
    if not candidates:
        return []

    board = _board_for_connection(kanban_db, conn)
    virtual_reservations: dict[str, int] = {}
    consumed_resources: set[str] = set()
    candidate_ids: set[str] = set()
    diagnostics: list[dict[str, Any]] = []

    for task_id, assignee in candidates:
        candidate_ids.add(task_id)
        if not assignee:
            continue
        exists = _profile_exists(assignee)
        if exists is None:
            # Preserve core's result when profile discovery is unavailable.
            return []
        if not exists:
            continue
        try:
            resource = admission_module.resource_for_assignee(cfg, assignee)
        except Exception as exc:
            diagnostics.append({
                "task_id": task_id,
                "lane": "dispatch",
                "reason": "resource_config_invalid",
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        if resource is None:
            continue

        consumed_resources.add(resource.name)
        if resource.name in virtual_reservations:
            active_count = virtual_reservations[resource.name]
        else:
            try:
                active = admission_module._active_resource_workers(
                    kanban_db, board, resource
                )
            except Exception as exc:
                diagnostics.append({
                    "task_id": task_id,
                    "lane": "dispatch",
                    "reason": "resource_admission_failed",
                    "resource_group": resource.name,
                    "resource_capacity": int(resource.capacity),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            active_count = len(active)
            virtual_reservations[resource.name] = active_count

        if active_count >= int(resource.capacity):
            diagnostics.append({
                "task_id": task_id,
                "lane": "dispatch",
                "reason": "resource_busy",
                "resource_group": resource.name,
                "resource_active": active_count,
                "resource_capacity": int(resource.capacity),
            })
        else:
            virtual_reservations[resource.name] = active_count + 1

    # Report other rows that are now capacity-blocked by an authoritative core
    # candidate. This is telemetry only: these rows never enter the replay and
    # cannot filter or reorder the core result.
    if consumed_resources:
        statuses_to_check = [("ready",)]
        if bool(cfg.get("review_dispatch", True)):
            statuses_to_check.append(("review",))
        for statuses in statuses_to_check:
            for row in _pending_rows(conn, statuses):
                task_id = str(row["id"])
                if task_id in candidate_ids:
                    continue
                assignee = str(row["assignee"] or "").strip()
                if not assignee or _profile_exists(assignee) is not True:
                    continue
                try:
                    resource = admission_module.resource_for_assignee(cfg, assignee)
                except Exception:
                    continue
                if (
                    resource is None
                    or resource.name not in consumed_resources
                    or virtual_reservations.get(resource.name, 0)
                    < int(resource.capacity)
                ):
                    continue
                diagnostics.append({
                    "task_id": task_id,
                    "lane": str(statuses[0]),
                    "reason": "resource_busy",
                    "resource_group": resource.name,
                    "resource_active": virtual_reservations[resource.name],
                    "resource_capacity": int(resource.capacity),
                })

    return diagnostics[:_diagnostic_limit(admission_module)]


def _safe_dry_run_resource_diagnostics(
    kanban_db: Any,
    admission_module: Any,
    conn: sqlite3.Connection,
    cfg: Mapping[str, Any],
    result: Any,
) -> list[dict[str, Any]]:
    try:
        return _dry_run_resource_diagnostics(
            kanban_db, admission_module, conn, cfg, result
        )
    except Exception as exc:
        return [{
            "task_id": None,
            "lane": "dispatch",
            "reason": "resource_admission_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }]


def _normalize_dispatch_diagnostics(
    result: Any,
    diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop stale busy evidence when that task spawned in this same tick."""
    spawned_ids = _spawned_task_ids(result)
    if not spawned_ids:
        return diagnostics
    return [
        item
        for item in diagnostics
        if not (
            item.get("reason") == "resource_busy"
            and item.get("task_id") is not None
            and str(item.get("task_id")) in spawned_ids
        )
    ]


def _attach_dispatch_diagnostics(
    result: Any,
    diagnostics: list[dict[str, Any]],
    *,
    dry_run: bool,
) -> None:
    if not dry_run:
        diagnostics = _normalize_dispatch_diagnostics(result, diagnostics)
    busy = [item for item in diagnostics if item.get("reason") == "resource_busy"]
    deferred_ids = {
        str(item.get("task_id"))
        for item in diagnostics
        if item.get("task_id") is not None
        and item.get("reason")
        in {
            "resource_busy",
            "resource_config_invalid",
            "resource_admission_failed",
        }
    }
    setattr(result, "resource_busy", busy)
    setattr(result, "resource_admission", diagnostics)
    setattr(
        result,
        "skipped_resource_busy",
        [str(item["task_id"]) for item in busy if item.get("task_id") is not None],
    )
    if not dry_run or not deferred_ids:
        return
    spawned = getattr(result, "spawned", None)
    if not isinstance(spawned, list):
        return
    retained = []
    for item in spawned:
        if isinstance(item, (tuple, list)) and item:
            task_id = item[0]
        elif isinstance(item, Mapping):
            task_id = item.get("task_id")
        else:
            task_id = None
        if str(task_id) in deferred_ids:
            continue
        retained.append(item)
    result.spawned = retained


def _install_resource_health_probes(
    kanban_db: Any,
    admission_module: Any,
) -> None:
    """Make core health probes ignore candidates blocked by capacity."""
    for attr, status in (("has_spawnable_ready", "ready"), ("has_spawnable_review", "review")):
        original = getattr(kanban_db, attr, None)
        if not callable(original) or getattr(
            original, "_h4v3_resource_health_installed", False
        ):
            continue

        def make_probe(original_fn: Any, lane: str):
            def guarded(conn: sqlite3.Connection) -> bool:
                try:
                    cfg = _load_kanban_cfg()
                except Exception as exc:
                    _record_resource_diagnostics(
                        admission_module,
                        [{
                            "task_id": None,
                            "lane": lane,
                            "reason": "resource_admission_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }],
                    )
                    logger.warning(
                        "resource admission health %s config load failed: %s",
                        lane,
                        exc,
                    )
                    # Let the existing core health surface see pending work;
                    # an unknown resource state must not look like a healthy
                    # capacity wait.
                    return bool(original_fn(conn))
                if not isinstance(cfg, Mapping):
                    cfg = {}
                raw = cfg.get(
                    getattr(admission_module, "RESOURCE_CONFIG_KEY", "worker_resources")
                )
                if raw in (None, {}):
                    return bool(original_fn(conn))
                try:
                    classified = _classify_pending_resources(
                        kanban_db, admission_module, conn, cfg, (lane,)
                    )
                except Exception as exc:
                    # An explicitly configured resource failure is not a
                    # legitimate capacity wait. Keep pending work visible to
                    # the existing stuck/failure health surface.
                    board = None
                    try:
                        board = _board_for_connection(kanban_db, conn)
                    except Exception:
                        pass
                    _record_resource_diagnostics(
                        admission_module,
                        [{
                            "task_id": None,
                            "lane": lane,
                            "reason": "resource_admission_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }],
                        board=board,
                    )
                    logger.warning(
                        "resource admission health %s classification failed: %s",
                        lane,
                        exc,
                    )
                    return True
                if classified is None:
                    return bool(original_fn(conn))
                spawnable, diagnostics = classified
                board = None
                try:
                    board = _board_for_connection(kanban_db, conn)
                except Exception:
                    pass
                _record_resource_diagnostics(
                    admission_module, diagnostics, board=board
                )
                return bool(spawnable)

            guarded._h4v3_resource_health_installed = True  # type: ignore[attr-defined]
            guarded._h4v3_resource_health_original = original_fn  # type: ignore[attr-defined]
            return guarded

        setattr(kanban_db, attr, make_probe(original, status))


def _install_dispatch_overlay(kanban_db: Any, admission_module: Any) -> None:
    """Annotate dispatch results and make dry-run capacity-aware."""
    global _last_resource_diagnostics
    original = getattr(kanban_db, "dispatch_once", None)
    if not callable(original) or getattr(original, "_h4v3_resource_dispatch_installed", False):
        return

    def guarded_dispatch_once(
        conn: sqlite3.Connection,
        *args: Any,
        **kwargs: Any,
    ):
        global _last_resource_diagnostics
        dry_run = bool(kwargs.get("dry_run"))
        if dry_run:
            # Core must select candidates first. Its ordered result already
            # reflects READY/REVIEW lane order, max_spawn, the reserved review
            # slot, profile caps, respawn guards, and default-assignee policy.
            # Resource replay below is deliberately downstream of those gates.
            result = original(conn, *args, **kwargs)
            try:
                cfg = _load_kanban_cfg()
            except Exception:
                cfg = {}
            if not isinstance(cfg, Mapping):
                cfg = {}
            diagnostics = _safe_dry_run_resource_diagnostics(
                kanban_db, admission_module, conn, cfg, result
            )
            _last_resource_diagnostics = []
            board = None
            try:
                board = _board_for_connection(kanban_db, conn)
            except Exception:
                pass
            _record_resource_diagnostics(admission_module, diagnostics, board=board)
            _last_resource_diagnostics = list(diagnostics)
            _attach_dispatch_diagnostics(result, diagnostics, dry_run=True)
            return result

        try:
            cfg = _load_kanban_cfg()
        except Exception:
            cfg = {}
        if not isinstance(cfg, Mapping):
            cfg = {}
        diagnostics = _safe_dispatch_resource_diagnostics(
            kanban_db,
            admission_module,
            conn,
            cfg,
            dry_run=False,
        )
        _last_resource_diagnostics = []
        result = original(conn, *args, **kwargs)
        # A real tick may reap a holder before it reaches claim_task. Preserve
        # the pre-tick busy evidence only when it remains relevant; otherwise
        # include any post-tick deferral discovered after the core pass.
        if not dry_run:
            diagnostics.extend(
                _safe_dispatch_resource_diagnostics(
                    kanban_db,
                    admission_module,
                    conn,
                    cfg,
                    dry_run=False,
                )
            )
        unique: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for item in diagnostics:
            key = (
                item.get("task_id"),
                item.get("lane"),
                item.get("reason"),
                item.get("resource_group"),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        unique = unique[:_diagnostic_limit(admission_module)]
        if not dry_run:
            unique = _normalize_dispatch_diagnostics(result, unique)
        board = None
        try:
            board = _board_for_connection(kanban_db, conn)
        except Exception:
            pass
        _record_resource_diagnostics(admission_module, unique, board=board)
        _last_resource_diagnostics = list(unique)
        _attach_dispatch_diagnostics(result, unique, dry_run=dry_run)
        return result

    guarded_dispatch_once._h4v3_resource_dispatch_installed = True  # type: ignore[attr-defined]
    guarded_dispatch_once._h4v3_resource_dispatch_original = original  # type: ignore[attr-defined]
    setattr(kanban_db, "dispatch_once", guarded_dispatch_once)


def _install_cli_dispatch_overlay() -> None:
    """Add resource-specific output to the existing CLI formatter."""
    try:
        from hermes_cli import kanban as cli_module
    except Exception:
        return
    original = getattr(cli_module, "_cmd_dispatch", None)
    if not callable(original) or getattr(original, "_h4v3_resource_cli_installed", False):
        return

    def guarded_cmd_dispatch(args: Any) -> int:
        global _last_resource_diagnostics
        output = io.StringIO()
        _last_resource_diagnostics = []
        try:
            with contextlib.redirect_stdout(output):
                code = original(args)
        except BaseException:
            print(output.getvalue(), end="")
            raise
        text = output.getvalue()
        diagnostics = list(_last_resource_diagnostics)
        if not diagnostics:
            print(text, end="")
            return code if isinstance(code, int) else 0
        busy = [item for item in diagnostics if item.get("reason") == "resource_busy"]
        if getattr(args, "json", False):
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                print(text, end="")
                return code if isinstance(code, int) else 0
            if isinstance(payload, dict):
                if busy:
                    payload["resource_busy"] = busy
                    busy_ids = {
                        str(item["task_id"])
                        for item in busy
                        if item.get("task_id") is not None
                    }
                    payload["skipped_resource_busy"] = sorted(busy_ids)
                    spawned = payload.get("spawned")
                    if isinstance(spawned, list):
                        payload["spawned"] = [
                            item for item in spawned
                            if not isinstance(item, dict)
                            or str(item.get("task_id")) not in busy_ids
                        ]
                other = [item for item in diagnostics if item not in busy]
                if other:
                    payload["resource_admission"] = other
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return code if isinstance(code, int) else 0
            print(text, end="")
            return code if isinstance(code, int) else 0

        print(text, end="")
        for item in diagnostics:
            task_id = item.get("task_id") or "?"
            reason = item.get("reason") or "resource_admission"
            if reason == "resource_busy":
                reason = "resource_busy"
            group = item.get("resource_group") or "resource"
            active = item.get("resource_active")
            capacity = item.get("resource_capacity")
            occupancy = (
                f" ({active}/{capacity})"
                if active is not None and capacity is not None
                else ""
            )
            print(f"Deferred ({reason}): {task_id} [{group}{occupancy}]")
        return code if isinstance(code, int) else 0

    guarded_cmd_dispatch._h4v3_resource_cli_installed = True  # type: ignore[attr-defined]
    guarded_cmd_dispatch._h4v3_resource_cli_original = original  # type: ignore[attr-defined]
    setattr(cli_module, "_cmd_dispatch", guarded_cmd_dispatch)


def install_core_claim_admission(kanban_db: Any, admission_module: Any) -> None:
    """Gate normal READY/REVIEW claims with the same resource policies.

    The wrapper returns ``None`` when a resource is busy.  That is the core
    dispatcher's normal "claim not obtained" path, so a capacity wait never
    increments failure counters or trips the worker circuit breaker.
    """
    install_dynamic_resource_policy(admission_module)
    install_cross_board_helpers(admission_module)
    _install_resource_health_probes(kanban_db, admission_module)
    _install_dispatch_overlay(kanban_db, admission_module)
    _install_cli_dispatch_overlay()

    for attr in ("claim_task", "claim_review_task"):
        original = getattr(kanban_db, attr, None)
        if not callable(original):
            continue
        if getattr(original, "_h4v3_resource_admission_installed", False):
            continue

        def make_guarded(original_fn: Any, function_name: str):
            lane = "review" if function_name == "claim_review_task" else "ready"

            def guarded(conn: sqlite3.Connection, task_id: str, *args: Any, **kwargs: Any):
                task_key = str(task_id)
                try:
                    cfg = _load_kanban_cfg()
                    assignee = _row_assignee(conn, task_key, cfg)
                    resource = admission_module.resource_for_assignee(cfg, assignee)
                except Exception as exc:
                    logger.warning(
                        "resource admission: refusing %s(%s): %s",
                        function_name,
                        task_key,
                        exc,
                    )
                    _record_resource_diagnostics(
                        admission_module,
                        [{
                            "task_id": task_key,
                            "lane": lane,
                            "reason": "resource_config_invalid",
                        }],
                    )
                    return None

                if resource is None:
                    return original_fn(conn, task_id, *args, **kwargs)

                board = _board_for_connection(kanban_db, conn)
                try:
                    with admission_module._resource_lock(
                        kanban_db, board, resource.name
                    ) as held:
                        if not held:
                            _record_resource_diagnostics(
                                admission_module,
                                [{
                                    "task_id": task_key,
                                    "lane": lane,
                                    "reason": "resource_locked",
                                    "resource_group": resource.name,
                                    "resource_capacity": int(resource.capacity),
                                }],
                                board=board,
                            )
                            return None

                        # A task may have been requeued after its prior run
                        # reached a terminal state while the detached worker
                        # was still alive.  Reap only a verified same-task
                        # worker; active or unverifiable PIDs remain a hard
                        # stop and can never be bypassed.
                        reap = {"ok": True, "reaped": False}
                        quiesce = getattr(
                            admission_module, "_quiesce_superseded_worker", None
                        )
                        if callable(quiesce):
                            reap = cast(dict[str, Any], quiesce(
                                conn,
                                task_key,
                                grace_seconds=resource.stale_worker_grace_seconds,
                            ))
                        if not reap.get("ok"):
                            _record_resource_diagnostics(
                                admission_module,
                                [{
                                    "task_id": task_key,
                                    "lane": lane,
                                    "reason": str(
                                        reap.get("reason")
                                        or "resource_admission_failed"
                                    ),
                                    "resource_group": resource.name,
                                    "resource_capacity": int(resource.capacity),
                                }],
                                board=board,
                            )
                            return None
                        if reap.get("reaped"):
                            _record_resource_diagnostics(
                                admission_module,
                                [{
                                    "task_id": task_key,
                                    "lane": lane,
                                    "reason": "superseded_worker_reaped",
                                    "resource_group": resource.name,
                                    "resource_capacity": int(resource.capacity),
                                }],
                                board=board,
                            )

                        active = admission_module._active_resource_workers(
                            kanban_db, board, resource
                        )
                        if len(active) >= int(resource.capacity):
                            _record_resource_diagnostics(
                                admission_module,
                                [{
                                    "task_id": task_key,
                                    "lane": lane,
                                    "reason": "resource_busy",
                                    "resource_group": resource.name,
                                    "resource_active": len(active),
                                    "resource_capacity": int(resource.capacity),
                                }],
                                board=board,
                            )
                            return None
                        # Keep the host resource lock through the CAS claim.
                        # Once this returns a task, status=running is itself an
                        # in-flight reservation visible to sibling-board scans.
                        claimed = original_fn(conn, task_id, *args, **kwargs)
                        if claimed is not None:
                            _record_resource_diagnostics(
                                admission_module,
                                [{
                                    "task_id": task_key,
                                    "lane": lane,
                                    "reason": "resource_admitted",
                                    "resource_group": resource.name,
                                    "resource_capacity": int(resource.capacity),
                                }],
                                board=board,
                            )
                        return claimed
                except Exception as exc:
                    logger.warning(
                        "resource admission: refusing %s(%s) for %s: %s",
                        function_name,
                        task_key,
                        resource.name,
                        exc,
                    )
                    _record_resource_diagnostics(
                        admission_module,
                        [{
                            "task_id": task_key,
                            "lane": lane,
                            "reason": "resource_admission_failed",
                            "resource_group": resource.name,
                            "resource_capacity": int(resource.capacity),
                        }],
                        board=board,
                    )
                    return None

            guarded._h4v3_resource_admission_installed = True  # type: ignore[attr-defined]
            guarded._h4v3_resource_admission_original = original_fn  # type: ignore[attr-defined]
            return guarded

        setattr(kanban_db, attr, make_guarded(original, attr))


def install_everywhere(admission_module: Any, kanban_db: Any | None = None) -> None:
    """Install dynamic matching + corrected helpers, optionally core gating."""
    install_dynamic_resource_policy(admission_module)
    install_cross_board_helpers(admission_module)
    if kanban_db is not None:
        install_core_claim_admission(kanban_db, admission_module)
