#!/usr/bin/env python3
"""
package_evidence.py

Assembles the protocol's immutable evidence directory (Section 4) from
the outputs of a capture + derive_evidence.py run + a manually-run NIST
ea_non_iid pass:

  entropy32_sp80090b_<capture-id>/
    manifest.json
    capture_status.json
    SHA256SUMS
    raw/raw_edges.csv
    derived/...
    nist/
      TOOLCHAIN.txt
      comparison_non_iid.command.txt
      comparison_non_iid.stdout.txt
      comparison_non_iid.stderr.txt
      comparison_non_iid.exit_status.txt

This does NOT run ea_non_iid itself (it's a Linux/WSL build, not portable
from here) - run it separately, save its stdout/stderr to files, and pass
those in along with the exact command line and exit status.

manifest.json is only filled in where this script can know the value
without guessing: capture timing/counts (from capture_status.json),
firmware provenance (from derivation_report.json's source_* fields, if
derive_evidence.py was run with --source-dir), and the NIST toolchain
identity given on this command line. Every other manifest field the
protocol calls for (hardware revision, physical source, environment,
compiler/core versions, ...) is written as null and listed at the end
of this script's output - fill those in by hand before treating the
package as complete, per Section 10's "unknown values are written as
null or unknown, not guessed."

Usage:
    python package_evidence.py \\
        --raw raw_edges.csv --derived-dir derived/ --capture-id 2026-09-19a \\
        --nist-command "./ea_non_iid -i -v comparison_bits.bin 1" \\
        --nist-stdout nist_stdout.txt --nist-stderr nist_stderr.txt \\
        --nist-exit-status 0 --nist-compiler-version "g++ 13.3.0" \\
        --out-dir entropy32_sp80090b_2026-09-19a/
"""
import argparse
import hashlib
import json
import os
import shutil
import sys

PINNED_NIST_REPO = "usnistgov/SP800-90B_EntropyAssessment"
PINNED_NIST_VERSION = "v1.1.8"
PINNED_NIST_COMMIT = "68ed165fd7a3eeef26b87a546ba23f338e82a3f3"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def default_status_path(raw_path):
    root, _ext = os.path.splitext(raw_path)
    return root + ".status.json"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--raw", required=True, help="raw_edges.csv from capture_serial_to_csv.py")
    p.add_argument("--derived-dir", required=True, help="derived/ dir from derive_evidence.py")
    p.add_argument("--capture-status",
                    help="capture_status.json from capture_serial_to_csv.py "
                         "(default: guessed from --raw's filename)")
    p.add_argument("--capture-id", required=True,
                    help="identifier for this run, used to name --out-dir by default")
    p.add_argument("--nist-command", required=True,
                    help="the exact command line used to run ea_non_iid")
    p.add_argument("--nist-stdout", required=True, help="path to ea_non_iid's saved stdout")
    p.add_argument("--nist-stderr", required=True, help="path to ea_non_iid's saved stderr")
    p.add_argument("--nist-exit-status", required=True, type=int)
    p.add_argument("--nist-tool-repo", default=PINNED_NIST_REPO)
    p.add_argument("--nist-tool-version", default=PINNED_NIST_VERSION)
    p.add_argument("--nist-tool-commit", default=PINNED_NIST_COMMIT)
    p.add_argument("--nist-compiler-version",
                    help="compiler used to build ea_non_iid, e.g. "
                         "\"g++ (Ubuntu 13.3.0-...) 13.3.0\" - goes in "
                         "nist/TOOLCHAIN.txt, NOT manifest.json's top-level "
                         "compiler_version (that field is the recorder "
                         "firmware's own toolchain, a separate unknown)")
    p.add_argument("--out-dir", help="default: entropy32_sp80090b_<capture-id>/")
    args = p.parse_args()

    out_dir = args.out_dir or f"entropy32_sp80090b_{args.capture_id}"
    if os.path.exists(out_dir):
        p.error(f"{out_dir} already exists — this package is meant to be "
                "immutable once assembled; choose a new --out-dir or --capture-id.")

    status_path = args.capture_status or default_status_path(args.raw)
    capture_status = None
    if os.path.exists(status_path):
        with open(status_path) as f:
            capture_status = json.load(f)
    else:
        print(f"!! No capture_status.json found at {status_path} - this raw "
              "capture predates the field, or came from somewhere other than "
              "capture_serial_to_csv.py. Packaging will continue, but "
              "manifest.json's capture timing/loss fields will be null.",
              file=sys.stderr)

    derivation_report_path = os.path.join(args.derived_dir, "derivation_report.json")
    with open(derivation_report_path) as f:
        derivation_report = json.load(f)

    os.makedirs(os.path.join(out_dir, "raw"))
    os.makedirs(os.path.join(out_dir, "derived"))
    os.makedirs(os.path.join(out_dir, "nist"))

    shutil.copy2(args.raw, os.path.join(out_dir, "raw", "raw_edges.csv"))
    for name in os.listdir(args.derived_dir):
        src = os.path.join(args.derived_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out_dir, "derived", name))

    if capture_status is not None:
        shutil.copy2(status_path, os.path.join(out_dir, "capture_status.json"))
    else:
        with open(os.path.join(out_dir, "capture_status.json"), "w") as f:
            json.dump({"capture_status": "unknown"}, f, indent=2)
            f.write("\n")

    recorder_ino = os.path.join(os.path.dirname(__file__), "..", "entropy32_recorder.ino")
    recorder_firmware_hash = sha256_file(recorder_ino) if os.path.exists(recorder_ino) else None

    toolchain_lines = [
        f"nist_tool_repository: {args.nist_tool_repo}",
        f"nist_tool_version: {args.nist_tool_version}",
        f"nist_tool_commit: {args.nist_tool_commit}",
        f"compiler_version: {args.nist_compiler_version or 'unknown'}",
    ]
    with open(os.path.join(out_dir, "nist", "TOOLCHAIN.txt"), "w") as f:
        f.write("\n".join(toolchain_lines) + "\n")
    with open(os.path.join(out_dir, "nist", "comparison_non_iid.command.txt"), "w") as f:
        f.write(args.nist_command + "\n")
    shutil.copy2(args.nist_stdout, os.path.join(out_dir, "nist", "comparison_non_iid.stdout.txt"))
    shutil.copy2(args.nist_stderr, os.path.join(out_dir, "nist", "comparison_non_iid.stderr.txt"))
    with open(os.path.join(out_dir, "nist", "comparison_non_iid.exit_status.txt"), "w") as f:
        f.write(str(args.nist_exit_status) + "\n")

    manifest = {
        "protocol_version": "0.1",
        "capture_id": args.capture_id,
        "capture_start_utc": (capture_status or {}).get("capture_start_utc"),
        "capture_end_utc": (capture_status or {}).get("capture_end_utc"),

        "repository": derivation_report.get("source_repo"),
        "repository_commit": derivation_report.get("source_commit"),
        "entropy32_firmware_blob_sha": derivation_report.get("entropy32_firmware_blob_sha"),

        "hardware_revision": None,
        "board_identifier": None,
        "capture_point": "post-LM393 D2 rising edge",
        "edge_polarity": "RISING",

        "geiger_counter_model": None,
        "physical_source_type": None,
        "source_identifier_if_any": None,
        "source_geometry": None,
        "source_distance": None,
        "shielding_or_enclosure": None,

        "recorder_hardware": "Entropy32 Recorder (ATmega328P, Arduino Nano form factor)",
        "recorder_firmware_hash": recorder_firmware_hash,
        "compiler_version": None,  # recorder's own toolchain - not the NIST tool's, see nist/TOOLCHAIN.txt
        "arduino_core_or_runtime_version": None,

        "clock_source": None,
        "nominal_clock_hz": None,
        "measured_clock_hz_if_known": None,
        "timestamp_resolution": None,
        "timer_width": 32,
        "overflow_extension_method": "host-side unwrap (tools/edge_timing.py): "
                                      "accumulate (raw[i]-raw[i-1]) mod 2**32 "
                                      "between consecutive edges",

        "MIN_INTERVAL_US": derivation_report.get("min_interval_us"),
        "RAW_POOL_BITS": 512,
        "pairing": derivation_report.get("pairing"),
        "tie_rule": derivation_report.get("tie_rule"),

        "ambient_temperature_if_available": None,
        "supply_voltage_if_available": None,
        "other_environment_notes": None,

        "raw_edge_count": (capture_status or {}).get("raw_edge_count", derivation_report.get("raw_edges")),
        "capture_status": (capture_status or {}).get("capture_status", "unknown"),
        "buffer_overruns": (capture_status or {}).get("buffer_overrun_count"),
        "transport_drops": (capture_status or {}).get("transport_drop_count"),

        "nist_tool_repository": args.nist_tool_repo,
        "nist_tool_version": args.nist_tool_version,
        "nist_tool_commit": args.nist_tool_commit,

        "file_sha256": {
            "raw/raw_edges.csv": sha256_file(args.raw),
        },
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    sums = []
    for root, _dirs, files in os.walk(out_dir):
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, out_dir).replace(os.sep, "/")
            sums.append((sha256_file(full), rel))
    sums.sort(key=lambda x: x[1])
    with open(os.path.join(out_dir, "SHA256SUMS"), "w") as f:
        for digest, rel in sums:
            f.write(f"{digest}  {rel}\n")

    print(f"Wrote {out_dir}/ ({len(sums)} file(s) hashed into SHA256SUMS)")

    null_fields = [k for k, v in manifest.items() if v is None]
    if null_fields:
        print("\nmanifest.json fields left null (fill in by hand - not "
              "guessed):", file=sys.stderr)
        for k in null_fields:
            print(f"  - {k}", file=sys.stderr)

    comparison_bits = derivation_report.get("comparison_bits", 0)
    if comparison_bits < 1_000_000:
        print(f"\nNote: {comparison_bits} comparison bits is below the "
              "protocol's 1,000,000-sample stop gate (Section 3) - this "
              "package documents a pilot run, not evidence ready for a "
              "min-entropy claim.", file=sys.stderr)


if __name__ == "__main__":
    main()
