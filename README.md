# Customs Data Platform

대한민국 관세 검토 서비스를 위한 Data/RAG/Knowledge DB 저장소입니다. 법령·HSK·세관장확인대상물품(CCCT)의 원장과 검색 인덱스를 관리하고, AI Agent에는 버전이 고정된 Evidence API만 제공합니다.

## 저장소 책임

- PostgreSQL: 시간 버전 법령 및 CCCT 원장, knowledge snapshot
- Qdrant: 현행 법령 dense/BM25 검색 인덱스
- Neo4j: 법령 조문 관계와 HSK–Requirement 관계
- `POST /internal/rag/v1/retrieve`: Agent용 근거 검색 계약
- `contracts/evidence/v1`: 제공자가 소유하는 retrieval/readiness canonical wire schema
- `evals`: Structured/Hybrid gold fixture와 품질 evaluator

애플리케이션 Case DB, Agent workflow, 제품 Backend/Frontend는 이 저장소의 책임이
아닙니다. 소비자는 DB에 직접 접속하지 않고 Evidence API의 release, provider
revision/build SHA-256과 retrieval schema SHA-256을 함께 고정해야 합니다.

## Agent HTTP build binding

`GET /internal/rag/v1/health/ready`의 `evidence_contract`는 release
`evidence-api-v1.1.0`의 정확한 Data commit, frozen retrieval schema,
allowlist source manifest와 provider build hash를 반환합니다. Agent는 그 값으로
다음 네 POST 요청 헤더를 모두 보내고 응답의 같은 네 헤더도 다시 비교해야 합니다.

```text
X-Evidence-Schema-SHA256
X-Evidence-Provider-Release
X-Evidence-Provider-Revision
X-Evidence-Provider-Build-SHA256
```

누락·불일치는 backend 조회 전에 `412`로 거부됩니다. build identity나 source
manifest가 누락·변조되면 readiness와 POST는 `503`으로 닫힙니다. retrieval v1
JSON body는 이 결속을 추가하면서도 변경하지 않습니다.

## 로컬 준비

Python 3.11과 [uv](https://docs.astral.sh/uv/)를 사용합니다.

```powershell
uv sync --locked --group dev
Copy-Item law_repository/.env.example law_repository/.env
```

실제 API key와 DB 비밀번호는 `.env` 또는 secret manager에만 입력합니다. example 파일에는 비밀값을 넣지 않습니다.
로컬 PostgreSQL host port 기본값은 다른 개발 stack과 충돌하지 않도록 `55432`이며,
컨테이너 내부 포트는 계속 `5432`입니다. `.env`의 `POSTGRES_PORT`로 변경할 수 있습니다.

## 검증

DB가 필요 없는 계약·평가·단위 검증:

```powershell
uv run --locked python tools/check_contract_drift.py
uv run --locked pytest -q evals/tests law_repository/tests/test_provider_contract.py law_repository/tests/test_provider_identity.py law_repository/tests/test_evidence.py law_repository/tests/test_evidence_http_contract.py law_repository/tests/test_hf_space_deploy.py law_repository/tests/test_ccct_p0.py law_repository/tests/test_bm25.py
```

서로 다른 프로세스의 Agent HTTP conformance를 검증할 때만 clean commit에서
sealed bundle을 만든 뒤 loopback 전용 runner를 명시적으로 실행합니다. 이
fixture는 58개 법령 Live readiness를 대체하지 않습니다.

```powershell
uv run --locked python tools/build_hf_space_context.py --output <EMPTY_EXTERNAL_BUNDLE>
uv run --locked python tools/run_evidence_conformance_server.py `
  --test-only-conformance --bundle-root <EMPTY_EXTERNAL_BUNDLE> `
  --host 127.0.0.1 --port 18010
```

3-store 통합 검증은 Docker 서비스를 기동하고 migration·ingestion을 완료한 별도 환경에서 실행합니다. 연결 실패나 미적재 상태의 skip은 Live Gate 통과가 아닙니다.

```powershell
uv run --locked python tools/live_preflight.py --phase assets
docker compose -f law_repository/docker-compose.yml up -d --wait
uv run --locked python tools/live_preflight.py --phase services
Set-Location law_repository
uv run --locked python scripts/migrate.py
uv run --locked python -m src.ingest.hsk
uv run --locked python -m src.ingest.laws
uv run --locked python -m src.ingest.ccct --load-only
uv run --locked python -m src.ingest.graph
# 유효한 LAW_API_OC로 실제 변경이력이 존재하는 기간을 지정한다.
uv run --locked python -m src.sync.delta --since 20260701
uv run --locked pytest -q --junitxml=../live.junit.xml tests
Set-Location ..
uv run --locked python tools/assert_no_skips.py live.junit.xml
```

`--phase release`는 secret 값을 출력하지 않고 `LAW_API_OC`와
`DATA_GO_KR_SERVICE_KEY`의 존재 여부와 58개 대상 법령 cache의 JSON 유효성을
법령 ID별로 확인합니다. 이 단계의 실패나 불완전 corpus
readiness 503을 offline PASS로 대체해서는 안 됩니다. 외부 수집 권한이 없는 경우에는
보유 데이터로 진단까지만 수행하고 Release Gate는 실패 상태로 유지합니다.

이 도구의 `PASS`는 자산·port·credential·cache **사전조건**만 의미합니다.
TCP port에 올바른 제품이 뜨 있는지까지 증명하지 않으므로, Evidence
`/health/ready`, 3-store Live suite, no-skip Gate를 모두 통과해야 Release PASS입니다.

구조와 경계는 [ARCHITECTURE.md](ARCHITECTURE.md), 비밀·데이터 정책은 [docs/SECURITY.md](docs/SECURITY.md)를 참고합니다.
