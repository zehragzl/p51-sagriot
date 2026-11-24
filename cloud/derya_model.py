"""Derya's TinyML model (float version for the cloud): a linear forecaster.

Plugs into the cloud service as
    python cloud/service.py --model tinyml=derya_model:predict

Model (trained in deryaauysl/sagriot, src/linear_model.py):
  input   last 24 h at 5 min: 288 soil values relative to the current value
          (in % of field capacity) + 288 VPD values (kPa) = 576 numbers
  output  change of soil moisture for the next 36 steps (3 h), % of field capacity
  size    576 x 36 weights + 36 biases = 20,772 parameters (one dense layer)
  data    Autonomous Greenhouse Challenge, MuGrow + AgriFusion, all three sensors

Tested on a greenhouse it was not trained on (internship evaluation code):
  MuGrow -> AgriFusion: 54.6 % of dry-soil events caught, 86.8 % of warnings real
  AgriFusion -> MuGrow: 45.2 % caught, 88.7 % real (physics model: 14.8 % and 24.0 %)

The model works in % of field capacity, so VWC is converted with the node's
field capacity (env SAGRIOT_FIELD_CAPACITY, default 64.9 = internship pot) and
the forecast is converted back to VWC. It was trained on rockwool greenhouses,
not on the pot: results on the pot are not measured yet.
"""
import os
from pathlib import Path

import numpy as np

CONTEXT, HORIZON = 288, 36
_W = np.load(Path(__file__).with_name("derya_model.npz"))
WEIGHTS, BIAS = _W["weights"].astype(float), _W["bias"].astype(float)   # (576, 36), (36,)


def field_capacity():
    return float(os.environ.get("SAGRIOT_FIELD_CAPACITY", 64.9))


def predict(vwc_24h, vpd_24h, fc=None):
    """Last 288 VWC (%) and VPD (kPa) samples -> 36 forecast VWC values, or None."""
    vwc = np.asarray(vwc_24h, dtype=float)[-CONTEXT:]
    vpd = np.asarray(vpd_24h, dtype=float)[-CONTEXT:]
    if len(vwc) < CONTEXT or len(vpd) < CONTEXT or not (np.isfinite(vwc).all() and np.isfinite(vpd).all()):
        return None
    fc = fc or field_capacity()
    soil = vwc / fc * 100
    x = np.concatenate([soil - soil[-1], vpd])
    change = x @ WEIGHTS + BIAS                   # % of field capacity
    return vwc[-1] + change * fc / 100
