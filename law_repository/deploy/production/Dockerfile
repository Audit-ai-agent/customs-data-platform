# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# Build only from the allowlist context produced by
# tools/build_production_context.py.  Both image arguments are intentionally
# required: release automation must pass immutable name@sha256 references.
ARG UV_IMAGE
ARG PYTHON_IMAGE
FROM ${UV_IMAGE} AS uv_binary
FROM ${PYTHON_IMAGE} AS runtime

COPY --from=uv_binary /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_NO_CACHE=1 \
    HF_HOME=/opt/model-cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    EVIDENCE_PROVIDER_BUNDLE_ROOT=/app \
    PATH=/opt/venv/bin:$PATH \
    HOME=/tmp

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra rag --no-install-project

# The sealed context contains only committed runtime source, canonical schemas,
# lockfiles, deployment docs, and the generated source/provider manifests.
COPY . /app

# Fetch immutable model revisions at build time.  Runtime networking to the
# model hub is disabled, making the image independent from a mutable "main".
RUN --mount=type=secret,id=HF_TOKEN,mode=0400,required=false \
    set -eu; \
    export HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0; \
    if [ -f /run/secrets/HF_TOKEN ]; then \
      export HF_TOKEN="$(cat /run/secrets/HF_TOKEN)"; \
    fi; \
    cd /app/law_repository; \
    /opt/venv/bin/python -c \
      "from huggingface_hub import snapshot_download; \
from src.embed.model_identity import DENSE_MODEL, RERANK_MODEL; \
snapshot_download(DENSE_MODEL.model_id, revision=DENSE_MODEL.revision); \
snapshot_download(RERANK_MODEL.model_id, revision=RERANK_MODEL.revision)"

# Verify the exact provider bundle before dropping privileges.  No DB or
# external service is contacted during image build.
RUN cd /app/law_repository \
    && /opt/venv/bin/python -c \
      "from src.provider_identity import load_provider_build_identity, load_runtime_provider_build_identity; \
root=__import__('pathlib').Path('/app'); \
load_provider_build_identity(bundle_root=root); \
load_runtime_provider_build_identity(bundle_root=root)" \
    && groupadd --gid 65532 evidence \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin evidence \
    && chmod -R a=rX /app /opt/venv /opt/model-cache \
    && mkdir -p /tmp/evidence \
    && chown 65532:65532 /tmp/evidence

USER 65532:65532
WORKDIR /app/law_repository
EXPOSE 8010

# TLS is terminated and authenticated by the approved ingress/service mesh.
# The application independently verifies the trusted proxy CIDR and the exact
# forwarded HTTPS assertion before service authentication.
CMD ["python", "-m", "src.production"]
