from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_local_three_store_ports_are_pinned_to_literal_loopback() -> None:
    compose = (ROOT / "law_repository" / "docker-compose.yml").read_text(encoding="utf-8")
    expected = {
        '"127.0.0.1:${POSTGRES_PORT:-55432}:5432"',
        '"127.0.0.1:${QDRANT_HTTP_PORT:-6333}:6333"',
        '"127.0.0.1:${QDRANT_GRPC_PORT:-6334}:6334"',
        '"127.0.0.1:${NEO4J_HTTP_PORT:-7474}:7474"',
        '"127.0.0.1:${NEO4J_BOLT_PORT:-7687}:7687"',
    }
    assert all(f"- {binding}" in compose for binding in expected)
    assert '- "${POSTGRES_PORT:-55432}:5432"' not in compose
    assert '- "${QDRANT_HTTP_PORT:-6333}:6333"' not in compose
    assert '- "${NEO4J_BOLT_PORT:-7687}:7687"' not in compose
