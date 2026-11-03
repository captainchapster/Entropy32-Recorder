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
      the reconciling counts for all of the above, plus source_repo/
      source_commit/entropy32_firmware_blob_sha identifying the firmware
      checkout these filter/pairing/bit-rule constants are claimed to
      match (pass --source-dir, or leave unpinned as "unknown" rather
      than guessed — see Section 10).

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
import glob
import hashlib
import json
import os
import subprocess
import sys

MIN_INTERVAL_US = 200


def edge_times(path):
    with open(path, newline="") as f:
        return [int(row["monotonic_timestamp_us"]) for row in csv.DictReader(f)]


def git_output(args, cwd):
    try:
        return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                               text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_provenance(source_dir, source_repo, source_commit, firmware_blob_sha):
    """Fill in whichever of source_repo/source_commit/firmware_blob_sha
    weren't given explicitly, from a local checkout at --source-dir (the
    firmware repo this evidence run's 200us-filter/pairing/bit-rule
    constants are claimed to match). Anything that can't be determined
    stays "unknown" rather than guessed, per the evidence protocol
    (Section 10)."""
    if source_dir:
        if source_commit is None:
            source_commit = git_output(["rev-parse", "HEAD"], source_dir) or "unknown"
        if source_repo is None:
            source_repo = git_output(["remote", "get-url", "origin"], source_dir) or "unknown"
        if firmware_blob_sha is None:
            inos = glob.glob(os.path.join(source_dir, "*.ino"))
            if len(inos) == 1:
                firmware_blob_sha = sha256_file(inos[0])
    return (source_repo or "unknown", source_commit or "unknown",
            firmware_blob_sha or "unknown")


class Reconstructor:
    """Section 5's reconstruction, one edge timestamp (already monotonic —
    see capture_serial_to_csv.py) at a time. derive() runs it over a whole
    capture; capture_serial_to_csv.py runs it live, so it can stop on the
    protocol's comparison-bit gate (Section 3) using exactly these rules.
    keep=False tracks only the counts, not the interval/bit lists."""

    def __init__(self, keep=True):
        self.keep = keep
        self.all_intervals = []
        self.accepted_intervals = []
        self.comparison_bits = []
        self.counts = dict(unfiltered_intervals=0, accepted_intervals=0,
                           short_interval_rejections=0, complete_pairs=0,
                           ties=0, comparison_zeroes=0, comparison_ones=0)
        self.prev = None
        self.last_accepted = None
        self.have_first = False
        self.first_interval = None

    @property
    def comparison_bits_count(self):
        return self.counts["comparison_zeroes"] + self.counts["comparison_ones"]

    def add(self, t):
        c = self.counts
        if self.prev is None:
            self.prev = self.last_accepted = t
            return
        c["unfiltered_intervals"] += 1
        if self.keep:
            self.all_intervals.append(t - self.prev)
        self.prev = t

        interval = t - self.last_accepted
        if interval < MIN_INTERVAL_US:
            c["short_interval_rejections"] += 1
            return  # do NOT update last_accepted
        c["accepted_intervals"] += 1
        if self.keep:
            self.accepted_intervals.append(interval)
        self.last_accepted = t

        if not self.have_first:
            self.first_interval = interval
            self.have_first = True
            return
        second_interval = interval
        c["complete_pairs"] += 1
        if second_interval > self.first_interval:
            bit = 1
            c["comparison_ones"] += 1
        elif second_interval < self.first_interval:
            bit = 0
            c["comparison_zeroes"] += 1
        else:
            bit = None
            c["ties"] += 1
        if self.keep and bit is not None:
            self.comparison_bits.append(bit)
        self.have_first = False


def derive(times):
    """One pass of Section 5's reconstruction over the capture's edge
    timestamps. Returns (all_intervals, accepted_intervals,
    comparison_bits, counts)."""
    rec = Reconstructor()
    for t in times:
        rec.add(t)
    counts = dict(rec.counts, dangling_unpaired=int(rec.have_first))
    return rec.all_intervals, rec.accepted_intervals, rec.comparison_bits, counts


def write_derived(out_dir, times, source_repo, source_commit, firmware_blob_sha):
    """Run derive() over times and write the four derived/ files into
    out_dir. Shared with verify_evidence.py, which re-derives a package
    into a scratch dir through this same code and compares the bytes.
    Returns (paths, report)."""
    os.makedirs(out_dir, exist_ok=True)
    all_intervals, accepted_intervals, comparison_bits, counts = derive(times)

    all_path = os.path.join(out_dir, "all_intervals_us.csv")
    with open(all_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["interval_us"])
        w.writerows([v] for v in all_intervals)

    accepted_path = os.path.join(out_dir, "firmware_accepted_intervals_us.csv")
    with open(accepted_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["interval_us"])
        w.writerows([v] for v in accepted_intervals)

    bits_path = os.path.join(out_dir, "comparison_bits.bin")
    with open(bits_path, "wb") as f:
        f.write(bytes(comparison_bits))

    report = {
        "source_repo": source_repo,
        "source_commit": source_commit,
        "entropy32_firmware_blob_sha": firmware_blob_sha,
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
    report_path = os.path.join(out_dir, "derivation_report.json")
    # LF on every platform, like package_evidence.py's own files.
    with open(report_path, "w", newline="\n") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    return (all_path, accepted_path, bits_path, report_path), report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("csv_path", help="raw_edges.csv from capture_serial_to_csv.py")
    p.add_argument("--out-dir", default="derived",
                    help="directory for the derived/ package (default: ./derived)")
    p.add_argument("--source-dir",
                    help="path to a local checkout of the firmware this run's "
                         "200us-filter/pairing/bit-rule constants are claimed "
                         "to match (e.g. entropy32_plus/) — used to auto-fill "
                         "--source-repo/--source-commit/--firmware-blob-sha "
                         "from git and the checked-out .ino, for anything not "
                         "given explicitly")
    p.add_argument("--source-repo", help="overrides auto-detection from --source-dir")
    p.add_argument("--source-commit", help="overrides auto-detection from --source-dir")
    p.add_argument("--firmware-blob-sha", help="overrides auto-detection from --source-dir")
    args = p.parse_args()

    source_repo, source_commit, firmware_blob_sha = detect_provenance(
        args.source_dir, args.source_repo, args.source_commit, args.firmware_blob_sha)

    times = edge_times(args.csv_path)
    (all_path, accepted_path, bits_path, report_path), report = write_derived(
        args.out_dir, times, source_repo, source_commit, firmware_blob_sha)
    print(f"{args.csv_path}: {report['raw_edges']} edge(s) -> "
          f"{report['unfiltered_intervals']} raw interval(s), "
          f"{report['accepted_intervals']} accepted, "
          f"{report['comparison_bits']} comparison bit(s)")

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

    if "unknown" in (source_repo, source_commit, firmware_blob_sha):
        print("\nNote: source_repo/source_commit/entropy32_firmware_blob_sha "
              "is \"unknown\" in this report — pass --source-dir (or the "
              "individual --source-* flags) pointing at the firmware "
              "checkout these 200us-filter/pairing/bit-rule constants are "
              "claimed to match, so the report is self-verifying instead of "
              "resting on an unpinned claim.", file=sys.stderr)


if __name__ == "__main__":
    main()
