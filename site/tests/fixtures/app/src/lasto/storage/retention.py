"""Frozen test fixture, not the app: what sitegen reads from src/lasto/storage/retention.py, copied
from the app at dab6e21 (Phase 2 as approved), and nothing else. See site/tests/fixtures/README.md.
"""

GIB = 2**30
BUDGET_BYTES = 20 * GIB
LOW_FREE_BYTES = 2 * GIB
NEWEST_KEPT_DAYS = 30  # for pruning, after Phase 5
