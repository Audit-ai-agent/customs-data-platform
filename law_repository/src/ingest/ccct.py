"""CCCT 무손실 원장과 Requirement 그래프 projection.

소스: 관세청 세관장확인대상물품 API (data.go.kr, serviceKey=.env)
  GET apis.data.go.kr/1220000/retrieveCcctLworCd/getRetrieveCcctLworCd
      ?serviceKey&hsSgn=<10자리>&imexTpcd=<1|2>
  실측(2026-07-02): hsSgn 정확 10자리만(접두어 불가), imexTpcd 필수(1=수출, 2=수입),
  numOfRows/pageNo 무시(쿼리당 전체 반환), 응답 XML.
  → 전수조사 = leaf 11,327 × 2 = 22,654 호출. 진행 캐시(JSONL)로 중단 재개,
    쿼터 초과(resultCode 22) 시 안전 중단 후 다음 날 이어서.

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

실행: law_repository/ 에서  python -m src.ingest.ccct [--sweep-only|--load-only] [--limit N]
"""

from __future__ import annotations

import json
import hashlib
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import requests
import xmltodict
from psycopg.types.json import Jsonb

from src.config import settings
from src.db.neo4j import driver as make_driver
from src.db.postgres import connect
from src.lawgo import as_list

URL = "https://apis.data.go.kr/1220000/retrieveCcctLworCd/getRetrieveCcctLworCd"
PROGRESS = Path(__file__).resolve().parents[2] / "data" / "ccct_progress.jsonl"
TRADE_TYPES = {"1": "수출", "2": "수입"}
WIRE_TRADE_TYPES = {"1": "export", "2": "import"}
WORKERS = 8
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30

_QUOTA_CODES = {"22", "23"}  # 일일 트래픽 초과 / 초당 요청 제한
_session = requests.Session()
_session.headers.update({"User-Agent": "customs-data-platform/ccct-collector"})
_session.trust_env = False


class QuotaExceeded(RuntimeError):
    pass


class CcctApiError(RuntimeError):
    """Content-free official API failure safe for logs and artifacts."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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


# ---------------------------------------------------------------- sweep


def _query(hs: str, tpcd: str, retries: int = 4) -> list[dict]:
    credential = settings.data_go_kr_service_key.strip()
    if not credential or credential.casefold() in {"test", "change_me", "your_service_key_here"}:
        raise CcctApiError("CCCT_API_CREDENTIAL_UNAVAILABLE")
    if not re.fullmatch(r"\d{10}", hs) or tpcd not in TRADE_TYPES:
        raise CcctApiError("CCCT_API_QUERY_INVALID")
    for attempt in range(retries + 1):
        response: requests.Response | None = None
        try:
            response = _session.get(
                URL,
                params={"serviceKey": credential, "hsSgn": hs, "imexTpcd": tpcd},
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                allow_redirects=False,
                stream=True,
            )
            status = int(response.status_code)
            if 300 <= status < 400:
                response.close()
                raise CcctApiError("CCCT_API_REDIRECT_REJECTED")
            if status == 429:
                response.close()
                raise QuotaExceeded("CCCT_API_QUOTA_EXCEEDED")
            if status < 200 or status >= 300:
                response.close()
                if status >= 500 and attempt < retries:
                    time.sleep(1.5 * (attempt + 1) + random.SystemRandom().uniform(0, 0.25))
                    continue
                raise CcctApiError("CCCT_API_REQUEST_REJECTED")

            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    if int(raw_length) > MAX_RESPONSE_BYTES:
                        response.close()
                        raise CcctApiError("CCCT_API_RESPONSE_TOO_LARGE")
                except ValueError:
                    response.close()
                    raise CcctApiError("CCCT_API_INVALID_CONTENT_LENGTH") from None
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    response.close()
                    raise CcctApiError("CCCT_API_RESPONSE_TOO_LARGE")
            response.close()
            break
        except (CcctApiError, QuotaExceeded):
            raise
        except (requests.ConnectionError, requests.Timeout):
            if response is not None:
                response.close()
            if attempt == retries:
                raise CcctApiError("CCCT_API_TRANSPORT_FAILURE") from None
            time.sleep(1.5 * (attempt + 1) + random.SystemRandom().uniform(0, 0.25))
    try:
        parsed = xmltodict.parse(bytes(body), disable_entities=True)
    except Exception:
        raise CcctApiError("CCCT_API_INVALID_XML") from None
    # 쿼터 초과 등 게이트웨이 오류는 <response> 가 아니라 OpenAPI_ServiceResponse 로 옴
    gw = parsed.get("OpenAPI_ServiceResponse")
    if gw:
        hdr = gw.get("cmmMsgHeader") or {}
        reason = (hdr.get("returnReasonCode") or "").strip()
        if reason in _QUOTA_CODES:
            raise QuotaExceeded("CCCT_API_QUOTA_EXCEEDED")
        raise CcctApiError("CCCT_API_GATEWAY_REJECTED")
    d = parsed.get("response") or {}
    code = ((d.get("header") or {}).get("resultCode") or "").strip()
    if code in _QUOTA_CODES:
        raise QuotaExceeded("CCCT_API_QUOTA_EXCEEDED")
    if code != "00":
        raise CcctApiError("CCCT_API_RESULT_REJECTED")
    items = as_list(((d.get("body") or {}).get("items") or {}).get("item"))
    return [dict(it) for it in items if it]


def leaf_codes() -> list[str]:
    drv = make_driver()
    try:
        with drv.session() as s:
            return [r["c"] for r in s.run("MATCH (n:HSK) RETURN n.code AS c ORDER BY c")]
    finally:
        drv.close()


def sweep(limit: int | None = None) -> None:
    """전수조사. 결과(빈 것 포함)를 PROGRESS 에 append — 재실행 시 완료분 skip."""
    done = {(rec["hs"], rec["tpcd"]) for rec in _iter_progress()}

    todo = [(hs, tpcd) for hs in leaf_codes() for tpcd in TRADE_TYPES if (hs, tpcd) not in done]
    if limit:
        todo = todo[:limit]
    print(f"[sweep] 완료 {len(done)} / 남음 {len(todo)} (workers={WORKERS})")
    if not todo:
        return

    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    stop = threading.Event()
    n_done = n_hit = 0

    def work(pair: tuple[str, str]):
        if stop.is_set():
            return None
        return pair, _query(*pair)

    with PROGRESS.open("a", encoding="utf-8") as out, ThreadPoolExecutor(WORKERS) as ex:
        futures = [ex.submit(work, p) for p in todo]
        n_fail = 0
        try:
            for fut in as_completed(futures):
                try:
                    res = fut.result()  # QuotaExceeded 는 여기서 전파
                except QuotaExceeded:
                    raise
                except Exception as e:
                    # 개별 실패는 격리 — progress 미기록이라 다음 실행이 재시도
                    n_fail += 1
                    if n_fail <= 5:
                        print(f"[sweep] 실패(다음 실행 때 재시도): {e}")
                    continue
                if res is None:
                    continue
                (hs, tpcd), items = res
                with lock:
                    out.write(
                        json.dumps({"hs": hs, "tpcd": tpcd, "items": items}, ensure_ascii=False)
                        + "\n"
                    )
                    out.flush()
                    n_done += 1
                    n_hit += bool(items)
                    if n_done % 500 == 0:
                        print(f"[sweep] {n_done}/{len(todo)} (요건 있는 코드 {n_hit})")
        except QuotaExceeded as e:
            stop.set()
            print(f"[sweep] 쿼터 초과로 중단 — 진행분은 저장됨, 내일 재실행: {e}")
        finally:
            stop.set()
    print(f"[sweep] 이번 실행 {n_done}건 조회, 요건 있음 {n_hit}건, 실패 {n_fail}건")


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


def _iter_progress():
    """PROGRESS 라인 이터레이터 — 강제 종료로 깨진 꼬리 라인은 무시(다음 sweep 이 재조회)."""
    if not PROGRESS.exists():
        return
    with PROGRESS.open(encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def persist_postgres(dataset: CcctDataset) -> None:
    """스냅샷을 하나의 PG 트랜잭션으로 적재하고 현행으로 승격."""

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


def load() -> None:
    """PROGRESS → PostgreSQL 원장 → Neo4j projection (멱등)."""
    dataset = build_dataset()
    print(
        f"[load] snapshot={dataset.snapshot_id[:21]}… | 조회 {len(dataset.queries)} | "
        f"raw {len(dataset.source_records)} | Requirement {len(dataset.requirements)}"
    )
    persist_postgres(dataset)
    project_neo4j(dataset)


def verify(drv=None) -> None:
    own = drv is None
    drv = drv or make_driver()
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


def main(argv: list[str]) -> int:
    # 리다이렉트 시 stdout/stderr 이 cp949 로 잡히는데 로그 메시지의 em-dash(—) 등은
    # cp949 에 없어 print 가 UnicodeEncodeError 로 죽는다(쿼터 초과 안전중단 메시지 포함).
    # 인코딩 실패를 치명적 크래시가 아니라 '?' 치환으로 흘려보낸다.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    limit = None
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    if "--load-only" not in argv:
        sweep(limit=limit)
    if "--sweep-only" not in argv:
        load()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
