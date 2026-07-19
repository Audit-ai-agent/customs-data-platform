"""환경설정 로딩 — 모든 시크릿/접속정보는 .env 에서. 값 하드코딩 금지."""

from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres
    postgres_host: str = "localhost"
    # Production request paths bypass per-request DNS with one explicit
    # address.  Localhost and literal-IP hosts are derived automatically.
    postgres_hostaddr: str = ""
    # Keep the local host default aligned with docker-compose.yml. Port 5432
    # may already belong to another project on a developer workstation.
    postgres_port: int = 55432
    postgres_db: str = "trade_doc"
    postgres_user: str = "trade"
    postgres_password: str = "change_me"
    postgres_sslmode: str = ""  # 관리형 PG(Neon 등)는 "require"

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_http_port: int = 6333
    qdrant_grpc_port: int = 6334
    qdrant_api_key: str = ""  # Qdrant Cloud 용 (로컬은 비움)

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_http_port: int = 7474
    neo4j_bolt_port: int = 7687
    neo4j_user: str = "neo4j"
    neo4j_password: str = "change_me"

    # 외부 API
    law_api_oc: str = ""
    data_go_kr_service_key: str = ""

    @model_validator(mode="after")
    def align_local_service_ports(self) -> "Settings":
        """Keep local client endpoints aligned with Docker's published ports.

        Explicit remote URLs/URIs are preserved. Only the documented localhost
        defaults are derived from their canonical compose port variables.
        """

        if self.qdrant_url == "http://localhost:6333":
            self.qdrant_url = f"http://localhost:{self.qdrant_http_port}"
        if self.neo4j_uri == "bolt://localhost:7687":
            self.neo4j_uri = f"bolt://localhost:{self.neo4j_bolt_port}"
        return self

    @property
    def postgres_dsn(self) -> str:
        dsn = (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
        return dsn + (f"?sslmode={self.postgres_sslmode}" if self.postgres_sslmode else "")


settings = Settings()
