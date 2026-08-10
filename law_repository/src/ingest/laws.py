"""Bounded customs-law ingestion for the three-law local Data profiles.

현행 조문을 law_articles(PG, 시간버전 원장)에 넣고 Qdrant customs_law_current 에
dense(bge-m3)+bm25(sparse, idf) 로 인덱싱한다.

- 로컬 build 시맨틱: 모든 현행 조문 valid_from=조문시행일자, valid_to=NULL,
  is_current=(valid_from<=today). 이 경량 UAT에는 백그라운드 delta 갱신이 없다.
- 멱등: 같은 (law_id, article_no, paragraph_no) 의 열린(valid_to IS NULL) 행 기준
  upsert. content_hash 동일하면 아무것도 안 함(재임베딩 skip — 불변식).
- 청킹: 조문 1청크. SPLIT_THRESHOLD 초과 조문만 항 단위 분할 + parent_article_pk.
  분할 시 부모 행은 Qdrant 미적재(자식이 검색 단위).

The functional-UAT builder is the sole official-law collector and calls this
loader with already-hashed local inputs. This module deliberately has no
direct Law API command or checkout-local raw-source cache.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime
from typing import Any, Sequence

from src import lawgo
from src.db.postgres import connect
from src.db.qdrant import (
    BM25_VECTOR,
    COLLECTION,
    DENSE_VECTOR,
    TEMPORAL_COLLECTION,
    client as qdrant_client,
    ensure_collection,
    ensure_temporal_collection,
)
from src.embed import bm25
from src.functional_uat_spec import FUNCTIONAL_UAT_TARGET_LAWS

# Preserve this ingestion-module spelling for store-only callers while the
# dependency-free profile spec is the canonical three-law boundary.
TARGET_LAWS = [dict(law) for law in FUNCTIONAL_UAT_TARGET_LAWS]

SPLIT_THRESHOLD = 6000  # bge-m3 8192 토큰 대비 여유. 현행 관세 3법 최장 5,325자 → 분할 0건 예상

_POINT_NS = uuid.uuid5(uuid.NAMESPACE_URL, "trade-doc-auditor/law-article")
_TEMPORAL_POINT_NS = uuid.uuid5(uuid.NAMESPACE_URL, "customs-law-history/temporal-article")
_TEMPORAL_DENSE_BATCH_SIZE = 8


def _hierarchy(name: str) -> str:
    if name.endswith("시행령"):
        return "시행령"
    if name.endswith("시행규칙"):
        return "시행규칙"
    return "법률"


def _to_date(yyyymmdd: str) -> date:
    return datetime.strptime(yyyymmdd, "%Y%m%d").date()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def point_id(law_id: str, article_no: int, paragraph_no: int | None, mst: str) -> str:
    return str(uuid.uuid5(_POINT_NS, f"{law_id}:{article_no}:{paragraph_no or 0}:{mst}"))


def temporal_point_id(article_pk: int, version_key: str) -> str:
    """Stable all-version Qdrant identity for one exact source version."""

    return str(uuid.uuid5(_TEMPORAL_POINT_NS, f"{article_pk}:{version_key}"))


def _temporal_embedding_order(rows: Sequence[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Group similarly sized full texts before bounded dense batches.

    The temporal index always embeds the complete stored text with the pinned
    8,192-token policy.  Reordering write work by text length only reduces
    per-batch padding on CPU; point identities, content, vector model, and
    final Qdrant payloads remain bound to their original PostgreSQL rows.
    """

    def key(row: tuple[Any, ...]) -> tuple[int, int]:
        if len(row) <= 9 or type(row[0]) is not int or not isinstance(row[9], str):
            raise ValueError("temporal embedding row is invalid")
        return len(row[9]), row[0]

    return sorted(rows, key=key)


# ---------------------------------------------------------------- 파싱/청킹


def build_meta(body: dict, mst: str) -> dict:
    info = body.get("법령", {}).get("기본정보", {})
    return {
        "law_id": (info.get("법령ID") or "").strip(),
        "law_name": (info.get("법령명_한글") or "").strip(),
        "mst": mst,
        "enforcement_date": lawgo.squash(info.get("시행일자")),
        "promulgation_date": lawgo.squash(info.get("공포일자")),
        "revision_type": lawgo.content_of(info.get("제개정구분")).strip() or "일부개정",
        "ministry": lawgo.content_of(info.get("소관부처")).strip(),
    }


def _assemble_text(unit: dict) -> str:
    """항/호/목 계층을 들여쓰기 평문으로 (프로토타입 검증 로직 이식)."""
    parts = []
    head = lawgo.clean_text(unit.get("조문내용"))
    if head:
        parts.append(head)
    for hang in lawgo.as_list(unit.get("항")):
        h = lawgo.clean_text(hang.get("항내용"))
        if h:
            parts.append(h)
        for ho in lawgo.as_list(hang.get("호")):
            t = lawgo.clean_text(ho.get("호내용"))
            if t:
                parts.append("  " + t)
            for mok in lawgo.as_list(ho.get("목")):
                m = lawgo.clean_text(mok.get("목내용"))
                if m:
                    parts.append("    " + m)
    return "\n".join(parts)


def parse_articles(body: dict, meta: dict) -> list[dict]:
    """조문단위 → 청크 행 목록. 임계 초과 조문은 항 단위 분할(부모+자식)."""
    units = lawgo.as_list(body.get("법령", {}).get("조문", {}).get("조문단위"))
    rows: list[dict] = []
    for u in units:
        if u.get("조문여부") and u.get("조문여부") != "조문":
            continue
        jo_no = lawgo.squash(u.get("조문번호"))
        jo_branch = lawgo.squash(u.get("조문가지번호")) or "0"
        text = _assemble_text(u)
        if not text:
            continue
        title = lawgo.clean_text(u.get("조문제목"))
        label = lawgo.jo_label(jo_no, jo_branch)
        heading = (
            f"[{meta['law_name']} {label}({title})]" if title else f"[{meta['law_name']} {label}]"
        )
        valid_from = lawgo.squash(u.get("조문시행일자")) or meta["enforcement_date"]
        base = {
            "article_no": lawgo.jo_code(jo_no, jo_branch),
            "title": title or None,
            "valid_from": _to_date(valid_from),
            "label": label,
        }
        full = f"{heading}\n{text}"
        if len(full) <= SPLIT_THRESHOLD:
            rows.append({**base, "paragraph_no": None, "content": full, "children": []})
        else:
            children = []
            for i, hang in enumerate(lawgo.as_list(u.get("항")), start=1):
                parts = [lawgo.clean_text(hang.get("항내용"))]
                for ho in lawgo.as_list(hang.get("호")):
                    parts.append("  " + lawgo.clean_text(ho.get("호내용")))
                    for mok in lawgo.as_list(ho.get("목")):
                        parts.append("    " + lawgo.clean_text(mok.get("목내용")))
                hang_text = "\n".join(p for p in parts if p.strip())
                if hang_text:
                    children.append(
                        {**base, "paragraph_no": i, "content": f"{heading}\n{hang_text}"}
                    )
            rows.append({**base, "paragraph_no": None, "content": full, "children": children})
    return rows


# ---------------------------------------------------------------- PG 적재


def _version_key(meta: dict) -> str:
    """Return an exact source identity while preserving legacy MST behavior.

    The legacy 3-law loader has no phased-source receipt and uses its raw
    MST.  The law-history collector supplies an explicit ``MST@YYYYMMDD``
    identity before calling this module.
    """

    value = meta.get("version_key", meta.get("mst"))
    if not isinstance(value, str) or not value:
        raise ValueError("law source has invalid version key")
    return value


def upsert_law(cur, meta: dict) -> None:
    cur.execute(
        """
        INSERT INTO laws (law_id, law_name, hierarchy, ministry)
        VALUES (%(law_id)s, %(law_name)s, %(hierarchy)s, %(ministry)s)
        ON CONFLICT (law_id) DO UPDATE
          SET law_name = EXCLUDED.law_name, ministry = EXCLUDED.ministry
        """,
        {**meta, "hierarchy": _hierarchy(meta["law_name"])},
    )
    cur.execute(
        """
        INSERT INTO law_versions
          (law_id, mst, version_key, promulgation_date, enforcement_date, revision_type)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (law_id, version_key) DO NOTHING
        """,
        (
            meta["law_id"],
            meta["mst"],
            _version_key(meta),
            _to_date(meta["promulgation_date"]),
            _to_date(meta["enforcement_date"]),
            meta["revision_type"],
        ),
    )


def upsert_law_history(cur, meta: dict) -> None:
    """Insert an immutable source-version display identity for law history.

    The retained legacy loader intentionally keeps one mutable law master.
    History queries instead use these per-version values so a later rename
    cannot relabel a past Evidence citation.
    """

    law_name = meta.get("law_name")
    ministry = meta.get("ministry")
    if not isinstance(law_name, str) or not law_name or not isinstance(ministry, str):
        raise ValueError("law-history source has invalid version metadata")
    hierarchy = _hierarchy(law_name)
    cur.execute(
        """
        INSERT INTO laws (law_id, law_name, hierarchy, ministry)
        VALUES (%(law_id)s, %(law_name)s, %(hierarchy)s, %(ministry)s)
        ON CONFLICT (law_id) DO UPDATE
          SET law_name = EXCLUDED.law_name, ministry = EXCLUDED.ministry
        """,
        {**meta, "hierarchy": hierarchy},
    )
    cur.execute(
        """
        INSERT INTO law_versions
          (law_id, mst, version_key, promulgation_date, enforcement_date, revision_type,
           law_name, hierarchy, ministry)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (law_id, version_key) DO NOTHING
        """,
        (
            meta["law_id"],
            meta["mst"],
            _version_key(meta),
            _to_date(meta["promulgation_date"]),
            _to_date(meta["enforcement_date"]),
            meta["revision_type"],
            law_name,
            hierarchy,
            ministry,
        ),
    )


def _upsert_article_row(
    cur,
    law_id: str,
    mst: str,
    version_key: str,
    row: dict,
    parent_pk: int | None = None,
) -> tuple[int, str]:
    """열린 행(valid_to IS NULL) 기준 upsert. 반환: (pk, 'new'|'changed'|'unchanged')."""
    chash = _content_hash(row["content"])
    is_current = row["valid_from"] <= date.today()
    cur.execute(
        """
        SELECT id, content_hash FROM law_articles
        WHERE law_id = %s AND article_no = %s
          AND paragraph_no IS NOT DISTINCT FROM %s AND item_no IS NULL
          AND valid_to IS NULL
        """,
        (law_id, row["article_no"], row["paragraph_no"]),
    )
    hit = cur.fetchone()
    if hit is None:
        cur.execute(
            """
            INSERT INTO law_articles
              (law_id, article_no, paragraph_no, title, content, content_hash,
               version_mst, version_key, valid_from, valid_to, is_current, parent_article_pk)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s)
            RETURNING id
            """,
            (
                law_id,
                row["article_no"],
                row["paragraph_no"],
                row["title"],
                row["content"],
                chash,
                mst,
                version_key,
                row["valid_from"],
                is_current,
                parent_pk,
            ),
        )
        return cur.fetchone()[0], "new"
    pk, old_hash = hit
    if old_hash == chash:
        return pk, "unchanged"
    # 로컬 build는 열린 행을 갱신한다. 별도 버전-승격 또는 delta 잡은 없다.
    cur.execute(
        """
        UPDATE law_articles
        SET title = %s, content = %s, content_hash = %s, version_mst = %s,
            version_key = %s, valid_from = %s, is_current = %s, qdrant_point_id = NULL,
            parent_article_pk = %s
        WHERE id = %s
        """,
        (
            row["title"],
            row["content"],
            chash,
            mst,
            version_key,
            row["valid_from"],
            is_current,
            parent_pk,
            pk,
        ),
    )
    return pk, "changed"


def load_law(cur, meta: dict, rows: list[dict]) -> dict:
    upsert_law(cur, meta)
    version_key = _version_key(meta)
    stats = {"new": 0, "changed": 0, "unchanged": 0}
    for row in rows:
        pk, status = _upsert_article_row(cur, meta["law_id"], meta["mst"], version_key, row)
        stats[status] += 1
        for child in row["children"]:
            _, cstatus = _upsert_article_row(
                cur,
                meta["law_id"],
                meta["mst"],
                version_key,
                child,
                parent_pk=pk,
            )
            stats[cstatus] += 1
    return stats


def _insert_temporal_article_row(
    cur,
    *,
    law_id: str,
    mst: str,
    version_key: str,
    row: dict,
    parent_pk: int | None = None,
) -> int:
    """Insert one immutable law-history article version.

    The law-history builder only appends versions at an explicitly validated
    effective date.  It never mutates the historical body in place, unlike
    the retained three-law loader above.
    """

    cur.execute(
        """
        INSERT INTO law_articles
          (law_id, article_no, paragraph_no, title, content, content_hash,
           version_mst, version_key, valid_from, valid_to, is_current, parent_article_pk)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, false, %s)
        RETURNING id
        """,
        (
            law_id,
            row["article_no"],
            row["paragraph_no"],
            row["title"],
            row["content"],
            _content_hash(row["content"]),
            mst,
            version_key,
            row["valid_from"],
            parent_pk,
        ),
    )
    return int(cur.fetchone()[0])


def load_law_history_version(cur, meta: dict, rows: list[dict]) -> dict[str, int | bool]:
    """Append one official law version to the temporal ledger.

    A refresh is idempotent by ``(law_id, version_key)``.  The Law API can expose a
    source-wide effective date *and* article-specific future effective dates,
    so intervals are advanced per article.  A whole law-version date closes
    only articles that disappear from the complete official body.  This
    prevents a future-provision row from replacing its still-effective text
    too early and never lets a current row stand in for older legal text.
    """

    is_repealed = meta.get("is_repealed") is True
    if not rows and not is_repealed:
        raise ValueError("law-history source has no articles")
    if rows and is_repealed:
        raise ValueError("law-history repeal source has articles")
    try:
        enforcement = _to_date(meta["enforcement_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("law-history source has an invalid enforcement date") from exc
    version_key = _version_key(meta)

    # Keep parent/child identity explicit.  Child rows are indexed separately
    # when a long article is split, but their interval and parent link remain
    # part of the same immutable official version.
    incoming: list[tuple[tuple[int, int | None], dict, tuple[int, int | None] | None]] = []
    seen: set[tuple[int, int | None]] = set()
    for parent in rows:
        parent_row_key: tuple[int, int | None] = (int(parent["article_no"]), None)
        if parent_row_key in seen or not isinstance(parent.get("valid_from"), date):
            raise ValueError("law-history source has duplicate or invalid articles")
        seen.add(parent_row_key)
        incoming.append((parent_row_key, parent, None))
        children = parent.get("children")
        if not isinstance(children, list):
            raise ValueError("law-history source has invalid article children")
        for child in children:
            child_key = (int(child["article_no"]), int(child["paragraph_no"]))
            if child_key in seen or not isinstance(child.get("valid_from"), date):
                raise ValueError("law-history source has duplicate or invalid articles")
            seen.add(child_key)
            incoming.append((child_key, child, parent_row_key))

    cur.execute(
        "SELECT 1 FROM law_versions WHERE law_id=%s AND version_key=%s",
        (meta["law_id"], version_key),
    )
    already_loaded = cur.fetchone() is not None
    if already_loaded:
        return {"inserted": 0, "already_loaded": True}

    cur.execute(
        "SELECT max(enforcement_date) FROM law_versions WHERE law_id=%s",
        (meta["law_id"],),
    )
    previous_version_date = cur.fetchone()[0]
    if previous_version_date is not None and enforcement <= previous_version_date:
        raise ValueError("law-history version effective date is not append-only")

    cur.execute(
        """
        SELECT id, article_no, paragraph_no, content_hash, valid_from, parent_article_pk
        FROM law_articles
        WHERE law_id=%s AND valid_to IS NULL AND item_no IS NULL
        """,
        (meta["law_id"],),
    )
    open_rows = {
        (int(article_no), int(paragraph_no) if paragraph_no is not None else None): {
            "id": int(article_id),
            "content_hash": str(content_hash),
            "valid_from": valid_from,
            "parent_article_pk": int(parent_pk) if parent_pk is not None else None,
        }
        for article_id, article_no, paragraph_no, content_hash, valid_from, parent_pk in cur.fetchall()
    }

    upsert_law_history(cur, meta)

    if is_repealed:
        if previous_version_date is None:
            raise ValueError("law-history repeal has no predecessor")
        for open_article in open_rows.values():
            if enforcement <= open_article["valid_from"]:
                raise ValueError("law-history repeal precedes an open article")
            cur.execute(
                "UPDATE law_articles SET valid_to=%s, is_current=false "
                "WHERE id=%s AND valid_to IS NULL",
                (enforcement, open_article["id"]),
            )
        return {"inserted": 0, "already_loaded": False}

    selected_pks: dict[tuple[int, int | None], int] = {}
    replaced_or_retained: set[tuple[int, int | None]] = set()
    inserted = 0
    # Parents precede children in ``incoming``, so an unchanged parent can be
    # reused and a replacement parent has a new immutable key before a child
    # is inserted.  This deliberately avoids an UPDATE of historical rows.
    for key, row, parent_key in incoming:
        parent_pk = selected_pks.get(parent_key) if parent_key is not None else None
        if parent_key is not None and parent_pk is None:
            raise ValueError("law-history source has an unresolved article parent")
        row_hash = _content_hash(row["content"])
        existing = open_rows.get(key)
        if (
            existing is not None
            and existing["content_hash"] == row_hash
            and existing["parent_article_pk"] == parent_pk
        ):
            if existing["valid_from"] != row["valid_from"]:
                raise ValueError("law-history source changes an immutable article effective date")
            selected_pks[key] = int(existing["id"])
            replaced_or_retained.add(key)
            continue

        if existing is not None:
            if row["valid_from"] <= existing["valid_from"]:
                raise ValueError("law-history article effective date is not append-only")
            cur.execute(
                "UPDATE law_articles SET valid_to=%s, is_current=false WHERE id=%s AND valid_to IS NULL",
                (row["valid_from"], existing["id"]),
            )
        elif previous_version_date is not None and row["valid_from"] < enforcement:
            # After the baseline every previously effective article must have
            # had an open predecessor.  A missing predecessor would create a
            # gap that cannot safely be inferred from a later full-text body.
            raise ValueError("law-history source omits a prior effective article")

        selected_pks[key] = _insert_temporal_article_row(
            cur,
            law_id=meta["law_id"],
            mst=meta["mst"],
            version_key=version_key,
            row=row,
            parent_pk=parent_pk,
        )
        replaced_or_retained.add(key)
        inserted += 1

    # An article absent from a complete official body is repealed or replaced
    # at the body version's effective date.  Do not close a row at or before
    # its own start date; that would manufacture an invalid empty interval.
    for key, existing in open_rows.items():
        if key in replaced_or_retained:
            continue
        if enforcement <= existing["valid_from"]:
            raise ValueError("law-history source removes an article before it takes effect")
        cur.execute(
            "UPDATE law_articles SET valid_to=%s, is_current=false WHERE id=%s AND valid_to IS NULL",
            (enforcement, existing["id"]),
        )
    return {"inserted": inserted, "already_loaded": False}


# ---------------------------------------------------------------- Qdrant 인덱싱


def index_qdrant(batch_size: int = 64) -> int:
    """is_current 이고 아직 포인트 없는 조문(분할 부모 제외)을 임베딩→upsert."""
    from qdrant_client import models

    from src.embed import dense

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.law_id, l.law_name, l.hierarchy, a.article_no,
                   a.paragraph_no, a.item_no, a.valid_from, a.content, a.version_mst
            FROM law_articles a JOIN laws l USING (law_id)
            WHERE a.is_current AND a.qdrant_point_id IS NULL
              AND NOT EXISTS (SELECT 1 FROM law_articles c WHERE c.parent_article_pk = a.id)
            ORDER BY a.id
            """
        )
        pending = cur.fetchall()
    if not pending:
        print("[qdrant] 인덱싱 대상 없음 (모두 최신)")
        return 0

    qc = qdrant_client()
    ensure_collection(qc)
    total = 0
    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        texts = [r[8] for r in batch]
        dvecs = dense.encode(texts)
        points, ids = [], []
        for r, dv in zip(batch, dvecs):
            pk, law_id, law_name, hierarchy, art_no, para_no, item_no, vfrom, text, mst = r
            pid = point_id(law_id, art_no, para_no, mst)
            sidx, sval = bm25.encode_doc(text)
            points.append(
                models.PointStruct(
                    id=pid,
                    vector={
                        DENSE_VECTOR: dv.tolist(),
                        BM25_VECTOR: models.SparseVector(indices=sidx, values=sval),
                    },
                    payload={
                        "article_pk": pk,
                        "law_id": law_id,
                        "law_name": law_name,
                        "hierarchy": hierarchy,
                        "article_no": art_no,
                        "paragraph_no": para_no,
                        "item_no": item_no,
                        "enforcement_date": vfrom.isoformat(),
                        "text": text,
                    },
                )
            )
            ids.append((pid, pk))
        qc.upsert(collection_name=COLLECTION, points=points)
        with connect() as conn, conn.cursor() as cur:
            cur.executemany("UPDATE law_articles SET qdrant_point_id = %s WHERE id = %s", ids)
        total += len(points)
        print(f"[qdrant] {total}/{len(pending)} upsert")
    return total


def index_temporal_qdrant(batch_size: int = 64) -> int:
    """Rebuild/update the law-history all-version Qdrant collection.

    Every point includes its half-open validity interval.  The repository
    applies the same interval as a Qdrant filter before RRF and checks the
    exact PostgreSQL row afterwards.  No current-only vector is reused.
    """

    from qdrant_client import models

    from src.embed import dense

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.law_id, version.law_name, version.hierarchy, a.article_no,
                   a.paragraph_no, a.item_no, a.valid_from, a.valid_to,
                   a.content, a.version_mst, a.version_key, a.content_hash
            FROM law_articles a
            JOIN law_versions version
              ON version.law_id=a.law_id AND version.version_key=a.version_key
            JOIN law_version_sources src
              ON src.law_id=a.law_id AND src.version_key=a.version_key
            WHERE NOT EXISTS (SELECT 1 FROM law_articles c WHERE c.parent_article_pk=a.id)
            ORDER BY a.id
            """
        )
        rows = _temporal_embedding_order(cur.fetchall())
    qc = qdrant_client()
    ensure_temporal_collection(qc)
    total = 0
    for index in range(0, len(rows), batch_size):
        batch = rows[index : index + batch_size]
        vectors = dense.encode([row[9] for row in batch], batch_size=_TEMPORAL_DENSE_BATCH_SIZE)
        points = []
        for row, vector in zip(batch, vectors):
            (
                article_pk,
                law_id,
                law_name,
                hierarchy,
                article_no,
                paragraph_no,
                item_no,
                valid_from,
                valid_to,
                text,
                mst,
                version_key,
                content_hash,
            ) = row
            sparse_indices, sparse_values = bm25.encode_doc(text)
            points.append(
                models.PointStruct(
                    id=temporal_point_id(int(article_pk), str(version_key)),
                    vector={
                        DENSE_VECTOR: vector.tolist(),
                        BM25_VECTOR: models.SparseVector(
                            indices=sparse_indices, values=sparse_values
                        ),
                    },
                    payload={
                        "article_pk": int(article_pk),
                        "law_id": law_id,
                        "law_name": law_name,
                        "hierarchy": hierarchy,
                        "article_no": article_no,
                        "paragraph_no": paragraph_no,
                        "item_no": item_no,
                        "valid_from_ordinal": valid_from.toordinal(),
                        "valid_to_ordinal": (
                            valid_to.toordinal() if valid_to is not None else date.max.toordinal()
                        ),
                        "version_mst": str(mst),
                        "version_key": str(version_key),
                        "content_hash": str(content_hash),
                        "text": text,
                    },
                )
            )
        qc.upsert(collection_name=TEMPORAL_COLLECTION, points=points)
        total += len(points)
    return total


# ---------------------------------------------------------------- 검증


def verify() -> None:
    with connect() as conn, conn.cursor() as cur:
        print("\n[PG 법령별 조문 수]")
        cur.execute(
            """
            SELECT l.law_name, count(*) FILTER (WHERE a.is_current) AS current,
                   count(*) AS total,
                   count(*) FILTER (WHERE a.qdrant_point_id IS NOT NULL) AS indexed
            FROM law_articles a JOIN laws l USING (law_id)
            GROUP BY l.law_name ORDER BY l.law_name
            """
        )
        for name, current, total, indexed in cur.fetchall():
            print(f"  {name}: 현행 {current} / 전체 {total} / 인덱싱 {indexed}")
        cur.execute("SELECT count(*) FROM law_articles WHERE parent_article_pk IS NOT NULL")
        print(f"  분할 청크(항 단위): {cur.fetchone()[0]}건")

    qc = qdrant_client()
    cnt = qc.count(COLLECTION).count
    print(f"[Qdrant] {COLLECTION} 포인트 수: {cnt}")


def smoke_search(query: str = "수입신고 시 세관장 확인이 필요한 물품") -> None:
    """dense arm / bm25 arm 각각 3건 — 융합·rerank 는 에이전트 소유(설계 §4.2)."""
    from qdrant_client import models

    from src.embed import dense

    qc = qdrant_client()
    dv = dense.encode([query])[0]
    hits = qc.query_points(COLLECTION, query=dv.tolist(), using=DENSE_VECTOR, limit=3)
    print(f"\n[smoke/dense] '{query}'")
    for p in hits.points:
        print(f"  {p.score:.4f} {p.payload['law_name']} 조문 {p.payload['article_no']}")
    sidx, sval = bm25.encode_query(query)
    hits = qc.query_points(
        COLLECTION,
        query=models.SparseVector(indices=sidx, values=sval),
        using=BM25_VECTOR,
        limit=3,
    )
    print("[smoke/bm25]")
    for p in hits.points:
        print(f"  {p.score:.4f} {p.payload['law_name']} 조문 {p.payload['article_no']}")
