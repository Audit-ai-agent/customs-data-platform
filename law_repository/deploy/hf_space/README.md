---
title: 관세법령 저장소 데모
emoji: ⚖️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 관세법령 저장소 웹 데모 (HF Space)

관세법·시행령·시행규칙 조문 검색(dense/BM25) + HS 품목 요건 + 시점 조회.
데이터는 Neon(Postgres) / Qdrant Cloud / Neo4j AuraDB 에 있고, 이 Space 는
임베딩 + 웹 UI 만 담당한다.

## 배포 원칙

Docker build 중 GitHub repository를 clone하지 않는다. 검토된 Data commit에서
`tools/build_hf_space_context.py`가 생성한 allowlist context 전체를 Space에 게시한다.
context에는 `requirements.txt`, `src/**`, 고정 계약 Schema, Dockerfile,
`source-manifest.json`, `provider-build.json`만 포함되며 Git history, `.env`,
raw Data, test, migration은 포함되지 않는다. 런타임은 manifest의 모든 파일,
Evidence Schema SHA-256, provider revision/build SHA-256이 일치해야 readiness를
통과한다.

Public Space는 포함된 Python source도 공개한다. `DEMO_TOKEN`은 HTTP 접근만
제한하며 source 공개를 막지 못하므로, 기본 배포 대상은 **private Space**다.

## 이 Space 만드는 법 (호스트 1회 작업)

1. Data 저장소의 변경을 commit하고 working tree가 clean인지 확인한다.
2. 저장소 밖의 빈 경로를 지정해 context를 생성한다.

   ```powershell
   uv run --locked python tools/build_hf_space_context.py --output <EXTERNAL_CONTEXT_DIR>
   ```

3. `source-manifest.json`의 `source_commit`과 파일 SHA-256 및
   `provider-build.json`의 `provider_build_sha256`을 release 증거에 보존한다.
4. huggingface.co → New Space → SDK: **Docker** → visibility: **Private**로 생성한다.
5. 호스트에 주입된 HF credential로 context 전체를 Space repository에 게시한다.
   GitHub PAT를 Dockerfile, URL, build argument에 넣지 않는다.
6. Settings → **Variables and secrets** 에 등록 (deploy/.env.cloud.example 의 키 그대로):
   - `POSTGRES_HOST` `POSTGRES_DB` `POSTGRES_USER` `POSTGRES_PASSWORD` `POSTGRES_SSLMODE=require`
   - `QDRANT_URL` `QDRANT_API_KEY`
   - `NEO4J_URI` `NEO4J_USER` `NEO4J_PASSWORD`
   - `DEMO_TOKEN` (팀원과 공유할 임의 문자열 — 없으면 전세계 공개)
7. 빌드 완료 후 팀원에게 링크 공유: `https://<space주소>/?token=<DEMO_TOKEN>`

- 무료 CPU Space 는 미사용 시 잠들었다가 첫 접속에서 깨어난다(~1분).
- Data 코드가 바뀌면 새 commit에서 context를 다시 생성·게시한 후 Factory
  rebuild한다. 기존 context를 재사용하지 않는다.
