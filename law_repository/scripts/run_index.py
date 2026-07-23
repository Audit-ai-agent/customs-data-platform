"""Qdrant 인덱싱 러너 — Start-Process 용 (인라인 -c 는 따옴표가 깨져 사용 금지).

실행: law_repository/ 에서  python scripts/run_index.py
완료 시 INDEX_DONE 출력 (감시 스크립트가 이 마커로 종료 판정).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(errors="replace", line_buffering=True)

from src.ingest.laws import index_qdrant, verify

index_qdrant()
verify()
print("INDEX_DONE")
