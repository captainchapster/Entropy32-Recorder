# Entropy32 Plus · SP 800-90B Reproducible Evidence Protocol v0.1

**Status:** evidence protocol, not certification.
**Drafted by:** Cosmographer / BHRIGU

This protocol is written to apply to any build of the Entropy32 Plus
firmware. Pin the specific repository, commit, and firmware blob hash you
are evaluating in your own manifest (§10) rather than in this document.

Entropy32 currently measures Geiger pulse inter-arrival time using D2, rejects intervals below `200 µs`, consumes accepted intervals in non-overlapping pairs, discards ties, emits `1` when the second interval is longer and `0` when shorter, and only then runs health tests / fills the 512-bit pool. Entropy32 Plus's own README correctly notes that runtime health tests are not full entropy validation, and that a long raw inter-arrival dataset still needs SP 800-90B non-IID assessment.

## 1. Evidence boundary

The acquisition point is:

```text
PHYSICAL SOURCE
    ↓
Geiger detector pulse
    ↓
LM393 comparator
    ↓
D2 rising edge
    ↓
[CAPTURE HERE]
    ↓
200 µs filter
    ↓
non-overlapping interval pairing
    ↓
comparison bit
    ↓
RCT / APT
    ↓
512-bit pool
    ↓
SHA-256
```

**Every D2 rising edge is recorded before any filtering, pairing, health test, pool logic or SHA-256.**

For this protocol:

```text
RAW EVIDENCE
= direct D2 edge observations

DERIVED EVIDENCE
= intervals, accepted intervals,
  comparison bits and statistical results
  reconstructed from those observations
```

One terminology note matters: SP 800-90B calls the digitized output of the defined noise source its “raw data.” Here we deliberately preserve an even earlier observational authority—the D2 edge timestamps—so that different candidate digitized-sample definitions can be reconstructed without recollecting the experiment. NIST explicitly expects access to digitized noise-source data for validation and external testing. ([NIST Publications](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-90b.pdf "Recommendation for the Entropy Sources Used for Random Bit Generation"))

---

## 2. Recorder requirements

The recorder must timestamp edges itself. **PC receive time must never be used as the entropy timestamp.**

Required properties:

| Property | Requirement |
| -------- | ----------- |
| Input               | rising edge on the same post-LM393 signal used as Entropy32 D2                          |
| Timestamp source    | free-running hardware timer                                                             |
| Resolution          | **≤ 1 µs preferred; never worse than the Entropy32 timing resolution**                  |
| Clock               | oscillator source and nominal frequency recorded                                        |
| Monotonicity        | recorder maintains an unambiguous monotonically increasing timestamp across timer wraps |
| Edge index          | 64-bit monotonically increasing counter, incremented for every captured edge            |
| ISR                 | only timestamp/store; no printing, formatting or blocking I/O                           |
| Buffer              | ring/DMA buffer; overflow counter mandatory                                             |
| Transport           | foreground transfer to PC                                                               |
| Drop detection      | sequence gaps or buffer overrun invalidate the capture                                  |
| Capture end         | sample-count based, not time based                                                      |

Current Entropy32 uses `micros()` on a 16 MHz ATmega328P. The standard Arduino AVR implementation derives `micros()` from Timer0 with a 64-clock prescaler; at 16 MHz its returned values advance in 4 µs units. ([GitHub](https://github.com/arduino/ArduinoCore-avr/blob/master/cores/arduino/wiring.c?utm_source=chatgpt.com "ArduinoCore-avr/cores/arduino/wiring.c at master · arduino/ArduinoCore-avr · GitHub"))

Therefore the recorder should preserve higher-resolution physical timestamps but also record enough timing/toolchain metadata to reproduce the deployed Entropy32 timing semantics. The manifest must state the actual Arduino AVR core/toolchain used by the device; the firmware repository alone does not pin that dependency.

### Overflow handling

Do **not** allow a wrapped 32-bit timestamp to become ambiguous.

Recommended stored edge record:

```text
edge_index
raw_timer_ticks
monotonic_timestamp_us
```

`monotonic_timestamp_us` must remain increasing for the whole run. If the underlying hardware timer wraps, the recorder must extend it before writing the evidence record.

### Dropped-event criterion

A run is valid only if:

```text
first_edge_index = 0
every next edge_index = previous + 1
buffer_overrun_count = 0
transport_drop_count = 0
records_received = recorder_records_sent
```

Any violation:

```text
CAPTURE_INVALID
```

Do not patch the file or interpolate missing events.

---

## 3. Minimum acquisition length

NIST requires at least **1,000,000 sequential noise-source samples** for the sequential dataset; if multiple consecutive segments are concatenated, each segment must contain at least 1,000 samples. ([NIST Publications](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-90b.pdf "Recommendation for the Entropy Sources Used for Random Bit Generation"))

For this evidence package, use a stricter practical stop gate:

```text
UNFILTERED_INTERARRIVAL_SAMPLES >= 1,000,000

AND

COMPARISON_BITS >= 1,000,000
```

That ensures there is enough evidence to inspect the physical timing observations **and** a full one-million-sample binary dataset corresponding to the stream actually entering Entropy32's health-test/pool path.

Because each comparison bit consumes two accepted intervals and ties emit no bit, the recorder must continue until the second condition is reached rather than assuming a fixed number of pulses.

The capture duration therefore depends on observed count rate. It is not defined by isotope, CPM estimate or wall-clock duration.

---

## 4. Immutable package

The final evidence directory is:

```text
entropy32_sp80090b_<capture-id>/

  manifest.json
  capture_status.json
  SHA256SUMS

  raw/
    raw_edges.csv

  derived/
    all_intervals_us.csv
    firmware_accepted_intervals_us.csv
    comparison_bits.bin
    derivation_report.json

  nist/
    TOOLCHAIN.txt
    comparison_non_iid.command.txt
    comparison_non_iid.stdout.txt
    comparison_non_iid.stderr.txt
```

### `raw/raw_edges.csv`

One record per physical D2 rising edge:

```text
edge_index,raw_timer_ticks,monotonic_timestamp_us
0,...
1,...
2,...
...
```

This file is the primary observational authority.

Once closed:

1. do not edit, sort, normalize or re-export it;
2. calculate SHA-256 immediately;
3. include that digest in `SHA256SUMS` and `manifest.json`.

---

## 5. Exact firmware reconstruction

Starting from `raw_edges.csv`, first derive every consecutive physical inter-arrival value:

```text
all_interval[n] =
edge_timestamp[n] - edge_timestamp[n-1]
```

Do **not** apply modulo, low-byte extraction, bucketing or truncation.

Then reconstruct current Entropy32 semantics.

Current firmware does something subtle and important: an interval below `200 µs` is rejected **without updating** **`lastPulseMicros`**. The next interval is therefore measured from the last accepted edge, not from the rejected edge.

Equivalent reconstruction:

```text
last_accepted_edge = NONE
have_first_interval = false

for each raw D2 edge in original order:

    if first edge:
        last_accepted_edge = edge
        continue

    interval =
        edge.time - last_accepted_edge.time

    if interval < 200 µs:
        reject
        DO NOT update last_accepted_edge
        continue

    accepted interval

    if have_first_interval == false:
        first_interval = interval
        have_first_interval = true

    else:
        second_interval = interval

        if second_interval > first_interval:
            emit comparison bit 1

        if second_interval < first_interval:
            emit comparison bit 0

        if second_interval == first_interval:
            emit no bit

        have_first_interval = false

    last_accepted_edge = edge
```

That is the current `geigerISR()` behavior.

### `derivation_report.json`

At minimum:

```json
{
  "source_repo": "your-org/your-repo",
  "source_commit": "<commit-sha>",
  "entropy32_firmware_blob_sha": "<firmware-blob-sha>",
  "min_interval_us": 200,
  "pairing": "NON_OVERLAPPING",
  "tie_rule": "DISCARD_PAIR",
  "bit_rule": "SECOND_LONGER=1;SECOND_SHORTER=0",
  "raw_edges": 0,
  "unfiltered_intervals": 0,
  "short_interval_rejections": 0,
  "accepted_intervals": 0,
  "complete_pairs": 0,
  "ties": 0,
  "comparison_zeroes": 0,
  "comparison_ones": 0,
  "comparison_bits": 0
}
```

The counts must reconcile exactly.

---

## 6. RAW and DERIVED must remain separate

Do not call `comparison_bits.bin` the original physical capture.

```text
RAW AUTHORITY
raw_edges.csv

        ↓ deterministic reconstruction

DERIVED
all_intervals_us.csv

        ↓ exact current 200 µs semantics

DERIVED
firmware_accepted_intervals_us.csv

        ↓ non-overlapping comparison

DERIVED
comparison_bits.bin
```

This separation is important because SP 800-90B's entropy assessment depends on what is explicitly defined as the digitized noise-source sample. NIST defines raw data as the output of the digitized noise source. ([NIST Publications](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-90b.pdf "Recommendation for the Entropy Sources Used for Random Bit Generation"))

The protocol therefore preserves enough earlier evidence to revisit that boundary later without repeating the physical experiment.

---

## 7. Raw interval alphabet issue

Do **not** silently convert interval values using:

```text
interval % 256
low_byte(interval)
truncate_to_uint8
arbitrary bins
```

Current Entropy32 source does none of those things.

SP 800-90B requires an alphabet larger than 256 symbols to be reduced to at most 256 before the specified entropy estimators are applied, and Section 6.4 requires the reduction to be justified/documented. ([NIST Publications](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-90b.pdf "Recommendation for the Entropy Sources Used for Random Bit Generation")) NIST even discusses timing samples as an example where selected lower-order bits might be reasonable—but that decision must follow a model/ranking argument, not be assumed in advance. ([NIST Publications](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-90b.pdf "Recommendation for the Entropy Sources Used for Random Bit Generation"))

Therefore:

```text
FULL INTERVAL DATA
= preserve unchanged

RAW-SOURCE SYMBOL REDUCTION
= separate documented analysis decision
```

No reduction rule is part of v0.1.

---

## 8. Ready NIST non-IID binary package

`comparison_bits.bin` is immediately compatible with the NIST tool because it has exactly one binary symbol per byte:

```text
0x00
0x01
0x00
...
```

Pin the current official NIST toolchain:

```text
repository:
usnistgov/SP800-90B_EntropyAssessment

tag:
v1.1.8

commit:
68ed165fd7a3eeef26b87a546ba23f338e82a3f3
```

The tag currently resolves to that exact commit.

Build and self-test:

```bash
make
cd selftest
./selftest
cd ..
make non_iid
```

Then:

```bash
./ea_non_iid \
  -i \
  -v \
  comparison_bits.bin \
  1
```

The official tool documents `-i` as unconditioned input, accepts one symbol per byte, and uses `bits_per_symbol=1` for this binary package.

Save verbatim:

```text
command line
tool git commit
compiler/version
stdout
stderr
exit status
SHA256(comparison_bits.bin)
```

For non-IID data, SP 800-90B requires all ten estimators—MCV, Collision, Markov, Compression, t-Tuple, LRS, MultiMCW, Lag, MultiMMC and LZ78Y—and uses the minimum result. ([NIST Publications](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-90b.pdf "Recommendation for the Entropy Sources Used for Random Bit Generation"))

Label the result:

```text
NON_IID_ESTIMATE_OF_DERIVED_COMPARISON_STREAM
```

Do **not** label it:

```text
FULL_ENTROPY32_VALIDATION
```

until the complete source boundary, raw-source estimate, restart testing and remaining SP 800-90B requirements are addressed.

---

## 9. Restart data is a separate required campaign

This first long sequential capture is **not** the restart test.

SP 800-90B requires:

```text
1000 restarts
×
1000 consecutive noise-source samples per restart
```

collected as soon as the source is ready for real-world output under the defined restart procedure. ([NIST Publications](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-90b.pdf "Recommendation for the Entropy Sources Used for Random Bit Generation"))

The standard then compares row/column restart estimates to the initial estimate; the final assessment uses the minimum when the restart criteria pass. ([NIST Publications](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-90b.pdf "Recommendation for the Entropy Sources Used for Random Bit Generation"))

Therefore restart collection should become a **second protocol** after the sequential acquisition boundary has been agreed.

---

## 10. Minimum manifest

`manifest.json` should contain at least:

```text
protocol_version
capture_id
capture_start_utc
capture_end_utc

repository
repository_commit
entropy32_firmware_blob_sha

hardware_revision
board_identifier
capture_point
edge_polarity

geiger_counter_model
physical_source_type
source_identifier_if_any
source_geometry
source_distance
shielding_or_enclosure

recorder_hardware
recorder_firmware_hash
compiler_version
arduino_core_or_runtime_version

clock_source
nominal_clock_hz
measured_clock_hz_if_known
timestamp_resolution
timer_width
overflow_extension_method

MIN_INTERVAL_US=200
RAW_POOL_BITS=512
pairing=NON_OVERLAPPING
tie_rule=DISCARD

ambient_temperature_if_available
supply_voltage_if_available
other_environment_notes

raw_edge_count
capture_status
buffer_overruns
transport_drops

nist_tool_repository
nist_tool_version
nist_tool_commit

file_sha256
```

Unknown values are written as `null` or `unknown`, not guessed.

---

## 11. Smallest recorder architecture

```text
                     ┌─────────────────────────┐
PHYSICAL SOURCE ───► │ Geiger detector         │
                     └───────────┬─────────────┘
                                 │ pulse
                                 ▼
                     ┌─────────────────────────┐
                     │ existing LM393 stage    │
                     └───────────┬─────────────┘
                                 │
                         post-LM393 D2 node
                                 │
                    ┌────────────┴─────────────┐
                    │                          │
                    ▼                          ▼
          Entropy32 Plus D2          validation recorder
                                     high-impedance input
                                             │
                                             ▼
                                   hardware edge timestamp
                                             │
                                      ring buffer
                                             │
                                             ▼
                                      USB → PC logger
```

**Preferred:** tap the existing post-LM393 signal with a high-impedance recorder input. That means the recorder observes the same electrical events as Entropy32 rather than creating a second analog entropy path.

The recorder needs no OLED, BIP39, SHA-256, entropy health logic or wallet functionality. Its only job is:

```text
EDGE
→ TIMESTAMP
→ SEQUENCE NUMBER
→ LOSS-DETECTABLE BUFFER
→ PC
```

No printing or USB work occurs inside the edge ISR.

A completely separate standalone recorder can mirror the same LM393 front end if desired, but that is secondary; the direct D2 tap is the smaller and stronger first evidence architecture.

---

## 12. Am-241

Am-241 is **not** a protocol requirement.

It is one possible physical source and should appear only in metadata:

```text
physical_source_type = "Am-241"
```

if that is what was actually used.

The protocol works equally with another source or background radiation. The isotope name, source activity or CPM alone does **not** establish min-entropy.

If Am-241 is used, record its available identifying/activity/geometry information and use it under applicable safety and legal requirements. The acquisition and statistical rules remain unchanged.

---

## 13. What this protocol proves

A successful run can establish:

- exactly what D2 edges were observed;
- that the dataset has no detected recorder drops;
- exact provenance to a named firmware commit and blob hash;
- reproducible reconstruction of the current 200 µs filter and non-overlapping pairing;
- the exact binary sequence entering the comparison-bit health/pool path;
- reproducible non-IID estimator results from a pinned NIST toolchain.

It **does not** by itself establish:

- NIST certification or formal validation;
- that radioactive decay as a category guarantees a particular entropy rate;
- that one physical source/device/environment represents all others;
- an IID claim;
- restart-test compliance;
- correctness of every Entropy32 hardware/software component;
- security of a resulting Bitcoin seed end-to-end;
- adequacy of health-test thresholds unless they are later tied to the assessed entropy.

NIST distinguishes entropy assessment from formal validation; formal validation involves accredited laboratory testing and program review. ([NIST Publications](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-90b.pdf "Recommendation for the Entropy Sources Used for Random Bit Generation"))

---

## 14. Execution checklist

1. Freeze the firmware repository commit being evaluated.
2. Record the exact `entropy32_plus.ino` blob hash.
3. Tap the **post-LM393 D2 rising-edge signal**.
4. Confirm recorder timestamp resolution ≤1 µs and document its clock.
5. Verify timestamp wrap extension is monotonic.
6. Verify ISR only timestamps/buffers events.
7. Verify sequence numbering and zero-overrun counters.
8. Perform a short pilot capture first.
9. Confirm every edge index is consecutive and `drops=0`.
10. Hash the raw capture immediately.
11. Run offline reconstruction with exact `MIN_INTERVAL_US=200` semantics.
12. Confirm rejected short intervals do **not** advance `lastPulseMicros`.
13. Pair accepted intervals non-overlapping.
14. Discard ties; emit `1` for second-longer and `0` for second-shorter.
15. Confirm all derivation counts reconcile.
16. Run the long capture until both ≥1M unfiltered intervals and ≥1M comparison bits exist.
17. Freeze and hash raw + derived files.
18. Pin NIST `SP800-90B_EntropyAssessment` v1.1.8 / `68ed165f…`.
19. Run NIST self-tests.
20. Run `ea_non_iid -i -v comparison_bits.bin 1`.
21. Preserve command, tool version, stdout/stderr and hashes.
22. Do **not** invent an interval-to-byte reduction rule for the full interval dataset.
23. Review the raw interval distribution before defining any Section 6.4 reduction.
24. Treat restart testing as the subsequent campaign, not as satisfied by this run.
25. Do not call the result certification or full SP 800-90B validation.
