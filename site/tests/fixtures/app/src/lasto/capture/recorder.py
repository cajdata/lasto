"""Frozen test fixture, not the app: what sitegen reads from src/lasto/capture/recorder.py, copied
from the app at dab6e21 (Phase 2 as approved), and nothing else. See site/tests/fixtures/README.md.
"""

FLUSH_AFTER = 1.5  # seconds of host time before a quiet second is written anyway
SEGMENT_SECONDS = 3600  # a new segment file every hour
ANCHOR_EVERY = 60.0  # seconds of host time between clock anchors
