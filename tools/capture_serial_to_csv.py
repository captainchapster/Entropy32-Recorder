#!/usr/bin/env python3
"""
capture_serial_to_csv.py

Reads the D2 edge stream from the Entropy32 Recorder board (running
entropy32_recorder.ino) and writes it to a CSV file shaped like the
evidence protocol's raw/raw_edges.csv (edge_index, raw_timer_ticks,
monotonic_timestamp_us).

raw_timer_ticks is the board's micros() reading exactly as received
(wraps every ~4294967296us, ~71.58 minutes). monotonic_timestamp_us
is that value unwrapped into an ever-increasing 64-bit microsecond
count, reconstructed here by accumulating (raw[i] - raw[i-1]) mod 2**32
between consecutive edges. This is only valid as long as consecutive
edges are less than one full wrap period (~71.58 min) apart; a bigger
gap (e.g. the capture was paused, or the source is extremely sparse)
would be indistinguishable from a single wrap and silently understate
elapsed time, so such gaps are flagged to stderr rather than fixed up
silently.

Requires: pip install pyserial

Usage:
    python capture_serial_to_csv.py --port /dev/ttyUSB0 --count 2000
    python capture_serial_to_csv.py --port COM5 --count 2000
"""
import argparse
import csv
import math
import sys

import serial

WRAP_PERIOD_US = 1 << 32
# Gaps within this margin of a full wrap period are ambiguous (could be a
# single wrap, or the wrap plus a fast-forward past a second one) and get
# flagged rather than trusted silently.
WRAP_WARN_MARGIN_US = 5 * 60 * 1_000_000  # 5 minutes

# Once we've heard from the board at least once, a read timeout just means
# the source hasn't fired yet (event rates are naturally variable) — not a
# wiring problem. Only nag about that once we've gone this many consecutive
# timeouts (~10 minutes) without hearing anything at all from the board.
IDLE_HEARTBEAT_TIMEOUTS = 60

# Thresholds (ms) used for the live Poisson-consistency check. Kept small —
# bounce/chatter shows up as an excess of very short inter-arrival times, and
# checking only the low end keeps the periodic status line to one line.
POISSON_CHECK_THRESHOLDS_MS = (1, 5, 10)


def poisson_status(diffs_us):
    """One-line verdict on whether short inter-arrival times (< 10ms) are
    consistent with a Poisson process, or flag likely comparator bounce."""
    mean = sum(diffs_us) / len(diffs_us)
    if mean <= 0:
        return "n/a"
    bounce = False
    for t_ms in POISSON_CHECK_THRESHOLDS_MS:
        t_us = t_ms * 1000
        n_obs = sum(1 for d in diffs_us if d < t_us)
        n_exp = (1 - math.exp(-t_us / mean)) * len(diffs_us)
        if n_exp >= 3 and n_obs / n_exp > 2.0:
            bounce = True
            break
    if bounce:
        return "WARNING: excess short (<10ms) intervals — check comparator bounce"
    return "ok"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True, help="e.g. /dev/ttyUSB0 or COM5")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--out", default="raw_edges.csv")
    p.add_argument("--count", type=int, default=2000,
                    help="stop after this many edges (~2000 is ~1hr at 20 CPM)")
    p.add_argument("--stats-every", type=int, default=100,
                    help="print a CPM/Poisson status line every N edges (0 to disable)")
    args = p.parse_args()

    print(f"Listening for device on {args.port}...")
    ser = serial.Serial(args.port, args.baud, timeout=10)
    n = 0
    skipped_lines = 0
    prev_raw = None
    monotonic = 0
    monotonic_start = None
    diffs_us = []
    connected = False
    consecutive_timeouts = 0
    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["edge_index", "raw_timer_ticks", "monotonic_timestamp_us"])
        while n < args.count:
            raw = ser.readline()
            if not raw:
                consecutive_timeouts += 1
                if not connected:
                    print("No data for 10s — check wiring/port.", file=sys.stderr)
                elif consecutive_timeouts % IDLE_HEARTBEAT_TIMEOUTS == 0:
                    idle_s = consecutive_timeouts * 10
                    print(f"Still connected, no edge for {idle_s}s — "
                          "normal for a quiet source, not a problem by itself.",
                          file=sys.stderr)
                continue
            if not connected:
                print("Device acknowledged.")
            connected = True
            consecutive_timeouts = 0
            line = raw.decode(errors="replace").strip()
            if not line or line == "edge_index,timestamp_us":
                continue
            if line == "OVERRUN_DETECTED":
                print("!! Board reported a dropped edge — this capture is "
                      "INVALID per the evidence protocol. Restart.",
                      file=sys.stderr)
                sys.exit(1)
            try:
                idx_str, ts_str = line.split(",")
                ts = int(ts_str)
            except ValueError:
                skipped_lines += 1
                continue  # ignore garbage/partial line

            if prev_raw is None:
                monotonic = ts
                monotonic_start = ts
            else:
                delta = (ts - prev_raw) % WRAP_PERIOD_US
                if delta > WRAP_PERIOD_US - WRAP_WARN_MARGIN_US:
                    print(f"!! Gap before edge {n} is within {WRAP_WARN_MARGIN_US // 1_000_000}s "
                          "of a full timer-wrap period — the reconstructed "
                          "monotonic_timestamp_us for this edge may be wrong "
                          "(possible missed multi-wrap gap). Inspect this run "
                          "before trusting it.", file=sys.stderr)
                monotonic += delta
                diffs_us.append(delta)
            prev_raw = ts

            writer.writerow([n, ts, monotonic])
            n += 1
            if args.stats_every and n % args.stats_every == 0:
                elapsed_us = monotonic - monotonic_start
                cpm = 60e6 * len(diffs_us) / elapsed_us if elapsed_us > 0 else float("nan")
                verdict = poisson_status(diffs_us) if len(diffs_us) >= 10 else "n/a (too few samples yet)"
                print(f"{n}/{args.count} edges captured — "
                      f"~{cpm:.2f} CPM, poisson check: {verdict}")

    print(f"Done. {n} edges written to {args.out}")
    if skipped_lines:
        print(f"Note: {skipped_lines} malformed/partial serial line(s) were "
              "skipped during this capture — check the link if this number "
              "is large.", file=sys.stderr)


if __name__ == "__main__":
    main()
