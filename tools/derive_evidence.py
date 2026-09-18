#!/usr/bin/env python3
"""
derive_evidence.py

Implements the offline reconstruction defined in Section 5 of
entropy32_evidence_protocol.md, turning a raw_edges.csv capture into the
protocol's `derived/` package:

  all_intervals_us.csv
      every consecutive raw D2 inter-arrival time, unfiltered — no
      modulo, low-byte extraction, bucketing or truncation (Section 7).

  firmware_accepted_intervals_us.csv
      intervals surviving Entropy32's current 200us reject filter.
      Rejecting an interval does NOT advance the last-accepted-edge
      reference — the next interval is measured from the last ACCEPTED
      edge, not the rejected one, matching geigerISR()'s actual
      behavior (Section 5).

  comparison_bits.bin
      one byte per bit (0x00/0x01), from non-overlapping pairs of
      accepted intervals: second-longer=1, second-shorter=0, ties
      discarded (no bit emitted).

  derivation_report.json
      the reconciling counts for all of the above.

comparison_bits.bin is meant for the protocol's pinned NIST toolchain
(SP800-90B_EntropyAssessment v1.1.8):

    ./ea_non_iid -i -v comparison_bits.bin 1

Note bits_per_symbol=1 here — NOT 8. This is not a byte-per-symbol
truncation of the interval magnitude; Section 7 explicitly identifies
that kind of reduction as needing a separate, documented justification
this protocol doesn't provide.

Usage:
    python derive_evidence.py raw_edges.csv --out-dir derived/
"""
import argparse
import csv
import json
import os
import sys

MIN_INTERVAL_US = 200


def edge_times(path):
    with open(path, newline="") as f:
        return [int(row["monotonic_timestamp_us"]) for row in csv.DictReader(f)]


def derive(times):
    """One pass of Section 5's reconstruction over the capture's edge
    timestamps (already monotonic — see capture_serial_to_csv.py).
    Returns (all_intervals, accepted_intervals, comparison_bits, counts)."""
    all_intervals = []
    accepted_intervals = []
    comparison_bits = []
    counts = dict(short_interval_rejections=0, complete_pairs=0, ties=0,
                  comparison_zeroes=0, comparison_ones=0, dangling_unpaired=0)

    last_accepted = None
    have_first = False
    first_interval = None

    for i, t in enumerate(times):
        if i == 0:
            last_accepted = t
            continue
        all_intervals.append(t - times[i - 1])

        interval = t - last_accepted
        if interval < MIN_INTERVAL_US:
            counts["short_interval_rejections"] += 1
            continue  # do NOT update last_accepted
        accepted_intervals.append(interval)
        last_accepted = t

        if not have_first:
            first_interval = interval
            have_first = True
        else:
            second_interval = interval
            counts["complete_pairs"] += 1
            if second_interval > first_interval:
                comparison_bits.append(1)
                counts["comparison_ones"] += 1
            elif second_interval < first_interval:
                comparison_bits.append(0)
                counts["comparison_zeroes"] += 1
            else:
                counts["ties"] += 1
            have_first = False

    if have_first:
        counts["dangling_unpaired"] = 1

    return all_intervals, accepted_intervals, comparison_bits, counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("csv_path", help="raw_edges.csv from capture_serial_to_csv.py")
    p.add_argument("--out-dir", default="derived",
                    help="directory for the derived/ package (default: ./derived)")
    args = p.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    times = edge_times(args.csv_path)
    all_intervals, accepted_intervals, comparison_bits, counts = derive(times)
    print(f"{args.csv_path}: {len(times)} edge(s) -> {len(all_intervals)} raw "
          f"interval(s), {len(accepted_intervals)} accepted, "
          f"{len(comparison_bits)} comparison bit(s)")

    all_path = os.path.join(args.out_dir, "all_intervals_us.csv")
    with open(all_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["interval_us"])
        w.writerows([v] for v in all_intervals)

    accepted_path = os.path.join(args.out_dir, "firmware_accepted_intervals_us.csv")
    with open(accepted_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["interval_us"])
        w.writerows([v] for v in accepted_intervals)

    bits_path = os.path.join(args.out_dir, "comparison_bits.bin")
    with open(bits_path, "wb") as f:
        f.write(bytes(comparison_bits))

    report = {
        "min_interval_us": MIN_INTERVAL_US,
        "pairing": "NON_OVERLAPPING",
        "tie_rule": "DISCARD_PAIR",
        "bit_rule": "SECOND_LONGER=1;SECOND_SHORTER=0",
        "raw_edges": len(times),
        "unfiltered_intervals": len(all_intervals),
        "short_interval_rejections": counts["short_interval_rejections"],
        "accepted_intervals": len(accepted_intervals),
        "complete_pairs": counts["complete_pairs"],
        "ties": counts["ties"],
        "comparison_zeroes": counts["comparison_zeroes"],
        "comparison_ones": counts["comparison_ones"],
        "comparison_bits": len(comparison_bits),
        "dangling_unpaired_intervals": counts["dangling_unpaired"],
    }
    report_path = os.path.join(args.out_dir, "derivation_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    print(f"\nWrote {all_path}, {accepted_path}, {bits_path}, {report_path}")
    print(json.dumps(report, indent=2))

    expected_accepted = 2 * report["complete_pairs"] + report["dangling_unpaired_intervals"]
    if expected_accepted != report["accepted_intervals"]:
        print(f"\n!! Reconciliation mismatch: 2*complete_pairs + "
              f"dangling_unpaired = {expected_accepted}, but "
              f"accepted_intervals = {report['accepted_intervals']}. "
              "This should never happen — inspect before trusting the "
              "output.", file=sys.stderr)
        sys.exit(1)

    if report["comparison_bits"] < 1_000_000:
        print(f"\nNote: {report['comparison_bits']} comparison bits is "
              "below the protocol's 1,000,000-sample stop gate "
              "(Section 3) — keep capturing.", file=sys.stderr)


if __name__ == "__main__":
    main()
