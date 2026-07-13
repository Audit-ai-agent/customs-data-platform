"""환경설정 로딩 — 모든 시크릿/접속정보는 .env 에서. 값 하드코딩 금지."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "trade_doc"
    postgres_user: str = "trade"
    postgres_password: str = "change_me"
    postgres_sslmode: str = ""      # 관리형 PG(Neon 등)는 "require"

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""        # Qdrant Cloud 용 (로컬은 비움)

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "change_me"

    # 외부 API
    law_api_oc: str = ""
    data_go_kr_service_key: str = ""

    @property
    def postgres_dsn(self) -> str:
        dsn = (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
        return dsn + (f"?sslmode={self.postgres_sslmode}" if self.postgres_sslmode else "")


settings = Settings()
