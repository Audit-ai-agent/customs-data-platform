# Evidence production service runbook

## 목적과 경계

이 Runbook은 `python -m src.production`으로 실행되는 Agent-to-Data Evidence
서비스 셸만 다룬다. 법령 수집, migration, knowledge generation 승격, DB
backup/restore는 별도 승인 작업이다. 서비스 장애를 해결하기 위해 빈 법령을
넣거나 target 수를 낮추거나 conformance backend를 운영에서 켜지 않는다.

## 배포 전 확인

다음 값은 출력하지 않고 존재·형식만 확인한다.

- signed image digest와 직전 rollback image digest
- source commit, provider build/schema/source-manifest SHA-256
- dense/reranker exact model revision
- Evidence service token secret file
- PostgreSQL read-only role와 `verify-full|verify-ca|require`
- Qdrant HTTPS endpoint와 API key
- Neo4j `neo4j+s|bolt+s` endpoint와 read-only role
- ingress certificate chain, hostname, expiry, exact proxy CIDR
- current published knowledge generation과 세 store 일치 상태

검토되지 않은 working tree에서 image context를 만들지 않는다. context builder와
image scanner, SBOM, signature, provenance Gate 중 하나라도 실패하면 배포하지
않는다.

## 정상 확인 순서

1. 새 replica를 traffic에서 제외한 상태로 기동한다.
2. TLS ingress를 통해 unauthenticated liveness가 200인지 확인한다.
3. service credential을 사용한 readiness가 200인지 확인한다.
4. readiness의 provider identity와 published generation을 release manifest와 비교한다.
5. pinned Agent의 실제 retrieval 한 건으로 request/response build header, request ID,
   snapshot, citation metadata를 확인한다.
6. logs/metrics에 token, header, case/item/document text가 없는지 canary 검사한다.
7. 새 replica에 제한된 traffic을 보내 latency, 401/412/413/503/504와 saturation을 관찰한다.
8. Gate가 유지될 때만 이전 replica를 drain한다.

실제 secret 값을 terminal history, issue, chat, artifact에 복사하지 않는다.

## 상태 코드 판단

| 상태 | 의미 | 조치 |
|---|---|---|
| 401 | service credential 없음·중복·불일치 | ingress가 Authorization을 보존하는지와 secret version을 값 출력 없이 확인 |
| 412 | Agent와 Data provider build 불일치 | traffic 중지, Agent pin·Data image digest·release 순서를 비교 |
| 413 | application body limit 초과 | 원문을 기록하지 말고 byte 수와 contract item 수만 확인 |
| 422 | Evidence request schema 불일치 | request/result schema version과 Agent wheel SHA 확인 |
| 503 readiness | corpus/store/generation/provider identity 불완전 | 자동 완료 금지, Data generation 진단으로 전환 |
| 503 busy | 동시성 한도 도달 또는 drain 중 | ingress retry 폭주를 차단하고 latency·DB·embedding 병목 확인 |
| 504 | 서비스 deadline 초과 | backend 작업은 slot을 계속 점유하므로 DB timeout/slow query와 saturation 확인 |

401을 우회하려고 query token을 켜거나, 412를 우회하려고 build header 검증을
제거하거나, 503을 성공으로 바꾸려고 fixture/빈 row를 넣지 않는다.

## 과부하와 장애

- liveness는 process 생존만 의미하며 readiness의 대체가 아니다.
- inflight가 한도에 붙으면 새 요청은 즉시 503으로 닫힌다. Agent retry에는
  exponential backoff와 jitter가 있어야 하며 무제한 retry를 금지한다.
- deadline 이후에도 underlying sync 작업이 끝날 때까지 admission slot이 유지된다.
  replica 재시작으로 반복 작업을 증폭시키지 않는다.
- provider identity, DB exception, secret file 오류의 원문은 응답·로그에 없어야 한다.
  exception type과 service request ID로 내부 진단한다.
- readiness가 장시간 실패하면 자동 완료를 중단하고 기존 검증된 Data generation과
  이전 signed image로 rollback한다.

## Credential와 인증서 회전

회전은 값의 in-place 편집이 아니라 staged rollout로 수행한다.

1. 새 credential/certificate secret version을 발급한다.
2. 새 Data replica와 Agent consumer를 제한된 경로에서 검증한다.
3. 실제 conformance 성공 후 traffic을 전환한다.
4. 이전 replica를 drain하고 이전 credential/certificate를 폐기한다.
5. 폐기 후 이전 credential/certificate의 실패를 negative test로 확인한다.

현재 service shell은 한 process에서 한 Bearer만 수락한다. 그러므로 무중단
rotation은 플랫폼의 replica/traffic staging과 Agent 변경 순서를 필요로 한다.
공유 token의 장기 사용을 production identity로 간주하지 않는다. workload
identity/mTLS를 도입하면 동일한 current/next/revoke 검증을 적용한다.

## 종료와 rollback

정상 종료는 신규 admission 차단 → 설정된 grace 동안 inflight drain → process
종료 순서다. orchestrator의 `stop_grace_period`는 application grace보다 길어야
한다. 강제 종료가 있었다면 완료되지 않은 request를 성공으로 기록하지 않는다.

Application rollback:

- 기존 tag를 이동하지 않는다.
- 직전 signature 검증된 image digest를 재배포한다.
- provider build가 바뀌면 Agent pin 호환성을 다시 확인한다.

Data rollback:

- application image rollback과 knowledge generation rollback을 혼합하지 않는다.
- 검증된 published-generation pointer와 세 store manifest가 일치하는 세대로만 되돌린다.
- rollback 후 readiness, pinned retrieval, citation hash를 다시 검증한다.

## 사후 증거

Incident에는 secret이나 문서 원문 없이 다음만 남긴다.

- UTC 구간, 영향과 감지 방식
- Data/Agent exact commit·image/wheel digest
- provider build, schema, knowledge generation ID
- bounded metric과 redacted log의 artifact hash
- 원인, 선택지, 결정 기준, rollback 결과
- 미검증 범위와 재발 방지 Gate
