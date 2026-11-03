#!/usr/bin/env python3
"""
verify_evidence.py

Independently checks an assembled entropy32_sp80090b_<capture-id>/
package (entropy32_evidence_protocol.md Section 4) - the one command a
replicator needs, and the last step before committing a new package.

Checks, in order:

  layout        every file Section 4 requires is present
  checksums     every file matches SHA256SUMS, and every file is listed
  manifest      manifest.json agrees with capture_status.json,
                derivation_report.json, nist/TOOLCHAIN.txt and the raw
                file's own hash/row count; the capture is COMPLETE with
                zero overruns and zero transport drops (Section 2)
  raw           edge_index runs 0,1,2,... with no gaps; the monotonic
                timestamp only increases; every monotonic step equals the
                raw_timer_ticks step mod 2**32 (the overflow extension the
                manifest declares)
  derivation    re-runs derive_evidence.py's Section 5 reconstruction from
                raw/raw_edges.csv in a scratch directory and requires the
                derived files to match byte for byte, and the report's
                counts to match and reconcile exactly
  nist          the saved ea_non_iid run read this package's
                comparison_bits.bin (by SHA-256) with the right sample
                count, exited 0, reported all ten non-IID estimators, and
                H_original is their minimum (Section 8)
  stop gate     whether the package reaches Section 3's 1,000,000-sample
                gate - below it, the package is a pilot, not evidence for a
                min-entropy claim

Optional, slower or needing extra checkouts:

  --firmware-dir ../entropy32_plus
      checks the Entropy32 firmware the derivation claims to match
      (entropy32_firmware_blob_sha, source_commit)
  --ea-non-iid PATH
      re-runs NIST's ea_non_iid against the package's comparison_bits.bin
      and requires its estimator output to match the saved stdout line for
      line. On Windows a POSIX path (e.g. ~/SP800-90B_EntropyAssessment/
      cpp/ea_non_iid) is run inside WSL automatically.

Exit status is 0 if nothing FAILed, 1 otherwise. WARN lines don't fail
the package but are worth reading.

Usage:
    python tools/verify_evidence.py entropy32_sp80090b_2026-10-03a
    python tools/verify_evidence.py entropy32_sp80090b_2026-10-03a \\
        --firmware-dir ../entropy32_plus \\
        --ea-non-iid ~/SP800-90B_EntropyAssessment/cpp/ea_non_iid
"""
import argparse
import csv
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from derive_evidence import git_output, sha256_file, write_derived  # noqa: E402
from edge_timing import near_wrap, wrap_delta  # noqa: E402

PINNED_NIST_REPO = "usnistgov/SP800-90B_EntropyAssessment"
PINNED_NIST_VERSION = "v1.1.8"
PINNED_NIST_COMMIT = "68ed165fd7a3eeef26b87a546ba23f338e82a3f3"
STOP_GATE = 1_000_000

REQUIRED_FILES = [
    "manifest.json",
    "capture_status.json",
    "SHA256SUMS",
    "raw/raw_edges.csv",
    "derived/all_intervals_us.csv",
    "derived/firmware_accepted_intervals_us.csv",
    "derived/comparison_bits.bin",
    "derived/derivation_report.json",
    "nist/TOOLCHAIN.txt",
    "nist/comparison_non_iid.command.txt",
    "nist/comparison_non_iid.stdout.txt",
    "nist/comparison_non_iid.stderr.txt",
    "nist/comparison_non_iid.exit_status.txt",
]
DERIVED_BYTE_EXACT = [
    "all_intervals_us.csv",
    "firmware_accepted_intervals_us.csv",
    "comparison_bits.bin",
]
SOURCE_KEYS = ["source_repo", "source_commit", "entropy32_firmware_blob_sha"]

# The ten estimators SP 800-90B requires for non-IID data (Section 8), as
# ea_non_iid -v names them in its "<name> = <value> / 1 bit(s)" lines.
NIST_ESTIMATORS = [
    "Most Common Value Estimate",
    "Collision Test Estimate",
    "Markov Test Estimate",
    "Compression Test Estimate",
    "T-Tuple Test Estimate",
    "LRS Test Estimate",
    "Multi Most Common in Window (MultiMCW) Prediction Test Estimate",
    "Lag Prediction Test Estimate",
    "Multi Markov Model with Counting (MultiMMC) Prediction Test Estimate",
    "LZ78Y Prediction Test Estimate",
]
OPENING_RE = re.compile(r"^Opening file: '(.*)' \(SHA-256 hash ([0-9a-f]{64})\)$")
LOADED_RE = re.compile(r"^Loaded (\d+) samples of (\d+) distinct (\d+)-bit-wide symbols$")
ESTIMATE_RE = re.compile(r"^\s*(.+?) = ([0-9.]+) / (\d+) bit\(s\)$")
H_ORIGINAL_RE = re.compile(r"^H_original: ([0-9.]+)$")
SKIPPED_RE = re.compile(r"\*\*\* Warning: not enough samples to run (.+?) test")


class Report:
    def __init__(self):
        self.failed = False
        self.warned = False

    def _line(self, level, section, msg):
        print(f"  {level:<4}  {section:<10}  {msg}")

    def ok(self, section, msg):
        self._line("PASS", section, msg)

    def info(self, section, msg):
        self._line("INFO", section, msg)

    def warn(self, section, msg):
        self.warned = True
        self._line("WARN", section, msg)

    def fail(self, section, msg):
        self.failed = True
        self._line("FAIL", section, msg)

    def check(self, cond, section, ok_msg, fail_msg):
        (self.ok if cond else self.fail)(section, ok_msg if cond else fail_msg)
        return cond


def load_json(path):
    with open(path) as f:
        return json.load(f)


def read_text(path):
    with open(path, newline="") as f:
        return f.read()


def check_layout(pkg, r):
    missing = [rel for rel in REQUIRED_FILES if not os.path.isfile(os.path.join(pkg, rel))]
    r.check(not missing, "layout",
            f"all {len(REQUIRED_FILES)} required files present",
            f"missing: {', '.join(missing)}")
    return not missing


def check_checksums(pkg, r):
    text = read_text(os.path.join(pkg, "SHA256SUMS"))
    if "\r" in text:
        # Packages assembled before package_evidence.py wrote LF have
        # CRLF here; `sha256sum -c` on Linux/macOS can't parse that
        # (use `tr -d '\r' < SHA256SUMS | sha256sum -c`), but the digests
        # themselves are still valid.
        r.warn("checksums", "SHA256SUMS has CRLF line endings (older package); "
                            "parsed tolerantly")
    listed = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, _sep, rel = line.partition("  ")
        listed[rel.lstrip("*")] = digest

    bad = [rel for rel, digest in listed.items()
           if not os.path.isfile(os.path.join(pkg, rel))
           or sha256_file(os.path.join(pkg, rel)) != digest]
    r.check(not bad, "checksums",
            f"{len(listed)} file(s) match SHA256SUMS",
            f"mismatched or missing: {', '.join(bad)}")

    on_disk = set()
    for root, _dirs, files in os.walk(pkg):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), pkg).replace(os.sep, "/")
            if rel != "SHA256SUMS":
                on_disk.add(rel)
    unlisted = sorted(on_disk - set(listed))
    r.check(not unlisted, "checksums",
            "every file in the package is covered by SHA256SUMS",
            f"not covered by SHA256SUMS: {', '.join(unlisted)}")


def check_raw(pkg, r):
    """Section 2's structural requirements on the primary record. Returns
    the monotonic timestamps (for re-derivation) or None if unreadable."""
    path = os.path.join(pkg, "raw", "raw_edges.csv")
    expected_header = ["edge_index", "raw_timer_ticks", "monotonic_timestamp_us"]
    times = []
    index_errors = step_errors = non_increasing = near_wraps = 0
    first_bad = None
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not r.check(header == expected_header, "raw",
                       "header is edge_index,raw_timer_ticks,monotonic_timestamp_us",
                       f"unexpected header {header!r}"):
            return None
        prev_raw = prev_mono = None
        for n, row in enumerate(reader):
            try:
                idx, raw, mono = (int(v) for v in row)
            except ValueError:
                r.fail("raw", f"row {n + 2} is not three integers: {row!r}")
                return None
            if idx != n:
                index_errors += 1
                first_bad = first_bad or f"row {n + 2}: edge_index {idx}, expected {n}"
            if prev_mono is not None:
                step = mono - prev_mono
                if step <= 0:
                    non_increasing += 1
                    first_bad = first_bad or f"row {n + 2}: timestamp did not increase"
                delta = wrap_delta(raw, prev_raw)
                if step != delta:
                    step_errors += 1
                    first_bad = first_bad or (f"row {n + 2}: monotonic step {step} != "
                                              f"raw tick step {delta} mod 2**32")
                if near_wrap(delta):
                    near_wraps += 1
            prev_raw, prev_mono = raw, mono
            times.append(mono)

    r.check(index_errors == 0, "raw",
            f"edge_index runs 0..{len(times) - 1} with no gaps",
            f"{index_errors} edge_index discontinuit(ies), first at {first_bad}")
    r.check(non_increasing == 0 and step_errors == 0, "raw",
            "monotonic timestamps increase and match the raw tick steps mod 2**32",
            f"{non_increasing} non-increasing step(s), {step_errors} step(s) "
            f"inconsistent with raw_timer_ticks; first: {first_bad}")
    if near_wraps:
        r.warn("raw", f"{near_wraps} gap(s) within 5 min of a full 2**32 us timer "
                      "wrap - a second wrap inside such a gap can't be ruled out")
    return times


def check_manifest(pkg, manifest, status, report, raw_rows, r):
    sec = "manifest"
    expected_dir = f"entropy32_sp80090b_{manifest.get('capture_id')}"
    if os.path.basename(os.path.normpath(pkg)) != expected_dir:
        r.warn(sec, f"directory name doesn't match capture_id (expected {expected_dir})")

    for rel, digest in (manifest.get("file_sha256") or {}).items():
        path = os.path.join(pkg, rel)
        r.check(os.path.isfile(path) and sha256_file(path) == digest, sec,
                f"file_sha256[{rel}] matches",
                f"file_sha256[{rel}] does not match the file")

    counts = {
        "raw/raw_edges.csv rows": raw_rows,
        "manifest raw_edge_count": manifest.get("raw_edge_count"),
        "capture_status raw_edge_count": status.get("raw_edge_count"),
        "derivation_report raw_edges": report.get("raw_edges"),
    }
    r.check(len(set(counts.values())) == 1, sec,
            f"raw edge count agrees everywhere ({raw_rows})",
            "raw edge counts disagree: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    valid = (status.get("capture_status") == "COMPLETE"
             and status.get("buffer_overrun_count") == 0
             and status.get("transport_drop_count") == 0)
    r.check(valid, sec,
            "capture COMPLETE, 0 buffer overruns, 0 transport drops",
            f"capture_status.json does not show a valid capture (Section 2: "
            f"CAPTURE_INVALID): {status}")

    pairs = [
        ("capture_status", "capture_status", status),
        ("buffer_overruns", "buffer_overrun_count", status),
        ("transport_drops", "transport_drop_count", status),
        ("capture_start_utc", "capture_start_utc", status),
        ("capture_end_utc", "capture_end_utc", status),
        ("repository", "source_repo", report),
        ("repository_commit", "source_commit", report),
        ("entropy32_firmware_blob_sha", "entropy32_firmware_blob_sha", report),
        ("MIN_INTERVAL_US", "min_interval_us", report),
        ("pairing", "pairing", report),
        ("tie_rule", "tie_rule", report),
    ]
    mismatched = [f"{mk}={manifest.get(mk)!r} vs {sk}={src.get(sk)!r}"
                  for mk, sk, src in pairs if manifest.get(mk) != src.get(sk)]
    r.check(not mismatched, sec,
            "agrees with capture_status.json and derivation_report.json",
            "disagrees: " + "; ".join(mismatched))

    toolchain = {}
    for line in read_text(os.path.join(pkg, "nist", "TOOLCHAIN.txt")).splitlines():
        key, _sep, value = line.partition(": ")
        toolchain[key.strip()] = value.strip()
    nist_mismatch = [k for k in ("nist_tool_repository", "nist_tool_version", "nist_tool_commit")
                     if manifest.get(k) != toolchain.get(k)]
    r.check(not nist_mismatch, sec,
            "NIST tool identity agrees with nist/TOOLCHAIN.txt",
            f"NIST tool fields disagree with TOOLCHAIN.txt: {', '.join(nist_mismatch)}")
    if (toolchain.get("nist_tool_repository"), toolchain.get("nist_tool_commit")) != \
            (PINNED_NIST_REPO, PINNED_NIST_COMMIT):
        r.warn(sec, f"NIST tool is not the protocol's pinned {PINNED_NIST_REPO} "
                    f"{PINNED_NIST_VERSION} @ {PINNED_NIST_COMMIT[:12]}")

    recorder_ino = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "entropy32_recorder.ino")
    recorded = manifest.get("recorder_firmware_hash")
    if recorded and os.path.isfile(recorder_ino) and sha256_file(recorder_ino) != recorded:
        r.info(sec, "this checkout's entropy32_recorder.ino differs from the recorder "
                    "firmware the package records (fine if it changed after the capture)")

    nulls = [k for k, v in manifest.items() if v is None]
    if nulls:
        r.info(sec, f"{len(nulls)} field(s) recorded as unknown (null): {', '.join(nulls)}")


def check_derivation(pkg, times, report, r):
    sec = "derivation"
    c = report
    reconcile = [
        ("unfiltered_intervals == raw_edges - 1",
         c["unfiltered_intervals"] == max(c["raw_edges"] - 1, 0)),
        ("accepted_intervals == unfiltered_intervals - short_interval_rejections",
         c["accepted_intervals"] == c["unfiltered_intervals"] - c["short_interval_rejections"]),
        ("accepted_intervals == 2*complete_pairs + dangling_unpaired_intervals",
         c["accepted_intervals"] == 2 * c["complete_pairs"] + c.get("dangling_unpaired_intervals", 0)),
        ("complete_pairs == comparison_bits + ties",
         c["complete_pairs"] == c["comparison_bits"] + c["ties"]),
        ("comparison_bits == comparison_zeroes + comparison_ones",
         c["comparison_bits"] == c["comparison_zeroes"] + c["comparison_ones"]),
    ]
    broken = [name for name, ok in reconcile if not ok]
    r.check(not broken, sec,
            "derivation_report.json counts reconcile exactly",
            f"counts don't reconcile: {'; '.join(broken)}")

    bits = open(os.path.join(pkg, "derived", "comparison_bits.bin"), "rb").read()
    r.check(len(bits) == c["comparison_bits"] and set(bits) <= {0, 1}, sec,
            f"comparison_bits.bin is {len(bits)} bytes of 0x00/0x01",
            f"comparison_bits.bin has {len(bits)} bytes (report says "
            f"{c['comparison_bits']}) or contains bytes other than 0x00/0x01")

    with tempfile.TemporaryDirectory(prefix="verify_evidence_") as scratch:
        _paths, fresh = write_derived(scratch, times, *(report.get(k) for k in SOURCE_KEYS))
        differing = [name for name in DERIVED_BYTE_EXACT
                     if sha256_file(os.path.join(scratch, name))
                     != sha256_file(os.path.join(pkg, "derived", name))]
    r.check(not differing, sec,
            "re-derived from raw: interval files and comparison_bits.bin match byte for byte",
            f"re-derivation from raw differs: {', '.join(differing)}")
    # Compared as values, not bytes: older reports were written with the
    # platform's line endings.
    changed = [k for k in fresh if k not in SOURCE_KEYS and fresh[k] != report.get(k)]
    r.check(not changed, sec,
            "re-derived counts match derivation_report.json",
            "re-derived counts differ: " + ", ".join(
                f"{k} {report.get(k)!r} -> {fresh[k]!r}" for k in changed))


def check_firmware(firmware_dir, report, r):
    """The .ino at the commit the derivation claims - read from git, so
    the checkout can be on any branch/commit."""
    sec = "firmware"
    commit = report.get("source_commit")
    inos = glob.glob(os.path.join(firmware_dir, "*.ino"))
    if len(inos) != 1:
        r.fail(sec, f"expected exactly one .ino in {firmware_dir}, found {len(inos)}")
        return
    name = os.path.basename(inos[0])
    try:
        blob = subprocess.run(["git", "show", f"{commit}:{name}"], cwd=firmware_dir,
                              capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        r.fail(sec, f"{firmware_dir} has no commit {commit} with {name} "
                    "(fetch it, or point at the right repository)")
        return
    r.check(hashlib.sha256(blob).hexdigest() == report.get("entropy32_firmware_blob_sha"), sec,
            f"{name} at {commit[:12]} matches entropy32_firmware_blob_sha",
            f"{name} at {commit[:12]} does not match entropy32_firmware_blob_sha - "
            "the derivation's filter/pairing rules may not be this firmware's")
    remote = git_output(["remote", "get-url", "origin"], firmware_dir)
    if remote != report.get("source_repo"):
        r.warn(sec, f"checkout's origin is {remote}, the derivation claims "
                    f"{report.get('source_repo')}")


def parse_nist_stdout(text):
    parsed = {"estimates": {}, "skipped": []}
    for line in text.splitlines():
        if m := SKIPPED_RE.search(line):
            parsed["skipped"].append(m.group(1).lower())
        elif m := OPENING_RE.match(line):
            parsed["sha256"] = m.group(2)
        elif m := LOADED_RE.match(line):
            parsed["samples"], parsed["symbols"], parsed["bit_width"] = map(int, m.groups())
        elif m := H_ORIGINAL_RE.match(line):
            parsed["h_original"] = float(m.group(1))
        elif m := ESTIMATE_RE.match(line):
            parsed["estimates"][m.group(1)] = float(m.group(2))
    return parsed


def check_nist(pkg, report, r):
    sec = "nist"
    nist = os.path.join(pkg, "nist")
    stdout = read_text(os.path.join(nist, "comparison_non_iid.stdout.txt"))
    parsed = parse_nist_stdout(stdout)
    bits_sha = sha256_file(os.path.join(pkg, "derived", "comparison_bits.bin"))

    exit_status = read_text(os.path.join(nist, "comparison_non_iid.exit_status.txt")).strip()
    r.check(exit_status == "0", sec, "ea_non_iid exited 0", f"ea_non_iid exit status {exit_status!r}")
    if read_text(os.path.join(nist, "comparison_non_iid.stderr.txt")).strip():
        r.warn(sec, "ea_non_iid wrote to stderr - read nist/comparison_non_iid.stderr.txt")

    command = read_text(os.path.join(nist, "comparison_non_iid.command.txt")).split()
    if not (command and command[0].endswith("ea_non_iid") and "-i" in command
            and command[-1] == "1"):
        r.warn(sec, f"recorded command isn't `ea_non_iid -i ... <file> 1`: {' '.join(command)}")

    r.check(parsed.get("sha256") == bits_sha, sec,
            "NIST read this package's comparison_bits.bin (SHA-256 matches)",
            f"NIST read a file with SHA-256 {parsed.get('sha256')}, not this "
            f"package's comparison_bits.bin ({bits_sha})")
    r.check(parsed.get("samples") == report["comparison_bits"] and parsed.get("bit_width") == 1,
            sec,
            f"NIST loaded {parsed.get('samples')} 1-bit samples",
            f"NIST loaded {parsed.get('samples')} samples of width "
            f"{parsed.get('bit_width')}, expected {report['comparison_bits']} 1-bit")

    missing = [e for e in NIST_ESTIMATORS if e not in parsed["estimates"]]
    # ea_non_iid skips an estimator outright (with a warning) when the
    # dataset is too small for it - expected for a pilot, but Section 8
    # needs all ten, so only tolerated below the stop gate.
    skipped = [e for e in missing if any(s in e.lower() for s in parsed["skipped"])]
    unexplained = [e for e in missing if e not in skipped]
    if unexplained:
        r.fail(sec, f"estimator(s) missing from stdout: {', '.join(unexplained)}")
    elif skipped and report["comparison_bits"] >= STOP_GATE:
        r.fail(sec, f"NIST skipped {', '.join(skipped)} for too few samples")
    elif skipped:
        r.warn(sec, f"NIST skipped {', '.join(skipped)} for too few samples - "
                    f"H_original covers {len(NIST_ESTIMATORS) - len(skipped)} of the "
                    "ten estimators Section 8 requires")
    else:
        r.ok(sec, "all ten non-IID estimators reported")
    if parsed["estimates"] and not unexplained:
        lowest = min(parsed["estimates"][e] for e in NIST_ESTIMATORS if e in parsed["estimates"])
        h = parsed.get("h_original")
        r.check(h is not None and abs(h - lowest) < 1e-6, sec,
                f"H_original = {h} is the minimum estimator",
                f"H_original {h} is not the minimum estimator ({lowest})")
    return parsed


def nist_comparable_lines(text):
    """ea_non_iid's stdout minus the 'Opening file' line, whose path
    differs between machines (its SHA-256 is checked separately)."""
    return [line for line in text.splitlines() if not OPENING_RE.match(line)]


def rerun_nist(pkg, ea_non_iid, r):
    sec = "nist rerun"
    bits = os.path.abspath(os.path.join(pkg, "derived", "comparison_bits.bin"))
    cmd = [ea_non_iid, "-i", "-v", bits, "1"]
    if sys.platform == "win32" and ea_non_iid.startswith(("/", "~")):
        # ea_non_iid is a Linux build: run it inside WSL, with the bits
        # file's path translated to WSL's /mnt/<drive>/ view.
        wsl_bits = subprocess.run(["wsl", "-e", "wslpath", "-a", bits],
                                  capture_output=True, text=True).stdout.strip()
        cmd = ["wsl", "-e", "sh", "-c", f'exec {ea_non_iid} -i -v "$0" 1', wsl_bits]
    print(f"  ....  {sec:<10}  running {' '.join(cmd)} (can take several minutes)")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        r.fail(sec, f"could not run ea_non_iid: {e}")
        return
    if not r.check(proc.returncode == 0, sec, "ea_non_iid exited 0",
                   f"ea_non_iid exited {proc.returncode}: {proc.stderr.strip()[:300]}"):
        return
    saved = read_text(os.path.join(pkg, "nist", "comparison_non_iid.stdout.txt"))
    fresh_lines = nist_comparable_lines(proc.stdout)
    saved_lines = nist_comparable_lines(saved)
    diff = next((i for i, (a, b) in enumerate(zip(fresh_lines, saved_lines)) if a != b), None)
    if diff is None and len(fresh_lines) != len(saved_lines):
        diff = min(len(fresh_lines), len(saved_lines))
    r.check(diff is None, sec,
            "fresh run's estimator output matches the saved stdout line for line",
            f"fresh run differs from saved stdout from line {diff}: "
            f"{(fresh_lines + [''])[diff]!r} vs {(saved_lines + [''])[diff]!r}"
            if diff is not None else "")
    r.info(sec, "this checks the result, not which ea_non_iid build you ran - "
                f"use the pinned {PINNED_NIST_VERSION} @ {PINNED_NIST_COMMIT[:12]}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("package", help="an entropy32_sp80090b_<capture-id>/ directory")
    p.add_argument("--firmware-dir",
                   help="local checkout of the Entropy32 firmware the derivation "
                        "claims to match (e.g. ../entropy32_plus)")
    p.add_argument("--ea-non-iid",
                   help="path to a built ea_non_iid to re-run NIST and compare "
                        "(POSIX paths run under WSL on Windows)")
    args = p.parse_args()

    pkg = args.package
    if not os.path.isdir(pkg):
        p.error(f"{pkg} is not a directory")
    print(f"Verifying {pkg}\n")
    r = Report()

    if not check_layout(pkg, r):
        print("\nFAILED - required files are missing; nothing further checked.")
        sys.exit(1)
    check_checksums(pkg, r)

    manifest = load_json(os.path.join(pkg, "manifest.json"))
    status = load_json(os.path.join(pkg, "capture_status.json"))
    report = load_json(os.path.join(pkg, "derived", "derivation_report.json"))

    times = check_raw(pkg, r)
    check_manifest(pkg, manifest, status, report, len(times) if times is not None else None, r)
    if times is not None:
        check_derivation(pkg, times, report, r)
    if args.firmware_dir:
        check_firmware(args.firmware_dir, report, r)
    parsed = check_nist(pkg, report, r)
    if args.ea_non_iid:
        rerun_nist(pkg, args.ea_non_iid, r)

    below_gate = [f"{name} {value:,}" for name, value in
                  (("unfiltered intervals", report["unfiltered_intervals"]),
                   ("comparison bits", report["comparison_bits"]))
                  if value < STOP_GATE]
    if below_gate:
        r.warn("stop gate", f"below Section 3's {STOP_GATE:,} gate: {', '.join(below_gate)}")
    else:
        r.ok("stop gate", f"meets Section 3's {STOP_GATE:,}-sample gate")

    print()
    if r.failed:
        print("FAILED - see the FAIL lines above.")
        sys.exit(1)
    h = parsed.get("h_original")
    if below_gate:
        print(f"VERIFIED as a PILOT package (H_original {h}) - internally consistent "
              "and reproducible, but below the stop gate, so not evidence for a "
              "min-entropy claim.")
    else:
        print(f"VERIFIED (H_original {h}).")


if __name__ == "__main__":
    main()
