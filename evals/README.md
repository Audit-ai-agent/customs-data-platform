# Retrieval Evaluation Assets

- `structured/`: CCCT Requirement의 행 보존, 필수서류, 기관, coverage를 검증합니다.
- `hybrid/`: 법령 article ranking의 Hit@5, Recall@10, nDCG@10을 검증합니다.
- `contract.py`: dependency-free canonical schema 및 domain invariant validator입니다.
- `tests/`: fixture와 evaluator 자체의 회귀 테스트입니다.

Gold fixture 변경은 근거 source, 기대값 변경 이유, reviewer 승인을 함께 기록해야 합니다. 실행 결과는 이 저장소에 commit하지 않고 호출자 또는 CI가 지정한 외부 artifact directory에 보존합니다.
