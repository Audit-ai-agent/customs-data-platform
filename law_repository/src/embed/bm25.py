"""BM25 sparse 인코더 (설계 §4.2 — 'BM25 검색 근거').

Qdrant 네이티브 sparse + `modifier: idf`(1.15.2+) 조합:
- IDF 는 Qdrant 가 서버사이드에서 계산.
- 클라이언트는 문서측 TF 성분만 보낸다:  tf*(k1+1) / (tf + k1*(1-b + b*len/avg_len))
- 쿼리측은 토큰당 1.0 (fastembed Bm25 와 동일한 관례).

한국어 처리(v2): Kiwi 형태소 분석기로 내용어(명사·수사·외국어·숫자·용언 어근)만
남기고 조사·어미·기호·법률 상투어를 버린다. 인접한 명사 연속열은 결합형도
함께 방출해("재수입"+"면세" → "재수입면세") 유사 제도("재수입↔재수출")의
변별력을 보존하고, "제241조(의2)" 같은 조문 번호는 정규식으로 한 토큰으로
고정해 정확한 조문 인용 일치에 강하게 둔다. 인덱스·쿼리가 반드시 같은
인코더를 써야 하므로 양쪽 모두 이 모듈만 사용할 것.

Kiwi 가 설치되지 않았으면 조용히 다른 토크나이저로 내려가지 않고 실패한다.
인덱스와 쿼리가 다른 인코더를 쓰면 검색이 조용히 망가지기 때문이다.

토큰 → 인덱스 는 md5 앞 4바이트(uint32) — 결정적이라 재적재에 안전.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import Counter
from typing import Any

K1 = 1.2
B = 0.75
# 65개 법령 5,889 말단 조문의 실측 평균 내용어 토큰 수(형태소 기준).
AVG_LEN = 141.0
ENCODER_VERSION = "ko-kiwi-md5-bm25/v2"

# 개별 토큰으로 남기는 내용어 품사.  용언(VV/VA)은 어근형("따르")으로 남는다.
_CONTENT_TAGS = frozenset({"NNG", "NNP", "NNB", "NR", "NP", "SN", "SL", "SH", "VV", "VA", "XR"})
# 결합형 생성에 참여하는 품사.  접두사(XPN: "재", "제")와 접미사(XSN: "장", "품")는
# 단독으로는 의미가 없지만 결합형("재수입", "세관장")을 만들 때 필요하다.
_RUN_TAGS = frozenset({"NNG", "NNP", "NNB", "NR", "SN", "SL", "SH", "XR", "XPN", "XSN"})
# 법률 상투어.  거의 모든 조문에 나타나 변별력이 없는 토큰만 둔다.
_STOPWORDS = frozenset(
    {
        "경우",
        "규정",
        "따르",
        "다음",
        "각호",
        "해당",
        "관련",
        "관하",
        "대하",
        "의하",
        "위하",
        "이상",
        "이하",
        "미만",
        "바",
        "것",
        "수",
        "때",
        "등",
        "및",
        "또는",
        "그",
        "이",
        "하",
        "되",
        "있",
        "없",
        "같",
        "정하",
        "받",
        "말하",
    }
)
_ARTICLE_RE = re.compile(r"제\s?(\d+)\s?조(?:\s?의\s?(\d+))?")

_kiwi: Any = None
_kiwi_lock = threading.Lock()


class KiwiUnavailable(RuntimeError):
    """The Kiwi morphological analyzer is not installed."""


def _get_kiwi() -> Any:
    global _kiwi
    if _kiwi is not None:
        return _kiwi
    with _kiwi_lock:
        if _kiwi is None:
            try:
                from kiwipiepy import Kiwi
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise KiwiUnavailable("BM25_KIWI_UNAVAILABLE") from exc
            _kiwi = Kiwi()
        return _kiwi


def kiwi_version() -> str:
    try:
        from kiwipiepy import __version__
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise KiwiUnavailable("BM25_KIWI_UNAVAILABLE") from exc
    return str(__version__)


def encoder_fingerprint() -> str:
    """Bind the encoder rules and the installed Kiwi release into one digest."""

    return hashlib.sha256(f"{ENCODER_VERSION}:{kiwi_version()}".encode("utf-8")).hexdigest()


def _analyze(text: str) -> list[Any]:
    """Return Kiwi tokens; the analysis itself is serialized (Kiwi is not thread-safe)."""

    kiwi = _get_kiwi()
    with _kiwi_lock:
        return list(kiwi.tokenize(text))


def tokenize(text: str) -> list[str]:
    """내용어 토큰 + 결합형 + 조문 번호 토큰. 인덱스와 쿼리가 같은 함수를 쓴다."""

    if not text or not text.strip():
        return []
    tokens: list[str] = []
    run: list[Any] = []

    def flush() -> None:
        if len(run) >= 2:
            forms = [_norm(token.form) for token in run]
            for left, right in zip(forms, forms[1:]):
                tokens.append(left + right)
            if len(forms) >= 3:
                tokens.append("".join(forms))
        run.clear()

    previous_end: int | None = None
    for token in _analyze(text):
        form = _norm(token.form)
        tag = str(token.tag)
        if tag in _CONTENT_TAGS and len(form) >= 2 and form not in _STOPWORDS:
            tokens.append(form)
        if tag in _RUN_TAGS:
            if previous_end is not None and token.start != previous_end:
                flush()
            run.append(token)
            previous_end = token.start + token.len
        else:
            flush()
            previous_end = None
    flush()
    # 조문 번호는 표기 공백("제 226 조")과 가지 번호("의2")까지 한 토큰으로 고정한다.
    # 결합형 단계가 이미 같은 토큰을 냈으면 tf 를 부풀리지 않는다.
    present = set(tokens)
    for match in _ARTICLE_RE.finditer(text):
        article = f"제{match.group(1)}조"
        if article not in present:
            tokens.append(article)
            present.add(article)
        if match.group(2):
            branch = f"{article}의{match.group(2)}"
            if branch not in present:
                tokens.append(branch)
                present.add(branch)
    return tokens


def _norm(form: str) -> str:
    return form.strip().lower()


def token_id(token: str) -> int:
    # Qdrant sparse-vector feature IDs require a stable 32-bit mapping.  This
    # digest is not used for authentication, signatures, or integrity.
    digest = hashlib.md5(token.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(digest[:4], "little")


def encode_doc(text: str) -> tuple[list[int], list[float]]:
    """문서측 sparse 벡터 (indices, values)."""
    tokens = tokenize(text)
    if not tokens:
        return [], []
    length = len(tokens)
    indices, values = [], []
    for tok, tf in Counter(tokens).items():
        indices.append(token_id(tok))
        values.append(tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / AVG_LEN)))
    return indices, values


def encode_query(text: str) -> tuple[list[int], list[float]]:
    """쿼리측 sparse 벡터 — 토큰당 1.0."""
    ids = {token_id(tok) for tok in tokenize(text)}
    return sorted(ids), [1.0] * len(ids)


__all__ = [
    "AVG_LEN",
    "B",
    "ENCODER_VERSION",
    "K1",
    "KiwiUnavailable",
    "encode_doc",
    "encode_query",
    "encoder_fingerprint",
    "kiwi_version",
    "token_id",
    "tokenize",
]
