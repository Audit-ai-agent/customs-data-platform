"""BM25 sparse 인코더 (설계 §4.2 — 'BM25 검색 근거').

Qdrant 네이티브 sparse + `modifier: idf`(1.15.2+) 조합:
- IDF 는 Qdrant 가 서버사이드에서 계산.
- 클라이언트는 문서측 TF 성분만 보낸다:  tf*(k1+1) / (tf + k1*(1-b + b*len/avg_len))
- 쿼리측은 토큰당 1.0 (fastembed Bm25 와 동일한 관례).

한국어 처리: 형태소 분석기 없이 한글 연속열 + 한글 2-gram 을 토큰으로 쓴다.
조사 붙은 표층형("물품을"↔"물품")도 2-gram 이 겹쳐 매칭된다. 인덱스·쿼리가
반드시 같은 인코더를 써야 하므로 양쪽 모두 이 모듈만 사용할 것.

토큰 → 인덱스 는 md5 앞 4바이트(uint32) — 결정적이라 재적재에 안전.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

K1 = 1.2
B = 0.75
AVG_LEN = 256.0  # fastembed Bm25 기본값과 동일한 상수 근사

_RUN = re.compile(r"[가-힣]+|[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for run in _RUN.findall(text.lower()):
        tokens.append(run)
        if run[0] >= "가":  # 한글 연속열이면 2-gram 추가
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


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
    return list(ids), [1.0] * len(ids)
