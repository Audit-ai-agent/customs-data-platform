"""P0 실제 PostgreSQL·Neo4j·Evidence API vertical slice."""
from __future__ import annotations

from datetime import date

import pytest

from src.db.neo4j import driver as make_driver
from src.db.postgres import connect
from src.evidence import RetrieveRequest, retrieve_evidence


@pytest.fixture(scope="module")
def snapshot_id():
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT snapshot_id FROM knowledge_snapshots "
                "WHERE source_type='ccct' AND is_current"
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"CCCT PostgreSQL 원장 접속 불가: {exc}")
    if not row:
        pytest.skip("CCCT snapshot 미적재")
    return row[0]


@pytest.fixture(scope="module")
def snapshot_effective_date(snapshot_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT (generated_at AT TIME ZONE 'Asia/Seoul')::date "
            "FROM knowledge_snapshots WHERE snapshot_id=%s",
            (snapshot_id,),
        )
        return cur.fetchone()[0]


def _request(items, review_as_of=None):
    return RetrieveRequest.model_validate({
        "schema_version": "1.0",
        "request_id": "integration-1",
        "case_id": "case-integration",
        "review_run_id": "run-integration",
        "review_as_of": review_as_of or date.today(),
        "jurisdiction": "KR",
        "trade_type": "import",
        "top_k": 8,
        "items": items,
        "uploaded_requirement_document_codes": [],
        "validation_issue_codes": [],
        "query_intent": "required_import_documents",
    })


def test_postgres_snapshot_is_lossless(snapshot_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT query_count, raw_row_count FROM knowledge_snapshots WHERE snapshot_id=%s",
            (snapshot_id,),
        )
        assert cur.fetchone() == (22654, 14726)
        cur.execute("SELECT count(*) FROM ccct_queries WHERE snapshot_id=%s", (snapshot_id,))
        assert cur.fetchone()[0] == 22654
        cur.execute("SELECT count(*) FROM ccct_source_records WHERE snapshot_id=%s", (snapshot_id,))
        assert cur.fetchone()[0] == 14726
        cur.execute("SELECT count(*) FROM ccct_requirements WHERE snapshot_id=%s", (snapshot_id,))
        assert cur.fetchone()[0] == 14726


def test_neo4j_projection_matches_postgres_without_legacy_edges(snapshot_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT requirement_id FROM ccct_requirements WHERE snapshot_id=%s",
            (snapshot_id,),
        )
        pg_ids = {row[0] for row in cur.fetchall()}
    drv = make_driver()
    try:
        drv.verify_connectivity()
        with drv.session() as session:
            graph_ids = {
                row["id"] for row in session.run(
                    "MATCH (r:Requirement {snapshot_id: $snapshot}) RETURN r.id AS id",
                    snapshot=snapshot_id,
                )
            }
            legacy = session.run(
                "MATCH ()-[r:REQUIRES]->() RETURN count(r) AS count"
            ).single()["count"]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"CCCT Neo4j projection 접속 불가: {exc}")
    finally:
        drv.close()
    assert graph_ids == pg_ids
    assert legacy == 0


def test_real_evidence_positive_negative_and_ambiguous(
    snapshot_id, snapshot_effective_date
):
    response = retrieve_evidence(_request([
        {
            "item_id": "mackerel", "description": "Frozen mackerel",
            "hs_code": "0303540000", "origin_country": "NO", "destination_country": "KR",
        },
        {
            "item_id": "bolt-exact", "description": "Bolts",
            "hs_code": "7318151000", "origin_country": "CN", "destination_country": "KR",
        },
        {
            "item_id": "bolt-hs6", "description": "Bolts",
            "hs_code": "731815", "origin_country": "CN", "destination_country": "KR",
        },
    ], snapshot_effective_date))
    # 한 품목이라도 HS가 ambiguous/coverage unknown이면 전체 응답도
    # 자동 완료가 불가능한 partial이어야 한다.
    assert response.status == "partial"
    assert response.knowledge_snapshot_id == snapshot_id
    by_id = {result.item_id: result for result in response.results}

    mackerel = by_id["mackerel"]
    assert (mackerel.hs_resolution, mackerel.coverage) == ("exact", "complete")
    assert len(mackerel.evidence) == 1
    assert mackerel.evidence[0].required_document_name == "수입식품등 수입신고확인증"
    assert mackerel.evidence[0].agencies == ["식품의약품안전처"]

    no_requirement = by_id["bolt-exact"]
    assert no_requirement.coverage == "complete"
    assert no_requirement.requirements_found is False
    assert no_requirement.evidence == []

    ambiguous = by_id["bolt-hs6"]
    assert ambiguous.hs_resolution == "ambiguous"
    assert ambiguous.resolved_hsk10 is None
    assert len(ambiguous.candidate_hsk10s) > 1
    assert ambiguous.coverage == "unknown"
    assert ambiguous.evidence == []


def test_multi_agency_and_multi_document_rows_are_not_collapsed(
    snapshot_id, snapshot_effective_date
):
    response = retrieve_evidence(_request([
        {
            "item_id": "horse", "description": "Horse",
            "hs_code": "0101299000", "origin_country": "US", "destination_country": "KR",
        },
        {
            "item_id": "chemical", "description": "Chemical",
            "hs_code": "3808929000", "origin_country": "US", "destination_country": "KR",
        },
    ], snapshot_effective_date))
    by_id = {result.item_id: result for result in response.results}
    assert len(by_id["horse"].evidence) == 9
    assert len(by_id["chemical"].evidence) == 6
    assert len({e.source_record_id for e in by_id["horse"].evidence}) == 9
    assert len({e.source_record_id for e in by_id["chemical"].evidence}) == 6


def test_real_backend_rejects_date_before_snapshot_coverage(snapshot_effective_date):
    response = retrieve_evidence(_request([{
        "item_id": "historical", "description": "Frozen mackerel",
        "hs_code": "0303540000", "origin_country": "NO", "destination_country": "KR",
    }], date(2010, 1, 1)))
    assert response.status == "failed"
    assert response.errors[0].code == "UNSUPPORTED_REVIEW_AS_OF"
    assert response.results[0].coverage == "unknown"
    assert response.results[0].evidence == []
