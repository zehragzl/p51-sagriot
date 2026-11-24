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

Model choice: when more models run (models.py: Chronos, Derya's model),
the state comes from the one with the lowest recent 3 h error
(ModelSelector); the what-ifs always use the physical baseline.

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
A sudden-drop fault stays until the pot is watered or someone confirms the
probe was checked (ack_fault(), later a Telegram button). After a displaced
probe every later reading is on a new baseline, so the twin cannot tell by
itself when the data is trustworthy again; letting the fault expire on a
timer would turn it into a false "thirsty" (seen on the replay of 9 Sept).
"""
from dataclasses import dataclass, field
import math

import time

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
                    self.last_irrigation_step = self.step
                    self.ack_fault()                    # watered: someone was at the pot
                    self.selector.discard_pending()     # unforecastable for every model
                elif change < -self.config.drop_fault and not after_water:
                    hold = self.config.fault_hold_min
                    self.fault_until_step = (-2 if hold is None   # -2: until cleared
                                             else self.step + self._steps(hold))
                    self.fault_reason = (f"ani düşüş: VWC {self.prev_vwc:.1f} → {vwc:.1f} "
                                         f"bir adımda (prob yerinden oynamış olabilir)")
            self.model.push(vwc, r["air_t"], r["rh"])
            self.prev_vwc = vwc
            self._run_models(vwc)
        else:
            t0 = time.perf_counter()
            self.last_decisions = {"baseline": self.model.decide()}
            self.last_infer_us = {"baseline": int((time.perf_counter() - t0) * 1e6)}

        decision = self.last_decisions["baseline"]
        used = self.last_decisions.get(self.selector.selected) or decision
        if self.fault_reason and (self.fault_until_step == -2
                                  or self.step <= self.fault_until_step):
            faults.append(self.fault_reason)
        if self.model.count == 0 and not faults:
            faults.append("henüz geçerli okuma yok")

        if faults:
            state = "fault"
        elif used.now or used.warn:
            state = "thirsty"
        else:
            state = "happy"
        return decision, self._state_dict(r, state, faults, used)

    def _run_models(self, vwc):
        """Every model forecasts from the same window; the selector scores them."""
        t0 = time.perf_counter()
        decisions = {"baseline": self.model.decide()}
        timings = {"baseline": int((time.perf_counter() - t0) * 1e6)}
        forecasts = {"baseline": self.model.last_forecast}
        for m in self.extra_models:
            fc = None
            t0 = time.perf_counter()
            if self.model.count >= 288:             # same 24 h context as the baseline
                try:
                    fc = m.predict(list(self.model.vwc), list(self.model.vpd))
                except Exception as e:              # a broken model must not stop the twin
                    print(f"{m.name}: prediction failed ({type(e).__name__}: {e})")
            timings[m.name] = int((time.perf_counter() - t0) * 1e6)
            forecasts[m.name] = fc
            decisions[m.name] = decision_from_forecast(vwc, fc, self.params)
        lo, hi = self.config.score_zone
        self.selector.update(vwc, forecasts,
                             relevant=lo <= self.params.soil_fc(vwc) <= hi)
        self.last_decisions = decisions
        self.last_infer_us = timings

    def ack_fault(self):
        """Clears a held fault (pot watered, or the probe was checked)."""
        self.fault_until_step = -1
        self.fault_reason = None

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
        return {"horizon_h": self.config.what_if_hours,
                "no_water": no_water, "water_now": water_now}

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
            "model": {"fitted": self.model.fitted, "samples": self.model.count,
                      "needed": 288,
                      "a": round(self.model.a, 5) if self.model.fitted else None,
                      "c": round(self.model.c, 5) if self.model.fitted else None},
            "params": {"field_capacity": self.params.field_capacity,
                       "step_minutes": self.params.step_minutes},
        }

    # -- persistence ---------------------------------------------------------
    def to_dict(self):
        return {"model": self.model.to_dict(), "step": self.step,
                "prev_vwc": self.prev_vwc,
                "last_irrigation_step": self.last_irrigation_step,
                "last_irrigation_ts": self.last_irrigation_ts,
                "fault_until_step": self.fault_until_step,
                "fault_reason": self.fault_reason,
                "selector": self.selector.to_dict()}

    def load_dict(self, data):
        self.model.load_dict(data.get("model", {}))
        self.selector.load_dict(data.get("selector", {}))
        for key in ("step", "prev_vwc", "last_irrigation_step", "last_irrigation_ts",
                    "fault_until_step", "fault_reason"):
            if key in data:
                setattr(self, key, data[key])
