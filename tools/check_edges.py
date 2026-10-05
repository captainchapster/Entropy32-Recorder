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

Detector dead time: a Geiger tube (and its comparator/pulse-shaping
circuit) cannot register a second pulse within some minimum time tau of
the last one. That makes the *detected* inter-arrival time
tau + Exponential(true decay rate), not a plain Exponential from zero —
so a naive Poisson comparison will always read as a deficit of short
gaps near tau, even on a perfectly healthy source. Pass --dead-time-ns
(e.g. a pulse width measured on an oscilloscope) to fold this into the
expectation using the standard non-paralyzable (Type I) dead-time model,
so only genuine deviations (bounce, chatter, wiring faults) get flagged.

Usage:
    python check_edges.py raw_edges.csv
    python check_edges.py raw_edges.csv --dead-time-ns 730
"""
import argparse
import csv
import math
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("csv_path")
    p.add_argument("--dead-time-ns", type=float, default=0.0,
                    help="Detector non-paralyzable dead time in nanoseconds "
                         "(e.g. a pulse width measured on an oscilloscope). "
                         "When set, the Poisson expectation is computed as "
                         "tau + Exponential(true rate) instead of a plain "
                         "exponential from zero, so dead-time-induced "
                         "deficits near tau aren't flagged as bounce/chatter. "
                         "Default 0 preserves the original uncorrected check.")
    args = p.parse_args()

    if args.dead_time_ns < 0:
        print("--dead-time-ns must not be negative.", file=sys.stderr)
        sys.exit(1)
    dead_time_us = args.dead_time_ns / 1000.0

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

    if dead_time_us >= mean:
        print(f"--dead-time-ns ({args.dead_time_ns:.0f} ns = {dead_time_us:.3f} us) "
              f"is >= the observed mean inter-arrival time ({mean:.3f} us) — "
              "that would imply a negative or infinite true event rate. "
              "Check the value (did you mean a smaller unit, or is the "
              "detector saturated?).", file=sys.stderr)
        sys.exit(1)

    # Non-paralyzable (Type I) dead-time model: a detected inter-arrival
    # time is tau + Exponential(true_rate), since the detector simply can't
    # see anything during [0, tau) after the previous pulse, and the
    # memoryless property of the Poisson process means whatever happens
    # once the detector goes live again is exponential at the *true*
    # underlying rate — not the dead-time-suppressed observed rate.
    true_mean = mean - dead_time_us  # 1 / true_rate, in us
    true_rate_per_min = 60e6 / true_mean if true_mean > 0 else float("nan")

    print(f"Samples: {n}")
    print(f"Span: {(monotonic[-1] - monotonic[0]) / 1e6:.1f} s "
          f"({(monotonic[-1] - monotonic[0]) / 6e7:.1f} min)")
    print(f"Mean inter-arrival: {mean / 1e3:.2f} ms  (~{rate_per_min:.2f} events/min observed)")
    print(f"Min / max inter-arrival: {min(diffs)} us / {max(diffs)} us")
    if dead_time_us > 0:
        print(f"Dead-time calibration: tau = {args.dead_time_ns:.0f} ns "
              f"(non-paralyzable model)")
        print(f"  Dead-time-corrected true rate: ~{true_rate_per_min:.2f} events/min "
              f"(vs {rate_per_min:.2f} events/min observed)")
        if dead_time_us < 1.0:
            print(f"  Note: tau ({args.dead_time_ns:.0f} ns) is below this capture's "
                  "1 us timestamp resolution — the correction below still applies "
                  "to the expectation math, but sub-microsecond dead-time effects "
                  "can't be directly resolved in monotonic_timestamp_us itself.")

    # Compare observed vs. expected-under-Poisson counts in a few buckets.
    # A big deficit near the top of the table (fewer short gaps than
    # expected) usually means dead time; a big excess usually means bounce.
    label = "dead-time-corrected Poisson" if dead_time_us > 0 else "Poisson"
    print(f"\nInter-arrival distribution vs. {label} expectation:")
    thresholds_ms = [1, 5, 10, 50, 100, 500, 1000]
    bounce_flag = False
    for t_ms in thresholds_ms:
        t_us = t_ms * 1000
        n_obs = sum(1 for d in diffs if d < t_us)
        if t_us <= dead_time_us:
            p_exp = 0.0
        else:
            p_exp = 1 - math.exp(-(t_us - dead_time_us) / true_mean)
        n_exp = p_exp * len(diffs)
        ratio = n_obs / n_exp if n_exp > 0.5 else float("nan")
        flag = ""
        if n_exp >= 3 and (ratio > 2.0 or ratio < 0.3):
            flag = "  <-- deviates from expectation"
            if ratio > 2.0 and t_ms <= 10:
                bounce_flag = True
        print(f"  < {t_ms:>5} ms: observed={n_obs:<6} expected~{n_exp:.1f}{flag}")

    if bounce_flag:
        problems.append("Excess of very short (<10ms) inter-arrival times vs. "
                         f"{label} expectation — check for comparator "
                         "bounce/chatter on the LM393 output.")

    if dead_time_us > 0:
        n_impossible = sum(1 for d in diffs if d < dead_time_us)
        if n_impossible:
            problems.append(f"{n_impossible} inter-arrival time(s) shorter than "
                             f"the configured dead time ({args.dead_time_ns:.0f} ns) — "
                             "the detector shouldn't be able to register two pulses "
                             "that close together under a non-paralyzable dead-time "
                             "model, so either --dead-time-ns is set too high, or "
                             "this is bounce/chatter rather than genuine dead time.")

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
