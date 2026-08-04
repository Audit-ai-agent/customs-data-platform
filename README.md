# Customs Data Platform

로컬 사용자 작업대에서 실제 HTTP 경계와 제한된 Data 동작을 확인하는 non-release
프로필은 [`docs/LOCAL_USER_UAT.md`](docs/LOCAL_USER_UAT.md)에 정리한다. 이 프로필은
Production V3 entrypoint, sealed generation, RAG 품질 Release를 대체하지 않는다.

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

## Production runtime v2 binding

운영 Agent는 v1 body를 유지한 `GET /internal/rag/v2/health/ready`와
`POST /internal/rag/v2/retrieve`를 사용합니다. v2는 위 네 build header에
knowledge generation, generation attestation, evaluation, runtime release를 더한
총 여덟 header를 GET과 POST 사이에 exact binding합니다. PostgreSQL/Qdrant/Neo4j가
하나의 sealed generation을 가리키고, 사람 검토 Gold 평가와 provider context가 모두
일치할 때만 readiness 200을 반환합니다.

현재 보존된 팀 Data처럼 58개 중 일부 법령만 있고 generation metadata가 없는 상태는
readiness 503이 정상입니다. 빈 법령을 추가하거나 expected count를 낮춰 통과시키지 않습니다.
test-only conformance identity는 HTTP 상호운용만 증명하며 제품 runtime lock이나 RAG release로
승격할 수 없습니다.

## Production-candidate runtime v3

`/internal/rag/v3/*`는 v2의 sealed knowledge generation을 그대로 결박하면서,
요건서류 코드의 **존재**와 실제 서류의 **검증 완료**를 분리합니다. v3 응답은
각 요건서류에 `presence_only`, `content_match_required`,
`external_verification_required`, `human_only` 중 하나의 승인된 정책과 원본 hash를
제공합니다. 매핑이 없거나 승인되지 않은 코드는 항상 `human_only`입니다.

저장소의 `contracts/evidence/v3/requirement-policy.lock.json`은 의도적으로
`unreleased`이며 승인 자료가 아닙니다. 운영 v3는 두 독립 도메인 검수자의
Ed25519 서명, adjudication, coverage matrix, 유효기간이 결박된 정책 JSON과 별도
reviewer trust store를 read-only로 마운트해야 합니다. reviewer store의 SHA-256은
provider image 안의 immutable lock에 미리 고정되므로 두 mount를 함께 임의 교체해도
readiness가 열리지 않습니다. 현재 검수자가 없고 trust lock도 `unreleased`이므로
구현 상태는 `Production candidate`이고 자동완료
근거로 사용할 수 없습니다. 자세한 계약은
[Evidence v3](contracts/evidence/v3/README.md), 결정 근거는
[ADR-0001](docs/ADR-0001-EVIDENCE-V3-REQUIREMENT-POLICY.md)을 따릅니다.

## 로컬 준비

Python 3.11과 [uv](https://docs.astral.sh/uv/)를 사용합니다.

```powershell
uv sync --locked --extra rag --group dev
Copy-Item law_repository/.env.example law_repository/.env
```

기본 dependency는 Evidence HTTP runtime과 PostgreSQL·Qdrant·Neo4j client를 포함합니다.
검색 모델·수치 계산은 `rag`, 외부 수집·Excel/XML 처리는 `ingestion`, 검사 도구는
`dev`로 분리되어 있습니다. 수집과 검색 인덱스 생성까지 수행할 때는
`--extra rag --extra ingestion`을 함께 지정합니다.

실제 API key와 DB 비밀번호는 `.env` 또는 secret manager에만 입력합니다. example 파일에는 비밀값을 넣지 않습니다.
로컬 PostgreSQL host port 기본값은 다른 개발 stack과 충돌하지 않도록 `55432`이며,
컨테이너 내부 포트는 계속 `5432`입니다. `.env`의 `POSTGRES_PORT`로 변경할 수 있습니다.
로컬 Compose의 PostgreSQL·Qdrant·Neo4j published port는 모두
`127.0.0.1`에 고정됩니다. 기본 비밀번호 또는 인증 없는 개발 store를 LAN/WAN에
노출하기 위해 host IP를 완화해서는 안 됩니다.

## 검증

DB가 필요 없는 계약·평가·단위 검증:

```powershell
uv run --locked --extra rag python tools/check_contract_drift.py
uv run --locked --extra rag pytest -q evals/tests law_repository/tests/test_provider_contract.py law_repository/tests/test_provider_identity.py law_repository/tests/test_evidence.py law_repository/tests/test_evidence_http_contract.py law_repository/tests/test_hf_space_deploy.py law_repository/tests/test_ccct_p0.py law_repository/tests/test_bm25.py
```

일반 PR CI는 lock·계약 drift·lint·type과 대표 offline 회귀만 확인합니다. 전체 test와
3-store ingestion/live 경로는 보존합니다. 보안·provenance를 포함한 전체 인증은 일반
CI 범위가 아니며 Release 판정 때만 별도로 실행합니다.

서로 다른 프로세스의 Agent HTTP conformance를 검증할 때만 clean commit에서
sealed bundle을 만든 뒤 loopback 전용 runner를 명시적으로 실행합니다. 이
fixture는 58개 법령 Live readiness를 대체하지 않습니다.

```powershell
uv run --locked --extra rag python tools/build_hf_space_context.py --output <EMPTY_EXTERNAL_BUNDLE>
uv run --locked --extra rag python tools/run_evidence_conformance_server.py `
  --test-only-conformance --bundle-root <EMPTY_EXTERNAL_BUNDLE> `
  --host 127.0.0.1 --port 18010
```

runtime v2 Agent 상호운용은 exact clean commit에서 production context를 만든 뒤 별도의
loopback runner로 검증합니다. token 값은 argv나 저장소에 넣지 않습니다.

```powershell
uv run --locked --extra rag python tools/build_production_context.py `
  --repository . --output <EMPTY_EXTERNAL_CONTEXT>
uv run --locked --extra rag python tools/run_evidence_v2_conformance_server.py `
  --test-only-conformance `
  --production-context <EMPTY_EXTERNAL_CONTEXT> `
  --service-token-file <ABSOLUTE_EXTERNAL_TOKEN_FILE> `
  --identity-output <NEW_EXTERNAL_IDENTITY_JSON> `
  --host 127.0.0.1 --port 18010
```

보존 팀 Data가 실제로 incomplete임을 볼륨 수정 없이 확인할 때만 다음 진단 runner를
사용합니다. 이 runner는 PostgreSQL transaction을 강제로 read-only로 만들고 실제 v2
인증·readiness·metrics 경계를 사용하지만, model을 시작하거나 identity를 합성하지 않으며
항상 release-ready를 거부합니다. DB 접속정보는 process environment/secret으로만 주입합니다.

```powershell
uv run --locked --extra rag python tools/run_preserved_data_v2_diagnostic.py `
  --test-only-preserved-readonly-diagnostic `
  --production-context <EMPTY_EXTERNAL_CONTEXT> `
  --service-token-file <ABSOLUTE_EXTERNAL_TOKEN_FILE> `
  --audit-output <NEW_EXTERNAL_AUDIT_JSON> `
  --host 127.0.0.1 --port 18011
```

이 진단의 성공 기준은 readiness `503`, retrieval POST `0`, audit의
`release_ready=false`입니다. 이는 실제 RAG 성공이나 세 store 정합성 PASS가 아닙니다.

보존된 실제 3법령·1,007조문을 현재 Agent V3와 연결하는 로컬 진단은
[`preserved-3law-v3-diagnostic`](docs/LOCAL_USER_UAT.md#preserved-3law-v3-diagnostic-actual-store-v3)
프로필만 사용합니다. 이 경로는 실제 PostgreSQL/Qdrant/Neo4j를 read-only로
조회하고 V3의 17개 identity header에 결박하지만, 3/58 범위·미평가 RAG
품질·미승인 요건서류 정책을 그대로 표시하며 자동완료를 허용하지 않습니다.

9개 로컬 사용자 시나리오를 같은 실제 Store에서 평가할 때는 별도
[`preserved-3law-v3-scenario-eval`](docs/LOCAL_USER_UAT.md#preserved-3law-v3-scenario-eval-actual-store-uat)
프로필을 사용합니다. 이 프로필은 HSK `0303540000`의 실제 Structured 결과에
`CCCT_DOC_46E819BB87B27371`가 존재해야만 시작하며, 그 코드 하나에만 서명된
test-only `approved=true`, `human_only` 정책을 결박합니다. 정책 모드는
`SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY`이고 법적 승인·Production readiness·
자동완료 효력은 항상 없습니다. 요청별 Structured/Dense/BM25/Graph 관측치는
원문·query·credential 없이 외부 JSONL Artifact에만 기록됩니다.

3-store 통합 검증은 Docker 서비스를 기동하고 migration·ingestion을 완료한 별도 환경에서 실행합니다. 연결 실패나 미적재 상태의 skip은 Live Gate 통과가 아닙니다.

```powershell
uv run --locked --extra rag --extra ingestion python tools/live_preflight.py --phase assets
docker compose -f law_repository/docker-compose.yml up -d --wait
uv run --locked --extra rag --extra ingestion python tools/live_preflight.py --phase services
Set-Location law_repository
uv run --locked --extra rag --extra ingestion python scripts/migrate.py
uv run --locked --extra rag --extra ingestion python -m src.ingest.hsk
uv run --locked --extra rag --extra ingestion python -m src.ingest.laws
uv run --locked --extra rag --extra ingestion python -m src.ingest.ccct --load-only
uv run --locked --extra rag --extra ingestion python -m src.ingest.graph
# 유효한 LAW_API_OC로 실제 변경이력이 존재하는 기간을 지정한다.
uv run --locked --extra rag --extra ingestion python -m src.sync.delta --since 20260701
uv run --locked --extra rag --extra ingestion pytest -q --junitxml=../live.junit.xml tests
Set-Location ..
uv run --locked --extra rag --extra ingestion python tools/assert_no_skips.py live.junit.xml
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

운영 Evidence API는 팀 데모 `src.webapp`과 분리된
`python -m src.production`만 사용합니다. 인증·TLS ingress·요청 제한·비식별
관측성·read-only container 배포 기준은
[production deployment](law_repository/deploy/production/README.md), 장애·회전·rollback
절차는 [production runbook](docs/RUNBOOK_EVIDENCE_PRODUCTION.md)을 따릅니다.
