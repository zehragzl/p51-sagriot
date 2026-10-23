# TinyML budget check

Answers one question before the real model exists: does an int8 model of the
target size fit the ESP32-S3 within the success-criterion limits?

## Test model

1D-CNN, input 288 x 2 (24 h of VWC + VPD at 5 min), output 36 (3 h ahead),
full int8 (TFLite). Three Conv1D layers (16, 32, 24 filters, kernel 5,
stride 2) + Dense 38 + Dense 36; 40,906 parameters, 0.38 M MACs.
Weights are random: this measures cost, not accuracy. Rebuild with
`python make_budget_model.py 16 32 24 38`.

## Result (20 Oct 2026, ESP32-S3-DevKitC-1 N16R8 clone, 240 MHz, ESP-IDF v5.3.2, esp-tflite-micro 1.4.1)

| Criterion | Target | Measured |
|---|---|---|
| Model size (flash) | <= 50 KB | 47,224 B |
| Tensor arena (RAM) | <= 100 KB | 13,388 B |
| Inference time | <= 100 ms | min 5.13 ms, mean 5.13 ms, max 5.46 ms (100 runs) |

Free internal heap after setup: 235,596 B.

## Limits of this result

- Random weights and one architecture; Derya's real model may differ in shape
  and ops (e.g. a GRU would need different kernels and more time).
- Wi-Fi and MQTT were not running; their RAM use comes on top.
- The 150 KB arena buffer is statically allocated; the free heap figure is
  after it.

## Run

    . ~/esp/esp-idf/export.sh
    idf.py set-target esp32s3
    idf.py build
    idf.py -p <port> flash monitor

If flashing fails with "No serial data received": hold BOOT, press RST,
release BOOT, then flash. The port name changes in download mode
(`ls /dev/cu.*`).
