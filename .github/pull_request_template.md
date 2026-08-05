## 변경 의도

<!-- 문제와 Evidence 계약/로컬 개발 영향만 짧게 적습니다. -->

## 검증

- [ ] `uv run --locked --no-sync --group dev python tools/check_contract_drift.py`를 실행했다.
- [ ] 변경 범위의 Ruff, Mypy, 관련 offline test를 실행했다.
- [ ] 비밀·원본 데이터·로컬 실행 결과를 포함하지 않았다.
- [ ] 필요한 경우 공용 Harness snapshot 갱신 PR을 연결했다.

## 리뷰

- [ ] 병합 전 한 명의 팀 리뷰어가 CI와 위 검증 결과를 확인한다.
