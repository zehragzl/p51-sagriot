import numpy as np

import derya_model
from models import load_model


def test_shape_and_plugin():
    vwc = np.linspace(55, 50, 288)
    vpd = np.full(288, 0.8)
    out = derya_model.predict(vwc, vpd)
    assert out.shape == (36,) and np.isfinite(out).all()
    model = load_model("tinyml=derya_model:predict")
    assert model.name == "tinyml" and len(model.predict(vwc, vpd)) == 36


def test_forecast_stays_near_current_value():
    # The model predicts changes from the current value; over 3 h they are small.
    vwc = np.linspace(55, 50, 288)
    out = derya_model.predict(vwc, np.full(288, 1.2))
    assert np.abs(out - vwc[-1]).max() < 10


def test_short_or_missing_input():
    assert derya_model.predict(np.full(100, 50.0), np.full(100, 0.5)) is None
    vwc = np.full(288, 50.0); vwc[10] = np.nan
    assert derya_model.predict(vwc, np.full(288, 0.5)) is None
