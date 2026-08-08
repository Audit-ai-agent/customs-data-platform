"""Local-only profile state for team development and functional UAT.

The source repository deliberately contains neither credentials nor generated
Data state.  This module owns the small, reproducible boundary below the
workspace ``_local`` directory.  It never writes into the Git checkout and it
never prints a generated secret.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Mapping

PROFILE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
DEV_FIXTURE_PROFILE = "dev-fixture"
DEV_STORE_PROFILE = "dev-store"
FUNCTIONAL_UAT_PROFILE = "functional-uat-3law"
FUNCTIONAL_UAT_KR_TRADE_PROFILE = "functional-uat-kr-trade"
SERVICE_TOKEN_FILENAME = "evidence-service-token"
CONFIG_FILENAME = "config.env"


class LocalProfileError(RuntimeError):
    """A local profile is missing, malformed, or unsafe to use."""


def workspace_root() -> Path:
    """Return the workspace root without relying on the caller's CWD."""

    return Path(__file__).resolve().parents[3]


def local_root(override: Path | None = None) -> Path:
    """Return the ignored local-state root.

    ``CUSTOMS_LOCAL_ROOT`` is intentionally opt-in for tests and isolated
    workspaces. The standard default is the common sibling ``_local``
    directory, never a repository-relative folder.
    """

    if override is not None:
        return override
    configured = os.environ.get("CUSTOMS_LOCAL_ROOT")
    return Path(configured) if configured else workspace_root() / "_local"


def artifact_root(override: Path | None = None) -> Path:
    if override is not None:
        return override
    configured = os.environ.get("CUSTOMS_ARTIFACT_ROOT")
    return Path(configured) if configured else workspace_root() / "_artifacts"


def shared_model_cache_directory(*, root: Path | None = None) -> Path:
    """Return the one ignored model cache shared by local Data profiles."""

    return _safe_directory(local_root(root) / "cache" / "huggingface")


def _validate_profile_name(profile: str) -> str:
    if PROFILE_NAME_PATTERN.fullmatch(profile) is None:
        raise LocalProfileError("local profile name is invalid")
    return profile


def _safe_directory(path: Path) -> Path:
    """Create a local directory only when no existing component is a symlink."""

    # ``Path.resolve`` would hide an existing symlink by returning its target.
    # Preserve the lexical path while inspecting every existing component.
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise LocalProfileError("local profile path must not contain a symbolic link")
    absolute.mkdir(parents=True, exist_ok=True)
    return absolute


def profile_directory(profile: str, *, root: Path | None = None) -> Path:
    return _safe_directory(local_root(root) / "profiles" / _validate_profile_name(profile))


def _write_private_file(path: Path, content: str) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise LocalProfileError("local profile file is unsafe")
        return
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_name, 0o600)
        except OSError:
            # Windows ACLs are inherited from the _local directory.  Failure
            # to emulate POSIX mode bits there is not a reason to expose data.
            pass
        os.replace(temporary_name, path)
    except OSError as exc:
        raise LocalProfileError("local profile file could not be written") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def ensure_service_token(profile: str, *, root: Path | None = None) -> Path:
    """Create or return one local bearer token without revealing its value."""

    directory = profile_directory(profile, root=root)
    token_path = directory / SERVICE_TOKEN_FILENAME
    if token_path.exists():
        if token_path.is_symlink() or not token_path.is_file():
            raise LocalProfileError("service token file is unsafe")
        try:
            existing = token_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise LocalProfileError("service token is unavailable") from exc
        if re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,512}", existing) is None:
            raise LocalProfileError("service token has an invalid format")
        return token_path
    # token_urlsafe uses only bearer-safe URL characters and is longer than
    # the minimum enforced by the HTTP server.
    _write_private_file(token_path, f"{secrets.token_urlsafe(48)}\n")
    return token_path


def read_service_token(profile: str, *, root: Path | None = None) -> str:
    path = ensure_service_token(profile, root=root)
    try:
        token = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise LocalProfileError("service token is unavailable") from exc
    if re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,512}", token) is None:
        raise LocalProfileError("service token has an invalid format")
    return token


def _ensure_data_store_profile(
    profile: str,
    *,
    ports: tuple[int, int, int, int, int],
    root: Path | None = None,
) -> Path:
    """Create one isolated three-store configuration below ``_local``."""

    directory = profile_directory(profile, root=root)
    postgres_port, qdrant_http_port, qdrant_grpc_port, neo4j_http_port, neo4j_bolt_port = ports
    config = directory / CONFIG_FILENAME
    values = {
        "CUSTOMS_DATA_PROFILE": profile,
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": str(postgres_port),
        "POSTGRES_DB": "trade_doc",
        "POSTGRES_USER": "trade",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(36),
        "QDRANT_URL": f"http://127.0.0.1:{qdrant_http_port}",
        "QDRANT_HTTP_PORT": str(qdrant_http_port),
        "QDRANT_GRPC_PORT": str(qdrant_grpc_port),
        "NEO4J_URI": f"bolt://127.0.0.1:{neo4j_bolt_port}",
        "NEO4J_HTTP_PORT": str(neo4j_http_port),
        "NEO4J_BOLT_PORT": str(neo4j_bolt_port),
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": secrets.token_urlsafe(36),
        "LAW_API_OC": "",
    }
    if not config.exists():
        rendered = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
        _write_private_file(config, rendered)
    elif config.is_symlink() or not config.is_file():
        raise LocalProfileError("functional UAT configuration is unsafe")
    for child in (directory / "data" / "laws",):
        _safe_directory(child)
    return directory


def ensure_dev_store_profile(*, root: Path | None = None) -> Path:
    """Create the optional ordinary local-store profile.

    It shares no Docker project, port, password, or volume name with the
    actual functional UAT profile.
    """

    return _ensure_data_store_profile(
        DEV_STORE_PROFILE,
        ports=(55432, 6333, 6334, 7474, 7687),
        root=root,
    )


def ensure_functional_uat_profile(*, root: Path | None = None) -> Path:
    """Create the non-Git Data configuration for the three-law UAT profile."""

    directory = _ensure_data_store_profile(
        FUNCTIONAL_UAT_PROFILE,
        ports=(55452, 6343, 6344, 7484, 7697),
        root=root,
    )
    ensure_service_token(FUNCTIONAL_UAT_PROFILE, root=root)
    return directory


def ensure_functional_uat_kr_trade_profile(*, root: Path | None = None) -> Path:
    """Create an isolated future official-trade guidance profile.

    This intentionally does not adopt, reset, or otherwise alter the legacy
    ``functional-uat-3law`` profile.  A registry declaration alone is not a
    built knowledge snapshot, so callers must still fail closed until the
    explicit source-build path has produced validated local state.
    """

    directory = _ensure_data_store_profile(
        FUNCTIONAL_UAT_KR_TRADE_PROFILE,
        ports=(55462, 6353, 6354, 7494, 7707),
        root=root,
    )
    ensure_service_token(FUNCTIONAL_UAT_KR_TRADE_PROFILE, root=root)
    return directory


def load_config(path: Path) -> dict[str, str]:
    """Read a deliberately tiny dotenv format without shell evaluation."""

    if not path.is_file() or path.is_symlink():
        raise LocalProfileError("local profile configuration is unavailable")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LocalProfileError("local profile configuration is unavailable") from exc
    result: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise LocalProfileError("local profile configuration is malformed")
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key) is None or "\x00" in value:
            raise LocalProfileError("local profile configuration is malformed")
        if key in result:
            raise LocalProfileError("local profile configuration has duplicate keys")
        result[key] = value
    return result


def _refresh_settings() -> None:
    """Refresh the shared Settings object after an explicit profile import."""

    from src import config as config_module
    from src.config import Settings

    refreshed = Settings(_env_file=None)
    for field_name in Settings.model_fields:
        setattr(config_module.settings, field_name, getattr(refreshed, field_name))


def _clear_law_api_credential() -> None:
    """Ensure an ingestion-only credential cannot leak into a service process."""

    os.environ.pop("LAW_API_OC", None)
    # ``settings`` is imported by the Law API helper as an object reference.
    # Update it in place instead of leaving a previous collection credential
    # reachable after its narrowly scoped use.
    config_module = sys.modules.get("src.config")
    if config_module is not None:
        config_module.settings.law_api_oc = ""


def _apply_profile_values(
    values: Mapping[str, str],
    *,
    include_law_api_credential: bool,
) -> None:
    """Import generated values while keeping collection-only state scoped."""

    for key, value in values.items():
        if key == "LAW_API_OC":
            continue
        os.environ[key] = value
    if include_law_api_credential:
        os.environ["LAW_API_OC"] = values["LAW_API_OC"]
    else:
        # A terminal may have exported a key before starting a service. Data
        # runtime processes must not inherit or forward it.
        os.environ.pop("LAW_API_OC", None)


def apply_data_store_environment(
    profile: str,
    *,
    root: Path | None = None,
    include_law_api_credential: bool = False,
) -> Path:
    """Load one generated Data-store profile into this process.

    Existing modules import ``src.config.settings`` by object reference.  The
    object is updated in place so ingestion, database, and graph modules see
    one profile without a repository ``.env`` file.
    """

    directory = profile_directory(profile, root=root)
    values = load_config(directory / CONFIG_FILENAME)
    required = {
        "POSTGRES_PASSWORD",
        "NEO4J_PASSWORD",
        "POSTGRES_PORT",
        "QDRANT_HTTP_PORT",
        "NEO4J_BOLT_PORT",
        "LAW_API_OC",
    }
    if not required <= set(values):
        raise LocalProfileError("functional UAT configuration is incomplete")
    _apply_profile_values(
        values,
        include_law_api_credential=include_law_api_credential,
    )
    cache_root = shared_model_cache_directory(root=root)
    os.environ["HF_HOME"] = str(cache_root)
    # Runtime is cache-only.  The explicit prefetch command temporarily
    # overrides this for its own downloader calls.
    os.environ["HF_HUB_OFFLINE"] = "1"

    _refresh_settings()
    return directory


def apply_functional_uat_runtime_environment(*, root: Path | None = None) -> Path:
    """Load functional-UAT store settings without its collection credential."""

    ensure_functional_uat_profile(root=root)
    return apply_data_store_environment(FUNCTIONAL_UAT_PROFILE, root=root)


def validate_functional_uat_law_api_credential(*, root: Path | None = None) -> Path:
    """Fail before expensive UAT setup without exporting the collection key."""

    _clear_law_api_credential()
    profile = ensure_functional_uat_profile(root=root)
    values = load_config(profile / CONFIG_FILENAME)
    if not values.get("LAW_API_OC", "").strip():
        raise LocalProfileError("functional UAT law API credential is unavailable")
    return profile


def apply_functional_uat_collection_environment(*, root: Path | None = None) -> Path:
    """Load the Law API credential only for the bounded source collection."""

    ensure_functional_uat_profile(root=root)
    try:
        profile = apply_data_store_environment(
            FUNCTIONAL_UAT_PROFILE,
            root=root,
            include_law_api_credential=True,
        )
        if not os.environ.get("LAW_API_OC", "").strip():
            raise LocalProfileError("functional UAT law API credential is unavailable")
        return profile
    except Exception:
        _clear_law_api_credential()
        raise


@contextmanager
def functional_uat_law_collection_environment(
    *,
    root: Path | None = None,
) -> Iterator[Path]:
    """Expose ``LAW_API_OC`` only while fetching the approved three laws."""

    profile = apply_functional_uat_collection_environment(root=root)
    try:
        yield profile
    finally:
        _clear_law_api_credential()


def profile_paths(profile: str, *, root: Path | None = None) -> Mapping[str, Path]:
    directory = profile_directory(profile, root=root)
    return {
        "directory": directory,
        "service_token": directory / SERVICE_TOKEN_FILENAME,
        "config": directory / CONFIG_FILENAME,
        "cache": shared_model_cache_directory(root=root),
        "data": directory / "data",
    }


__all__ = [
    "CONFIG_FILENAME",
    "DEV_FIXTURE_PROFILE",
    "DEV_STORE_PROFILE",
    "FUNCTIONAL_UAT_KR_TRADE_PROFILE",
    "FUNCTIONAL_UAT_PROFILE",
    "LocalProfileError",
    "SERVICE_TOKEN_FILENAME",
    "apply_functional_uat_collection_environment",
    "apply_functional_uat_runtime_environment",
    "apply_data_store_environment",
    "artifact_root",
    "ensure_functional_uat_profile",
    "ensure_functional_uat_kr_trade_profile",
    "ensure_dev_store_profile",
    "ensure_service_token",
    "functional_uat_law_collection_environment",
    "load_config",
    "local_root",
    "profile_directory",
    "profile_paths",
    "read_service_token",
    "shared_model_cache_directory",
    "validate_functional_uat_law_api_credential",
    "workspace_root",
]
