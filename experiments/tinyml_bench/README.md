# Derya's model on the ESP32-S3

Replays the 21-day internship log (the same 6,020 samples as
`baseline_bench`) through `firmware/components/tinyml` on the board and,
for every complete 24 h window, compares the decision and the 3 h forecast
with Derya's float model in Python (`cloud/derya_model.py`, embedded in
`main/tinyml_expected.c`). It also times the model and, on the same
windows, the baseline.

Targets: same decision >= 99 %, model <= 50 KB flash, time measured.

## Result on the PC (host build, gcc, not the chip)

    same decision as Python: 5728/5733 = 99.91 %
    3 h forecast vs Python:  mean 0.0019, max 0.0079 VWC points
    model in flash:          31536 bytes
    peak stack:              ~1.1 KB (estimate; the chip value comes from the run)

The 5 mismatches are windows where the forecast passes within a few
hundredths of a VWC point of the 70 % line.

## Result on the ESP32-S3

To be measured (26 Nov). Copy the monitor output here.

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
