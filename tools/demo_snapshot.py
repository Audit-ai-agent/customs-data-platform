"""데모·스크린샷용 Data 스토어 스냅샷 CLI.

RDB(PostgreSQL) 원장의 주요 테이블·메타데이터와 벡터 DB(Qdrant)의
Top-K 하이브리드 검색 결과를 터미널 한 화면에 보기 좋게 출력한다.
읽기 전용이며 어떤 스토어에도 쓰기를 하지 않는다.

실행 (저장소 루트, 프로파일 스토어가 떠 있는 상태에서):
  python tools/demo_snapshot.py rdb
  python tools/demo_snapshot.py vector "세관장확인대상물품" --k 5
  python tools/demo_snapshot.py vector "수입 신고" --as-of 2020-06-01
  python tools/demo_snapshot.py all
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAW_ROOT = ROOT / "law_repository"
for import_root in (ROOT, LAW_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.local_profiles import (  # noqa: E402
    FUNCTIONAL_UAT_LAW_HISTORY_PROFILE,
    LocalProfileError,
    apply_data_store_environment,
)

WIDTH = 88  # 섹션 구분선 폭 (스크린샷 기준 한 화면)

# ---------------------------------------------------------------- 색·폭 유틸

USE_COLOR = True
BOLD, DIM = "1", "2"
CYAN, GREEN, YELLOW, MAGENTA, GREY = "36", "32", "33", "35", "90"


def c(text: str, *codes: str) -> str:
    if not USE_COLOR or not codes:
        return text
    return "".join(f"\x1b[{code}m" for code in codes) + text + "\x1b[0m"


def _enable_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _w(text: str) -> int:
    """터미널 표시 폭 — 한글 등 전각 문자는 2칸."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _clip(text: str, width: int) -> str:
    if _w(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + cw > width - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _w(text))
    return " " * gap + text if align == "right" else text + " " * gap


# ---------------------------------------------------------------- 출력 부품


def banner(title: str, subtitle: str) -> None:
    inner = max(_w(title), _w(subtitle)) + 4
    bar = c("│", GREY)
    print(c("╭" + "─" * inner + "╮", GREY))
    print(bar + "  " + c(title, BOLD, CYAN) + " " * (inner - 2 - _w(title)) + bar)
    print(bar + "  " + c(subtitle, GREY) + " " * (inner - 2 - _w(subtitle)) + bar)
    print(c("╰" + "─" * inner + "╯", GREY))


def section(title: str) -> None:
    rule = "─" * max(0, WIDTH - _w(title) - 5)
    print()
    print(c("── ", GREY) + c(title, BOLD, YELLOW) + c(" " + rule, GREY))


def bullet(text: str) -> None:
    print("  " + c("▸", CYAN) + " " + text)


def table(headers: list[str], rows: list[list[str]], aligns: list[str] | None = None) -> None:
    aligns = aligns or ["left"] * len(headers)
    widths = [_w(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _w(cell))

    def line(left: str, mid: str, right: str) -> str:
        return c(left + mid.join("─" * (w + 2) for w in widths) + right, GREY)

    def render(cells: list[str], *, header: bool = False) -> str:
        parts = []
        for i, cell in enumerate(cells):
            body = _pad(cell, widths[i], aligns[i])
            parts.append(" " + (c(body, BOLD, CYAN) if header else body) + " ")
        bar = c("│", GREY)
        return bar + bar.join(parts) + bar

    print(line("┌", "┬", "┐"))
    print(render(headers, header=True))
    print(line("├", "┼", "┤"))
    for row in rows:
        print(render(row))
    print(line("└", "┴", "┘"))


def _fmt_bytes(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{n / (1 << 10):.0f} KB"


def _score_bar(ratio: float, width: int = 12) -> str:
    filled = round(max(0.0, min(1.0, ratio)) * width)
    return c("█" * filled, GREEN) + c("░" * (width - filled), GREY)


# ------------------------------------------------------------------ RDB 패널

# 표시 순서·설명 (마이그레이션 주석 기준). 목록에 없는 테이블도 뒤에 붙는다.
CURATED_TABLES = [
    ("laws", "법령 마스터 (개정 불변 단위)"),
    ("law_versions", "개정 이력 — MST·공포/시행일"),
    ("law_articles", "조문 temporal 원장 (유효구간 버전)"),
    ("law_version_sources", "공식 원문 수집 영수증 (URI·SHA-256)"),
    ("law_history_refreshes", "법령 이력 refresh 원장"),
    ("knowledge_snapshots", "공식 지식 스냅샷 (변경 감지 단위)"),
    ("ccct_queries", "세관장확인 조회 원장 (빈 응답 보존)"),
    ("ccct_requirements", "CCCT 요건 정규화 projection"),
    ("ccct_requirement_documents", "요건 서류 코드 사전"),
    ("ccct_source_records", "CCCT 원본 레코드 (raw jsonb)"),
    ("hsk_ccct_archives", "HSK·CCCT 공식 archive 이력"),
    ("hsk_snapshots", "HSK 트리 스냅샷"),
    ("hsk_node_versions", "HSK 노드 버전"),
    ("hsk_ccct_archive_ccct_bindings", "archive ↔ CCCT 스냅샷 바인딩"),
    ("sync_state", "동기화 watermark"),
    ("audit_findings", "심사 findings 참조 (버전 고정)"),
    ("schema_migrations", "적용된 마이그레이션 기록"),
]


def cmd_rdb(profile: str) -> None:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    from src.config import settings

    with psycopg.connect(settings.postgres_dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT current_setting('server_version') AS v")
        pg_version = cur.fetchone()["v"]
        banner(
            "PostgreSQL 원장 — source of truth",
            f"{settings.postgres_db} @ {settings.postgres_host}:{settings.postgres_port}"
            f" · PostgreSQL {pg_version} · profile {profile}",
        )

        cur.execute(
            """
            SELECT c.relname AS name, pg_total_relation_size(c.oid) AS bytes
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY c.relname
            """
        )
        sizes = {r["name"]: int(r["bytes"]) for r in cur.fetchall()}
        counts: dict[str, int] = {}
        for name in sizes:
            cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(name)))
            counts[name] = int(cur.fetchone()["n"])

        section("주요 테이블")
        ordered = [(n, d) for n, d in CURATED_TABLES if n in sizes]
        ordered += [(n, "") for n in sizes if n not in {t for t, _ in CURATED_TABLES}]
        table(
            ["테이블", "행수", "크기", "설명"],
            [[n, f"{counts[n]:,}", _fmt_bytes(sizes[n]), d] for n, d in ordered],
            aligns=["left", "right", "right", "left"],
        )

        section("원장 메타데이터")
        cur.execute("SELECT hierarchy, count(*) AS n FROM laws GROUP BY hierarchy ORDER BY n DESC")
        hier = " · ".join(f"{r['hierarchy']} {r['n']}" for r in cur.fetchall())
        law_total = f"{counts.get('laws', 0):,}"
        bullet(f"법령 {c(law_total, BOLD)}건  ({hier})")

        # '현행' 은 repository 의 temporal predicate 와 동일하게 오늘 유효한 버전
        cur.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (
                     WHERE valid_from <= current_date
                       AND (valid_to > current_date OR valid_to IS NULL)
                   ) AS live,
                   min(valid_from) AS lo, max(valid_from) AS hi
            FROM law_articles
            """
        )
        art = cur.fetchone()
        if art["total"]:
            art_total = f"{art['total']:,}"
            bullet(
                f"조문 버전 {c(art_total, BOLD)}행 · 현행 {art['live']:,}행"
                f" · 시행일 범위 {art['lo']} → {art['hi']}"
            )

        if counts.get("law_version_sources"):
            cur.execute(
                "SELECT count(*) AS n, max(collected_at) AS latest FROM law_version_sources"
            )
            src = cur.fetchone()
            src_total = f"{src['n']:,}"
            bullet(
                f"공식 수집 영수증 {c(src_total, BOLD)}건"
                f" · 최근 수집 {src['latest']:%Y-%m-%d %H:%M %Z}"
            )
        if counts.get("hsk_node_versions"):
            hsk_total = f"{counts['hsk_node_versions']:,}"
            bullet(
                f"HSK 노드 버전 {c(hsk_total, BOLD)}행"
                f" · CCCT 요건 {counts.get('ccct_requirements', 0):,}행"
            )

        if counts.get("law_articles"):
            section("법령 원장 미리보기 — 조문 버전 상위")
            cur.execute(
                """
                SELECT l.law_name, l.hierarchy,
                       count(DISTINCT a.version_mst) AS versions,
                       count(*) AS article_rows,
                       count(*) FILTER (
                         WHERE a.valid_from <= current_date
                           AND (a.valid_to > current_date OR a.valid_to IS NULL)
                       ) AS live_rows,
                       to_char(max(a.valid_from), 'YYYY-MM-DD') AS latest
                FROM laws l JOIN law_articles a USING (law_id)
                GROUP BY l.law_id, l.law_name, l.hierarchy
                ORDER BY count(*) DESC
                LIMIT 8
                """
            )
            table(
                ["법령명", "위계", "개정 버전", "조문 버전", "현행 조문", "최근 시행일"],
                [
                    [
                        _clip(r["law_name"], 30),
                        r["hierarchy"],
                        f"{r['versions']:,}",
                        f"{r['article_rows']:,}",
                        f"{r['live_rows']:,}",
                        r["latest"],
                    ]
                    for r in cur.fetchall()
                ],
                aligns=["left", "left", "right", "right", "right", "left"],
            )


# --------------------------------------------------------------- 벡터 DB 패널


def _temporal_retrieve(query: str, k: int, as_of: date, use_rerank: bool) -> dict:
    """전 버전 temporal 컬렉션에 대한 as_of 하이브리드 Top-K.

    hybrid.retrieve 와 동일한 RRF+rerank 절차를, 시점 필터가 랭킹 전에
    적용되는 temporal 컬렉션 위에서 수행한다 (설계 §6과 동일한 순서).
    """
    from src import hybrid
    from src import repository as repo
    from src.embed import rerank

    arms = {}
    for arm in (repo.DENSE_VECTOR, repo.BM25_VECTOR):
        arms[arm] = repo.search(
            query,
            arm=arm,
            limit=hybrid.RECALL,
            as_of=as_of,
            collection=repo.TEMPORAL_COLLECTION,
            temporal=True,
        )
    fused = hybrid.rrf_fuse([[h.article_pk for h in hits] for hits in arms.values()])
    order = sorted(fused, key=fused.get, reverse=True)
    texts = {h.article_pk: h.text for hits in arms.values() for h in hits}
    dense_score = {h.article_pk: h.score for h in arms[repo.DENSE_VECTOR]}
    bm25_score = {h.article_pk: h.score for h in arms[repo.BM25_VECTOR]}

    pool = order[: max(hybrid.RERANK_POOL, k)]
    rr = (
        rerank.scores(query, [texts[pk][: hybrid.RERANK_CHARS] for pk in pool])
        if use_rerank
        else None
    )
    reranked = rr is not None
    score_of = dict(zip(pool, rr)) if reranked else {}
    if reranked:
        pool = sorted(pool, key=lambda pk: score_of[pk], reverse=True)

    final = pool[:k]
    meta = hybrid._article_meta(final)
    dense_top1 = arms[repo.DENSE_VECTOR][0].score if arms[repo.DENSE_VECTOR] else 0.0
    evidence = []
    for pk in final:
        m = meta.get(pk)
        if not m:
            continue
        label = hybrid._label(m["article_no"], m["paragraph_no"])
        evidence.append(
            {
                "evidence_id": f"law-{pk}",
                "law_name": m["law_name"],
                "hierarchy": m["hierarchy"],
                "article_label": label,
                "title": m["title"],
                "score": round(score_of[pk], 4) if reranked else None,
                "rrf_score": round(fused[pk], 6),
                "dense_score": round(dense_score[pk], 4) if pk in dense_score else None,
                "bm25_score": round(bm25_score[pk], 4) if pk in bm25_score else None,
                "valid_from": str(m["valid_from"]),
                "valid_to": str(m["valid_to"]) if m["valid_to"] else None,
                "is_current": m["is_current"],
                "version_mst": m["version_mst"],
                "text": texts[pk],
            }
        )
    return {
        "query": query,
        "used_query": query,
        "rewritten": False,
        "low_confidence": dense_top1 < hybrid.LOW_CONF,
        "reranked": reranked,
        "as_of": str(as_of),
        "evidence": evidence,
    }


def _print_hits(result: dict, elapsed: float, collection: str) -> None:
    flags = []
    if result["rewritten"]:
        flags.append(f"재질의 → '{result['used_query']}'")
    if result["low_confidence"]:
        flags.append("저신뢰")
    ranking = "bge-reranker-v2-m3 [0,1]" if result["reranked"] else "RRF 순위 (rerank 없음)"
    meta = f'질의 "{c(result["query"], BOLD)}" · 점수 {ranking}'
    if result["as_of"]:
        meta += f" · as_of {c(result['as_of'], BOLD, MAGENTA)}"
    if flags:
        meta += "  " + c("(" + ", ".join(flags) + ")", YELLOW)
    print("  " + meta)
    print()

    if not result["evidence"]:
        print("  " + c("결과 없음 — 컬렉션 적재 상태를 확인하세요", YELLOW))
        return

    top_rrf = max(e["rrf_score"] for e in result["evidence"])
    for i, e in enumerate(result["evidence"], 1):
        if e["score"] is not None:
            score_str, ratio = f"{e['score']:.4f}", e["score"]
        else:
            score_str, ratio = f"rrf {e['rrf_score']:.4f}", e["rrf_score"] / top_rrf
        head = f"{e['law_name']} {e['article_label']}"
        if e["title"]:
            head += f" — {e['title']}"
        validity = (
            f"현행 {e['valid_from']} →"
            if e["is_current"] and not e["valid_to"]
            else f"{e['valid_from']} → {e['valid_to'] or '현행'}"
        )
        print(
            f"  {c(f'{i:>2}', BOLD)}  {c(score_str, BOLD, GREEN)}  {_score_bar(ratio)}"
            f"  {c(_clip(head, 52), BOLD)}"
        )
        parts = []
        if e["dense_score"] is not None:
            parts.append(f"dense {e['dense_score']:.4f}")
        if e["bm25_score"] is not None:
            parts.append(f"bm25 {e['bm25_score']:.3f}")
        parts.append(f"rrf {e['rrf_score']:.6f}")
        parts.append(validity)
        parts.append(f"MST {e['version_mst']}")
        print("      " + c(" · ".join(parts), GREY))
        preview = " ".join(e["text"].split())
        print("      " + c("“" + _clip(preview, WIDTH - 10) + "”", DIM))
        print()

    pipeline = "dense(bge-m3 1024d) + bm25 → RRF"
    if result["reranked"]:
        pipeline += " → rerank(bge-reranker-v2-m3)"
    print("  " + c(f"⏱ {elapsed:.2f}s · 컬렉션 {collection} · 파이프라인 {pipeline}", GREY))


def _rerank_available() -> bool:
    """캐시 전용 런타임(HF_HUB_OFFLINE)에서 rerank 모델 유무를 미리 확인한다.

    미캐시 상태로 rerank 를 시도하면 매 실행마다 로드 실패 로그가 찍히므로,
    없으면 조용히 RRF 순위로 내려간다.
    """
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        return True
    hf_home = os.environ.get("HF_HOME")
    if not hf_home:
        return True
    from src.embed.model_identity import RERANK_MODEL

    marker = "models--" + RERANK_MODEL.model_id.replace("/", "--")
    return (Path(hf_home) / "hub" / marker).exists()


def cmd_vector(
    profile: str, query: str | None, k: int, as_of: date | None, no_rerank: bool
) -> None:
    from src.config import settings
    from src.db import qdrant as qd

    qc = qd.client()
    banner(
        "Qdrant 벡터 프로젝션 — rebuildable",
        f"{settings.qdrant_url} · dense 1024d cosine + bm25 sparse(IDF) · profile {profile}",
    )

    section("컬렉션")
    point_count: dict[str, int] = {}
    rows = []
    for name in (qd.COLLECTION, qd.TEMPORAL_COLLECTION):
        if not qc.collection_exists(name):
            point_count[name] = 0
            rows.append([name, "-", "미생성", "-", "-"])
            continue
        info = qc.get_collection(name)
        vectors = info.config.params.vectors or {}
        dense = vectors.get(qd.DENSE_VECTOR) if isinstance(vectors, dict) else vectors
        dense_desc = f"{dense.size}d · {dense.distance}" if dense else "-"
        sparse = info.config.params.sparse_vectors or {}
        sparse_desc = "bm25 (IDF)" if qd.BM25_VECTOR in sparse else "-"
        point_count[name] = int(qc.count(name, exact=True).count)
        rows.append(
            [
                name,
                f"{point_count[name]:,}",
                dense_desc,
                sparse_desc,
                str(len(info.payload_schema or {})),
            ]
        )
    table(
        ["컬렉션", "포인트", "dense 벡터", "sparse 벡터", "payload 인덱스"],
        rows,
        aligns=["left", "right", "left", "left", "right"],
    )

    query = query or "세관장확인대상물품"
    # 현행 컬렉션이 비어 있는 law-history 프로파일이면 temporal 로 자동 전환
    if (
        as_of is None
        and not point_count.get(qd.COLLECTION)
        and point_count.get(qd.TEMPORAL_COLLECTION)
    ):
        as_of = date.today()
        print()
        print(
            "  "
            + c(f"현행 컬렉션이 비어 있어 temporal 컬렉션(as_of {as_of})으로 검색합니다", YELLOW)
        )

    use_rerank = not no_rerank
    if use_rerank and not _rerank_available():
        use_rerank = False
        print("  " + c("rerank 모델 미캐시 — RRF 순위로 출력합니다", GREY))

    section(f"Top-{k} 하이브리드 검색")
    print("  " + c("모델 로딩·질의 인코딩 중…", GREY), flush=True)
    started = time.perf_counter()
    if as_of is None:
        from src import hybrid

        result = hybrid.retrieve(query, limit=k, use_rerank=use_rerank)
        collection = qd.COLLECTION
    else:
        result = _temporal_retrieve(query, k, as_of, use_rerank=use_rerank)
        collection = qd.TEMPORAL_COLLECTION
    _print_hits(result, time.perf_counter() - started, collection)


# ------------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--profile",
        default=FUNCTIONAL_UAT_LAW_HISTORY_PROFILE,
        help="적용할 _local 프로파일 (기본: functional-uat-law-history)",
    )
    parser.add_argument("--no-color", action="store_true", help="ANSI 색 없이 출력")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rdb", help="PostgreSQL 주요 테이블·메타데이터 스냅샷")

    vector = sub.add_parser("vector", help="Qdrant 컬렉션 + Top-K 하이브리드 검색")
    vector.add_argument("query", nargs="?", help="검색 질의 (기본: 세관장확인대상물품)")
    vector.add_argument("--k", type=int, default=5, help="Top-K (기본 5)")
    vector.add_argument("--as-of", dest="as_of", help="시점 검색 YYYY-MM-DD (temporal 컬렉션)")
    vector.add_argument("--no-rerank", action="store_true", help="rerank 생략 (RRF 순위)")

    both = sub.add_parser("all", help="rdb + vector 를 한 번에 (풀샷)")
    both.add_argument("query", nargs="?", help="검색 질의")
    both.add_argument("--k", type=int, default=5)
    both.add_argument("--as-of", dest="as_of")
    both.add_argument("--no-rerank", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    global USE_COLOR
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    USE_COLOR = not args.no_color and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    if USE_COLOR:
        _enable_ansi()

    try:
        apply_data_store_environment(args.profile)
    except LocalProfileError:
        print(f"BLOCKED: '{args.profile}' 프로파일이 초기화되어 있지 않습니다", file=sys.stderr)
        return 2

    as_of = None
    if getattr(args, "as_of", None):
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            print("BLOCKED: --as-of 는 YYYY-MM-DD 형식이어야 합니다", file=sys.stderr)
            return 2

    print(c(f"customs-data-platform · {datetime.now():%Y-%m-%d %H:%M}", GREY))
    try:
        if args.command in ("rdb", "all"):
            cmd_rdb(args.profile)
        if args.command in ("vector", "all"):
            if args.command == "all":
                print()
            cmd_vector(args.profile, args.query, args.k, as_of, args.no_rerank)
    except Exception as error:  # 크레덴셜이 섞인 드라이버 예외를 그대로 노출하지 않는다
        print(
            f"BLOCKED: 스토어 접근 실패 ({type(error).__name__}) — "
            "docker compose 상태와 프로파일 포트를 확인하세요",
            file=sys.stderr,
        )
        return 2
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
