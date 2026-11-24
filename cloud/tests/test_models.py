"""Tests for the extra models and the twin's model choice."""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

CLOUD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLOUD))

from baseline import HORIZON, Params                          # noqa: E402
from models import FunctionModel, ModelSelector, load_model   # noqa: E402
from service import Service                                   # noqa: E402
from twin import Twin                                          # noqa: E402

DSS = Path(os.environ.get("SAGRIOT_DSS", CLOUD.parents[1] / "sagriot-dss"))
SNAPSHOT = DSS / "data" / "snapshot_0909.csv"


def drying(n, start=50.0, rate=0.02):
    return [start - rate * i for i in range(n)]


def feed(selector, series, forecasters, relevant=True):
    """forecasters: name -> f(step index) -> 36 values."""
    for i, v in enumerate(series):
        selector.update(v, {n: f(i) for n, f in forecasters.items()}, relevant)


def oracle(series, bias=0.0):
    def f(i):
        future = series[i + 1:i + 1 + HORIZON]
        return None if len(future) < HORIZON else np.array(future) + bias
    return f


def test_selector_keeps_the_default_until_every_model_is_scored():
    s = ModelSelector(["baseline", "other"], min_scores=12)
    series = drying(HORIZON + 5)
    feed(s, series, {"baseline": oracle(series, 1.0), "other": oracle(series)})
    assert s.selected == "baseline"           # only 5 scores so far


def test_selector_moves_to_the_clearly_better_model():
    s = ModelSelector(["baseline", "other"], min_scores=12)
    series = drying(200)
    feed(s, series, {"baseline": oracle(series, 1.0), "other": oracle(series, 0.1)})
    assert s.selected == "other"
    assert s.mae("other") == pytest.approx(0.1) and s.mae("baseline") == pytest.approx(1.0)


def test_selector_does_not_switch_for_a_small_gain():
    s = ModelSelector(["baseline", "other"], min_scores=12, margin=0.10)
    series = drying(200)
    feed(s, series, {"baseline": oracle(series, 1.0), "other": oracle(series, 0.95)})
    assert s.selected == "baseline"           # 5 % better is not enough


def test_forecasts_outside_the_zone_and_across_watering_are_not_scored():
    s = ModelSelector(["baseline"])
    series = drying(100)
    feed(s, series, {"baseline": oracle(series)}, relevant=False)
    assert s.summary()["baseline"]["scored"] == 0
    feed(s, series[:10], {"baseline": oracle(series)})
    s.discard_pending()
    assert not s.pending


def test_selector_state_survives_a_restart():
    s = ModelSelector(["baseline", "other"])
    series = drying(120)
    feed(s, series, {"baseline": oracle(series, 1.0), "other": oracle(series, 0.1)})
    t = ModelSelector(["baseline", "other"])
    t.load_dict(json.loads(json.dumps(s.to_dict())))
    assert t.selected == s.selected and t.mae("other") == pytest.approx(s.mae("other"))
    assert len(t.pending) == len(s.pending)


def test_load_model_specs():
    assert load_model("persistence").name == "persistence"
    m = load_model("tinyml=models:persistence")
    assert m.name == "tinyml" and len(m.predict([40.0] * 288, [1.0] * 288)) == HORIZON
    assert load_model("chronos").name == "chronos"   # loads lazily, no torch needed here
    with pytest.raises(ValueError):
        load_model("tinyml")


def reading(seq, vwc):
    return {"node": "n", "seq": seq, "ts": f"t{seq}", "vwc": vwc, "air_t": 24.0,
            "rh": 60.0, "soil_t": 21.0, "err": 0}


def test_twin_runs_every_model_and_survives_a_broken_one():
    def broken(vwc, vpd):
        raise RuntimeError("model file missing")
    twin = Twin(Params(field_capacity=64.9),
                extra_models=[load_model("persistence"), FunctionModel("broken", broken)])
    for i in range(300):
        _, state = twin.process(reading(i, 52.0 - 0.02 * i))
    assert set(state["models"]) == {"baseline", "persistence", "broken"}
    assert state["models"]["broken"]["vwc_pred_3h"] is None
    assert state["models"]["persistence"]["vwc_pred_3h"] == pytest.approx(state["vwc"])
    assert state["selected_model"] in ("baseline", "persistence")


def test_service_sends_one_cloud_decision_per_model():
    svc = Service(models=[load_model("persistence")])
    out = svc.handle("sagriot/n/reading", json.dumps(reading(0, 50.0)))
    models = [json.loads(p)["model"] for t, p, _ in out if t.endswith("/cloud_decision")]
    assert models == ["baseline", "persistence"]


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="sagriot-dss not found")
def test_on_the_record_the_twin_listens_to_the_baseline_not_persistence():
    from replay_check import load_log, run
    states = run(load_log(SNAPSHOT), Params(field_capacity=64.9),
                 models=[load_model("persistence")])
    last = states[-1]["models"]
    assert last["baseline"]["mae_3h"] < last["persistence"]["mae_3h"]
    assert all(s["selected_model"] == "baseline" for s in states)


def test_selector_learns_a_constant_bias():
    s = ModelSelector(["baseline", "wet"], min_scores=12)
    series = drying(200)
    feed(s, series, {"baseline": oracle(series), "wet": oracle(series, 0.7)})
    assert s.bias("wet") == pytest.approx(np.full(HORIZON, 0.7))
    assert s.summary()["wet"]["bias_3h"] == pytest.approx(0.7)
    t = ModelSelector(["baseline", "wet"])
    t.load_dict(json.loads(json.dumps(s.to_dict())))
    assert t.bias("wet") == pytest.approx(s.bias("wet"))


def test_twin_corrects_a_biased_model_but_not_the_reference():
    def wet(vwc, vpd, fc=None):          # always 0.7 VWC too wet, otherwise exact slope
        return np.asarray(vwc[-1] - 0.02 * np.arange(1, HORIZON + 1)) + 0.7
    twin = Twin(Params(field_capacity=64.9),
                extra_models=[FunctionModel("wet", wet), load_model("persistence")])
    for i in range(500):
        _, state = twin.process(reading(i, 52.0 - 0.02 * i))
    m = state["models"]
    assert m["wet"]["bias_3h"] == pytest.approx(0.7, abs=0.05)
    assert twin.selector.scores["wet"][-1] < 0.05       # corrected forecasts are scored
    raw_last = wet(list(twin.model.vwc), None)[-1]
    assert m["wet"]["vwc_pred_3h"] == pytest.approx(raw_last - 0.7, abs=0.05)
    assert m["persistence"]["vwc_pred_3h"] == pytest.approx(state["vwc"])   # untouched


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="sagriot-dss not found")
def test_record_bias_correction_helps_deryas_model():
    from replay_check import load_log, run
    states = run(load_log(SNAPSHOT), Params(field_capacity=64.9),
                 models=[load_model("tinyml=derya_model:predict")])
    m = states[-1]["models"]
    assert m["tinyml"]["mae_3h"] < 0.2                   # 0.357 without the correction
    assert sum(s["state"] == "thirsty" for s in states) == 1237   # decisions unchanged
