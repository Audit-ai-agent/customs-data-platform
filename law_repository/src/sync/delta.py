"""Phase 3 — 델타 동기화 + 승격 잡 (설계 §8, 로드맵 Phase 3).

단일 순차 잡(잠금 회피): 변경감지 → 버전 반영 → 승격 → 재임베딩 → watermark.

- 변경감지: lsHstInf 를 watermark 이후 일자별 폴링. regDt = 법령정보센터 DB
  변경일(공포일 아님 — 과거 MST 재등재도 섞여 옴, 2026-07-02 실측).
  law_versions 에 이미 있는 (law_id, mst) 는 처리 완료로 보고 skip → 멱등.
- 버전 반영: 신규 MST 본문을 통째로 받아 조문별 content_hash 비교(로컬 diff).
  바뀐 조문만: 열린 행 valid_to 닫기 + 새 행 insert. 공포≠시행이면 새 행은
  is_current=false 로 pre-load(Qdrant 미적재) — 승격은 promote() 단일 경로.
- 폐지: 열린 행 전부 valid_to=시행일 (승격 잡이 당일 demote + Qdrant 삭제).
- 승격: valid_to<=today 인 현행 demote(+Qdrant delete), 오늘 유효구간에 든
  비현행 promote → index_qdrant() 가 point 없는 현행만 임베딩.

Neo4j 재구성 정책(2026-07-02 확정): 전부개정·폐지 = 법령 단위 노드·엣지 전체
재구축, 일부개정 = 변경 조문 국소 갱신. 조문 그래프 미구축이므로 지금은
_neo4j_hook() no-op 스텁만 둔다.

실행: law_repository/ 에서  python -m src.sync.delta  [--since YYYYMMDD] [--dry-run]
스케줄: 매일 1회 (pm2/작업스케줄러). 같은 날 재실행 안전(멱등).
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

from src import lawgo
from src.db.postgres import connect
from src.db.qdrant import COLLECTION, client as qdrant_client
from src.ingest.laws import TARGET_LAWS, build_meta, index_qdrant, parse_articles, upsert_law

_TARGET_IDS = {t["law_id"]: t["name"] for t in TARGET_LAWS}


# ---------------------------------------------------------------- 변경감지


def fetch_changes(known: set[tuple[str, str]], since: date, until: date) -> list[dict]:
    """[since, until] 일자별 lsHstInf → 대상 법령의 '신규 MST' 목록 (시행일 오름차순)."""
    seen: dict[tuple[str, str], dict] = {}
    day = since
    while day <= until:
        page = 1
        while True:
            data = lawgo.get(
                "lawSearch.do",
                target="lsHstInf",
                regDt=day.strftime("%Y%m%d"),
                display=100,
                page=page,
            )
            ls = data.get("LawSearch", {})
            items = lawgo.as_list(ls.get("law"))
            for it in items:
                law_id = lawgo.squash(it.get("법령ID"))
                mst = lawgo.squash(it.get("법령일련번호"))
                if law_id not in _TARGET_IDS or (law_id, mst) in known:
                    continue
                seen[(law_id, mst)] = {
                    "law_id": law_id,
                    "mst": mst,
                    "enforcement_date": lawgo.squash(it.get("시행일자")),
                    "promulgation_date": lawgo.squash(it.get("공포일자")),
                    "revision_type": (it.get("제개정구분명") or "").strip(),
                }
            total = int(ls.get("totalCnt", 0) or 0)
            if not items or page * 100 >= total:
                break
            page += 1
        day += timedelta(days=1)
    return sorted(seen.values(), key=lambda c: c["enforcement_date"])


# ---------------------------------------------------------------- 버전 반영


def _open_row(cur, law_id: str, article_no: int, paragraph_no: int | None):
    cur.execute(
        """
        SELECT id, content_hash, valid_from FROM law_articles
        WHERE law_id = %s AND article_no = %s
          AND paragraph_no IS NOT DISTINCT FROM %s AND item_no IS NULL
          AND valid_to IS NULL
        """,
        (law_id, article_no, paragraph_no),
    )
    return cur.fetchone()


def _insert_row(cur, law_id: str, mst: str, row: dict, parent_pk: int | None = None) -> int:
    import hashlib

    cur.execute(
        """
        INSERT INTO law_articles
          (law_id, article_no, paragraph_no, title, content, content_hash,
           version_mst, valid_from, valid_to, is_current, parent_article_pk)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, false, %s)
        RETURNING id
        """,
        (
            law_id,
            row["article_no"],
            row["paragraph_no"],
            row["title"],
            row["content"],
            hashlib.sha256(row["content"].encode("utf-8")).hexdigest(),
            mst,
            row["valid_from"],
            parent_pk,
        ),
    )
    return cur.fetchone()[0]


def _reconcile_article(
    cur, law_id: str, mst: str, row: dict, parent_pk: int | None = None
) -> tuple[int | None, str]:
    """새 버전의 조문 1건을 열린 행과 대조. 반환 (새 행 pk 또는 None, 상태)."""
    import hashlib

    chash = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
    hit = _open_row(cur, law_id, row["article_no"], row["paragraph_no"])
    if hit is None:
        return _insert_row(cur, law_id, mst, row, parent_pk), "added"
    pk, old_hash, old_from = hit
    if old_hash == chash:
        return None, "same"
    if row["valid_from"] < old_from:
        return None, "stale"  # 과거 버전 늦은 도착 — 현행 추적 대상 아님
    if row["valid_from"] == old_from:
        # 동일 시행일 재공포(정정) — 열린 행 교체, 재임베딩 필요
        cur.execute(
            """
            UPDATE law_articles
            SET title=%s, content=%s, content_hash=%s, version_mst=%s, qdrant_point_id=NULL
            WHERE id=%s
            """,
            (row["title"], row["content"], chash, mst, pk),
        )
        return pk, "corrected"
    # 통상 개정: 열린 행 닫기(반열림 [from,to)) + 새 행. is_current 전환은 promote() 가.
    cur.execute("UPDATE law_articles SET valid_to=%s WHERE id=%s", (row["valid_from"], pk))
    return _insert_row(cur, law_id, mst, row, parent_pk), "amended"


def apply_version(cur, change: dict) -> dict:
    """신규 MST 1건 반영. law_versions insert + 조문 reconcile + 삭제 조문 닫기."""
    law_id, mst = change["law_id"], change["mst"]
    eff = datetime.strptime(change["enforcement_date"], "%Y%m%d").date()

    if change["revision_type"] == "폐지":
        cur.execute(
            "UPDATE law_articles SET valid_to=%s WHERE law_id=%s AND valid_to IS NULL",
            (eff, law_id),
        )
        cur.execute(
            """
            INSERT INTO law_versions (law_id, mst, promulgation_date, enforcement_date, revision_type)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (law_id, mst) DO NOTHING
            """,
            (
                law_id,
                mst,
                datetime.strptime(change["promulgation_date"], "%Y%m%d").date(),
                eff,
                "폐지",
            ),
        )
        _neo4j_hook(law_id, "폐지")
        return {"repealed": cur.rowcount}

    body = lawgo.get("lawService.do", target="law", MST=mst)
    meta = build_meta(body, mst)
    meta.update(
        promulgation_date=change["promulgation_date"],
        enforcement_date=change["enforcement_date"],
        revision_type=change["revision_type"] or meta["revision_type"],
    )
    upsert_law(cur, meta)

    rows = parse_articles(body, meta)
    stats = {"added": 0, "same": 0, "stale": 0, "corrected": 0, "amended": 0, "removed": 0}
    new_keys = set()
    for row in rows:
        new_keys.add((row["article_no"], row["paragraph_no"]))
        pk, status = _reconcile_article(cur, law_id, mst, row)
        stats[status] += 1
        for child in row["children"]:
            new_keys.add((child["article_no"], child["paragraph_no"]))
            _, cstatus = _reconcile_article(cur, law_id, mst, child, parent_pk=pk)
            stats[cstatus] += 1

    # 새 버전에서 사라진 조문 → 시행일에 닫힘 (승격 잡이 demote)
    cur.execute(
        "SELECT id, article_no, paragraph_no FROM law_articles "
        "WHERE law_id=%s AND valid_to IS NULL AND valid_from < %s",
        (law_id, eff),
    )
    for pk, art_no, para_no in cur.fetchall():
        if (art_no, para_no) not in new_keys:
            cur.execute("UPDATE law_articles SET valid_to=%s WHERE id=%s", (eff, pk))
            stats["removed"] += 1

    _neo4j_hook(law_id, change["revision_type"])
    return stats


def _neo4j_hook(law_id: str, revision_type: str) -> None:
    """조문 그래프 재구성 (정책 확정: 전부개정·폐지=법령 단위 전체 재구축,
    일부개정=국소 갱신). 조문 그래프 미구축 — 구축 시 구현. 현재 no-op."""


# ---------------------------------------------------------------- 승격


def promote(cur, qc=None, as_of: date | None = None) -> dict:
    """유효구간 기준으로 is_current 를 as_of 시점에 맞춘다. 재실행 멱등.

    demote: 현행인데 valid_to<=as_of → is_current=false + Qdrant point 삭제.
    promote: 비현행인데 as_of 가 [valid_from, valid_to) 안 → is_current=true
             (임베딩은 이후 index_qdrant() 가 point 없는 현행만 수행).
    """
    as_of = as_of or date.today()
    cur.execute(
        """
        UPDATE law_articles SET is_current=false
        WHERE is_current AND valid_to IS NOT NULL AND valid_to <= %s
        RETURNING id, qdrant_point_id
        """,
        (as_of,),
    )
    demoted = cur.fetchall()
    stale_points = [str(p) for _, p in demoted if p]
    if stale_points:
        if qc is not None:
            qc.delete(collection_name=COLLECTION, points_selector=stale_points)
        cur.execute(
            "UPDATE law_articles SET qdrant_point_id=NULL WHERE id = ANY(%s)",
            ([pk for pk, _ in demoted],),
        )
    cur.execute(
        """
        UPDATE law_articles SET is_current=true
        WHERE NOT is_current AND valid_from <= %s
          AND (valid_to IS NULL OR valid_to > %s)
        RETURNING id
        """,
        (as_of, as_of),
    )
    promoted = cur.fetchall()
    return {"demoted": len(demoted), "promoted": len(promoted)}


# ---------------------------------------------------------------- watermark & 실행


def read_watermark(cur) -> date | None:
    cur.execute("SELECT last_synced_at FROM sync_state WHERE id=1")
    row = cur.fetchone()
    return row[0].date() if row else None


def write_watermark(cur) -> None:
    cur.execute(
        """
        INSERT INTO sync_state (id, last_synced_at) VALUES (1, now())
        ON CONFLICT (id) DO UPDATE SET last_synced_at = EXCLUDED.last_synced_at
        """
    )


def run(argv: list[str]) -> int:
    # cp949 콘솔 유니코드 출력 크래시 방어 (ccct 988664b 와 동일)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    dry = "--dry-run" in argv
    since_arg = next(
        (
            a.split("=", 1)[1] if "=" in a else argv[argv.index(a) + 1]
            for a in argv
            if a.startswith("--since")
        ),
        None,
    )
    today = date.today()

    with connect() as conn, conn.cursor() as cur:
        wm = read_watermark(cur)
        since = datetime.strptime(since_arg, "%Y%m%d").date() if since_arg else wm
        if since is None:
            print("[delta] watermark 없음 — --since YYYYMMDD 로 최초 기준일을 지정하세요.")
            write_watermark(cur)
            print("[delta] watermark 를 오늘로 초기화했습니다. 다음 실행부터 delta 폴링.")
            return 0
        cur.execute("SELECT law_id, mst FROM law_versions")
        known = set(cur.fetchall())

    print(f"[delta] 변경 폴링: {since} → {today} (대상 {len(_TARGET_IDS)}개 법령)")
    changes = fetch_changes(known, since, today)
    print(
        f"[delta] 신규 버전 {len(changes)}건: "
        + ", ".join(
            f"{_TARGET_IDS[c['law_id']]}@{c['mst']}(시행 {c['enforcement_date']}, {c['revision_type']})"
            for c in changes
        )
        if changes
        else "[delta] 신규 버전 없음"
    )
    if dry:
        return 0

    qc = qdrant_client()
    with connect() as conn, conn.cursor() as cur:
        for change in changes:
            stats = apply_version(cur, change)
            print(f"[delta] {_TARGET_IDS[change['law_id']]} MST {change['mst']}: {stats}")
        pstats = promote(cur, qc)
        print(f"[promote] {pstats}")
        write_watermark(cur)

    embedded = index_qdrant()
    if embedded:
        print(f"[delta] 재임베딩 {embedded}건")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
