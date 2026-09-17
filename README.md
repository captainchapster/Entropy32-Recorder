# Entropy32 Recorder

![Assembled PCB](images/entropy32_recorder.png)

Firmware, hardware, and tooling for Entropy32 — a Geiger-counter-based
hardware random number generator built around an ATmega328P (Arduino Nano
form factor).

## Repository layout

- **`entropy32_recorder.ino`** — pilot sketch that temporarily replaces the
  normal Entropy32 firmware. It timestamps every rising edge on D2
  (post-LM393 comparator output) and streams `edge_index,timestamp_us` CSV
  rows over serial. It intentionally skips the production firmware's
  200us filter, interval pairing, and SHA-256 conditioning — it's for
  capturing raw edge timing data only. Reflash the normal firmware when
  done.
- **`tools/capture_serial_to_csv.py`** — reads the serial stream produced by
  the sketch above and writes it to a CSV file matching the evidence
  protocol's `raw/raw_edges.csv` shape (`edge_index`, `raw_timer_ticks`,
  `monotonic_timestamp_us`). Requires `pyserial`.
- **`KiCad/`** — PCB design (schematic, layout, 3D models, and
  fabrication/production outputs for the Entropy32 board).

## Hardware

The board pairs a Geiger tube's LM393 comparator output with an ATmega328P
(D2, using the board's existing pull-down for bias). Design files are in
[`KiCad/`](KiCad/), including BOM, designators, netlist, and pick-and-place
files under `KiCad/production/`.

## Usage

1. Flash `entropy32_recorder.ino` to the board.
2. Run the capture tool:

   ```
   pip install pyserial
   python tools/capture_serial_to_csv.py --port COM5 --count 2000
   ```

3. If the board reports `OVERRUN_DETECTED`, the ring buffer overflowed and
   the capture is invalid — restart it.

## License

- Firmware and software (`entropy32_recorder.ino`, `tools/`): [MIT](LICENSE)
- Hardware (`KiCad/`): [CERN-OHL-S v2](LICENSE-HARDWARE)
