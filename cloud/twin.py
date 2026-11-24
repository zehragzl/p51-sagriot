"""Light digital twin of one pot (docs/digital_twin_plan.md).

The twin keeps a model of the pot that is updated with every reading:
the same driven-drying physics as the baseline (baseline.py), plus what
it has seen happen (watering, faults). From that it gives

  - a state:   happy  (😊 mutlu)    soil is fine for the next 3 h
               thirsty (😟 susamış) Rule 2 fires now or within 3 h
               fault  (⚠️ arıza)    the data cannot be trusted; wins over the others
  - two what-ifs, both checkable later against what really happens:
               no_water:  "if I don't water": when soil_fc crosses 70 % and 60 %
               water_now: "if I water now":   soil back to field capacity,
                          and how long until it crosses 70 % again

Field capacity: after each watering the twin watches the drainage and
takes the VWC where it stops falling (less than 0.15 points in 15 min,
from 15 min after the peak, within 3 h) as a field-capacity reading; the
(lower) median of the last five is used for the thresholds, the baseline and the
models (internship pot: 64.9, 71.8, 62.6, 64.2, 63.5 -> 64.2 against the
64.9 found by hand; 21 Aug was the day the probe was touched). Until the
first watering the configured value is used. Turn off with auto_fc.

Model choice: when more models run (models.py: Chronos, Derya's model),
the state comes from the one with the lowest recent 3 h error
(ModelSelector); the what-ifs always use the physical baseline. Each
extra model's bias on this site is learned from its scored forecasts
and subtracted from its next ones (bias_correct; the baseline is left
as it is, it is the reference).

One direction only (digital shadow): the twin reads the node, it does not
send anything back yet. The `config` message (twin -> node) comes later.

Fault checks, and why each threshold:
  - err bits from the node (bit 0 SHT31, bit 1 probe), missing values
  - values out of physical range (e.g. the 82.9 °C soil reading of 20 Nov)
  - vwc below 1 %: the probe is in air, not in soil (20 Nov test: 0.00)
  - sudden drop: vwc falls more than 2.5 points in one step. In the 21-day
    internship record the largest 5-min drop outside watering was 0.96
    points; the probe displacement of 9 Sept fell 12.8 points in 12 min.
    Drops in the hour after watering are drainage and are not faults.
A reported watering (Telegram "Suladım") is only a claim: the twin marks
it pending and waits for the VWC rise. If the rise comes it is
confirmed; if it does not come within 30 min the twin raises a fault (the
water did not reach the probe, or the probe is out of place).

A sudden-drop fault stays until the pot is watered or someone confirms the
probe was checked (ack_fault(), later a Telegram button). After a displaced
probe every later reading is on a new baseline, so the twin cannot tell by
itself when the data is trustworthy again; letting the fault expire on a
timer would turn it into a false "thirsty" (seen on the replay of 9 Sept).
"""
from dataclasses import dataclass, field
import math

import statistics
import time

import numpy as np

from baseline import Baseline, Decision, Params, decision_from_forecast
from models import ModelSelector

STATES = {
    "happy":   {"emoji": "😊", "label": "mutlu"},
    "thirsty": {"emoji": "😟", "label": "susamış"},
    "fault":   {"emoji": "⚠️", "label": "arıza"},
}

RANGES = {                  # physical limits; outside = sensor fault
    "vwc": (0.0, 100.0),
    "soil_t": (-20.0, 70.0),
    "air_t": (-20.0, 60.0),
    "rh": (0.0, 100.0),
}


@dataclass
class TwinConfig:
    drop_fault: float = 2.5          # VWC points lost in one step = fault
    irrigation_rise: float = 2.0     # VWC points gained in one step = watering
    post_irrigation_min: float = 60  # drainage window after watering
    fault_hold_min: float | None = None  # None: until watering or ack_fault()
    probe_air_vwc: float = 1.0       # below this the probe is not in soil
    what_if_hours: float = 120       # how far the what-ifs look (5 days: one dry-down)
    score_zone: tuple = (60.0, 85.0)  # soil_fc band where model forecasts are scored
    watering_check_min: float = 30   # a reported watering must show in VWC within this
    auto_fc: bool = True             # learn field capacity from the drainage plateaus
    fc_watch_min: float = 180        # look for the plateau up to 3 h after a watering
    fc_settle_min: float = 15        # ignore the first 15 min after the peak
    fc_flat_per_15min: float = 0.15  # "plateau": VWC changes less than this in 15 min
    fc_keep: int = 5                 # median of the last 5 plateaus
    calm_min: float = 30             # thirsty -> happy needs a watering or 30 min without a warning
    bias_correct: bool = True        # subtract each extra model's learned bias on this site


def _missing(v):
    return v is None or (isinstance(v, float) and not math.isfinite(v))


@dataclass
class Twin:
    params: Params = field(default_factory=Params)
    config: TwinConfig = field(default_factory=TwinConfig)
    model: Baseline = None
    step: int = 0                    # readings processed
    prev_vwc: float | None = None
    last_irrigation_step: int | None = None
    last_irrigation_ts: str | None = None
    fault_until_step: int = -1
    fault_reason: str | None = None
    fc_estimates: list = field(default_factory=list)  # plateau VWC after recent waterings
    fc_track: list | None = None                  # VWC since the last watering, while watching
    watering_reported_step: int | None = None   # farmer said "Suladım", not yet seen
    watering_reported_ts: str | None = None
    watering_status: str | None = None          # pending / confirmed / unconfirmed
    last_reading: dict | None = None
    shown_state: str | None = None   # state last published, for the hysteresis
    calm_steps: int = 0
    extra_models: list = field(default_factory=list)   # models.ChronosModel, FunctionModel
    selector: ModelSelector = None
    last_decisions: dict = field(default_factory=dict)  # model -> Decision, for the service
    last_infer_us: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.model is None:
            self.model = Baseline(self.params)
        if self.selector is None:
            self.selector = ModelSelector(["baseline"] + [m.name for m in self.extra_models])

    def _steps(self, minutes):
        return int(math.ceil(minutes / self.params.step_minutes))

    # -- checks -----------------------------------------------------------
    def _sensor_faults(self, r):
        faults = []
        err = r.get("err") or 0
        if err & 1:
            faults.append("SHT31 okunamadı")
        if err & 2:
            faults.append("toprak probu okunamadı")
        for key, (lo, hi) in RANGES.items():
            v = r.get(key)
            if key in ("vwc", "air_t", "rh") and _missing(v) and not err:
                faults.append(f"{key} değeri yok")
            elif not _missing(v) and not (lo <= v <= hi):
                faults.append(f"{key} = {v} fiziksel olarak mümkün değil")
        vwc = r.get("vwc")
        if not _missing(vwc) and 0.0 <= vwc < self.config.probe_air_vwc:
            faults.append("VWC ≈ 0: prob toprakta değil")
        return faults

    def _inputs_ok(self, r):
        for key in ("vwc", "air_t", "rh"):
            v = r.get(key)
            lo, hi = RANGES[key]
            if _missing(v) or not (lo <= v <= hi):
                return False
        return r["vwc"] >= self.config.probe_air_vwc

    # -- one reading --------------------------------------------------------
    def process(self, r):
        """Updates the twin with one reading; returns (Decision, twin state dict)."""
        self.step += 1
        faults = self._sensor_faults(r)

        if self._inputs_ok(r):
            vwc = float(r["vwc"])
            if self.prev_vwc is not None:
                change = vwc - self.prev_vwc
                after_water = (self.last_irrigation_step is not None and
                               self.step - self.last_irrigation_step
                               <= self._steps(self.config.post_irrigation_min))
                if change > self.config.irrigation_rise:
                    if not after_water:                 # one event, not one per step
                        self.last_irrigation_ts = r.get("ts")
                        self.fc_track = [self.prev_vwc]
                    self.last_irrigation_step = self.step
                    self.ack_fault()                    # watered: someone was at the pot
                    if self.watering_status in ("pending", "unconfirmed"):
                        self.watering_status = "confirmed"   # the sensor agrees
                        self.watering_reported_step = self.step
                    else:
                        self.watering_status = None     # watered without a report
                    self.selector.discard_pending()     # unforecastable for every model
                elif change < -self.config.drop_fault and not after_water:
                    hold = self.config.fault_hold_min
                    self.fault_until_step = (-2 if hold is None   # -2: until cleared
                                             else self.step + self._steps(hold))
                    self.fault_reason = (f"ani düşüş: VWC {self.prev_vwc:.1f} → {vwc:.1f} "
                                         f"bir adımda (prob yerinden oynamış olabilir)")
            self.model.push(vwc, r["air_t"], r["rh"])
            self.prev_vwc = vwc
            self._watch_field_capacity(vwc)
            self._run_models(vwc)
        else:
            t0 = time.perf_counter()
            self.last_decisions = {"baseline": self.model.decide()}
            self.last_infer_us = {"baseline": int((time.perf_counter() - t0) * 1e6)}

        self.last_reading = r
        since = None if self.watering_reported_step is None else self.step - self.watering_reported_step
        if self.watering_status == "pending" and since >= self._steps(self.config.watering_check_min):
            self.watering_status = "unconfirmed"
        elif self.watering_status == "confirmed" and since > self._steps(60):
            self.watering_status = None                 # announced; forget it after an hour
        return self.last_decisions["baseline"], self._compose(r, faults, new_reading=True)

    def _compose(self, r, faults, new_reading=False):
        """State from the last decisions, the reading's faults and the held ones."""
        faults = list(faults)
        used = self.last_decisions.get(self.selector.selected) or self.last_decisions["baseline"]
        if self.fault_reason and (self.fault_until_step == -2
                                  or self.step <= self.fault_until_step):
            faults.append(self.fault_reason)
        if self.watering_status == "unconfirmed":
            faults.append(f"sulama bildirildi ama {self.config.watering_check_min:.0f} dk içinde "
                          f"nem artmadı (su proba ulaşmamış ya da prob yerinden çıkmış olabilir)")
        if self.model.count == 0 and not faults:
            faults.append("henüz geçerli okuma yok")
        if faults:
            state = "fault"
        elif used.now or used.warn:
            state = "thirsty"
        else:
            state = "happy"
        # hysteresis: near the line the warning can come and go every cycle;
        # leave "thirsty" only after a watering or 30 min without a warning
        if state == "happy" and self.shown_state == "thirsty":
            watered_now = self.last_irrigation_step == self.step
            if new_reading:
                self.calm_steps += 1
            if not watered_now and self.calm_steps < self._steps(self.config.calm_min):
                state = "thirsty"
        elif new_reading:
            self.calm_steps = 0                         # a warning (or a fault) restarts the wait
        if new_reading or state != self.shown_state:
            self.shown_state = state
        return self._state_dict(r, state, faults, used)

    def report_watering(self, ts=None):
        """The farmer said "Suladım" (Telegram). Only the next readings can confirm it."""
        self.watering_reported_step = self.step
        self.watering_reported_ts = ts
        self.watering_status = "pending"

    def current_state(self):
        """State dict for the last reading again (after a button, without a new reading)."""
        if self.last_reading is None:
            return None
        return self._compose(self.last_reading, self._sensor_faults(self.last_reading))

    def _run_models(self, vwc):
        """Every model forecasts from the same window; the selector scores them."""
        t0 = time.perf_counter()
        decisions = {"baseline": self.model.decide()}
        timings = {"baseline": int((time.perf_counter() - t0) * 1e6)}
        forecasts = {"baseline": self.model.last_forecast}
        raw = dict(forecasts)
        for m in self.extra_models:
            fc = None
            t0 = time.perf_counter()
            if self.model.count >= 288:             # same 24 h context as the baseline
                try:
                    fc = m.predict(list(self.model.vwc), list(self.model.vpd),
                                   fc=self.params.field_capacity)
                except Exception as e:              # a broken model must not stop the twin
                    print(f"{m.name}: prediction failed ({type(e).__name__}: {e})")
            timings[m.name] = int((time.perf_counter() - t0) * 1e6)
            raw[m.name] = fc
            bias = (self.selector.bias(m.name)
                    if self.config.bias_correct and getattr(m, "bias_correct", True) else None)
            if fc is not None and bias is not None:
                fc = np.asarray(fc, dtype=float) - bias   # this site's learned bias
            forecasts[m.name] = fc
            decisions[m.name] = decision_from_forecast(vwc, fc, self.params)
        lo, hi = self.config.score_zone
        self.selector.update(vwc, forecasts,
                             relevant=lo <= self.params.soil_fc(vwc) <= hi, raw=raw)
        self.last_decisions = decisions
        self.last_infer_us = timings
        self._last_forecasts = forecasts

    def _watch_field_capacity(self, vwc):
        """After a watering, the VWC where drainage stops is the field capacity."""
        if self.fc_track is None:
            return
        self.fc_track.append(vwc)
        track = self.fc_track
        settle = self._steps(self.config.fc_settle_min)
        win = self._steps(15)
        peak = max(range(len(track)), key=track.__getitem__)
        for k in range(peak + settle, len(track) - win):
            if abs(track[k] - track[k + win]) <= self.config.fc_flat_per_15min:
                self._add_fc_estimate(track[k])
                self.fc_track = None
                return
        if len(track) > self._steps(self.config.fc_watch_min):
            self.fc_track = None                    # never settled (watered again, too little water)

    def _add_fc_estimate(self, value):
        self.fc_estimates = (self.fc_estimates + [round(float(value), 2)])[-self.config.fc_keep:]
        if self.config.auto_fc:
            self.params.field_capacity = round(float(statistics.median_low(self.fc_estimates)), 1)

    def ack_fault(self):
        """Clears a held fault (pot watered, or the probe was checked)."""
        self.fault_until_step = -1
        self.fault_reason = None
        if self.watering_status == "unconfirmed":
            self.watering_status = None

    # -- what-ifs ------------------------------------------------------------
    def _minutes_until_below(self, forecast, fc_line):
        line = self.params.vwc_line(fc_line)
        below = [k for k, v in enumerate(forecast) if v < line]
        return None if not below else int(round((below[0] + 1) * self.params.step_minutes))

    def what_if(self):
        """None until the model is fitted (24 h of readings)."""
        if not self.model.fitted:
            return None
        steps = self._steps(self.config.what_if_hours * 60)
        p = self.params
        dry = self.model.forecast(steps)
        no_water = {
            "warn_in_min": 0 if self.model.vwc[-1] < p.vwc_line(p.fc_warn_low)
            else self._minutes_until_below(dry, p.fc_warn_low),
            "crit_in_min": 0 if self.model.vwc[-1] < p.vwc_line(p.fc_crit_low)
            else self._minutes_until_below(dry, p.fc_crit_low),
            "vwc_end": round(max(float(dry[-1]), 0.0), 1),
        }
        wet = self.model.forecast(steps, start_vwc=p.field_capacity)
        water_now = {
            "vwc_after": p.field_capacity,
            "warn_in_min": self._minutes_until_below(wet, p.fc_warn_low),
        }
        # curves for the panel: the next 24 h, one point every 30 min
        every, n = self._steps(30), self._steps(24 * 60)
        no_water["curve"] = [round(max(float(v), 0.0), 2) for v in dry[every - 1:n:every]]
        water_now["curve"] = [round(max(float(v), 0.0), 2) for v in wet[every - 1:n:every]]
        return {"horizon_h": self.config.what_if_hours, "curve_step_min": every * self.params.step_minutes,
                "no_water": no_water, "water_now": water_now}

    def _forecast_curve(self):
        """3 h forecast of the model the twin follows (None before 24 h)."""
        fc = None
        if self.selector.selected == "baseline":
            fc = self.model.last_forecast
        else:
            fc = getattr(self, "_last_forecasts", {}).get(self.selector.selected)
        return None if fc is None else [round(float(v), 2) for v in fc]

    def _drying_per_day(self):
        """VWC points lost per day at the current VPD (physical model)."""
        if not self.model.fitted:
            return None
        per_step = -self.model.rate_per_step(self.model.vpd[-1])
        return round(per_step * 24 * 60 / self.params.step_minutes, 2)

    # -- output --------------------------------------------------------------
    def _state_dict(self, r, state, faults, d: Decision):
        vwc = self.model.vwc[-1] if self.model.count else None
        return {
            "v": 1, "node": r.get("node"), "seq": r.get("seq"), "ts": r.get("ts"),
            "state": state, **STATES[state], "reasons": faults,
            "vwc": vwc, "soil_fc": None if vwc is None else self.params.soil_fc(vwc),
            "vpd": self.model.vpd[-1] if self.model.count else None,
            "air_t": r.get("air_t"), "rh": r.get("rh"), "soil_t": r.get("soil_t"),
            "decision": {"now": d.now, "warn": d.warn, "t_cross_min": d.t_cross_min,
                         "vwc_pred_3h": d.vwc_pred_3h},
            "selected_model": self.selector.selected,
            "models": {name: {"now": dd.now, "warn": dd.warn, "t_cross_min": dd.t_cross_min,
                              "vwc_pred_3h": dd.vwc_pred_3h,
                              **self.selector.summary().get(name, {})}
                       for name, dd in self.last_decisions.items()},
            "what_if": self.what_if(),
            "last_irrigation_ts": self.last_irrigation_ts,
            "watering": {"status": self.watering_status,
                         "reported_ts": self.watering_reported_ts},
            "model": {"fitted": self.model.fitted, "samples": self.model.count,
                      "needed": 288,
                      "a": round(self.model.a, 5) if self.model.fitted else None,
                      "c": round(self.model.c, 5) if self.model.fitted else None},
            "params": {"field_capacity": self.params.field_capacity,
                       "step_minutes": self.params.step_minutes,
                       "warn_vwc": round(self.params.vwc_line(self.params.fc_warn_low), 2),
                       "crit_vwc": round(self.params.vwc_line(self.params.fc_crit_low), 2),
                       "fc_source": (f"measured ({len(self.fc_estimates)} waterings)"
                                     if self.config.auto_fc and self.fc_estimates else "configured"),
                       "fc_estimates": self.fc_estimates},
            "forecast": self._forecast_curve(),
            "drying_per_day": self._drying_per_day(),
        }

    # -- persistence ---------------------------------------------------------
    def to_dict(self):
        return {"model": self.model.to_dict(), "step": self.step,
                "prev_vwc": self.prev_vwc,
                "last_irrigation_step": self.last_irrigation_step,
                "last_irrigation_ts": self.last_irrigation_ts,
            "watering": {"status": self.watering_status,
                         "reported_ts": self.watering_reported_ts},
                "fault_until_step": self.fault_until_step,
                "fault_reason": self.fault_reason,
                "watering_reported_step": self.watering_reported_step,
                "watering_reported_ts": self.watering_reported_ts,
                "watering_status": self.watering_status,
                "last_reading": self.last_reading,
                "fc_estimates": self.fc_estimates, "fc_track": self.fc_track,
                "shown_state": self.shown_state, "calm_steps": self.calm_steps,
                "selector": self.selector.to_dict()}

    def load_dict(self, data):
        self.model.load_dict(data.get("model", {}))
        self.selector.load_dict(data.get("selector", {}))
        if self.config.auto_fc and data.get("fc_estimates"):
            self.params.field_capacity = round(float(statistics.median_low(data["fc_estimates"])), 1)
        for key in ("step", "prev_vwc", "last_irrigation_step", "last_irrigation_ts",
                    "fault_until_step", "fault_reason", "watering_reported_step",
                    "watering_reported_ts", "watering_status", "last_reading",
                    "fc_estimates", "fc_track", "shown_state", "calm_steps"):
            if key in data:
                setattr(self, key, data[key])
