# 관세법령 저장소 (law_repository)

무역서류 AI 감사 프로젝트의 법령 저장소 레이어.
관세법·시행령·시행규칙 현행 조문(현재 snapshot 1,007개)을 자동 수집해서 3개 저장소에 나눠 담는다:

| 저장소 | 역할 |
|---|---|
| Postgres (`:55432`, 설정 가능) | 시간버전 법령 + CCCT 조회·raw item·Requirement 무손실 원장 |
| Qdrant (`:6333`) | 현행 조문 검색 인덱스 — 의미검색(bge-m3 dense) + 키워드(BM25) |
| Neo4j (`:7474`/`:7687`) | 그래프 — 조문 위임/인용, HSK→Requirement→법령/서류/기관 |

위 host port는 로컬 Compose에서 모두 `127.0.0.1`에만 publish된다.
개발 기본 credential과 인증 없는 Qdrant를 외부 interface에 노출하지 않는다.

배경·설계 근거는 `docs/` (로컬 문서) 및 팀 공유 보고서 참고.

---

## A. 그냥 써보고 싶은 팀원 (설치 없음)

호스트(서버 담당자)가 데모 서버를 띄운 상태라면 **브라우저에서
`http://<호스트IP>:8010`** 만 열면 된다. 끝.

- **조문 검색**: 일상어로 질문 (예: "물건이 상해서 돌려보내면 세금 돌려받을 수 있나?")
  → dense(의미)/bm25(키워드) 결과 나란히, 클릭하면 조문 전문
- **품목 요건**: HS코드 입력 (예: `0203291000` 삼겹살) → 세관장확인 대상 여부 + 필요서류·기관
- **조문 직접 조회**: 시행령 92조를 시점 `2026-04-01`로 조회하면 개정 전 본문이 나온다 (시간원장 데모)

## B. 전체 환경을 내 PC에 구축하려는 팀원

전제: Docker Desktop, Python 3.11+

```bash
gh repo clone Audit-ai-agent/customs-data-platform
cd customs-data-platform
uv sync --locked --extra rag --extra ingestion --group dev --python 3.11
cd law_repository

cp .env.example .env        # 비밀번호 등 값 채우기 (기본값으로도 로컬 구동 가능)
docker compose up -d        # Postgres / Qdrant / Neo4j 기동
uv run --project .. --locked --extra rag --extra ingestion python ../tools/live_preflight.py --phase services
uv run --project .. --locked --extra rag --extra ingestion python scripts/migrate.py   # 스키마 적용

uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.hsk
uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.laws
uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.ccct --load-only
uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.graph

uv run --project .. --locked --extra rag --extra ingestion pytest tests/   # 단위 + 3-store 통합 검증
```

로컬 host port는 `.env`의 `POSTGRES_PORT`, `QDRANT_HTTP_PORT`,
`QDRANT_GRPC_PORT`, `NEO4J_HTTP_PORT`, `NEO4J_BOLT_PORT`로 변경할 수 있다.
컨테이너 내부 표준 포트는 바뀌지 않는다. 다른 프로젝트가 `5432`를 사용 중이라도
그 컨테이너를 중단하지 말고 기본 `55432` 또는 다른 빈 port를 사용한다.

- ③에서 `--load-only` 를 빼면 관세청 API 전수 재수집을 시도하는데, 일일 쿼터(1만건) 때문에
  3일 걸린다. 레포에 커밋된 `data/ccct_progress.jsonl` 이 그 결과물이니 그대로 적재하면 된다.
- 이후 최신화는 `python -m src.sync.delta` (매일 1회 권장, 재실행 안전/멱등).

## C. 서버를 띄우는 사람 (호스트)

```bash
python -m src.webapp        # http://0.0.0.0:8010 — 팀원에게 내 IP 공유
```

- 기동 시 임베딩 모델을 예열하므로 준비까지 수십 초.
- Windows 방화벽이 물으면 "허용". 수동 개방:
  `netsh advfirewall firewall add rule name="lawrepo-demo" dir=in action=allow protocol=TCP localport=8010`
- **인증이 없다. 사내망/같은 공유기 안에서만 쓰고 외부 인터넷에 노출하지 말 것.**

## D. 상시 운영 — 서버리스 무료 조합 (노트북 안 켜둬도 됨)

데이터를 관리형 서비스에 올리고, 웹 데모는 private HF Space, 매일 동기화는
승인된 호스트 작업 스케줄러가 수행한다:

| 구성요소 | 서비스 (무료 티어) | 준비물 |
|---|---|---|
| Postgres | [Neon](https://neon.tech) | 프로젝트 생성 → 접속정보 |
| Qdrant | [Qdrant Cloud](https://cloud.qdrant.io) | free cluster → URL + API key |
| Neo4j | [Neo4j AuraDB](https://neo4j.com/cloud/aura) | free instance → URI + 비밀번호 |
| 웹서버+임베딩 | [HF Spaces](https://huggingface.co) Docker | commit 기반 allowlist context |
| 델타 동기화 | 호스트 PC 작업 스케줄러 | `scripts/register_delta_task.ps1` 1회 실행 (아래 참고) |

순서:
1. 위 3개 DB 계정 생성 → 접속정보를 `deploy/.env.cloud.example` 형식으로 `.env.cloud` 에 정리
2. 로컬에서 `.env` 를 클라우드 값으로 바꾸고 **초기 적재 1회** (위 B 의 migrate + ingest 4단계 그대로 — 대상만 클라우드로 바뀜)
3. **정기 동기화 등록**: `powershell -File scripts\register_delta_task.ps1` (관리자 불필요, 1회).
   매일 09:30, PC 가 꺼져 있었으면 다음 부팅 때 밀린 만큼 따라잡는다 (`data/delta_cloud.log` 에 기록).
   ⚠️ GitHub Actions 크론은 못 쓴다 — 법제처가 해외 IP(깃허브 러너)를 차단함 (2026-07-06 실측).
4. clean commit에서 HF Space context 생성 후 private Space에 게시
   (`deploy/hf_space/README.md`) → 팀원에게 `https://<space>/?token=<DEMO_TOKEN>` 공유

## 터미널 도구 (환경 구축한 사람용)

```bash
python -m src.cli           # 대화형 검색 (:hsk, :art, :asof 명령 — 파일 docstring 참고)
python -m src.repository    # 스모크 데모
```

## AI Agent 내부 Evidence API v1

로컬 demo·contract 확인에는 `python -m src.webapp`을 사용할 수 있지만 이것은
운영 서비스가 아니다. 운영에서 Agent는 인증·TLS·제한·lifecycle을 적용한
`python -m src.production`으로 실행되는 서비스의
`POST /internal/rag/v1/retrieve`를 호출한다. wire trade type은 `import|export`,
jurisdiction은 `KR`이다. HSK10은 `exact`, HS6는 하위 leaf가 하나일 때만
`unique_descendant`로 확정하며, 여러 leaf면 `ambiguous`로 반환한다.

`coverage=complete` 일 때만 `requirements_found=false`를 공식 요건 없음으로
해석할 수 있다. 저장소 장애는 `partial|failed`와 구조화된 `errors[]`로
반환되므로 Agent가 fail-closed 처리해야 한다. 구조화 CCCT 요건은 `top_k`로
잘라 필수서류를 유실하지 않고 해당 HSK의 유효한 요건을 모두 반환한다.

`query_intent`에 `law_support`가 포함되면 Evidence Service는 Qdrant dense/BM25
두 arm을 RRF로 합치고 lexical rerank한 뒤 Neo4j 조문 그래프를 1-hop 확장한다.
법령 결과는 `eligibility=supporting`이며, 구조화 CCCT Requirement만
`eligibility=finding`이 될 수 있다. Qdrant 포인트 수가 PostgreSQL 인덱싱 대상과
다르면 법령 검색을 `partial`로 차단한다.

CCCT query coverage는 시점별 원장이 아니므로 현재 V1은 snapshot 생성일만 지원한다.
그 외 과거/미래 `review_as_of`는 근거 없는 “요건 없음”을 막기 위해
`UNSUPPORTED_REVIEW_AS_OF`로 실패 처리한다.

## 코드 구조

```
src/
  lawgo.py          법제처 API 클라이언트 (target 규약 문서화)
  ingest/           적재기 — laws(조문) / hsk(품목) / ccct(요건) / graph(조문그래프)
  sync/delta.py     델타 동기화 + 승격 잡 (매일 1회)
  embed/            bge-m3 dense + BM25 sparse 인코더 (인덱스/쿼리 동일 인코더)
  repository.py     조회 계약 — search / resolve_as_of / hsk_requirements / expand_article
  evidence.py       Agent 내부 Evidence API v1 계약·조합·오류 의미
  cli.py            대화형 CLI
  webapp.py         팀 데모 웹서버 (FastAPI)
  production.py     운영 전용 인증·제한·관측성·lifecycle ASGI 경계
  production_settings.py 운영 fail-closed 설정과 secret/TLS/DB 검증
migrations/         Postgres DDL (유효구간 EXCLUDE 제약 포함)
tests/              스키마/적재/Evidence 계약/델타/그래프/cross-store 정합성
```

주의: repository의 `search()` 자체는 arm 별 `{article_pk, score, text}`만 반환한다.
RRF·경량 rerank·Evidence packaging은 그 위 Evidence Service orchestration이 소유한다.
