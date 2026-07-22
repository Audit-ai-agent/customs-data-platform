# LOCAL USER UAT Evidence profiles

This document describes non-release Evidence processes used only by the local
user workbench. They do not replace the sealed Production V3 entrypoint.

## `local-user-uat` synthetic V3

`tools/run_evidence_v3_conformance_server.py --profile local-user-uat` starts
an authenticated, literal-loopback V3 HTTP service. It uses the real V3 wire,
identity binding, request correlation, and policy projection, but it never
opens PostgreSQL, Qdrant, or Neo4j and is not RAG-quality evidence.

The profile provides two explicit controls:

- `7318151000`: covered import query with no required document. This is the
  clean user-flow Positive Control.
- `0303540000`: covered import query requiring the synthetic
  `TEST_ONLY_IMPORT_CONFIRMATION` document. This validates the safe
  untrusted-supporting-document path.

The server writes its exact V3 identity outside the immutable build context.
The local Agent manifest must embed those exact bytes. The service exposes
authenticated `/internal/rag/v3/health/uat-ready`; all responses state that
release readiness, RAG evaluation, and automatic completion are false.

## Preserved three-law diagnostic

`tools/run_three_law_v2_diagnostic.py` is preserved from the reviewed
TASK-0018 history. It validates and queries the existing three-law Data state
without treating it as a 58-law generation. It is a V2, read-only diagnostic;
it must not be presented as the synthetic V3 profile or as Production RAG.

## Boundary rules

- Never set a Production release lock to these identities.
- Never publish either diagnostic service outside loopback.
- Never use a test reviewer key or `test_issuer` as Production trust.
- Never report `auto_review_complete` from these profiles.
- Store bearer tokens outside Git and pass them through regular files.
