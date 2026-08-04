"""법제처 국가법령정보 DRF API 클라이언트 (공통).

확정된 target (2026-07-02, guideList 실측 + 실키 호출 검증 — 작업현황 §10 blocker 해소):
- 현행법령 본문   : lawService.do?target=law&ID=<법령ID>          (MST 없이 현행 조회)
- 시행일 법령 목록 : lawSearch.do?target=eflaw&query=<법령명>
- 법령 변경이력    : lawSearch.do?target=lsHstInf&regDt=<YYYYMMDD>  (Phase 3)
- 조문별 변경이력  : lawService.do?target=lsJoHstInf&ID=<법령ID>&JO=<6자리>  (Phase 3)

JO 6자리 규약 = 조문번호 4자리 + 조문가지번호 2자리 (제226조 → '022600',
제5조의2 → '000502'). law_articles.article_no(int)도 같은 인코딩을 쓴다:
article_no = 조문번호*100 + 가지번호.

API 응답 JSON 키에 공백이 섞여 오므로(法제처 특성) 모든 키를 정규화한다.
"""

from __future__ import annotations

import html
import json
import os
import random
import re
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import requests

from src.config import settings

BASE = "https://www.law.go.kr/DRF"
RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 4

_session: Any | None = None


class LawApiError(RuntimeError):
    """Content-free official API failure safe for logs and artifacts."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _request_runtime() -> tuple[Any, Any]:
    """Load the official-law HTTP client only for ingestion commands."""

    global _session
    import requests

    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": "trade-doc-auditor/law-repository"})
        _session.trust_env = False
    return requests, _session


def _clean_keys(obj):
    """API 가 키에 공백을 섞어 반환하므로 재귀적으로 제거."""
    if isinstance(obj, dict):
        return {re.sub(r"\s+", "", k): _clean_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_keys(v) for v in obj]
    return obj


def squash(s: str | None) -> str:
    """값(날짜·번호)의 공백 제거."""
    return re.sub(r"\s+", "", s or "")


def as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def clean_text(s) -> str:
    if not s:
        return ""
    if isinstance(s, list):
        s = "\n".join(clean_text(x) for x in s)
    s = html.unescape(str(s)).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return s.strip()


def content_of(x) -> str:
    """{'content': ...} 형태 필드(법종구분·소관부처 등) 언래핑."""
    if isinstance(x, dict):
        return str(x.get("content", ""))
    return str(x) if x else ""


def _read_bounded_json(response: requests.Response) -> dict[str, Any]:
    raw_length = response.headers.get("Content-Length")
    if raw_length:
        try:
            if int(raw_length) > MAX_RESPONSE_BYTES:
                raise LawApiError("LAW_API_RESPONSE_TOO_LARGE")
        except ValueError:
            raise LawApiError("LAW_API_INVALID_CONTENT_LENGTH") from None

    body = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise LawApiError("LAW_API_RESPONSE_TOO_LARGE")
        payload = json.loads(bytes(body).decode("utf-8-sig"))
    except LawApiError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise LawApiError("LAW_API_INVALID_JSON") from None
    finally:
        response.close()
    if not isinstance(payload, dict):
        raise LawApiError("LAW_API_INVALID_JSON")
    return _clean_keys(payload)


def _retry_delay(attempt: int) -> float:
    return min(8.0, float(2**attempt)) + random.SystemRandom().uniform(0.0, 0.25)


def get(endpoint: str, **params) -> dict:
    requests, session = _request_runtime()
    api_credential = settings.law_api_oc.strip()
    if not api_credential or api_credential.casefold() in {"test", "change_me"}:
        raise LawApiError("LAW_API_CREDENTIAL_UNAVAILABLE")
    if endpoint not in {"lawService.do", "lawSearch.do"}:
        raise LawApiError("LAW_API_ENDPOINT_NOT_ALLOWED")

    params.setdefault("OC", api_credential)
    params.setdefault("type", "JSON")
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(_retry_delay(attempt))
        response: requests.Response | None = None
        try:
            response = session.get(
                f"{BASE}/{endpoint}",
                params=params,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                allow_redirects=False,
                stream=True,
            )
            status = int(response.status_code)
            if 300 <= status < 400:
                response.close()
                raise LawApiError("LAW_API_REDIRECT_REJECTED")
            if status == 429 or 500 <= status < 600:
                response.close()
                if attempt + 1 < MAX_ATTEMPTS:
                    continue
                raise LawApiError("LAW_API_RETRYABLE_FAILURE")
            if status < 200 or status >= 300:
                response.close()
                raise LawApiError("LAW_API_REQUEST_REJECTED")
            return _read_bounded_json(response)
        except LawApiError:
            raise
        except (requests.ConnectionError, requests.Timeout):
            if response is not None:
                response.close()
            if attempt + 1 >= MAX_ATTEMPTS:
                raise LawApiError("LAW_API_TRANSPORT_FAILURE") from None
    raise LawApiError("LAW_API_TRANSPORT_FAILURE")


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def fetch_current_law(law_id: str, use_cache: bool = True) -> dict:
    """'현행' 법령 본문. target=law + ID(법령ID).

    ⚠️ 실측(2026-07-12): 공포됐지만 미시행인 개정이 있는 법령은 이 endpoint 가
    **미래 시행 버전**을 반환한다(기본정보 시행일자가 미래). 그 경우 오늘 유효한
    버전은 fetch_law_by_mst(resolve_effective_version(...)) 로 따로 가져와야 한다
    — src.ingest.laws 의 backfill 참조.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache = RAW_DIR / f"law_current_{law_id}.json"
    if use_cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    body = get("lawService.do", target="law", ID=law_id)
    _write_cache(cache, body)
    return body


def fetch_law_by_mst(mst: str, use_cache: bool = True) -> dict:
    """특정 버전(MST=법령일련번호) 본문. 버전 고정이라 캐시 영구 유효."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache = RAW_DIR / f"law_mst_{mst}.json"
    if use_cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    body = get("lawService.do", target="law", MST=mst)
    _write_cache(cache, body)
    return body


def jo_code(no: str | int, branch: str | int = 0) -> int:
    """조문번호+가지번호 → article_no 정수 인코딩 (JO 규약과 동일)."""
    return int(no) * 100 + int(branch or 0)


def jo_label(no: str, branch: str) -> str:
    n = int(no) if str(no).isdigit() else no
    if branch and str(branch) not in ("00", "0", ""):
        b = int(branch) if str(branch).isdigit() else branch
        return f"제{n}조의{b}"
    return f"제{n}조"
