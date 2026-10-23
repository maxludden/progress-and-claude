"""Time-remaining estimation from timestamped progress samples."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

MIN_SAMPLES = 3
MIN_ELAPSED_SECONDS = 2.0
EMA_ALPHA = 0.3
_STALL_FLOOR_SECONDS = 30.0


def estimate_eta(samples: Sequence[tuple[float, float]], now: float) -> float | None:
    """Estimate seconds remaining using an exponential moving average of the rate.

    The estimate is withheld (``None``) until there are at least
    :data:`MIN_SAMPLES` samples spanning :data:`MIN_ELAPSED_SECONDS`, when no
    forward progress was made, or when the task looks stalled (no sample for much
    longer than the typical interval).

    Args:
        samples: ``(timestamp, progress)`` pairs, progress in 0..1.
        now: Current timestamp.

    Returns:
        Estimated seconds until progress reaches 1.0, or ``None`` if unknown.
    """
    ordered = sorted(samples)
    if len(ordered) < MIN_SAMPLES:
        return None
    first_ts, _ = ordered[0]
    last_ts, last_p = ordered[-1]
    elapsed = last_ts - first_ts
    if elapsed < MIN_ELAPSED_SECONDS or last_p >= 1.0:
        return None
    if now - last_ts > max(_STALL_FLOOR_SECONDS, 4 * elapsed / (len(ordered) - 1)):
        return None

    rate: float | None = None
    for (t0, p0), (t1, p1) in pairwise(ordered):
        dt, dp = t1 - t0, p1 - p0
        if dt <= 0 or dp <= 0:
            continue
        instant = dp / dt
        rate = instant if rate is None else EMA_ALPHA * instant + (1 - EMA_ALPHA) * rate
    if rate is None or rate <= 0:
        return None
    return (1.0 - last_p) / rate
