"""Root conftest: puts the repository root on sys.path so tests can `import src.*`.

No shared fixtures live here yet; per-test fixtures stay next to their tests.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
