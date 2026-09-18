#!/usr/bin/env python3
"""
check_edges.py

Fast pre-flight sanity check for a raw_edges.csv capture, before spending
time running it through NIST STS / SP 800-90B tooling. Checks things a
full statistical test suite won't tell you directly:

  - monotonic_timestamp_us is actually monotonic (catches unhandled
    timer-wrap bugs, or a hand-edited/corrupted file)
  - edge_index has no gaps (catches dropped/skipped lines)
  - inter-arrival time distribution is broadly consistent with a Poisson
    process (flags comparator bounce/chatter: real decay events are
    memoryless, so a pileup of anomalously short intervals relative to
    what an exponential distribution predicts is a hardware/wiring smell,
    not a source-quality finding)
  - reports the sample count against the SP 800-90B rule of thumb
    (>= 1,000,000 samples for a real min-entropy estimate; a few thousand
    is only good for a pipeline smoke test)

Usage:
    python check_edges.py raw_edges.csv
"""
import argparse
import csv
import math
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("csv_path")
    args = p.parse_args()

    indices = []
    monotonic = []
    with open(args.csv_path, newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            indices.append(int(row["edge_index"]))
            monotonic.append(int(row["monotonic_timestamp_us"]))

    n = len(monotonic)
    if n < 2:
        print("Not enough rows to analyze.", file=sys.stderr)
        sys.exit(1)

    problems = []

    # edge_index should be a contiguous 0..n-1 sequence
    if indices != list(range(n)):
        problems.append("edge_index is not a contiguous 0..n-1 sequence "
                         "(lines were dropped, reordered, or the file was edited).")

    # monotonic_timestamp_us must never go backwards or stay flat
    non_increasing = sum(1 for a, b in zip(monotonic, monotonic[1:]) if b <= a)
    if non_increasing:
        problems.append(f"{non_increasing} row(s) where monotonic_timestamp_us "
                         "did not strictly increase — likely an unhandled timer "
                         "wrap or duplicate timestamp.")

    diffs = [b - a for a, b in zip(monotonic, monotonic[1:])]
    mean = sum(diffs) / len(diffs)
    rate_per_min = 60e6 / mean if mean > 0 else float("nan")

    print(f"Samples: {n}")
    print(f"Span: {(monotonic[-1] - monotonic[0]) / 1e6:.1f} s "
          f"({(monotonic[-1] - monotonic[0]) / 6e7:.1f} min)")
    print(f"Mean inter-arrival: {mean / 1e3:.2f} ms  (~{rate_per_min:.2f} events/min)")
    print(f"Min / max inter-arrival: {min(diffs)} us / {max(diffs)} us")

    # Compare observed vs. expected-under-Poisson counts in a few buckets.
    # A big deficit near the top of the table (fewer short gaps than
    # expected) usually means dead time; a big excess usually means bounce.
    print("\nInter-arrival distribution vs. Poisson expectation:")
    thresholds_ms = [1, 5, 10, 50, 100, 500, 1000]
    bounce_flag = False
    for t_ms in thresholds_ms:
        t_us = t_ms * 1000
        n_obs = sum(1 for d in diffs if d < t_us)
        p_exp = 1 - math.exp(-t_us / mean)
        n_exp = p_exp * len(diffs)
        ratio = n_obs / n_exp if n_exp > 0.5 else float("nan")
        flag = ""
        if n_exp >= 3 and (ratio > 2.0 or ratio < 0.3):
            flag = "  <-- deviates from Poisson expectation"
            if ratio > 2.0 and t_ms <= 10:
                bounce_flag = True
        print(f"  < {t_ms:>5} ms: observed={n_obs:<6} expected~{n_exp:.1f}{flag}")

    if bounce_flag:
        problems.append("Excess of very short (<10ms) inter-arrival times vs. "
                         "Poisson expectation — check for comparator "
                         "bounce/chatter on the LM393 output.")

    if n < 1_000_000:
        print(f"\nNote: {n} samples is fine for a pipeline smoke test, but "
              "SP 800-90B-style min-entropy estimation wants >= 1,000,000 "
              "samples before the result means anything.")

    print()
    if problems:
        print("ISSUES FOUND:")
        for p_ in problems:
            print(f"  - {p_}")
        sys.exit(1)
    else:
        print("No structural issues found.")


if __name__ == "__main__":
    main()
