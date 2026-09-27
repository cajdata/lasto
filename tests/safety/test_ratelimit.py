"""Rule 6: rate limiting with a hard ceiling and busy back-off."""

import pytest

from lasto.safety.ratelimit import BACKOFF_MAX, HARD_CEILING_PER_SECOND, PURPOSE_RATES, RateLimiter
from lasto.safety.requests import Purpose


def test_defaults_match_the_spec():
    assert HARD_CEILING_PER_SECOND == 20.0
    assert PURPOSE_RATES[Purpose.LOGGING] == 20.0
    assert PURPOSE_RATES[Purpose.DISCOVERY] == 5.0
    assert all(0 < rate <= HARD_CEILING_PER_SECOND for rate in PURPOSE_RATES.values())


@pytest.mark.parametrize("rate", [0.0, -1.0, 20.01, 1000.0])
def test_rates_above_the_ceiling_are_refused(rate):
    with pytest.raises(ValueError):
        RateLimiter({**PURPOSE_RATES, Purpose.LOGGING: rate})


def test_spacing_per_purpose():
    limiter = RateLimiter()
    assert limiter.delay(Purpose.DISCOVERY, 10.0) == 0.0
    limiter.commit(Purpose.DISCOVERY, 10.0)
    assert limiter.delay(Purpose.DISCOVERY, 10.0) == pytest.approx(0.2)
    assert limiter.delay(Purpose.DISCOVERY, 10.2) == pytest.approx(0.0)


def test_ceiling_across_purposes():
    limiter = RateLimiter()
    limiter.commit(Purpose.DISCOVERY, 10.0)
    # Logging has its own budget but still waits for the shared 20/s ceiling.
    assert limiter.delay(Purpose.LOGGING, 10.0) == pytest.approx(0.05)
    assert limiter.delay(Purpose.LOGGING, 10.05) == pytest.approx(0.0)


def test_backoff_doubles_up_to_the_maximum():
    limiter = RateLimiter()
    delays = []
    for _ in range(8):
        limiter.backoff(0.0)
        delays.append(limiter.delay(Purpose.LOGGING, 0.0))
    assert delays[:3] == pytest.approx([0.2, 0.4, 0.8])
    assert delays[-1] == BACKOFF_MAX
    limiter.clear_backoff()
    limiter.backoff(100.0)
    assert limiter.delay(Purpose.LOGGING, 100.0) == pytest.approx(0.2)
