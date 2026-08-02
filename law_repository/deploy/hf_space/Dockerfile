# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# HF Spaces (Docker Space) 용 — 관세법령 저장소 웹 데모
# Build context는 tools/build_hf_space_context.py가 committed source로 생성한 allowlist bundle이다.
FROM ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa AS uv_binary
FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS builder

COPY --from=uv_binary /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_NO_CACHE=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra rag --no-install-project

FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS runtime

WORKDIR /app
ARG SOURCE_REV=unknown
LABEL org.opencontainers.image.revision=$SOURCE_REV

ENV PATH=/opt/venv/bin:$PATH
COPY --from=builder /opt/venv /opt/venv

# Context is an allowlist bundle generated from one clean commit.  Preserve its
# paths so provider_identity can verify every manifest entry at runtime.
COPY . /app
WORKDIR /app/law_repository

# bge-m3 + bge-reranker-v2-m3(A1 rerank) 를 이미지에 미리 다운로드 — 콜드스타트에서 모델 받는 시간 제거.
# 익명 다운로드는 HF Hub rate limit 로 빌드가 죽을 수 있어(2026-07-13 실측 BUILD_ERROR)
# Space secret HF_TOKEN 을 빌드타임 시크릿으로 마운트해 인증 다운로드한다.
ENV HF_HOME=/app/hf_cache
RUN --mount=type=secret,id=HF_TOKEN,mode=0444,required=false \
    export HF_TOKEN=$(cat /run/secrets/HF_TOKEN 2>/dev/null || true) \
 && python -c "from FlagEmbedding import BGEM3FlagModel; BGEM3FlagModel('BAAI/bge-m3', use_fp16=False)" \
 && python -c "from FlagEmbedding import FlagReranker; FlagReranker('BAAI/bge-reranker-v2-m3', use_fp16=False)" \
 && chmod -R a+rwX /app/hf_cache

# DB 접속정보(POSTGRES_*, QDRANT_*, NEO4J_*)와 DEMO_TOKEN 은
# Space Settings > Variables and secrets 에 등록한다 (.env 파일 불필요 — env 가 우선).
ENV PORT=7860
EXPOSE 7860
CMD ["python", "-m", "src.webapp"]
