"""Frozen test fixture, not the app: what sitegen reads from src/lasto/operations/drive.py, copied
from the app at dab6e21 (Phase 2 as approved), and nothing else. See site/tests/fixtures/README.md.
"""

SILENCE = 60.0  # seconds without a frame that end a session (armed mode, §0)
POLL = 0.01  # seconds between reads of the channel
TICK = 1.0  # seconds between audit catch-ups, live feed writes, and stop-file checks
PROGRESS = 10.0  # seconds between progress lines
