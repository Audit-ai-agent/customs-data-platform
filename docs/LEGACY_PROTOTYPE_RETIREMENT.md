# Legacy Vector Prototype Retirement

루트 `test/`는 2026-07-14 저장소 재구성에서 활성 tree에서 제거했습니다.

## 결정 이유

- 독립 `requirements.txt`와 ad-hoc script로 현재 `law_repository`의 3-store 구조 및 Evidence API와 계약이 다릅니다.
- 오래된 dense artifact와 raw 응답을 포함해 현재 CI와 품질 Gate의 대상을 불명확하게 만듭니다.
- Structured/Hybrid 품질 자산은 `evals/`로 재구성되어 중복 실행 경로가 필요하지 않습니다.

삭제는 Git 이력에서 제거한 것이 아닙니다. 기존 commit과 `upstream/upset_vector_db`에서 언제든 복원할 수 있고, 개인 작업공간의 archive에도 원본 snapshot이 별도로 보존됩니다.
