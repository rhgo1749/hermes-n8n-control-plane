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
import ipaddress
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
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
        parsed = original_policies(cfg)
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


def install_core_claim_admission(kanban_db: Any, admission_module: Any) -> None:
    """Gate normal READY/REVIEW claims with the same resource policies.

    The wrapper returns ``None`` when a resource is busy.  That is the core
    dispatcher's normal "claim not obtained" path, so a capacity wait never
    increments failure counters or trips the worker circuit breaker.
    """
    install_dynamic_resource_policy(admission_module)
    install_cross_board_helpers(admission_module)

    for attr in ("claim_task", "claim_review_task"):
        original = getattr(kanban_db, attr, None)
        if not callable(original):
            continue
        if getattr(original, "_h4v3_resource_admission_installed", False):
            continue

        def make_guarded(original_fn: Any, function_name: str):
            def guarded(conn: sqlite3.Connection, task_id: str, *args: Any, **kwargs: Any):
                try:
                    cfg = _load_kanban_cfg()
                    assignee = _row_assignee(conn, str(task_id), cfg)
                    resource = admission_module.resource_for_assignee(cfg, assignee)
                except Exception as exc:
                    logger.warning(
                        "resource admission: refusing %s(%s): %s",
                        function_name,
                        task_id,
                        exc,
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
                            return None
                        active = admission_module._active_resource_workers(
                            kanban_db, board, resource
                        )
                        if len(active) >= int(resource.capacity):
                            return None
                        # Keep the host resource lock through the CAS claim.
                        # Once this returns a task, status=running is itself an
                        # in-flight reservation visible to sibling-board scans.
                        return original_fn(conn, task_id, *args, **kwargs)
                except Exception as exc:
                    logger.warning(
                        "resource admission: refusing %s(%s) for %s: %s",
                        function_name,
                        task_id,
                        resource.name,
                        exc,
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
