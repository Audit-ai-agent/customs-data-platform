"""Bounded CCCT reference ledger and Requirement graph projection.

The checked-in ``ccct_progress.jsonl`` is the fixed reference input for local
Data work and functional UAT.  This repository deliberately does not contain
a live data.go.kr sweep client, service key setting, quota workflow, or
network refresh command.  Refreshing that source is a separately approved
data-governance task, not an ordinary development or UAT operation.

저장 모델:
  PostgreSQL ccct_queries          — 빈 items 응답까지 조회 커버리지로 보존
  PostgreSQL ccct_source_records   — API item 원문 jsonb를 1행도 잃지 않고 보존
  PostgreSQL ccct_requirements     — canonical key로 정규화한 스냅샷별 원장

그래프 projection:
  (:HSK)-[:HAS_REQUIREMENT]->(:Requirement)-[:BASED_ON]->(:Law)
  (:Requirement)-[:APPROVED_BY]->(:Agency)
  (:Requirement)-[:REQUIRES_DOCUMENT]->(:Certificate)

기존 REQUIRES 관계는 (HSK, Law, trade_type)가 같은 여러 기관·서류
원본을 덮어쓰므로 적재 시 제거한다.

응답 필드: aplyStrtDt(적용시작→valid_from), dcerCfrmLworCd/Nm(확인법령),
reqApreIttCd/Nm(요건승인기관), reqCfrmIstmNm(요건확인서류명), bfhnAffcRtmTpcd(유형코드).

실행: law_repository/ 에서 ``python -m src.ingest.ccct``
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

URL = "https://apis.data.go.kr/1220000/retrieveCcctLworCd/getRetrieveCcctLworCd"
PROGRESS = Path(__file__).resolve().parents[2] / "data" / "ccct_progress.jsonl"
WIRE_TRADE_TYPES = {"1": "export", "2": "import"}


def _clean(value: object) -> str:
    """API 표시값을 해시 가능한 형태로 최소 정규화한다."""
    return re.sub(r"\s+", " ", str(value or "").strip())


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    raw = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso_date(value: object) -> str | None:
    raw = _clean(value)
    if not raw:
        return None
    if not re.fullmatch(r"\d{8}", raw):
        raise ValueError(f"CCCT 날짜는 YYYYMMDD여야 함: {raw!r}")
    parsed = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    return parsed.isoformat()


def source_uri(hsk10: str, tpcd: str) -> str:
    """서비스키를 노출하지 않는 재현 가능한 출처 URI."""
    return f"{URL}?{urlencode({'hsSgn': hsk10, 'imexTpcd': tpcd})}"


def document_code(name: str) -> str:
    normalized = _clean(name).casefold()
    return f"CCCT_DOC_{_sha256(normalized)[:16].upper()}"


def canonical_requirement_id(
    *,
    hsk10: str,
    trade_type: str,
    law_code: str,
    agency_code: str,
    document_name: str,
    valid_from: str | None,
    valid_to: str | None,
    rtm_tpcd: str,
) -> str:
    """스냅샷이 바뀌어도 같은 요건을 가리키는 안정 ID."""
    key = {
        "hsk10": hsk10,
        "trade_type": trade_type,
        "law_code": law_code,
        "agency_code": agency_code,
        "document": _clean(document_name),
        "valid_from": valid_from,
        "valid_to": valid_to,
        "rtm_tpcd": rtm_tpcd,
    }
    return f"ccct_req_{_sha256(key)}"


@dataclass(frozen=True)
class CcctDataset:
    snapshot_id: str
    content_hash: str
    queries: tuple[dict, ...]
    requirements: tuple[dict, ...]
    source_records: tuple[dict, ...]
    documents: tuple[dict, ...]


def build_dataset(records=None) -> CcctDataset:
    """progress JSONL을 무손실 스냅샷과 canonical Requirement로 변환.

    순수 함수로 두어 DB 없이도 row 보존성·ID 안정성을 검증한다.
    """
    records = list(_iter_progress() if records is None else records)
    queries: list[dict] = []
    source_records: list[dict] = []
    requirements_by_id: dict[str, dict] = {}
    documents_by_code: dict[str, dict] = {}
    seen_queries: set[tuple[str, str]] = set()

    for rec in records:
        hsk10 = _clean(rec.get("hs"))
        tpcd = _clean(rec.get("tpcd"))
        if not re.fullmatch(r"\d{10}", hsk10):
            raise ValueError(f"잘못된 CCCT 조회 HSK: {hsk10!r}")
        if tpcd not in WIRE_TRADE_TYPES:
            raise ValueError(f"잘못된 CCCT 수출입 코드: {tpcd!r}")
        trade_type = WIRE_TRADE_TYPES[tpcd]
        query_key = (hsk10, trade_type)
        if query_key in seen_queries:
            raise ValueError(f"progress에 중복 조회가 존재함: {query_key}")
        seen_queries.add(query_key)

        items = rec.get("items") or []
        if not isinstance(items, list):
            raise ValueError(f"CCCT items는 list여야 함: {query_key}")
        uri = source_uri(hsk10, tpcd)
        raw_response = {"hs": rec.get("hs"), "tpcd": rec.get("tpcd"), "items": items}
        queries.append(
            {
                "hsk10": hsk10,
                "trade_type": trade_type,
                "source_uri": uri,
                "item_count": len(items),
                "query_hash": _sha256(raw_response),
                "raw_response": raw_response,
            }
        )

        for item_index, raw_item in enumerate(items):
            if not isinstance(raw_item, dict):
                raise ValueError(f"CCCT item은 object여야 함: {query_key}[{item_index}]")
            item_hsk = _clean(raw_item.get("hsSgn")) or hsk10
            if item_hsk != hsk10:
                raise ValueError(f"질의/응답 HSK 불일치: {hsk10} != {item_hsk}")
            law_code = _clean(raw_item.get("dcerCfrmLworCd"))
            law_name = _clean(raw_item.get("dcerCfrmLworNm"))
            agency_code = _clean(raw_item.get("reqApreIttCd"))
            agency_name = _clean(raw_item.get("reqApreIttNm"))
            doc_name = _clean(raw_item.get("reqCfrmIstmNm"))
            if not all((law_code, law_name, agency_code, agency_name, doc_name)):
                raise ValueError(f"필수 CCCT 필드 누락: {query_key}[{item_index}]")
            valid_from = _iso_date(raw_item.get("aplyStrtDt"))
            valid_to = _iso_date(raw_item.get("aplyEndDt"))
            rtm_tpcd = _clean(raw_item.get("bfhnAffcRtmTpcd"))
            doc_code = document_code(doc_name)
            req_id = canonical_requirement_id(
                hsk10=hsk10,
                trade_type=trade_type,
                law_code=law_code,
                agency_code=agency_code,
                document_name=doc_name,
                valid_from=valid_from,
                valid_to=valid_to,
                rtm_tpcd=rtm_tpcd,
            )
            normalized = {
                "requirement_id": req_id,
                "hsk10": hsk10,
                "trade_type": trade_type,
                "law_code": law_code,
                "law_name": law_name,
                "agency_code": agency_code,
                "agency_name": agency_name,
                "document_code": doc_code,
                "document_name": doc_name,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "rtm_tpcd": rtm_tpcd,
            }
            version_hash = _sha256(normalized)
            normalized["requirement_version_hash"] = version_hash
            previous = requirements_by_id.get(req_id)
            if previous is not None and previous != normalized:
                raise ValueError(f"canonical Requirement 충돌: {req_id}")
            requirements_by_id[req_id] = normalized
            documents_by_code[doc_code] = {
                "document_code": doc_code,
                "display_name": doc_name,
                "normalized_name": doc_name.casefold(),
            }

            raw_hash = _sha256(raw_item)
            src_key = {
                "hsk10": hsk10,
                "trade_type": trade_type,
                "source_item_index": item_index,
                "raw_record_hash": raw_hash,
            }
            source_records.append(
                {
                    "source_record_id": f"ccct_src_{_sha256(src_key)}",
                    "requirement_id": req_id,
                    "hsk10": hsk10,
                    "trade_type": trade_type,
                    "source_item_index": item_index,
                    "source_uri": uri,
                    "raw_record_hash": raw_hash,
                    "raw_record": raw_item,
                }
            )

    # 스냅샷은 조회 커버리지(빈 응답 포함)와 모든 raw row의 집합으로 결정.
    manifest = {
        "queries": sorted(q["query_hash"] for q in queries),
        "source_records": sorted(r["raw_record_hash"] for r in source_records),
    }
    content_hash = _sha256(manifest)
    return CcctDataset(
        snapshot_id=f"ccct_{content_hash}",
        content_hash=content_hash,
        queries=tuple(queries),
        requirements=tuple(requirements_by_id.values()),
        source_records=tuple(source_records),
        documents=tuple(documents_by_code.values()),
    )


# ---------------------------------------------------------------- load

_PROJECT_CYPHER = """
UNWIND $rows AS row
MATCH (h:HSK {code: row.hsk10})
MERGE (req:Requirement {id: row.requirement_id})
SET req.source_type = 'ccct',
    req.snapshot_id = $snapshot_id,
    req.version_hash = row.requirement_version_hash,
    req.hsk10 = row.hsk10,
    req.trade_type = row.trade_type,
    req.valid_from = row.valid_from,
    req.valid_to = row.valid_to,
    req.rtm_tpcd = row.rtm_tpcd
MERGE (h)-[:HAS_REQUIREMENT]->(req)
MERGE (law:Law {code: row.law_code})
SET law.name = row.law_name
MERGE (req)-[:BASED_ON]->(law)
MERGE (ag:Agency {code: row.agency_code})
SET ag.name = row.agency_name
MERGE (req)-[:APPROVED_BY]->(ag)
MERGE (cert:Certificate {code: row.document_code})
SET cert.name = row.document_name, cert.source_type = 'ccct'
MERGE (req)-[:REQUIRES_DOCUMENT]->(cert)
"""


def _iter_progress_from_path(path: Path):
    """Yield one bounded local ledger file; an incomplete final line is ignored."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _iter_progress():
    """Yield bounded reference records; an incomplete final JSON line is ignored."""
    yield from _iter_progress_from_path(PROGRESS)


def persist_postgres(dataset: CcctDataset) -> None:
    """스냅샷을 하나의 PG 트랜잭션으로 적재하고 현행으로 승격."""

    from psycopg.types.json import Jsonb

    from src.db.postgres import connect

    def batches(rows, size: int = 1000):
        rows = list(rows)
        for start in range(0, len(rows), size):
            yield rows[start : start + size]

    with connect() as conn, conn.cursor() as cur:
        # partial load가 current로 노출되지 않도록 모든 insert와 승격을 단일 tx로 묶는다.
        cur.execute(
            "UPDATE knowledge_snapshots SET is_current=false "
            "WHERE source_type='ccct' AND is_current"
        )
        cur.execute(
            """
            INSERT INTO knowledge_snapshots
              (snapshot_id, source_type, content_hash, query_count, raw_row_count,
               source_uri, is_current)
            VALUES (%s, 'ccct', %s, %s, %s, %s, false)
            ON CONFLICT (snapshot_id) DO UPDATE SET
              content_hash=EXCLUDED.content_hash,
              query_count=EXCLUDED.query_count,
              raw_row_count=EXCLUDED.raw_row_count,
              source_uri=EXCLUDED.source_uri,
              is_current=false
            """,
            (
                dataset.snapshot_id,
                dataset.content_hash,
                len(dataset.queries),
                len(dataset.source_records),
                URL,
            ),
        )
        for batch in batches(dataset.documents):
            cur.execute(
                """
                INSERT INTO ccct_requirement_documents
                  (document_code, display_name, normalized_name)
                SELECT x.document_code, x.display_name, x.normalized_name
                FROM jsonb_to_recordset(%s) AS x(
                  document_code text, display_name text, normalized_name text
                )
                ON CONFLICT (document_code) DO UPDATE SET
                  display_name=EXCLUDED.display_name,
                  normalized_name=EXCLUDED.normalized_name
                """,
                (Jsonb(batch),),
            )
        for batch in batches(dataset.queries):
            cur.execute(
                """
                INSERT INTO ccct_queries
                  (snapshot_id, hsk10, trade_type, source_uri, item_count, query_hash, raw_response)
                SELECT %s, x.hsk10, x.trade_type, x.source_uri, x.item_count,
                       x.query_hash, x.raw_response
                FROM jsonb_to_recordset(%s) AS x(
                  hsk10 text, trade_type text, source_uri text, item_count integer,
                  query_hash text, raw_response jsonb
                )
                ON CONFLICT (snapshot_id, hsk10, trade_type) DO UPDATE SET
                  source_uri=EXCLUDED.source_uri,
                  item_count=EXCLUDED.item_count,
                  query_hash=EXCLUDED.query_hash,
                  raw_response=EXCLUDED.raw_response
                """,
                (dataset.snapshot_id, Jsonb(batch)),
            )
        for batch in batches(dataset.requirements):
            cur.execute(
                """
                INSERT INTO ccct_requirements
                  (snapshot_id, requirement_id, requirement_version_hash, hsk10, trade_type,
                   law_code, law_name, agency_code, agency_name, document_code, document_name,
                   valid_from, valid_to, rtm_tpcd)
                SELECT %s, x.requirement_id, x.requirement_version_hash, x.hsk10,
                       x.trade_type, x.law_code, x.law_name, x.agency_code, x.agency_name,
                       x.document_code, x.document_name, x.valid_from, x.valid_to, x.rtm_tpcd
                FROM jsonb_to_recordset(%s) AS x(
                  requirement_id text, requirement_version_hash text, hsk10 text,
                  trade_type text, law_code text, law_name text, agency_code text,
                  agency_name text, document_code text, document_name text,
                  valid_from date, valid_to date, rtm_tpcd text
                )
                ON CONFLICT (snapshot_id, requirement_id) DO UPDATE SET
                  requirement_version_hash=EXCLUDED.requirement_version_hash,
                  law_name=EXCLUDED.law_name,
                  agency_name=EXCLUDED.agency_name,
                  document_code=EXCLUDED.document_code,
                  document_name=EXCLUDED.document_name,
                  valid_from=EXCLUDED.valid_from,
                  valid_to=EXCLUDED.valid_to,
                  rtm_tpcd=EXCLUDED.rtm_tpcd
                """,
                (dataset.snapshot_id, Jsonb(batch)),
            )
        for batch in batches(dataset.source_records):
            cur.execute(
                """
                INSERT INTO ccct_source_records
                  (snapshot_id, source_record_id, requirement_id, hsk10, trade_type,
                   source_item_index, source_uri, raw_record_hash, raw_record)
                SELECT %s, x.source_record_id, x.requirement_id, x.hsk10, x.trade_type,
                       x.source_item_index, x.source_uri, x.raw_record_hash, x.raw_record
                FROM jsonb_to_recordset(%s) AS x(
                  source_record_id text, requirement_id text, hsk10 text, trade_type text,
                  source_item_index integer, source_uri text, raw_record_hash text,
                  raw_record jsonb
                )
                ON CONFLICT (snapshot_id, source_record_id) DO UPDATE SET
                  requirement_id=EXCLUDED.requirement_id,
                  source_uri=EXCLUDED.source_uri,
                  raw_record_hash=EXCLUDED.raw_record_hash,
                  raw_record=EXCLUDED.raw_record
                """,
                (dataset.snapshot_id, Jsonb(batch)),
            )
        cur.execute(
            "UPDATE knowledge_snapshots SET is_current=true WHERE snapshot_id=%s",
            (dataset.snapshot_id,),
        )


def project_neo4j(dataset: CcctDataset) -> None:
    """현행 PG snapshot을 Requirement 중심 그래프로 투영."""

    from src.db.neo4j import driver as make_driver

    drv = make_driver()
    try:
        with drv.session() as s:
            s.run("CREATE CONSTRAINT law_code IF NOT EXISTS FOR (n:Law) REQUIRE n.code IS UNIQUE")
            s.run(
                "CREATE CONSTRAINT agency_code IF NOT EXISTS FOR (n:Agency) REQUIRE n.code IS UNIQUE"
            )
            s.run(
                "CREATE CONSTRAINT requirement_id IF NOT EXISTS FOR (n:Requirement) REQUIRE n.id IS UNIQUE"
            )
            s.run(
                "CREATE CONSTRAINT certificate_code IF NOT EXISTS FOR (n:Certificate) REQUIRE n.code IS UNIQUE"
            )
            # cleanup + 현행 projection 전체를 단일 tx로 묶어 중간 그래프를 노출하지 않는다.
            tx = s.begin_transaction()
            try:
                tx.run("MATCH ()-[rel:REQUIRES]->() DELETE rel").consume()
                tx.run("MATCH (:Law)-[rel:APPROVED_BY]->(:Agency) DELETE rel").consume()
                tx.run("MATCH (req:Requirement {source_type: 'ccct'}) DETACH DELETE req").consume()
                tx.run(
                    "MATCH (cert:Certificate {source_type: 'ccct'}) DETACH DELETE cert"
                ).consume()
                rows = list(dataset.requirements)
                for i in range(0, len(rows), 1000):
                    tx.run(
                        _PROJECT_CYPHER,
                        rows=rows[i : i + 1000],
                        snapshot_id=dataset.snapshot_id,
                    ).consume()
                tx.commit()
            except Exception:
                tx.rollback()
                raise
        verify(drv)
    finally:
        drv.close()


def load(dataset: CcctDataset | None = None) -> None:
    """PROGRESS → PostgreSQL 원장 → Neo4j projection (멱등).

    ``dataset`` is optional so a caller that just verified the tracked bytes
    can project that exact in-memory snapshot rather than re-reading it.
    """
    dataset = dataset or build_dataset()
    print(
        f"[load] snapshot={dataset.snapshot_id[:21]}… | 조회 {len(dataset.queries)} | "
        f"raw {len(dataset.source_records)} | Requirement {len(dataset.requirements)}"
    )
    persist_postgres(dataset)
    project_neo4j(dataset)


def verify(drv=None) -> None:
    own = drv is None
    if drv is None:
        from src.db.neo4j import driver as make_driver

        drv = make_driver()
    try:
        with drv.session() as s:
            req = s.run(
                "MATCH (n:Requirement {source_type: 'ccct'}) RETURN count(n) AS c"
            ).single()["c"]
            laws = s.run("MATCH (n:Law) RETURN count(*) AS c").single()["c"]
            ags = s.run("MATCH (n:Agency) RETURN count(*) AS c").single()["c"]
            certs = s.run(
                "MATCH (n:Certificate {source_type: 'ccct'}) RETURN count(*) AS c"
            ).single()["c"]
            hsk = s.run(
                "MATCH (h:HSK)-[:HAS_REQUIREMENT]->(:Requirement {source_type: 'ccct'}) "
                "RETURN count(DISTINCT h) AS c"
            ).single()["c"]
            print(
                f"[verify] Requirement {req} | 확인법령 {laws} | 기관 {ags} | 서류 {certs} | 요건 HSK {hsk}"
            )
            sample = s.run(
                """
                MATCH (h:HSK)-[:HAS_REQUIREMENT]->(req:Requirement)-[:BASED_ON]->(law:Law),
                      (req)-[:APPROVED_BY]->(ag:Agency),
                      (req)-[:REQUIRES_DOCUMENT]->(cert:Certificate)
                RETURN h.code AS hs, h.name_ko AS name, req.trade_type AS tt,
                       law.name AS law, cert.name AS doc, ag.name AS agency
                LIMIT 3
                """
            )
            for r in sample:
                print(
                    f"  {r['hs']}({r['name']}) --{r['tt']}--> {r['law']} / {r['doc']} / {r['agency']}"
                )
    finally:
        if own:
            drv.close()


def main(_argv: list[str] | None = None) -> int:
    """Load the bounded reference input into the local Data stores."""

    for _stream in (sys.stdout, sys.stderr):
        try:
            configure = getattr(_stream, "reconfigure", None)
            if callable(configure):
                configure(errors="replace")
        except (AttributeError, ValueError):
            pass
    load()
    return 0


if __name__ == "__main__":
    sys.exit(main())
