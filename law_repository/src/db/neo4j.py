"""Neo4j 드라이버 — 조문 위임/인용 그래프 + HSK 요건 그래프 (설계 §4.3).

Law/Article 노드를 두 서브그래프가 공유. article_pk 가 PG·Qdrant 와 동일 키.
"""

from __future__ import annotations

from neo4j import Driver, GraphDatabase

from src.config import settings


def driver() -> Driver:
    return GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
