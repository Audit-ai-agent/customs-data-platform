"""Phase 1 — HSK 마스터 적재 (설계 §4.3 / 로드맵 Phase 1).

소스 (data/, 연 단위 개정 → 연 1회 재적재):
- 관세청_HS부호 단위별 품목명_YYYYMMDD.xlsx — 5개 시트, [코드, 한글품목명, 영문품목명].
  시트명과 무관하게 5/7/9자리가 섞여 있으므로 레벨은 len(code)로 판정.
- 관세청_HS부호_YYYYMMDD.xlsx — leaf 속성. len==10 만 leaf(불변식),
  7/8/9자리 행은 계층 노드로 이미 존재하므로 속성 병합에서 제외.

그래프 모델:
- 전 코드      → (:HSNode {code, level, name_ko, name_en})
- leaf(10자리) → 추가 라벨 :HSK + {hsk10, hs6, heading4, chapter2,
                 valid_from, valid_to, quantity_unit, weight_unit,
                 category_code, category_name}
  상위 자리 요건 상속(설계 §4.3)은 CHILD_OF 엣지 없이 hs6/heading4/chapter2
  속성 매칭으로 처리하므로 leaf 에 접두어를 박아 둔다.

멱등: HSNode.code 유니크 제약 + MERGE. 재적재 시 전년 대비
diff(신설/삭제/명칭변경)를 적재 전에 산출해 보고한다.

실행: law_repository/ 에서  python -m src.ingest.hsk
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from neo4j import Driver

from src.db.neo4j import driver as make_driver

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
LEVEL_NAMES_XLSX = DATA_DIR / "관세청_HS부호 단위별 품목명_20260101.xlsx"
LEAF_ATTRS_XLSX = DATA_DIR / "관세청_HS부호_20260101.xlsx"

BATCH_SIZE = 1000


# ---------------------------------------------------------------- 파일 로딩


def load_level_names(path: Path = LEVEL_NAMES_XLSX) -> pd.DataFrame:
    """5개 시트 → [code, level, name_ko, name_en]. 코드는 문자열(앞자리 0 보존)."""
    xl = pd.ExcelFile(path)
    frames = []
    for sheet in xl.sheet_names:
        df = xl.parse(sheet, dtype=str)
        if len(df.columns) != 3:
            raise ValueError(f"{path} [{sheet}]: 3컬럼 기대, {list(df.columns)}")
        df.columns = ["code", "name_ko", "name_en"]
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out["code"] = out["code"].str.strip()
    out["level"] = out["code"].str.len()
    if out["code"].duplicated().any():
        dups = out[out["code"].duplicated(keep=False)]["code"].tolist()
        raise ValueError(f"레벨 파일에 중복 코드: {dups[:10]}")
    bad = set(out["level"].unique()) - {2, 4, 5, 6, 7, 8, 9, 10}
    if bad:
        raise ValueError(f"예상 밖 코드 길이: {bad}")
    return out


def load_leaf_attrs(path: Path = LEAF_ATTRS_XLSX) -> pd.DataFrame:
    """leaf(10자리) 속성 프레임. index=code.

    파일에 7/8/9자리 행이 섞여 있으나 HSK leaf = 10자리만(불변식) — 나머지는 버림.
    날짜는 ISO 문자열(YYYY-MM-DD)로 저장.
    """
    df = pd.read_excel(path, dtype=str)
    df["HS부호"] = df["HS부호"].str.strip()
    leaf = df[df["HS부호"].str.len() == 10].copy()
    if leaf["HS부호"].duplicated().any():
        raise ValueError("leaf 파일에 10자리 중복 코드 존재 — 현행 필터 필요")
    for col in ("적용시작일자", "적용종료일자"):
        leaf[col] = pd.to_datetime(leaf[col]).dt.strftime("%Y-%m-%d")
    out = pd.DataFrame(
        {
            "valid_from": leaf["적용시작일자"],
            "valid_to": leaf["적용종료일자"],
            "quantity_unit": leaf["수량단위코드"],
            "weight_unit": leaf["중량단위코드"],
            "category_code": leaf["성질통합분류코드"],
            "category_name": leaf["성질통합분류코드명"],
        }
    )
    out.index = leaf["HS부호"]
    return out


def build_nodes(levels: pd.DataFrame, leaf_attrs: pd.DataFrame) -> list[dict]:
    """레벨 명칭 + leaf 속성(prefix 조인)을 UNWIND 용 dict 리스트로."""
    attrs = leaf_attrs.to_dict("index")
    nodes: list[dict] = []
    missing_attrs = 0
    for row in levels.itertuples(index=False):
        node = {
            "code": row.code,
            "level": int(row.level),
            "name_ko": row.name_ko,
            "name_en": row.name_en,
            "is_leaf": row.level == 10,
        }
        if row.level == 10:
            node.update(
                {
                    "hsk10": row.code,
                    "hs6": row.code[:6],
                    "heading4": row.code[:4],
                    "chapter2": row.code[:2],
                }
            )
            extra = attrs.get(row.code)
            if extra is None:
                missing_attrs += 1
            else:
                node.update({k: (None if pd.isna(v) else v) for k, v in extra.items()})
        nodes.append(node)
    if missing_attrs:
        print(f"[warn] leaf 속성 없는 10자리 코드 {missing_attrs}건 (명칭만 적재)")
    return nodes


# ---------------------------------------------------------------- diff 골격


@dataclass
class HskDiff:
    """전년 대비 변경 감지 골격 — 연 1회 재적재 시 신설/삭제/명칭변경 보고."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str, str]] = field(default_factory=list)  # (code, old, new)
    is_initial_load: bool = False  # 적재 전 DB 가 비어 있었는가

    def summary(self) -> str:
        return f"신설 {len(self.added)} · 삭제 {len(self.removed)} · 명칭변경 {len(self.renamed)}"


def diff_with_db(drv: Driver, nodes: list[dict]) -> HskDiff:
    """DB 의 기존 HSNode 와 이번 파일을 비교. 최초 적재면 전부 added."""
    with drv.session() as s:
        existing = {
            r["code"]: r["name_ko"]
            for r in s.run("MATCH (n:HSNode) RETURN n.code AS code, n.name_ko AS name_ko")
        }
    new = {n["code"]: n["name_ko"] for n in nodes}
    diff = HskDiff(is_initial_load=not existing)
    diff.added = sorted(set(new) - set(existing))
    diff.removed = sorted(set(existing) - set(new))
    diff.renamed = [
        (c, existing[c], new[c]) for c in sorted(set(new) & set(existing)) if existing[c] != new[c]
    ]
    # TODO(Phase 3 연계): removed 는 삭제 대신 tombstone(valid_to 마감) 처리 검토.
    return diff


# ---------------------------------------------------------------- 적재

_CONSTRAINT = "CREATE CONSTRAINT hsnode_code IF NOT EXISTS FOR (n:HSNode) REQUIRE n.code IS UNIQUE"

# 재적재 시 속성 잔존을 막기 위해 SET n = row 로 전체 치환(코드 키는 MERGE 가 보존).
# leaf 의 :HSK 라벨(요건 그래프 §4.3 조회용)은 apoc 없이 별도 쿼리로 부여.
_UPSERT_PLAIN = """
UNWIND $rows AS row
MERGE (n:HSNode {code: row.code})
SET n = row
"""

_LABEL_LEAVES = """
MATCH (n:HSNode) WHERE n.is_leaf
SET n:HSK
"""


def upsert_nodes(drv: Driver, nodes: list[dict]) -> None:
    with drv.session() as s:
        s.run(_CONSTRAINT)
        for i in range(0, len(nodes), BATCH_SIZE):
            s.run(_UPSERT_PLAIN, rows=nodes[i : i + BATCH_SIZE])
        s.run(_LABEL_LEAVES)


def verify(drv: Driver) -> None:
    """적재 후 검증 리포트 — 레벨별 행수·leaf 라벨·샘플 traverse."""
    with drv.session() as s:
        print("\n[레벨별 노드 수]")
        for r in s.run("MATCH (n:HSNode) RETURN n.level AS level, count(*) AS cnt ORDER BY level"):
            print(f"  level {r['level']:>2}: {r['cnt']:>6}")
        hsk = s.run("MATCH (n:HSK) RETURN count(*) AS c").single()["c"]
        leaf10 = s.run("MATCH (n:HSNode {level: 10}) RETURN count(*) AS c").single()["c"]
        print(
            f"[불변식] :HSK 라벨 = {hsk} / level 10 = {leaf10} → {'OK' if hsk == leaf10 else 'FAIL'}"
        )
        sample = s.run(
            """
            MATCH (n:HSK) WHERE n.quantity_unit IS NOT NULL
            WITH n LIMIT 1
            MATCH (c2:HSNode {code: n.chapter2}), (h4:HSNode {code: n.heading4})
            RETURN n.hsk10 AS hsk10, n.name_ko AS name,
                   c2.name_ko AS chapter, h4.name_ko AS heading,
                   n.quantity_unit AS unit, n.category_name AS category
            """
        ).single()
        print("[샘플 traverse]", dict(sample) if sample else "없음")


def main(nodes: list[dict] | None = None) -> int:
    """Load HSK nodes, or project one already-attested in-memory snapshot.

    The optional value lets the law-history builder validate exact tracked
    bytes and then project the same parsed nodes without a second file read.
    Existing CLI/no-argument callers preserve their previous behaviour.
    """

    if nodes is None:
        levels = load_level_names()
        leaf_attrs = load_leaf_attrs()
        nodes = build_nodes(levels, leaf_attrs)
    print(f"파일 로드: 코드 {len(nodes)}건 (leaf {sum(n['is_leaf'] for n in nodes)}건)")

    drv = make_driver()
    try:
        diff = diff_with_db(drv, nodes)
        print(f"diff: {diff.summary()}" + (" (최초 적재)" if diff.is_initial_load else ""))
        if diff.renamed:
            for code, old, new in diff.renamed[:10]:
                print(f"  명칭변경 {code}: {old!r} → {new!r}")
        if diff.removed:
            print(f"  삭제 후보(미처리, tombstone 은 Phase 3): {diff.removed[:10]} ...")
        upsert_nodes(drv, nodes)
        verify(drv)
    finally:
        drv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
