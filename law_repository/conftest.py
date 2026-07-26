"""pytest 가 `import src...` 를 찾도록 레포 루트를 sys.path 에 추가."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
