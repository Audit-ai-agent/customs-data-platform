# Security and Data Handling

## 비밀정보

- `.env`, API key, DB DSN, token은 Git에 커밋하지 않는다.
- `.env.example`과 배포 example에는 빈 값 또는 명백한 placeholder만 둔다.
- GitHub Actions에는 repository/environment secret을 사용한다.
- 로그·오류 메시지에 credential, query parameter의 service key, 개인정보를 출력하지 않는다.
- 노출이 의심되는 key는 값의 진위와 무관하게 폐기·재발급한 후 이력을 점검한다.

`LAW_API_OC`는 새 working tree의 example과 코드 default에서 제거했습니다. 실제 값은 실행 환경에서만 주입합니다.

기존 공개 upstream 이력에는 두 example 파일에 동일한 비어 있지 않은 9자 값이 남아 있습니다. 실제 credential인지 소유자가 확인하고, 실제 또는 의심 credential이면 제공기관에서 폐기·재발급해야 합니다. 이 작업은 외부 계정 권한이 필요하므로 본 저장소 재구성에서는 history rewrite나 key 회전을 수행하지 않았으며 release 전 보안 blocker로 남깁니다. 값 자체는 문서·로그·이슈에 재출력하지 않습니다.

## 데이터

- `data/raw`는 재수집 가능한 로컬 artifact로 취급하고 기본적으로 추적하지 않는다.
- Gold fixture에는 실제 거래 개인정보·상업 비밀을 넣지 않는다.
- 원본 source URI는 인증정보를 제거한 재현 가능한 형태로 저장한다.
- Knowledge snapshot promotion은 정합성 검사 후 수행하고 rollback 가능한 이전 snapshot을 보존한다.

## 서비스 경계

- Agent는 typed Evidence API만 호출하며 PostgreSQL/Qdrant/Neo4j에 직접 접속하지 않는다.
- 외부에 `/internal` endpoint를 공개할 때는 service identity, TLS, allowlist/rate limit을 적용한다.
- 문서 또는 검색 text를 명령으로 해석하지 않는다.
