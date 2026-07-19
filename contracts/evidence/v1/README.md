# Evidence API v1 Contract

`rag_retrieval_v1.schema.json`은 retrieval body, `readiness_v1.schema.json`은
provider build/readiness를 정의하는 Data Platform 소유 canonical 계약입니다.

- 기존 v1 artifact를 직접 수정해 breaking change를 만들지 않습니다.
- FastAPI 모델 변경 시 `python tools/check_contract_drift.py`를 반드시 통과합니다.
- 소비자는 파일의 SHA-256과 Data Platform release를 함께 pin합니다.
- 새 필수 필드, 의미 변경, enum 제거는 v2로만 제공합니다.

Provider release `evidence-api-v1.1.0`은 frozen retrieval body를 바꾸지 않고,
readiness identity와 다음 POST request/response header 결속을 추가합니다.

```text
X-Evidence-Schema-SHA256
X-Evidence-Provider-Release
X-Evidence-Provider-Revision
X-Evidence-Provider-Build-SHA256
```

현재 frozen artifact SHA-256:

```text
531b59ba4e86f78cef20a4fca052996efd8b88c48853ac12113ca76e69cdd69a
```
