"""
edge_timing.py

Shared timer-wrap math for reconstructing an ever-increasing
monotonic_timestamp_us from the board's raw micros() readings (which wrap
every ~4294967296us, ~71.58 minutes).

Used by the live capture loop in capture_serial_to_csv.py. Kept in its
own module so it can be tested in isolation from the serial I/O.
"""

WRAP_PERIOD_US = 1 << 32
# Gaps within this margin of a full wrap period are ambiguous (could be a
# single wrap, or the wrap plus a fast-forward past a second one) and get
# flagged rather than trusted silently.
WRAP_WARN_MARGIN_US = 5 * 60 * 1_000_000  # 5 minutes


def wrap_delta(ts, prev_ts):
    """Real elapsed device ticks since prev_ts, unwrapping one timer wrap.
    Only valid if prev_ts and ts are less than one full wrap period apart
    (~71.58 min) — see near_wrap()."""
    return (ts - prev_ts) % WRAP_PERIOD_US


def near_wrap(delta):
    """True if this delta is close enough to a full wrap period that a
    missed multi-wrap gap can't be ruled out — the reconstructed
    monotonic value for it may understate real elapsed time."""
    return delta > WRAP_PERIOD_US - WRAP_WARN_MARGIN_US
