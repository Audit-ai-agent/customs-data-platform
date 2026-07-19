"""Strict configuration for the production Evidence API process.

The team demo intentionally keeps developer-friendly defaults.  The production
entrypoint does not reuse those defaults as an authorization or transport
policy: it requires an explicit service credential and an explicit TLS
termination mode, then validates the Data-store connections before serving.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config import Settings as DataStoreSettings

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{32,512}$")
_SAFE_LOG_LEVELS = {"critical", "error", "warning", "info"}
_INSECURE_PASSWORDS = {
    "",
    "change_me",
    "changeme",
    "password",
    "postgres",
    "neo4j",
}
_TLS_POSTGRES_MODES = {"require", "verify-ca", "verify-full"}
TransportMode = Literal["direct_tls", "trusted_proxy_tls", "loopback_dev"]


class ProductionConfigurationError(RuntimeError):
    """The Evidence service must not start with an unsafe deployment config."""


@dataclass(frozen=True)
class RuntimeSecrets:
    """Resolved runtime-only secrets.

    Secret bytes are deliberately kept out of Pydantic serialization and are
    never included in logs, metrics, readiness, or exception messages.
    """

    service_token: bytes


class ProductionSettings(BaseSettings):
    """Environment-owned production service settings.

    The production process never reads a repository ``.env`` file.  Operators
    must inject settings through the process environment or a mounted secret
    file.  This avoids a developer ``.env`` silently changing a deployment.
    """

    model_config = SettingsConfigDict(
        env_prefix="EVIDENCE_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    service_token: SecretStr | None = None
    service_token_file: Path | None = None

    transport_mode: TransportMode = "trusted_proxy_tls"
    trusted_proxy_cidrs: str = ""
    allow_insecure_loopback_dev: bool = False

    bind_host: str = "0.0.0.0"
    port: int = Field(default=8010, ge=1, le=65535)
    max_request_body_bytes: int = Field(
        default=1_048_576,
        ge=4_096,
        le=4_194_304,
    )
    max_response_body_bytes: int = Field(
        default=2_097_152,
        ge=4_096,
        le=8_388_608,
    )
    max_concurrency: int = Field(default=8, ge=1, le=64)
    request_deadline_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    shutdown_grace_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    retry_after_seconds: int = Field(default=2, ge=1, le=60)
    enable_openapi: bool = False
    log_level: str = "info"

    @model_validator(mode="after")
    def validate_security_boundary(self) -> "ProductionSettings":
        configured = int(self.service_token is not None) + int(
            self.service_token_file is not None
        )
        if configured != 1:
            raise ValueError(
                "exactly one of EVIDENCE_SERVICE_TOKEN or "
                "EVIDENCE_SERVICE_TOKEN_FILE is required"
            )
        if self.service_token_file is not None and not self.service_token_file.is_absolute():
            raise ValueError("EVIDENCE_SERVICE_TOKEN_FILE must be an absolute path")

        self.log_level = self.log_level.lower()
        if self.log_level not in _SAFE_LOG_LEVELS:
            raise ValueError("EVIDENCE_LOG_LEVEL must be critical, error, warning, or info")

        if self.transport_mode == "loopback_dev":
            if not self.allow_insecure_loopback_dev:
                raise ValueError(
                    "loopback_dev requires EVIDENCE_ALLOW_INSECURE_LOOPBACK_DEV=true"
                )
            if not _is_loopback_host(self.bind_host):
                raise ValueError("loopback_dev must bind only to a loopback address")
        elif self.allow_insecure_loopback_dev:
            raise ValueError(
                "EVIDENCE_ALLOW_INSECURE_LOOPBACK_DEV is valid only with loopback_dev"
            )

        networks = _parse_proxy_networks(self.trusted_proxy_cidrs)
        if self.transport_mode == "trusted_proxy_tls" and not networks:
            raise ValueError(
                "trusted_proxy_tls requires one or more EVIDENCE_TRUSTED_PROXY_CIDRS"
            )
        if self.transport_mode != "trusted_proxy_tls" and networks:
            raise ValueError(
                "EVIDENCE_TRUSTED_PROXY_CIDRS is valid only with trusted_proxy_tls"
            )
        return self

    def proxy_networks(
        self,
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return _parse_proxy_networks(self.trusted_proxy_cidrs)

    def resolve_secrets(self) -> RuntimeSecrets:
        if self.service_token is not None:
            candidate = self.service_token.get_secret_value()
        else:
            assert self.service_token_file is not None
            try:
                if self.service_token_file.is_symlink():
                    raise ProductionConfigurationError(
                        "service token file must not be a symbolic link"
                    )
                raw = self.service_token_file.read_bytes()
            except OSError as exc:
                raise ProductionConfigurationError(
                    "service token file is unavailable"
                ) from exc
            if len(raw) > 1_024:
                raise ProductionConfigurationError("service token file is too large")
            try:
                candidate = raw.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise ProductionConfigurationError(
                    "service token file must contain an ASCII bearer token"
                ) from exc

        if not _TOKEN_PATTERN.fullmatch(candidate):
            raise ProductionConfigurationError(
                "service token must be 32-512 ASCII bearer-safe characters"
            )
        return RuntimeSecrets(service_token=candidate.encode("ascii"))


def _is_loopback_host(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _parse_proxy_networks(
    value: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in (part.strip() for part in value.split(",")):
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=True)
        except ValueError as exc:
            raise ValueError("trusted proxy CIDRs must be canonical IP networks") from exc
        if network.prefixlen == 0:
            raise ValueError("a trusted proxy CIDR must not trust the entire address space")
        networks.append(network)
    return tuple(networks)


def _host_from_url(value: str) -> str | None:
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def _all_data_stores_are_loopback(settings: DataStoreSettings) -> bool:
    qdrant_host = _host_from_url(settings.qdrant_url)
    neo4j_host = _host_from_url(settings.neo4j_uri)
    return (
        _is_loopback_host(settings.postgres_host)
        and bool(qdrant_host)
        and _is_loopback_host(qdrant_host)
        and bool(neo4j_host)
        and _is_loopback_host(neo4j_host)
    )


def validate_production_data_store_settings(
    service: ProductionSettings,
    data: DataStoreSettings,
) -> None:
    """Reject weak credentials and non-TLS remote store connections.

    Plain local Docker connections are allowed only in the explicit,
    loopback-bound development mode.  This function performs configuration
    validation only; it never opens a database or network connection.
    """

    if data.postgres_password.strip().lower() in _INSECURE_PASSWORDS:
        raise ProductionConfigurationError("PostgreSQL password is missing or insecure")
    if data.neo4j_password.strip().lower() in _INSECURE_PASSWORDS:
        raise ProductionConfigurationError("Neo4j password is missing or insecure")

    local_exception = (
        service.transport_mode == "loopback_dev"
        and service.allow_insecure_loopback_dev
        and _all_data_stores_are_loopback(data)
    )
    if local_exception:
        return

    if data.postgres_sslmode.lower() not in _TLS_POSTGRES_MODES:
        raise ProductionConfigurationError("remote PostgreSQL must require TLS")
    if urlsplit(data.qdrant_url).scheme.lower() != "https":
        raise ProductionConfigurationError("remote Qdrant must use HTTPS")
    if urlsplit(data.neo4j_uri).scheme.lower() not in {"bolt+s", "neo4j+s"}:
        raise ProductionConfigurationError("remote Neo4j must use an encrypted URI")
