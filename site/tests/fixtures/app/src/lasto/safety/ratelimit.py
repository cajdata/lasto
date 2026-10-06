"""Frozen test fixture, not the app: what sitegen reads from src/lasto/safety/ratelimit.py, copied
from the app at dab6e21 (Phase 2 as approved), and nothing else. See site/tests/fixtures/README.md.
"""

HARD_CEILING_PER_SECOND = 20.0
CEILING_WINDOW = 1.0
CEILING_FRAMES = int(HARD_CEILING_PER_SECOND * CEILING_WINDOW)
PURPOSE_RATES = MappingProxyType(
    {
        Purpose.LOGGING: 20.0,
        Purpose.SNAPSHOT: 5.0,
        Purpose.IDENTIFY: 5.0,
        Purpose.DISCOVERY: 5.0,
        Purpose.INTERLOCK_PROBE: 2.0,
    }
)
BACKOFF_START = 0.2
BACKOFF_MAX = 5.0
