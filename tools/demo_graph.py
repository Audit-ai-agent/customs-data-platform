"""보고서용 조문 위임/인용 그래프 그림 — SVG 생성 + (선택) PNG 캡처.

그림 1  법령 위계 위임 구조 (매크로): 시행령·시행규칙을 가진 법령 패밀리를
        법률 → 시행령 → 시행규칙 3층으로 놓고, DELEGATES/REFERS_TO 를 법령 단위로
        집계해 엣지 두께·라벨로 표시한다.
그림 2  한 조문의 ego 그래프 (마이크로): 기본 관세법 제226조(세관장확인).
        위임(DELEGATES)·인용(CITES)·피인용·타법 인용(REFERS_TO)을 같은 3층 위에
        그리고, 위임받은 시행령 조문이 다시 시행규칙으로 내리는 2-hop 도 포함한다.

엣지는 Neo4j 를 읽지 않고 PostgreSQL 조문(오늘 유효한 whole-article 행)에
``src.ingest.graph.extract_refs`` 를 그대로 적용해 만든다 — 적재 코드와 동일한
분류(CITES / DELEGATES 상위→하위 / REFERS_TO)를 쓰므로 그림과 그래프 적재
로직이 어긋나지 않는다. 어떤 스토어에도 쓰기를 하지 않는다.

실행 (저장소 루트):
  python tools/demo_graph.py                       # 그림 1·2 SVG + PNG
  python tools/demo_graph.py --no-png              # SVG 만
  python tools/demo_graph.py --law-id 001556 --article 22600   # 다른 앵커 조문
  python tools/demo_graph.py --out C:\\path\\to\\dir
"""

from __future__ import annotations

import argparse
import html
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
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
    artifact_root,
)

# ------------------------------------------------------------------ 스타일

FONT = "'Malgun Gothic', 'Apple SD Gothic Neo', 'Noto Sans KR', sans-serif"
HIER_ORDER = ("법률", "시행령", "시행규칙")
HIER_FILL = {"법률": "#1f4e79", "시행령": "#2e75b6", "시행규칙": "#9dc3e6"}
HIER_TEXT = {"법률": "#ffffff", "시행령": "#ffffff", "시행규칙": "#1f2d3d"}
OTHER_FILL, OTHER_TEXT = "#f2f2f2", "#333333"  # 타 법령·기타 개별법
EDGE_STYLE = {
    "DELEGATES": {"stroke": "#1f4e79", "dash": "", "label": "위임 DELEGATES"},
    "CITES": {"stroke": "#8c8c8c", "dash": "6 4", "label": "동일 법령 인용 CITES"},
    "REFERS_TO": {"stroke": "#e07b00", "dash": "", "label": "타 법령 인용 REFERS_TO"},
}
LANE_FILL = {"법률": "#f7f9fc", "시행령": "#fbfcfe", "시행규칙": "#f7f9fc"}

# 긴 법령명 줄임 (접두 치환이라 '… 시행령' 꼬리는 유지)
SHORT_NAMES = {
    "자유무역협정의 이행을 위한 관세법의 특례에 관한 법률": "FTA관세특례법",
    "수출용 원재료에 대한 관세 등 환급에 관한 특례법": "환급특례법",
}


def short_name(name: str) -> str:
    for long, short in SHORT_NAMES.items():
        if name.startswith(long):
            return short + name[len(long) :]
    return name


def text_w(text: str, size: float) -> float:
    return sum(1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.58 for ch in text) * size


def clip(text: str, width: int) -> str:
    if text_w(text, 1) <= width:
        return text
    out, used = [], 0.0
    for ch in text:
        cw = 1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.58
        if used + cw > width - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def label_of(article_no: int) -> str:
    lab = f"제{article_no // 100}조"
    if article_no % 100:
        lab += f"의{article_no % 100}"
    return lab


# ------------------------------------------------------------------ 데이터


@dataclass
class Article:
    pk: int
    law_id: str
    law_name: str
    hierarchy: str
    article_no: int
    title: str | None


@dataclass
class Corpus:
    articles: dict[int, Article]
    edges: list[tuple[str, int, int, str]]  # (type, s_pk, t_pk, basis)
    hier_of_law: dict[str, str]
    base_of_law: dict[str, str]
    law_names: dict[str, str]
    counts: Counter = field(default_factory=Counter)


def load_corpus() -> Corpus:
    from src.db.postgres import connect
    from src.ingest.graph import HIER_RANK, LawRegistry, extract_refs

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT law_id, law_name, hierarchy FROM laws")
        laws = {lid: (n, h) for lid, n, h in cur.fetchall()}
        reg = LawRegistry(laws)
        # ingest.graph 의 is_current 대신 repository temporal predicate 와 같은
        # '오늘 유효' 기준 — law-history 프로파일은 is_current 를 채우지 않는다.
        cur.execute(
            """
            SELECT a.id, a.law_id, l.law_name, l.hierarchy, a.article_no, a.title, a.content
            FROM law_articles a JOIN laws l USING (law_id)
            WHERE a.paragraph_no IS NULL
              AND a.valid_from <= current_date
              AND (a.valid_to > current_date OR a.valid_to IS NULL)
            """
        )
        rows = cur.fetchall()

    articles = {r[0]: Article(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows}
    pk_by_key = {(r[1], r[4]): r[0] for r in rows}
    edges: dict[tuple[str, int, int], str] = {}
    for pk, law_id, _, _, art_no, _, content in rows:
        for (t_law, t_art), basis in extract_refs(content, law_id, art_no, reg).items():
            t_pk = pk_by_key.get((t_law, t_art))
            if t_pk is None:
                continue
            if t_law == law_id:
                key = ("CITES", pk, t_pk)
            elif reg.base_of[law_id] == reg.base_of[t_law]:
                upper_first = HIER_RANK[reg.hier_of[law_id]] < HIER_RANK[reg.hier_of[t_law]]
                key = ("DELEGATES", pk, t_pk) if upper_first else ("DELEGATES", t_pk, pk)
            else:
                key = ("REFERS_TO", pk, t_pk)
            edges.setdefault(key, basis)
    corpus = Corpus(
        articles=articles,
        edges=[(k[0], k[1], k[2], b) for k, b in edges.items()],
        hier_of_law=reg.hier_of,
        base_of_law=reg.base_of,
        law_names=reg.name_of,
    )
    corpus.counts = Counter(e[0] for e in corpus.edges)
    return corpus


# ------------------------------------------------------------------ SVG 조립


class Svg:
    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.parts: list[str] = []

    def rect(self, x, y, w, h, fill, stroke="none", rx=0, opacity=1.0, sw=1):
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" opacity="{opacity}"/>'
        )

    def text(self, x, y, s, size=13, fill="#222", anchor="middle", weight="normal", opacity=1.0):
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}" opacity="{opacity}">{html.escape(s)}</text>'
        )

    def path(self, d, stroke, sw=1.5, dash="", marker=True, opacity=0.9):
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        mk = f' marker-end="url(#arrow-{stroke.lstrip("#")})"' if marker else ""
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{sw:.1f}" '
            f'stroke-linecap="round" opacity="{opacity}"{extra}{mk}/>'
        )

    def edge_label(self, x, y, s, color, size=11):
        w = text_w(s, size) + 8
        self.rect(x - w / 2, y - size / 2 - 3, w, size + 6, "#ffffff", rx=3, opacity=0.92)
        self.text(x, y + size / 2 - 2, s, size=size, fill=color, weight="bold")

    def render(self) -> str:
        markers = "".join(
            f'<marker id="arrow-{st["stroke"].lstrip("#")}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{st["stroke"]}"/></marker>'
            for st in EDGE_STYLE.values()
        )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}" font-family="{FONT}">'
            f"<defs>{markers}</defs>"
            f'<rect width="100%" height="100%" fill="#ffffff"/>' + "".join(self.parts) + "</svg>"
        )


def draw_lanes(svg: Svg, lane_y: dict[str, float], lane_h: float, x0: float, x1: float) -> None:
    for hier in HIER_ORDER:
        y = lane_y[hier] - lane_h / 2
        svg.rect(x0, y, x1 - x0, lane_h, LANE_FILL[hier], rx=10)
        svg.text(x0 + 14, y + 22, hier, size=14, fill="#6b7a90", anchor="start", weight="bold")


def draw_legend(svg: Svg, x: float, y: float, types: list[str]) -> None:
    for i, t in enumerate(types):
        st = EDGE_STYLE[t]
        yy = y + i * 22
        svg.path(f"M{x},{yy} L{x + 46},{yy}", st["stroke"], sw=2.5, dash=st["dash"])
        svg.text(x + 56, yy + 4, st["label"], size=12, fill="#333", anchor="start")


def vertical_edge(svg, sx, sy, tx, ty, color, sw, dash="", bow=0.0):
    """위→아래 엣지. bow 가 0이면 직선, 아니면 옆으로 휘는 베지어."""
    if bow:
        d = f"M{sx},{sy} C{sx + bow},{sy + (ty - sy) * 0.35} {tx + bow},{ty - (ty - sy) * 0.35} {tx},{ty}"
        mid = ((sx + tx) / 2 + bow * 0.75, (sy + ty) / 2)
    else:
        d = f"M{sx},{sy} L{tx},{ty}"
        mid = ((sx + tx) / 2, (sy + ty) / 2)
    svg.path(d, color, sw=sw, dash=dash)
    return mid


# ------------------------------------------------------------------ 그림 1


def figure_hierarchy(corpus: Corpus, min_refers: int) -> Svg:
    families: dict[str, dict[str, str]] = defaultdict(dict)
    for lid, base in corpus.base_of_law.items():
        families[base][corpus.hier_of_law[lid]] = lid
    multi = {b: m for b, m in families.items() if len(m) > 1}

    art_count = Counter(a.law_id for a in corpus.articles.values())
    deleg = Counter()
    refers = Counter()
    for t, s, d, _ in corpus.edges:
        sl, tl = corpus.articles[s].law_id, corpus.articles[d].law_id
        if t == "DELEGATES":
            deleg[(sl, tl)] += 1
        elif t == "REFERS_TO":
            refers[(sl, tl)] += 1

    family_laws = {lid for m in multi.values() for lid in m.values()}
    # 패밀리 밖 개별법(법률만 있는 법령)의 패밀리 법령 인용은 한 노드로 묶는다
    outside = {lid for lid in corpus.law_names if lid not in family_laws}
    outside_refers = Counter()
    for (sl, tl), n in refers.items():
        if sl in outside and tl in family_laws:
            outside_refers[tl] += n

    order = sorted(
        multi, key=lambda b: -sum(n for (s, t), n in deleg.items() if s in multi[b].values())
    )
    node_w, node_h, col_gap = 236, 64, 96
    margin_x, top, lane_h, lane_gap = 60, 96, 150, 12
    cols = len(order) + (1 if outside_refers else 0)
    width = margin_x * 2 + cols * node_w + (cols - 1) * col_gap
    height = top + 3 * lane_h + 2 * lane_gap + 150
    svg = Svg(width, height)
    lane_y = {h: top + lane_h / 2 + i * (lane_h + lane_gap) for i, h in enumerate(HIER_ORDER)}
    draw_lanes(svg, lane_y, lane_h, margin_x - 24, width - margin_x + 24)

    pos: dict[str, tuple[float, float]] = {}
    for ci, base in enumerate(order):
        cx = margin_x + node_w / 2 + ci * (node_w + col_gap)
        for hier, lid in multi[base].items():
            pos[lid] = (cx, lane_y[hier])
    other_pos = None
    if outside_refers:
        other_pos = (margin_x + node_w / 2 + len(order) * (node_w + col_gap), lane_y["법률"])

    max_d = max(deleg.values()) if deleg else 1
    # DELEGATES (패밀리 내부, 세로)
    for (sl, tl), n in sorted(deleg.items(), key=lambda kv: -kv[1]):
        if sl not in pos or tl not in pos:
            continue
        (sx, sy), (tx, ty) = pos[sl], pos[tl]
        skip = corpus.hier_of_law[sl] == "법률" and corpus.hier_of_law[tl] == "시행규칙"
        sw = 1.5 + 7 * n / max_d
        mid = vertical_edge(
            svg,
            sx + (60 if skip else 0),
            sy + node_h / 2,
            tx + (60 if skip else 0),
            ty - node_h / 2,
            EDGE_STYLE["DELEGATES"]["stroke"],
            sw,
            bow=110 if skip else 0,
        )
        svg.edge_label(mid[0], mid[1], f"{n}", EDGE_STYLE["DELEGATES"]["stroke"])

    # REFERS_TO (패밀리 간, 가로 곡선). 같은 노드로 모이는 화살표는 도착 y 를
    # 분산시켜 화살촉과 라벨이 겹치지 않게 한다.
    visible = [
        ((sl, tl), n)
        for (sl, tl), n in sorted(refers.items(), key=lambda kv: -kv[1])
        if sl in pos and tl in pos and n >= min_refers
    ]
    dropped = sum(n for (sl, tl), n in refers.items() if sl in pos and tl in pos and n < min_refers)
    arrivals = Counter(tl for (_, tl), _ in visible)
    arrival_slot: Counter = Counter()
    col_step = node_w + col_gap
    for (sl, tl), n in visible:
        (sx, sy), (tx, ty) = pos[sl], pos[tl]
        direction = 1 if tx > sx else -1
        slots = arrivals[tl]
        offset = (
            (arrival_slot[tl] - (slots - 1) / 2) * min(14, 40 / max(1, slots - 1))
            if slots > 1
            else 0
        )
        arrival_slot[tl] += 1
        x_a, x_b = sx + direction * node_w / 2, tx - direction * node_w / 2
        cols_apart = max(1, round(abs(tx - sx) / col_step))
        lift = -(28 + 60 / cols_apart) if sy == ty else 0
        cx1, cx2 = x_a + direction * 70, x_b - direction * 70
        d = f"M{x_a},{sy} C{cx1},{sy + lift} {cx2},{ty + offset + lift} {x_b},{ty + offset}"
        svg.path(d, EDGE_STYLE["REFERS_TO"]["stroke"], sw=1.2 + 4 * n / max(refers.values()))
        label_x = x_a + (x_b - x_a) * (0.5 if cols_apart > 1 else 0.42)
        svg.edge_label(
            label_x, (sy + ty) / 2 + lift * 0.75, f"{n}", EDGE_STYLE["REFERS_TO"]["stroke"]
        )

    if other_pos:
        ox, oy = other_pos
        for tl, n in outside_refers.most_common():
            tx, ty = pos[tl]
            d = f"M{ox - node_w / 2},{oy - 12} C{ox - 90},{oy - 60} {tx + 90},{ty - 60} {tx + node_w / 2},{ty - 12}"
            svg.path(d, EDGE_STYLE["REFERS_TO"]["stroke"], sw=1.2 + 4 * n / max(refers.values()))
            svg.edge_label((ox + tx) / 2, oy - 58, f"{n}", EDGE_STYLE["REFERS_TO"]["stroke"])

    # 노드
    for lid, (x, y) in pos.items():
        hier = corpus.hier_of_law[lid]
        svg.rect(x - node_w / 2, y - node_h / 2, node_w, node_h, HIER_FILL[hier], rx=10)
        svg.text(
            x,
            y - 4,
            clip(short_name(corpus.law_names[lid]), 18),
            size=15,
            fill=HIER_TEXT[hier],
            weight="bold",
        )
        svg.text(
            x, y + 18, f"조문 {art_count[lid]:,}건", size=12, fill=HIER_TEXT[hier], opacity=0.85
        )
    if other_pos:
        ox, oy = other_pos
        svg.rect(
            ox - node_w / 2,
            oy - node_h / 2,
            node_w,
            node_h,
            OTHER_FILL,
            stroke="#bbbbbb",
            rx=10,
            sw=1.2,
        )
        svg.text(
            ox, oy - 4, f"기타 개별법 {len(outside)}개", size=15, fill=OTHER_TEXT, weight="bold"
        )
        svg.text(
            ox,
            oy + 18,
            f"조문 {sum(art_count[l] for l in outside):,}건",
            size=12,
            fill=OTHER_TEXT,
            opacity=0.8,
        )

    # 제목·범례·각주
    svg.text(
        margin_x - 24,
        40,
        "법령 위계 위임 구조 — 법률 → 시행령 → 시행규칙",
        size=22,
        fill="#1f2d3d",
        anchor="start",
        weight="bold",
    )
    svg.text(
        margin_x - 24,
        66,
        f"오늘 유효 조문 {len(corpus.articles):,}건 기준 · DELEGATES {corpus.counts['DELEGATES']:,} · "
        f"REFERS_TO {corpus.counts['REFERS_TO']:,} · CITES {corpus.counts['CITES']:,} (동일 법령 내, 미표시) · "
        f"숫자 = 조문 간 엣지 수",
        size=12,
        fill="#6b7a90",
        anchor="start",
    )
    draw_legend(svg, margin_x - 24, height - 92, ["DELEGATES", "REFERS_TO"])
    notes = [f"{s} = {l}" for l, s in SHORT_NAMES.items()]
    if dropped:
        notes.append(f"패밀리 간 REFERS_TO {min_refers}건 미만 {dropped}건 미표시")
    svg.text(margin_x - 24, height - 24, " · ".join(notes), size=11, fill="#8a95a5", anchor="start")
    return svg


# ------------------------------------------------------------------ 그림 2


def figure_ego(corpus: Corpus, law_id: str, article_no: int, hops: int) -> Svg:
    anchor = next(
        (a for a in corpus.articles.values() if a.law_id == law_id and a.article_no == article_no),
        None,
    )
    if anchor is None:
        raise SystemExit(f"BLOCKED: 앵커 조문 {law_id}/{article_no} 이 오늘 유효 조문에 없습니다")

    shown: list[tuple[str, int, int, str, int]] = []  # + hop
    nodes = {anchor.pk}
    for t, s, d, b in corpus.edges:
        if s == anchor.pk or d == anchor.pk:
            shown.append((t, s, d, b, 1))
            nodes.update((s, d))
    if hops >= 2:
        frontier = {d for t, s, d, _, _ in shown if s == anchor.pk and t == "DELEGATES"}
        for t, s, d, b in corpus.edges:
            if t == "DELEGATES" and s in frontier and d not in nodes:
                shown.append((t, s, d, b, 2))
                nodes.add(d)

    arts = {pk: corpus.articles[pk] for pk in nodes}
    out_cites = [d for t, s, d, _, _ in shown if s == anchor.pk and t == "CITES"]
    in_cites = [s for t, s, d, _, _ in shown if d == anchor.pk and t == "CITES"]
    in_refers = [s for t, s, d, _, _ in shown if d == anchor.pk and t == "REFERS_TO"]
    out_refers = [d for t, s, d, _, _ in shown if s == anchor.pk and t == "REFERS_TO"]
    same_family = lambda pk: corpus.base_of_law[arts[pk].law_id] == corpus.base_of_law[law_id]  # noqa: E731

    lanes: dict[str, list[int]] = {h: [] for h in HIER_ORDER}
    law_lane = sorted(in_cites, key=lambda pk: arts[pk].article_no) + [anchor.pk]
    law_lane += sorted(out_cites, key=lambda pk: arts[pk].article_no)
    law_lane += sorted(
        in_refers + out_refers, key=lambda pk: (arts[pk].law_name, arts[pk].article_no)
    )
    placed = set(law_lane)
    lanes["법률"] = law_lane
    for pk in sorted(nodes - placed, key=lambda pk: (arts[pk].law_name, arts[pk].article_no)):
        lanes[arts[pk].hierarchy].append(pk)

    node_w, node_h, gap = 168, 72, 26
    margin_x, top, lane_h, lane_gap = 60, 100, 168, 12
    widest = max(len(v) for v in lanes.values())
    width = max(1100, margin_x * 2 + widest * node_w + (widest - 1) * gap)
    height = top + 3 * lane_h + 2 * lane_gap + 150
    svg = Svg(width, height)
    lane_y = {h: top + lane_h / 2 + i * (lane_h + lane_gap) for i, h in enumerate(HIER_ORDER)}
    draw_lanes(svg, lane_y, lane_h, margin_x - 24, width - margin_x + 24)

    pos: dict[int, tuple[float, float]] = {}
    for hier, pks in lanes.items():
        if not pks:
            continue
        total = len(pks) * node_w + (len(pks) - 1) * gap
        x0 = (width - total) / 2 + node_w / 2
        for i, pk in enumerate(pks):
            pos[pk] = (x0 + i * (node_w + gap), lane_y[hier])

    for t, s, d, basis, hop in sorted(shown, key=lambda e: (e[4], e[0])):
        (sx, sy), (tx, ty) = pos[s], pos[d]
        st = EDGE_STYLE[t]
        sw, opacity = (2.4, 0.95) if hop == 1 else (1.6, 0.6)
        if sy == ty:  # 같은 층: 위로 볼록한 곡선
            direction = 1 if tx > sx else -1
            x_a, x_b = sx + direction * node_w / 2, tx - direction * node_w / 2
            lift = -(48 + 14 * abs(tx - sx) / (node_w + gap))
            d_path = f"M{x_a},{sy - 14} C{x_a + direction * 40},{sy + lift} {x_b - direction * 40},{ty + lift} {x_b},{ty - 14}"
            mid = ((x_a + x_b) / 2, sy + lift * 0.72 - 14)
            svg.path(d_path, st["stroke"], sw=sw, dash=st["dash"], opacity=opacity)
        else:
            if sy < ty:
                d_path = (
                    f"M{sx},{sy + node_h / 2} C{sx},{sy + 70} {tx},{ty - 70} {tx},{ty - node_h / 2}"
                )
            else:
                d_path = (
                    f"M{sx},{sy - node_h / 2} C{sx},{sy - 70} {tx},{ty + 70} {tx},{ty + node_h / 2}"
                )
            mid = ((sx + tx) / 2, (sy + ty) / 2)
            svg.path(d_path, st["stroke"], sw=sw, dash=st["dash"], opacity=opacity)
        svg.edge_label(mid[0], mid[1], clip(basis, 14), st["stroke"], size=10)

    for pk, (x, y) in pos.items():
        a = arts[pk]
        is_anchor = pk == anchor.pk
        fill = HIER_FILL[a.hierarchy] if same_family(pk) else OTHER_FILL
        text = HIER_TEXT[a.hierarchy] if same_family(pk) else OTHER_TEXT
        svg.rect(
            x - node_w / 2,
            y - node_h / 2,
            node_w,
            node_h,
            fill,
            stroke="#e07b00" if is_anchor else ("#bbbbbb" if not same_family(pk) else "none"),
            rx=10,
            sw=3 if is_anchor else 1.2,
        )
        svg.text(x, y - 12, clip(short_name(a.law_name), 13), size=11, fill=text, opacity=0.85)
        svg.text(x, y + 7, label_of(a.article_no), size=15, fill=text, weight="bold")
        svg.text(x, y + 25, clip(a.title or "", 13), size=11, fill=text, opacity=0.9)

    summary = Counter()
    for t, s, d, _, hop in shown:
        if hop == 1:
            summary[
                "위임"
                if t == "DELEGATES" and s == anchor.pk
                else "인용"
                if t == "CITES" and s == anchor.pk
                else "피인용"
                if t == "CITES"
                else "타법 인용"
                if s == anchor.pk
                else "타법 피인용"
            ] += 1
    two_hop = sum(1 for e in shown if e[4] == 2)
    svg.text(
        margin_x - 24,
        40,
        f"{short_name(anchor.law_name)} {label_of(anchor.article_no)}({anchor.title}) — 위임·인용 ego 그래프",
        size=22,
        fill="#1f2d3d",
        anchor="start",
        weight="bold",
    )
    order = ("위임", "인용", "피인용", "타법 인용", "타법 피인용")
    parts = [f"{k} {summary[k]}" for k in order if summary[k]]
    if two_hop:
        parts.append(f"2-hop 위임(시행령→시행규칙) {two_hop}")
    svg.text(
        margin_x - 24,
        66,
        " · ".join(parts) + " · 엣지 라벨 = 본문 근거 문구 · 위임 방향은 항상 상위→하위",
        size=12,
        fill="#6b7a90",
        anchor="start",
    )
    draw_legend(svg, margin_x - 24, height - 92, list(EDGE_STYLE))
    svg.text(
        margin_x - 24,
        height - 24,
        "회색 노드 = 타 법령 · 주황 테두리 = 앵커 조문 · 흐린 엣지 = 2-hop",
        size=11,
        fill="#8a95a5",
        anchor="start",
    )
    return svg


# ------------------------------------------------------------------ PNG


def find_browser() -> str | None:
    candidates = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    return next((p for p in candidates if p and Path(p).exists()), None)


def svg_to_png(
    browser: str, svg_path: Path, png_path: Path, width: int, height: int, scale: int
) -> bool:
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        f"--force-device-scale-factor={scale}",
        f"--window-size={width},{height}",
        f"--screenshot={png_path}",
        svg_path.resolve().as_uri(),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and png_path.exists()


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--profile", default=FUNCTIONAL_UAT_LAW_HISTORY_PROFILE)
    parser.add_argument("--out", type=Path, help="출력 디렉터리 (기본: _artifacts/demo-graph)")
    parser.add_argument("--law-id", default="001556", help="그림 2 앵커 법령ID (기본 관세법)")
    parser.add_argument(
        "--article", type=int, default=22600, help="그림 2 앵커 조문 코드 (기본 22600=제226조)"
    )
    parser.add_argument("--hops", type=int, default=2, choices=(1, 2), help="그림 2 위임 hop 수")
    parser.add_argument(
        "--min-refers", type=int, default=3, help="그림 1 패밀리 간 REFERS_TO 표시 하한"
    )
    parser.add_argument("--scale", type=int, default=2, help="PNG 배율")
    parser.add_argument("--no-png", action="store_true")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        apply_data_store_environment(args.profile)
    except LocalProfileError:
        print(f"BLOCKED: '{args.profile}' 프로파일이 초기화되어 있지 않습니다", file=sys.stderr)
        return 2

    out = args.out or artifact_root() / "demo-graph"
    out.mkdir(parents=True, exist_ok=True)
    try:
        corpus = load_corpus()
    except Exception as error:  # 드라이버 예외에 크레덴셜이 섞일 수 있어 유형만 출력
        print(f"BLOCKED: PostgreSQL 읽기 실패 ({type(error).__name__})", file=sys.stderr)
        return 2
    print(
        f"[corpus] 조문 {len(corpus.articles):,} · CITES {corpus.counts['CITES']:,} · "
        f"DELEGATES {corpus.counts['DELEGATES']:,} · REFERS_TO {corpus.counts['REFERS_TO']:,}"
    )

    figures = {
        "fig1_hierarchy_delegation": figure_hierarchy(corpus, args.min_refers),
        f"fig2_ego_{args.law_id}_{args.article}": figure_ego(
            corpus, args.law_id, args.article, args.hops
        ),
    }
    browser = None if args.no_png else find_browser()
    if not args.no_png and browser is None:
        print("PNG 생략: Edge/Chrome 을 찾지 못했습니다 (SVG 는 생성)", file=sys.stderr)
    for name, svg in figures.items():
        svg_path = out / f"{name}.svg"
        svg_path.write_text(svg.render(), encoding="utf-8")
        print(f"[svg] {svg_path}  ({svg.width}x{svg.height})")
        if browser:
            png_path = out / f"{name}.png"
            if svg_to_png(browser, svg_path, png_path, svg.width, svg.height, args.scale):
                print(f"[png] {png_path}  (@{args.scale}x)")
            else:
                print(f"PNG 실패: {name} — SVG 를 브라우저로 열어 캡처하세요", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
