#!/usr/bin/env python3
"""
capture_serial_to_csv.py

Reads the D2 edge stream from d2_edge_capture.ino and writes it to a
CSV file shaped like the evidence protocol's raw/raw_edges.csv
(edge_index, raw_timer_ticks, monotonic_timestamp_us).

For this pilot, raw_timer_ticks and monotonic_timestamp_us are the
same value (the board's micros() reading) — no timer-wrap extension
is applied. Fine for a short pilot; the full campaign would need that
handled properly.

Requires: pip install pyserial

Usage:
    python capture_serial_to_csv.py --port /dev/ttyUSB0 --count 2000
    python capture_serial_to_csv.py --port COM5 --count 2000
"""
import argparse
import csv
import sys

import serial


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True, help="e.g. /dev/ttyUSB0 or COM5")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--out", default="raw_edges.csv")
    p.add_argument("--count", type=int, default=2000,
                    help="stop after this many edges (~2000 is ~1hr at 20 CPM)")
    args = p.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=10)
    n = 0
    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["edge_index", "raw_timer_ticks", "monotonic_timestamp_us"])
        while n < args.count:
            raw = ser.readline()
            if not raw:
                print("No data for 10s — check wiring/port.", file=sys.stderr)
                continue
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
                continue  # ignore garbage/partial line
            writer.writerow([n, ts, ts])
            n += 1
            if n % 250 == 0:
                print(f"{n}/{args.count} edges captured...")

    print(f"Done. {n} edges written to {args.out}")


if __name__ == "__main__":
    main()
