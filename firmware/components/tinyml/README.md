# tinyml (Derya)

The learned model on the node: int8 TensorFlow Lite Micro model and the code
that runs it. Same input and output as the baseline: last 24 h (288 samples
of VWC and VPD) in, 36-step (3 h) VWC forecast out.

## Selected model: linear forecaster

A small MLP was tried and did not beat a linear model on unseen greenhouses
(larger MLPs overfit the training greenhouse), so the model is linear: one
dense layer, 576 inputs -> 36 outputs, 20,772 parameters.

- Inputs: 288 soil values minus the current value (% of field capacity) and 288 VPD values (kPa).
- Output: change of soil moisture for the next 36 steps, added to the current value.
- Training, evaluation and weights: `deryaauysl/sagriot`, `src/linear_model.py`.
- Float version in the cloud: `cloud/derya_model.py` (`--model tinyml=derya_model:predict`).

| Train -> test (AGC) | Events caught | Warnings real | Physics model caught |
|---|---|---|---|
| MuGrow -> AgriFusion | 54.6 % | 86.8 % | 14.8 % |
| AgriFusion -> MuGrow | 45.2 % | 88.7 % | 24.0 % |

Next: int8 conversion and the ESP-IDF component, starting from
`experiments/tinyml_budget/`.
