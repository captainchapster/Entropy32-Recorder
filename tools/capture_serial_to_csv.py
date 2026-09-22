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
gap (e.g. the source is extremely sparse) would be indistinguishable
from a single wrap and silently understate elapsed time, so such gaps
are flagged rather than fixed up silently.

Every row is fsync'd to disk as soon as it's written — at this event
source's rate that's essentially free, and each edge is an
irreplaceable physical event, so there's nothing to configure and
nothing to report: a row on screen is already durable.

This script intentionally refuses to run at all if --out already has
data — one capture is one continuous, uninterrupted run from edge 0, per
the evidence protocol; there is no --resume. If the capture stops for
any reason, start a fresh run under a new --out filename.

On a fully complete run only (never on an interrupted or invalidated
one — see Drop detection below), a capture_status.json sibling is
written next to --out (e.g. raw_edges.csv -> raw_edges.status.json),
recording raw_edge_count, capture_start_utc/capture_end_utc, and the
zero overrun/drop counts implied by reaching that point at all. This is
the source for the evidence protocol's capture_status.json (Section 4).

Drop detection: the board tags every edge it sends with its own
sequential index (in addition to the timestamp). This script checks that
index against how many edges it has received so far; any mismatch means
a serial line was dropped, duplicated, or reordered in transit, and any
line that fails to parse at all means a line was garbled — either way,
per the evidence protocol's dropped-event criterion, that immediately
invalidates the capture. There is no soft "skip a few bad lines and
carry on": the run aborts on the first such event, because a garbled or
missing line is indistinguishable from a genuinely lost edge, and this
protocol does not interpolate.

Requires: pip install pyserial

Usage:
    python capture_serial_to_csv.py --port /dev/ttyUSB0 --count 2000
    python capture_serial_to_csv.py --port COM5 --count 2000
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import serial

from edge_timing import WRAP_WARN_MARGIN_US, wrap_delta, near_wrap


def format_duration(seconds):
    """Compact human-readable duration, e.g. '3h 22m' or '6w 2d' — scales
    from seconds up to weeks since a full campaign can run ~6 weeks."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    if days < 7:
        return f"{days}d {hours}h"
    weeks, days = divmod(days, 7)
    return f"{weeks}w {days}d"


# Once we've heard from the board at least once, a read timeout just means
# the source hasn't fired yet (event rates are naturally variable) — not a
# wiring problem. Only nag about that once we've gone this many consecutive
# timeouts (~10 minutes) without hearing anything at all from the board.
IDLE_HEARTBEAT_TIMEOUTS = 60

# Thresholds (ms) used for the live Poisson-consistency check. Kept small —
# bounce/chatter shows up as an excess of very short inter-arrival times, and
# checking only the low end keeps the live status line to one line.
POISSON_CHECK_THRESHOLDS_MS = (1, 5, 10)


def status_path(out_path):
    """capture_status.json sits next to --out, named from it — e.g.
    raw_edges.csv -> raw_edges.status.json — so a package-assembly step
    can find and rename it into the protocol's capture_status.json."""
    root, _ext = os.path.splitext(out_path)
    return root + ".status.json"


def poisson_status(mean_us, diff_count, bounce_counts):
    """One-line verdict on whether short inter-arrival times (< 10ms) are
    consistent with a Poisson process, or flag likely comparator bounce.

    Takes running totals rather than the raw inter-arrival list so this
    can be called every edge (real captures run to >=1,000,000 edges,
    per the evidence protocol's SP 800-90B sample requirement) without
    rescanning history each time — see bounce_counts in main()."""
    if diff_count < 10:
        return "n/a (too few samples yet)"
    if mean_us <= 0:
        return "n/a"
    for t_ms in POISSON_CHECK_THRESHOLDS_MS:
        t_us = t_ms * 1000
        n_obs = bounce_counts[t_ms]
        n_exp = (1 - math.exp(-t_us / mean_us)) * diff_count
        if n_exp >= 3 and n_obs / n_exp > 2.0:
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
                    help="recompute CPM/Poisson/ETA every N edges (0 to "
                         "disable them); the edge counter itself still "
                         "updates every edge. On a live terminal this all "
                         "happens on one in-place line; when output is "
                         "redirected (no live terminal to update), a "
                         "snapshot line is logged every N edges instead")
    p.add_argument("-y", "--yes", action="store_true",
                    help="if --out already exists, overwrite and start a "
                         "fresh capture without asking for confirmation")
    args = p.parse_args()

    # --- output plumbing -------------------------------------------------
    # A single mutable flag tracks whether the in-place progress line is
    # sitting on the current row. Anything else that wants to print (a
    # status line, a warning, an error) goes through log()/warn() so it
    # always lands on a clean line instead of gluing itself onto the
    # progress counter.
    progress_open = False
    last_len = 0
    show_progress = sys.stdout.isatty()

    def log(msg=""):
        nonlocal progress_open
        if progress_open:
            sys.stdout.write("\n")
            progress_open = False
        print(msg)

    def warn(msg):
        nonlocal progress_open
        if progress_open:
            sys.stdout.write("\n")
            progress_open = False
        print(msg, file=sys.stderr)

    def die(msg):
        warn(msg)
        sys.exit(1)

    count_width = len(str(args.count))

    def show(content):
        # Overwrites the current line in place. content's length varies
        # edge to edge (the poisson verdict and ETA strings aren't fixed
        # width), so pad out to the previous line's length rather than
        # relying on a terminal clear-line escape that not every console
        # honors.
        nonlocal progress_open, last_len
        if not show_progress:
            return
        pad = max(0, last_len - len(content))
        sys.stdout.write("\r" + content + " " * pad)
        sys.stdout.flush()
        last_len = len(content)
        progress_open = True

    # CPM / Poisson check / ETA are computed here, on demand, not on every
    # edge — see compute_stats()'s call site below, which only calls this
    # every --stats-every edges. The result is cached in stats_suffix and
    # reused as-is on the edges in between, so the live line still updates
    # every edge (cheap: just the counter and percentage) without redoing
    # the CPM/Poisson/ETA math it hasn't been asked to refresh yet.
    stats_suffix = ""

    def compute_stats(n_done):
        elapsed_us = monotonic - monotonic_start
        mean_us = elapsed_us / diff_count
        cpm = 60e6 * diff_count / elapsed_us if elapsed_us > 0 else float("nan")
        verdict = poisson_status(mean_us, diff_count, bounce_counts)
        remaining = args.count - n_done
        if remaining <= 0:
            eta_str = "done"
        elif cpm and cpm > 0 and not math.isnan(cpm):
            eta_seconds = remaining / cpm * 60
            finish_time = datetime.now() + timedelta(seconds=eta_seconds)
            eta_str = (f"{format_duration(eta_seconds)} remaining, "
                       f"ETA {finish_time.strftime('%Y-%m-%d %H:%M')}")
        else:
            eta_str = "ETA: calculating"
        return f" — {cpm:.2f} CPM, poisson check: {verdict}, {eta_str}"

    def progress_line(n_done):
        pct = 100.0 * n_done / args.count
        return (f"  {n_done:>{count_width}}/{args.count} edges "
                f"({pct:5.1f}%){stats_suffix}")

    # --- pre-flight --------------------------------------------------------
    if os.path.exists(args.out):
        if args.yes:
            log(f"{args.out} already exists — overwriting (--yes given).")
        else:
            log(f"{args.out} already exists. One capture is one continuous, "
                "uninterrupted run from edge 0, so continuing it isn't an "
                "option — but it can be overwritten and restarted from "
                "edge 0 here.")
            try:
                reply = input(f"Overwrite {args.out} and start a new "
                               "capture? [y/N]: ").strip().lower()
            except EOFError:
                reply = ""
            if reply not in ("y", "yes"):
                die("Not overwriting. Choose a different --out, delete the "
                    "file yourself, or pass --yes to skip this prompt.")

    log("Entropy32 Recorder capture")
    log(f"  port     {args.port} @ {args.baud} baud")
    log(f"  output   {args.out}")
    log(f"  target   {args.count} edges")
    log()
    log(f"Waiting for device on {args.port}...")

    ser = serial.Serial(args.port, args.baud, timeout=10)
    n = 0
    prev_raw = None
    monotonic = 0
    monotonic_start = None
    diff_count = 0
    bounce_counts = {t_ms: 0 for t_ms in POISSON_CHECK_THRESHOLDS_MS}
    connected = False
    consecutive_timeouts = 0
    interrupted = False
    capture_start_utc = None
    capture_end_utc = None
    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["edge_index", "raw_timer_ticks", "monotonic_timestamp_us"])
        try:
            while n < args.count:
                raw = ser.readline()
                if not raw:
                    consecutive_timeouts += 1
                    if not connected:
                        warn("No data for 10s — check wiring/port.")
                    elif consecutive_timeouts % IDLE_HEARTBEAT_TIMEOUTS == 0:
                        idle_s = consecutive_timeouts * 10
                        warn(f"Still connected, no edge for {idle_s}s — "
                             "normal for a quiet source, not a problem by itself.")
                    continue
                if not connected:
                    log("Device connected — receiving edges.")
                connected = True
                consecutive_timeouts = 0
                line = raw.decode(errors="replace").strip()
                if not line or line == "edge_index,timestamp_us":
                    continue
                if line == "OVERRUN_DETECTED":
                    die("!! Board reported a dropped edge — this capture is "
                        "INVALID per the evidence protocol. Restart with a "
                        f"fresh --out; the {n} edge(s) already written to "
                        f"{args.out} cannot be used.")
                try:
                    idx_str, ts_str = line.split(",")
                    idx = int(idx_str)
                    ts = int(ts_str)
                except ValueError:
                    die(f"!! Unparseable serial line: {line!r} — a garbled "
                        "line can't be distinguished from a dropped or "
                        "corrupted edge, so this capture is INVALID per the "
                        f"evidence protocol. Restart with a fresh --out; "
                        f"the {n} edge(s) already written to {args.out} "
                        "cannot be used.")

                if idx != n:
                    die(f"!! Device reported edge_index {idx} but {n} "
                        "edge(s) have been received so far — a serial line "
                        "was dropped, duplicated, or reordered in transit. "
                        "This capture is INVALID per the evidence protocol. "
                        f"Restart with a fresh --out; the {n} edge(s) "
                        f"already written to {args.out} cannot be used.")

                if prev_raw is None:
                    monotonic = 0
                    monotonic_start = monotonic
                    capture_start_utc = datetime.now(timezone.utc)
                else:
                    delta = wrap_delta(ts, prev_raw)
                    if near_wrap(delta):
                        warn(f"!! Gap before edge {n} is within "
                             f"{WRAP_WARN_MARGIN_US // 60_000_000} min of a "
                             "full timer-wrap period — the reconstructed "
                             "monotonic_timestamp_us for this edge may be "
                             "wrong (possible missed multi-wrap gap). "
                             "Inspect this run before trusting it.")
                    monotonic += delta
                    diff_count += 1
                    for t_ms in POISSON_CHECK_THRESHOLDS_MS:
                        if delta < t_ms * 1000:
                            bounce_counts[t_ms] += 1
                prev_raw = ts
                capture_end_utc = datetime.now(timezone.utc)

                writer.writerow([n, ts, monotonic])
                f.flush()
                os.fsync(f.fileno())
                n += 1

                if args.stats_every and diff_count > 0 and n % args.stats_every == 0:
                    stats_suffix = compute_stats(n)
                    # On a live terminal the line below already carries this
                    # and refreshes every edge; without one (output piped
                    # to a file), that update is invisible, so log this
                    # snapshot as a permanent line instead.
                    if not show_progress:
                        log(progress_line(n))

                show(progress_line(n))
        except KeyboardInterrupt:
            interrupted = True

    if progress_open:
        sys.stdout.write("\n")
        progress_open = False

    if interrupted:
        warn(f"\nInterrupted by user after {n} edge(s) — all of it is "
             f"safely on disk, but per the evidence protocol this capture "
             "is not valid evidence (not a complete, uninterrupted run). "
             "Start a fresh run under a new --out to try again.")
    else:
        log(f"Capture complete: {n} edges written to {args.out}")
        status = {
            "raw_edge_count": n,
            "capture_start_utc": capture_start_utc.isoformat() if capture_start_utc else None,
            "capture_end_utc": capture_end_utc.isoformat() if capture_end_utc else None,
            "port": args.port,
            "baud": args.baud,
            # Always 0 here by construction, not by counting: the board's
            # OVERRUN_DETECTED, an unparseable line, and an edge_index gap
            # all die() immediately above rather than incrementing a
            # counter and continuing, so any run that reaches this point had
            # zero of each - see the module docstring on why this protocol
            # doesn't soft-skip bad events.
            "buffer_overrun_count": 0,
            "transport_drop_count": 0,
            "capture_status": "COMPLETE",
        }
        sp = status_path(args.out)
        with open(sp, "w") as sf:
            json.dump(status, sf, indent=2)
            sf.write("\n")
        log(f"Wrote {sp}")


if __name__ == "__main__":
    main()
