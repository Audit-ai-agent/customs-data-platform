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


def test_compose_uses_project_scoped_containers_digest_pins_and_no_default_passwords() -> None:
    compose = (ROOT / "law_repository" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "container_name:" not in compose
    assert "@sha256:" in compose
    assert "change_me" not in compose
    assert "com.customs.data.profile" in compose
    assert compose.count("image:") == 3
    assert all("@sha256:" in line for line in compose.splitlines() if "image:" in line)
    for service in ("postgres", "qdrant", "neo4j"):
        marker = f"  {service}:"
        section = compose.split(marker, 1)[1]
        assert "healthcheck:" in section.split("\n  ", 1)[0] or "healthcheck:" in section
