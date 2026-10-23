# Baseline on the ESP32-S3 (success criterion 1)

Replays the 21-day internship log (sagriot-dss `data/snapshot_0909.csv`,
6,020 five-minute samples) through `firmware/components/baseline` on the
board and compares every decision with the Python reference
(`DrivenDrying(("vpd",))`, persistence future VPD, FC 64.9), which is
embedded in `firmware/components/replay/replay_data.c`.

## Result (20 Oct 2026, ESP32-S3-DevKitC-1 N16R8 clone, 240 MHz, ESP-IDF v5.3.2)

| Criterion | Target | First run (double) | Final (float) |
|---|---|---|---|
| Same decision as Python | >= 99 % | 5733/5733 = 100 % | 5732/5733 = 99.98 % |
| Time per decision (fit + decide) | <= 1 ms | mean 0.998 ms | mean 0.082 ms, max 0.109 ms |
| RAM (state + peak stack) | <= 4 KB | 3,252 B | 3,188 B (state 2,332 B + stack 856 B) |

Detail (final): fit mean 76 us / max 96 us, decide mean 5.7 us / max 13 us.
Whole replay of 6,020 samples: 0.69 s.

## What changed between the runs

The ESP32-S3 FPU is single precision; double arithmetic is emulated in
software (`__adddf3` in ROM showed up in the backtrace). The first version
summed in double and took ~0.7 ms per fit. The final version:

- fits in float, centring x and delta on their means first (numerically
  stable; algebraically the same 2x2 solution as the spec),
- turns the soil_fc line into a VWC threshold once, so each forecast step
  is one float comparison instead of a divide and a round,
- yields every 256 samples in the bench so the task watchdog stays quiet.

## The one mismatch

Sample 3963, persistence mode: Python crosses the line at 80 min, the C port
at 85 min. Python's forecast at 80 min is 45.397528 % VWC against a line of
45.397550 % (69.95 % FC), i.e. 0.00002 % VWC below it. A float32 vs float64
boundary case, as expected in the spec (section 9). On the PC the seasonal
mode agrees 5733/5733.

## Run

    . ~/esp/esp-idf/export.sh
    idf.py set-target esp32s3
    idf.py build
    idf.py -p <port> flash monitor

Leave the monitor with Ctrl+T then Ctrl+X.
