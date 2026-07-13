# Customs Data Platform

대한민국 관세 검토 서비스를 위한 Data/RAG/Knowledge DB 저장소입니다. 법령·HSK·세관장확인대상물품(CCCT)의 원장과 검색 인덱스를 관리하고, AI Agent에는 버전이 고정된 Evidence API만 제공합니다.

## 저장소 책임

- PostgreSQL: 시간 버전 법령 및 CCCT 원장, knowledge snapshot
- Qdrant: 현행 법령 dense/BM25 검색 인덱스
- Neo4j: 법령 조문 관계와 HSK–Requirement 관계
- `POST /internal/rag/v1/retrieve`: Agent용 근거 검색 계약
- `contracts/evidence/v1`: 제공자가 소유하는 canonical wire schema
- `evals`: Structured/Hybrid gold fixture와 품질 evaluator

애플리케이션 Case DB, Agent workflow, 제품 Backend/Frontend는 이 저장소의 책임이 아닙니다. 소비자는 DB에 직접 접속하지 않고 Evidence API의 release와 schema SHA-256을 고정해야 합니다.

## 로컬 준비

Python 3.11과 [uv](https://docs.astral.sh/uv/)를 사용합니다.

```powershell
uv sync --locked --group dev
Copy-Item law_repository/.env.example law_repository/.env
```

실제 API key와 DB 비밀번호는 `.env` 또는 secret manager에만 입력합니다. example 파일에는 비밀값을 넣지 않습니다.

## 검증

DB가 필요 없는 계약·평가·단위 검증:

```powershell
uv run --locked python tools/check_contract_drift.py
uv run --locked pytest -q evals/tests law_repository/tests/test_provider_contract.py law_repository/tests/test_evidence.py law_repository/tests/test_ccct_p0.py law_repository/tests/test_bm25.py
```

3-store 통합 검증은 Docker 서비스를 기동하고 migration·ingestion을 완료한 별도 환경에서 실행합니다. 연결 실패나 미적재 상태의 skip은 Live Gate 통과가 아닙니다.

```powershell
docker compose -f law_repository/docker-compose.yml up -d --wait
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

구조와 경계는 [ARCHITECTURE.md](ARCHITECTURE.md), 비밀·데이터 정책은 [docs/SECURITY.md](docs/SECURITY.md)를 참고합니다.
