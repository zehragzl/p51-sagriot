"""Tests for the cloud baseline, the twin and the service core.

  cd cloud && python -m pytest tests -q
  SAGRIOT_DSS=../../sagriot-dss python -m pytest tests -q    # + internship record

The record-based tests need the internship repo (zehragzl/sagriot-dss) and
are skipped without it.
"""
import json
import math
import os
import sys
from pathlib import Path

import pytest

CLOUD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLOUD))

from baseline import Baseline, Params, compute_vpd, CONTEXT   # noqa: E402
from service import Service                                   # noqa: E402
from twin import Twin                                          # noqa: E402

DSS = Path(os.environ.get("SAGRIOT_DSS", CLOUD.parents[1] / "sagriot-dss"))
SNAPSHOT = DSS / "data" / "snapshot_0909.csv"
needs_record = pytest.mark.skipif(not SNAPSHOT.exists(), reason="sagriot-dss not found")


def reading(seq, vwc, air_t=24.0, rh=60.0, err=0, **extra):
    return {"node": "t", "seq": seq, "ts": f"t{seq}", "vwc": vwc, "air_t": air_t,
            "rh": rh, "soil_t": 21.0, "err": err, **extra}


def drying_twin(start=60.0, rate=0.05, n=CONTEXT):
    """A twin fed n cycles of steady drying."""
    twin = Twin(Params(field_capacity=64.9))
    for i in range(n):
        twin.process(reading(i, start - rate * i))
    return twin


# -- baseline ----------------------------------------------------------------
def test_vpd_matches_reference_formula():
    es = 0.6108 * math.exp(17.27 * 25 / (25 + 237.3))
    assert compute_vpd(25, 50) == round(es * 0.5, 3)


def test_missing_values_are_not_pushed():
    b = Baseline()
    assert not b.push(None, 24, 60)
    assert not b.push(float("nan"), 24, 60)
    assert b.push(50, 24, 60) and b.count == 1


def test_no_forecast_before_24h_then_steady_drying_is_learned():
    twin = drying_twin(n=CONTEXT - 1)
    assert not twin.model.fit()
    twin.process(reading(CONTEXT, 60 - 0.05 * CONTEXT))
    assert twin.model.fitted
    assert twin.model.rate_per_step(twin.model.vpd[-1]) == pytest.approx(-0.05, abs=1e-3)


def test_strict_line_like_the_c_port():
    p = Params(field_capacity=64.9)
    vwc_70 = 0.70 * 64.9                       # soil_fc = 70.0 -> not below
    assert p.soil_fc(vwc_70) == 70.0
    assert not (vwc_70 < p.vwc_line(70))
    assert p.soil_fc(vwc_70 - 0.1) < 70 and vwc_70 - 0.1 < p.vwc_line(70)


# -- twin --------------------------------------------------------------------
def test_states_happy_then_thirsty():
    twin = Twin(Params(field_capacity=64.9))
    _, s = twin.process(reading(0, 60.0))
    assert s["state"] == "happy"
    _, s = twin.process(reading(1, 40.0))        # 61.6 %FC: Rule 2 now
    assert s["state"] == "fault"                  # ...but a 20-point drop in one step
    twin.ack_fault()
    _, s = twin.process(reading(2, 39.9))
    assert s["state"] == "thirsty" and s["decision"]["now"]


@pytest.mark.parametrize("bad, why", [
    (dict(err=2, vwc=None), "toprak probu"),
    (dict(err=1, air_t=None, rh=None), "SHT31"),
    (dict(soil_t=82.9), "soil_t"),             # the 20 Nov probe reading
    (dict(vwc=0.0), "prob toprakta değil"),     # probe in air
    (dict(rh=140.0), "rh"),
])
def test_sensor_faults(bad, why):
    twin = Twin()
    _, s = twin.process({**reading(0, 50.0), **bad})
    assert s["state"] == "fault"
    assert any(why in r for r in s["reasons"]), s["reasons"]


def test_bad_readings_do_not_enter_the_model():
    twin = Twin()
    twin.process(reading(0, 50.0))
    twin.process(reading(1, 0.0))                 # probe in air
    twin.process(reading(2, None, err=2))
    assert list(twin.model.vwc) == [50.0]


def test_watering_is_not_a_fault_and_drainage_after_it_is_not_either():
    twin = drying_twin(start=50.0)
    _, s = twin.process(reading(500, 70.0))       # watering: +36 points
    assert s["state"] != "fault" and s["last_irrigation_ts"] == "t500"
    _, s = twin.process(reading(501, 66.0))       # drainage: -4 in one step
    assert s["state"] != "fault"


def test_sudden_drop_holds_until_watering():
    twin = drying_twin(start=60.0, rate=0.01)
    _, s = twin.process(reading(500, 52.0))
    assert s["state"] == "fault"
    for k in range(30):                           # stays a fault, not "thirsty"
        _, s = twin.process(reading(501 + k, 52.0 - 0.01 * k))
        assert s["state"] == "fault"
    _, s = twin.process(reading(600, 64.0))       # watered
    assert s["state"] == "happy"


def test_what_if_is_consistent_with_the_rate():
    twin = drying_twin(start=60.0, rate=0.05)
    wi = twin.what_if()
    p = twin.params
    steps_to_warn = (twin.model.vwc[-1] - p.vwc_line(70)) / 0.05
    assert wi["no_water"]["warn_in_min"] == pytest.approx(steps_to_warn * 5, abs=10)
    steps_after_water = (p.field_capacity - p.vwc_line(70)) / 0.05
    assert wi["water_now"]["warn_in_min"] == pytest.approx(steps_after_water * 5, abs=10)
    assert wi["water_now"]["warn_in_min"] > wi["no_water"]["warn_in_min"]


def test_state_survives_a_restart(tmp_path):
    a = Service(state_dir=tmp_path)
    for i in range(10):
        a.handle("sagriot/node01/reading", json.dumps(reading(i, 55 - 0.1 * i)))
    b = Service(state_dir=tmp_path)
    assert b.twin("node01").model.count == 10
    assert b.twin("node01").prev_vwc == pytest.approx(54.1)


# -- service -----------------------------------------------------------------
def test_service_answers_with_cloud_decision_and_retained_twin():
    svc = Service({"node01": {"field_capacity": 30.0}})
    out = svc.handle("sagriot/node01/reading",
                     json.dumps(reading(7, 25.0, t_send_ms=1234)))
    topics = {t: (json.loads(p), r) for t, p, r in out}
    cd, retained = topics["sagriot/node01/cloud_decision"]
    assert cd["seq"] == 7 and cd["model"] == "baseline" and cd["t_send_ms"] == 1234
    assert cd["now"] is False and not retained          # 83 %FC with FC = 30
    twin, retained = topics["sagriot/node01/twin"]
    assert retained and twin["state"] == "happy" and twin["params"]["field_capacity"] == 30.0


def test_service_ignores_other_topics_and_bad_json():
    svc = Service()
    assert svc.handle("sagriot/node01/decision", "{}") == []
    assert svc.handle("sagriot/node01/reading", "not json") == []
    assert svc.handle("other/topic", "{}") == []


def test_ack_clears_a_fault_through_the_service():
    svc = Service()
    svc.handle("sagriot/n/reading", json.dumps(reading(0, 60.0)))
    out = svc.handle("sagriot/n/reading", json.dumps(reading(1, 50.0)))
    assert json.loads(out[1][1])["state"] == "fault"
    svc.handle("sagriot/n/twin_ack", "{}")
    out = svc.handle("sagriot/n/reading", json.dumps(reading(2, 49.9)))
    assert json.loads(out[1][1])["state"] != "fault"


# -- internship record (docs/digital_twin_plan.md, two scenarios) ------------
@needs_record
def test_scenario_drying_cycles_on_the_record():
    from replay_check import load_log, run, transitions
    states = run(load_log(SNAPSHOT), Params(field_capacity=64.9))
    changes = [(a, b) for _, a, b, _, _ in transitions(states)]
    # five watering cycles: happy -> thirsty -> happy each time, no fault
    assert changes.count(("happy", "thirsty")) >= 4
    assert changes.count(("thirsty", "happy")) == 5
    assert not any(s["state"] == "fault" for s in states)


@needs_record
def test_scenario_probe_shift_is_a_fault_not_thirst():
    from replay_check import inject_probe_shift, load_log, run
    frame, at = inject_probe_shift(load_log(SNAPSHOT))
    states = run(frame, Params(field_capacity=64.9))
    after = [s for s in states if s["ts"] >= at.isoformat()]
    assert after and all(s["state"] == "fault" for s in after)
    assert any(s["decision"]["now"] for s in after)     # baseline alone would say "water"


@needs_record
def test_cloud_baseline_matches_the_c_port():
    """Same windows as tools/compare_baseline.py, against the C host test."""
    import csv
    import io
    import subprocess
    from replay_check import load_log
    c_test = CLOUD.parent / "firmware/components/baseline/test/test_baseline"
    if not c_test.exists():
        pytest.skip("build first: make -C firmware/components/baseline/test")
    frame = load_log(SNAPSHOT)[["soil_vwc", "air_temp", "air_humidity"]]
    data = Path(os.environ.get("TMPDIR", "/tmp")) / "sagriot_cloud_test.csv"
    frame.to_csv(data, index=False, header=["vwc", "air_temp", "air_humidity"],
                 float_format="%.6f", na_rep="nan")
    out = subprocess.run([str(c_test), str(data), "64.9", "persistence"],
                         capture_output=True, text=True, check=True).stdout
    c_rows = {int(r["index"]): r for r in csv.DictReader(io.StringIO(out))}
    b = Baseline(Params(field_capacity=64.9))
    agree = total = 0
    for i, (v, t, h) in enumerate(frame.itertuples(index=False)):
        b.push(v, t, h)
        if i in c_rows and b.count == CONTEXT:
            d = b.decide()
            c = c_rows[i]
            total += 1
            agree += (d.now == bool(int(c["irrigate_now"]))
                      and d.t_cross_min == int(c["when_minutes"]))
    assert total > 5000 and agree / total >= 0.999


# -- reported watering (Telegram "Suladım") ------------------------------------
def test_reported_watering_is_confirmed_by_the_rise():
    twin = Twin(Params(field_capacity=64.9))
    twin.process(reading(0, 41.0))
    _, s = twin.process(reading(1, 40.9))
    assert s["state"] == "thirsty"
    twin.report_watering("t-report")
    s = twin.current_state()
    assert s["state"] == "thirsty" and s["watering"]["status"] == "pending"
    _, s = twin.process(reading(2, 60.0))                # the sensor sees it
    assert s["state"] == "happy" and s["watering"]["status"] == "confirmed"


def test_reported_watering_without_a_rise_becomes_a_fault():
    twin = Twin(Params(field_capacity=64.9, step_minutes=5))
    twin.process(reading(0, 41.0))
    twin.report_watering()
    for k in range(5):                                   # 25 min: still waiting
        _, s = twin.process(reading(1 + k, 40.9))
        assert s["state"] == "thirsty"
    _, s = twin.process(reading(6, 40.9))                # 30 min
    assert s["state"] == "fault"
    assert any("sulama bildirildi" in r for r in s["reasons"])
    twin.ack_fault()
    _, s = twin.process(reading(7, 40.9))
    assert s["state"] == "thirsty" and s["watering"]["status"] is None


def test_service_takes_the_watered_button_and_republishes():
    svc = Service()
    svc.handle("sagriot/n/reading", json.dumps(reading(0, 41.0)))
    out = svc.handle("sagriot/n/feedback", json.dumps({"action": "watered", "time": "now"}))
    assert len(out) == 1 and out[0][0] == "sagriot/n/twin" and out[0][2] is True
    assert json.loads(out[0][1])["watering"]["status"] == "pending"
    assert svc.handle("sagriot/n/feedback", json.dumps({"action": "not_needed"})) == []
    out = svc.handle("sagriot/n/twin_ack", "{}")
    assert out and out[0][0] == "sagriot/n/twin"


# -- field capacity from the data, state hysteresis ----------------------------
def watering_curve(twin, seq0, before, peak, plateau, n_drain=8, n_flat=12):
    """A watering: rise to peak, drain to plateau, then flat."""
    seq = seq0
    twin.process(reading(seq, before)); seq += 1
    twin.process(reading(seq, peak)); seq += 1
    for k in range(1, n_drain + 1):
        twin.process(reading(seq, peak - (peak - plateau) * k / n_drain)); seq += 1
    for _ in range(n_flat):
        twin.process(reading(seq, plateau)); seq += 1
    return seq


def test_field_capacity_is_learned_from_the_drainage_plateau():
    twin = Twin(Params(field_capacity=40.0))        # configured value is wrong
    seq = watering_curve(twin, 0, 25.0, 60.0, 55.0)
    assert twin.fc_estimates and abs(twin.fc_estimates[-1] - 55.0) < 0.2
    assert twin.params.field_capacity == pytest.approx(55.0, abs=0.2)
    _, s = twin.process(reading(seq, 54.9))
    assert s["params"]["fc_source"].startswith("measured")


def test_median_low_ignores_one_odd_plateau():
    twin = Twin(Params(field_capacity=40.0))
    seq = watering_curve(twin, 0, 25.0, 60.0, 55.0)
    seq = watering_curve(twin, seq, 30.0, 75.0, 70.0)   # probe touched: odd plateau
    assert twin.params.field_capacity == pytest.approx(55.0, abs=0.2)
    watering_curve(twin, seq, 30.0, 61.0, 56.0)
    assert twin.params.field_capacity == pytest.approx(56.0, abs=0.2)


def test_auto_fc_can_be_turned_off():
    from twin import TwinConfig
    twin = Twin(Params(field_capacity=40.0), TwinConfig(auto_fc=False))
    watering_curve(twin, 0, 25.0, 60.0, 55.0)
    assert twin.params.field_capacity == 40.0 and twin.fc_estimates


def test_thirsty_does_not_flicker_near_the_line():
    twin = Twin(Params(field_capacity=64.9))
    line = twin.params.vwc_line(70)
    twin.process(reading(0, line + 1.0))
    _, s = twin.process(reading(1, line - 0.05))
    assert s["state"] == "thirsty"
    for k in range(4):                               # hovering just above the line
        _, s = twin.process(reading(2 + k, line + 0.05))
        assert s["state"] == "thirsty"
    for k in range(6):                               # 30 min calm
        _, s = twin.process(reading(10 + k, line + 0.05))
    assert s["state"] == "happy"


@needs_record
def test_record_field_capacity_close_to_the_hand_value():
    from replay_check import load_log, run
    states = run(load_log(SNAPSHOT), Params(field_capacity=45.0))   # start wrong
    p = states[-1]["params"]
    assert p["fc_estimates"] == pytest.approx([64.98, 71.84, 62.6, 64.24, 63.48], abs=0.05)
    assert abs(p["field_capacity"] - 64.9) < 1.0
