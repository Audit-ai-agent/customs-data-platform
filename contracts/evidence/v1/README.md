# Evidence API v1 Contract

`rag_retrieval_v1.schema.json`은 Data Platform이 소유하는 canonical 계약입니다.

- 기존 v1 artifact를 직접 수정해 breaking change를 만들지 않습니다.
- FastAPI 모델 변경 시 `python tools/check_contract_drift.py`를 반드시 통과합니다.
- 소비자는 파일의 SHA-256과 Data Platform release를 함께 pin합니다.
- 새 필수 필드, 의미 변경, enum 제거는 v2로만 제공합니다.

현재 frozen artifact SHA-256:

```text
531b59ba4e86f78cef20a4fca052996efd8b88c48853ac12113ca76e69cdd69a
```
