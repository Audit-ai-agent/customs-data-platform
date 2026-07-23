"""대화형 검색 CLI — repository 를 사람 손으로 두드려 보는 도구.

실행: law_repository/ 에서  python -m src.cli
(도커 3-스토어 기동 필요. 첫 dense 검색은 bge-m3 로딩으로 수십 초 걸림.)

명령:
  <질의문>                 dense + bm25 두 arm 검색 (융합·rerank 없음 — arm 원본 그대로)
  :hsk <코드> [수입|수출]   HSK 요건 traverse (6/10자리, 예 :hsk 0204100000 수입)
  :art <법령> <조문>        시점 조회 + 그래프 확장 (예 :art 법 226 / :art 영 5의2)
  :expand <article_pk>     조문 그래프 확장 (위임 DELEGATES / 인용 CITES)
  :asof <YYYY-MM-DD|off>   이후 검색·조회를 특정 시점으로 고정 (off 로 현행 복귀)
  :limit <N>               arm 당 결과 수 (기본 5)
  :q                       종료
"""

from __future__ import annotations

import sys
from datetime import date, datetime

from src import repository as repo
from src.db.qdrant import BM25_VECTOR, DENSE_VECTOR

# 법령 별칭 → 법령ID (src.lawgo 관세 3법)
LAW_ALIAS = {
    "법": "001556",
    "관세법": "001556",
    "영": "002421",
    "시행령": "002421",
    "규칙": "006392",
    "시행규칙": "006392",
}
LAW_NAME = {"001556": "관세법", "002421": "시행령", "006392": "시행규칙"}


def _heading(text: str, width: int = 72) -> str:
    head = text.splitlines()[0].strip()
    return head if len(head) <= width else head[: width - 1] + "…"


def _parse_article_no(token: str) -> int:
    """'226' → 22600, '5의2' → 502 (article_no = 조번호*100 + 가지번호)."""
    if "의" in token:
        no, branch = token.split("의", 1)
        return int(no) * 100 + int(branch)
    return int(token) * 100


class State:
    as_of: date | None = None
    limit: int = 5


def do_search(query: str, st: State) -> None:
    when = st.as_of.isoformat() if st.as_of else "현행"
    for arm in (DENSE_VECTOR, BM25_VECTOR):
        print(f"\n── {arm} ({when}) " + "─" * 40)
        hits = repo.search(query, arm=arm, limit=st.limit, as_of=st.as_of)
        if not hits:
            print("  (결과 없음)")
        for i, h in enumerate(hits, 1):
            print(f"  {i}. pk={h.article_pk:<5} {h.score:>7.4f}  {_heading(h.text)}")


def do_hsk(args: list[str], st: State) -> None:
    if not args:
        print("사용법: :hsk <코드> [수입|수출]")
        return
    trade = args[1] if len(args) > 1 else None
    req = repo.hsk_requirements(args[0], trade_type=trade, as_of=st.as_of)
    if not req["resolved_hsk10"]:
        print(f"\nHS 해소: {req['hs_resolution']}")
        if req["candidate_hsk10s"]:
            print("  후보: " + ", ".join(req["candidate_hsk10s"][:20]))
        print("  HSK10을 확정하기 전에는 요건 판단을 수행하지 않습니다.")
        return
    anc = req["ancestors"]
    print(f"\n{req['hsk10']}  {req['name_ko'] or '(명칭 없음)'}")
    print(f"  계층: {anc['chapter2']['name']} > {anc['heading4']['name']} > {anc['hs6']['name']}")
    if not req["requirements"]:
        print("  요건 없음")
    for q in req["requirements"]:
        print(f"  [{q['trade_type']}/{q['source']}] {q['law_name']}")
        print(f"      서류: {q['document']} / 기관: {', '.join(q['agencies'])}")


def do_expand(pk: int) -> None:
    edges = repo.expand_article(pk)
    if not edges:
        print("  연결 없음 (그래프에 없는 pk 이거나 참조 0건)")
    for e in edges:
        law = LAW_NAME.get(e["law_id"], e["law_id"])
        art = e["article_no"]
        label = (
            f"{law} 제{art // 100}조" + (f"의{art % 100}" if art % 100 else "")
            if art
            else e["title"]
        )
        print(f"  -{e['rel']}→ pk={e['article_pk']} {label} ({e['title'] or ''})")


def do_art(args: list[str], st: State) -> None:
    if len(args) < 2 or args[0] not in LAW_ALIAS and args[0] not in LAW_NAME:
        print("사용법: :art <법|영|규칙|법령ID> <조번호[의N]>  예) :art 법 226")
        return
    law_id = LAW_ALIAS.get(args[0], args[0])
    try:
        art_no = _parse_article_no(args[1])
    except ValueError:
        print(f"조번호 해석 불가: {args[1]}")
        return
    row = repo.resolve_as_of(law_id, art_no, None, as_of=st.as_of)
    if row is None:
        print("  해당 시점에 그 조문 없음")
        return
    print(
        f"\npk={row['article_pk']}  {LAW_NAME[law_id]} ({row['title']})  "
        f"[{row['valid_from']} ~ {row['valid_to'] or '현행'}]"
    )
    body = row["content"]
    print("  " + "\n  ".join(body.splitlines()[:12]))
    if body.count("\n") > 12:
        print("  … (이하 생략)")
    print("[그래프 확장]")
    do_expand(row["article_pk"])


def main() -> int:
    # cp949 콘솔에서 유니코드(—·법령명 특수문자) 출력 크래시 방어 (ccct 와 동일)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    st = State()
    print(__doc__.split("명령:")[1].rstrip() if "명령:" in __doc__ else "")
    print("\n질의문 또는 명령 입력 (:q 종료)")
    while True:
        try:
            line = input("\n검색> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        # 파이프/리다이렉트로 들어온 깨진 입력(짝 없는 서로게이트)이 토크나이저를
        # 죽이지 않게 정화 — cp949 파이프 실측(2026-07-06)
        line = line.encode("utf-8", "replace").decode("utf-8").strip()
        if not line:
            continue
        if not line.startswith(":"):
            do_search(line, st)
            continue

        cmd, *args = line[1:].split()
        if cmd in ("q", "quit", "exit"):
            return 0
        elif cmd == "hsk":
            do_hsk(args, st)
        elif cmd == "art":
            do_art(args, st)
        elif cmd == "expand" and args and args[0].isdigit():
            do_expand(int(args[0]))
        elif cmd == "asof" and args:
            if args[0] == "off":
                st.as_of = None
                print("현행 모드")
            else:
                try:
                    st.as_of = datetime.strptime(args[0], "%Y-%m-%d").date()
                    print(f"시점 고정: {st.as_of}")
                except ValueError:
                    print("형식: :asof 2026-01-01  또는  :asof off")
        elif cmd == "limit" and args and args[0].isdigit():
            st.limit = int(args[0])
            print(f"arm 당 {st.limit}건")
        else:
            print("명령: :hsk :art :expand :asof :limit :q  (도움말은 파일 docstring)")


if __name__ == "__main__":
    sys.exit(main())
