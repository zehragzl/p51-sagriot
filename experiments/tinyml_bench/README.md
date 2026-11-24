# Derya's model on the ESP32-S3

Replays the 21-day internship log (the same 6,020 samples as
`baseline_bench`) through `firmware/components/tinyml` on the board and,
for every complete 24 h window, compares the decision and the 3 h forecast
with Derya's float model in Python (`cloud/derya_model.py`, embedded in
`main/tinyml_expected.c`). It also times the model and, on the same
windows, the baseline.

Targets: same decision >= 99 %, model <= 50 KB flash, time measured.
Replay code and expected values: `main/`.

## Result on the PC (host build, gcc, not the chip)

    same decision as Python: 5728/5733 = 99.91 %
    3 h forecast vs Python:  mean 0.0019, max 0.0079 VWC points
    model in flash:          31536 bytes
    peak stack:              ~1.1 KB (estimate; the chip value comes from the run)

The 5 mismatches are windows where the forecast passes within a few
hundredths of a VWC point of the 70 % line.

## Result on the ESP32-S3 (24 Nov 2026, Zehra's ESP32-S3 board, 240 MHz, ESP-IDF v5.3)

| | Target | Measured |
|---|---|---|
| Same decision as Python (float) | >= 99 % | 5728/5733 = 99.91 % |
| 3 h forecast vs Python | | mean 0.0019, max 0.0079 VWC points |
| Model in flash | <= 50 KB | 31,536 B |
| tinyml forecast + decide | | mean 0.748 ms, max 2.15 ms |
| baseline fit + decide (same windows) | | mean 0.081 ms, max 0.114 ms |
| RAM | | 24 h buffer 2,332 B (shared with the baseline) + peak stack 1,136 B |

Whole replay of 6,020 samples: 5.12 s. The chip gives the same numbers as
the PC build for decisions and forecasts (float32 on both).

The learned model is about 9x slower than the baseline (20,736 weight
reads per forecast against two passes over 288 samples), but both are far
below one node cycle; energy per decision is still to be measured with
the power setup. The max (2.15 ms) is likely flash-cache misses: the
31.5 KB of weights are read from flash through the cache (not measured
separately).

## Run

    . ~/esp/esp-idf/export.sh
    cd experiments/tinyml_bench
    idf.py set-target esp32s3
    idf.py build
    idf.py -p <port> flash monitor

Leave the monitor with Ctrl+T then Ctrl+X. If the model is retrained:

    python tools/export_tinyml.py
    python tools/compare_tinyml.py ../sagriot-dss data/snapshot_0909.csv \
        --write-expected experiments/tinyml_bench/main/tinyml_expected.c
