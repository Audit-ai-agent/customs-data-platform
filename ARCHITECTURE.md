# Data Platform Architecture

## 목적과 불변식

이 플랫폼의 목적은 Agent가 검증 가능한 법령·요건 근거를 재현 가능하게 받도록 하는 것입니다. 검색 결과의 편의보다 다음 안전 불변식을 우선합니다.

1. PostgreSQL 원장이 진실의 원천이며 Qdrant와 Neo4j는 재생성 가능한 projection이다.
2. `knowledge_snapshot_id`, source record, version hash가 없는 결과는 최종 근거가 될 수 없다.
3. 정확한 HSK10 또는 유일한 HSK10 descendant로 해소된 HS6만 finding 근거를 만들 수 있다.
4. 장애·모호성·커버리지 누락은 `partial|failed`로 반환하며 “요건 없음”으로 바꾸지 않는다.
5. Structured CCCT Requirement는 `top_k`로 자르지 않는다. Hybrid 법령 결과만 순위를 사용한다.
6. 설정된 대상 법령 corpus가 일부만 적재되면 readiness와 Hybrid 법령 검색은 fail-closed 한다.
7. 대상 58개 법령은 비어 있지 않은 현행 조문과 content hash·version provenance가
   있어야 하며, PostgreSQL/Qdrant index count 및 PostgreSQL/Neo4j article_pk
   양방향 집합이 일치해야 한다.
8. readiness에서 확인한 provider build와 POST 요청·응답의 네 결속 헤더가 다르면
   Evidence 본문을 사용하지 않는다.

## 데이터 흐름

```text
공식 법령/HSK/CCCT source
  -> raw/normalized ingestion
  -> PostgreSQL versioned ledger + snapshot
  -> Qdrant dense/BM25 projection
  -> Neo4j article and HSK/Requirement projection
  -> Evidence API v1
  -> AI Agent (version-pinned consumer)
```

`/api/search`와 `/api/retrieve`는 데모·기존 Hybrid 검색 경로이고, Agent의 안정 경계는 `/internal/rag/v1/retrieve`입니다. 두 경로는 공존하지만 Agent 계약은 후자만 보장합니다.

## 계약 변경 정책

- retrieval canonical schema: `contracts/evidence/v1/rag_retrieval_v1.schema.json`
- readiness canonical schema: `contracts/evidence/v1/readiness_v1.schema.json`
- OpenAPI와 canonical schema의 wire keyword 차이는 `tools/check_contract_drift.py`가 실패시킨다.
- v1 breaking change는 금지한다. 필요한 경우 `contracts/evidence/v2`와 새 endpoint를 추가한다.
- 소비자는 provider release/revision/build와 retrieval schema SHA-256을 함께 고정한다.
- production app은 Database backend만 만들며 test-only conformance backend를 환경변수로
  활성화할 수 없다.

## 평가 기준

- Structured gold: 모든 case 계약 유효, required document/agency row 보존, false-positive 0
- Hybrid gold: Hit@5 1.0, Recall@10 1.0, nDCG@10 0.8 이상
- citation metadata completeness 1.0
- Live suite의 예상치 못한 skip 0

평가 기준은 release gate이며 실세계 전체 모집단 성능을 보장한다는 의미가 아닙니다.
