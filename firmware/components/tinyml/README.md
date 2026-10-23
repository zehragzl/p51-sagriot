# tinyml (Derya)

The learned model on the node: int8 TensorFlow Lite Micro model and the code
that runs it. Same input and output as the baseline: last 24 h (288 samples
of VWC and VPD) in, 36-step (3 h) VWC forecast out.

Not an ESP-IDF component yet. Starting point: `experiments/tinyml_budget/`
shows how a model of this shape is loaded and timed on the ESP32-S3.
