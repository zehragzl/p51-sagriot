"""Two-coefficient "driven drying" baseline, cloud copy.

Same model and decision as the node (firmware/components/baseline/baseline.c),
written from docs/baseline_port_spec.md, so the cloud's decision can be
compared with the node's one cycle at a time:

    delta_vwc = a * vpd + c          (ridge fit over the last 288 samples)
    forecast  = vwc_now + cumulative sum of min(a * vpd_future + c, 0)
    now       = soil_fc(vwc_now) < 70            (Rule 2, strict)
    warn      = soil_fc < 70 anywhere in the next 36 steps

Differences from the C port, on purpose:
  - float64 instead of float32 (the cloud has no FPU limit);
  - samples with a missing value are not pushed (see push()); the node
    pushes them as NaN, which spoils its fit for 24 h.

The model also exposes the fitted rate (rate_per_step) so the digital twin
can run longer what-if forecasts with the same physics.
"""
from collections import deque
from dataclasses import dataclass, field
import math

import numpy as np

CONTEXT = 288          # 24 h of 5-min samples  (spec 1)
HORIZON = 36           # 3 h of 5-min steps     (spec 1)
RIDGE = 1e-3           # forecasters.py: ridge=1e-3


def compute_vpd(air_t, rh):
    """features.py: compute_vpd, FAO-56 / Tetens, rounded to 3 decimals (spec 3)."""
    es = 0.6108 * math.exp(17.27 * air_t / (air_t + 237.3))
    return round(es - es * rh / 100.0, 3)


@dataclass
class Params:
    field_capacity: float = 64.9   # internship pot (spec 6); set per node
    fc_warn_low: float = 70.0      # soil_fc warning line, %FC
    fc_crit_low: float = 60.0      # soil_fc critical line, %FC
    step_minutes: float = 5.0      # one buffer step = one node cycle

    def vwc_line(self, fc_line):
        """soil_fc < line  <=>  vwc < this value (round half to even, as baseline.c)."""
        return (fc_line - 0.05) / 100.0 * self.field_capacity

    def soil_fc(self, vwc):
        return round(vwc / self.field_capacity * 100.0, 1)


@dataclass
class Decision:
    now: bool = False
    warn: bool = False
    t_cross_min: int = -1
    vwc_pred_3h: float | None = None
    fitted: bool = False


@dataclass
class Baseline:
    params: Params = field(default_factory=Params)
    vwc: deque = field(default_factory=lambda: deque(maxlen=CONTEXT))
    vpd: deque = field(default_factory=lambda: deque(maxlen=CONTEXT))
    a: float = 0.0
    c: float = 0.0
    fitted: bool = False

    # -- data -------------------------------------------------------------
    def push(self, vwc, air_t, rh):
        """Adds one cycle. Returns False (and stores nothing) if a value is missing."""
        if any(v is None or not math.isfinite(v) for v in (vwc, air_t, rh)):
            return False
        self.vwc.append(float(vwc))
        self.vpd.append(compute_vpd(float(air_t), float(rh)))
        self.fitted = False
        return True

    @property
    def count(self):
        return len(self.vwc)

    # -- model (spec 4.1) -------------------------------------------------
    def fit(self):
        """Ridge fit of delta = a*vpd + c over the full window; needs 288 samples."""
        if self.count < CONTEXT:
            self.fitted = False
            return False
        v = np.asarray(self.vwc, dtype=float)
        x = np.asarray(self.vpd, dtype=float)[1:]
        d = np.diff(v)
        design = np.column_stack([x, np.ones_like(x)])
        gram = design.T @ design
        penalty = np.eye(2) * RIDGE * np.trace(gram) / 2.0
        penalty[1, 1] = 0.0                      # no penalty on the intercept
        try:
            self.a, self.c = np.linalg.solve(gram + penalty, design.T @ d)
        except np.linalg.LinAlgError:
            self.fitted = False
            return False
        self.fitted = bool(np.isfinite(self.a) and np.isfinite(self.c))
        return self.fitted

    def rate_per_step(self, vpd):
        """Drying rate for one step at this VPD; never positive (non_increasing)."""
        return min(self.a * vpd + self.c, 0.0)

    def forecast(self, steps=HORIZON, start_vwc=None, vpd_future=None):
        """VWC for the next `steps` steps; persistence VPD unless vpd_future is given.

        start_vwc lets the twin ask "what if the soil were at X now".
        """
        if not self.fitted:
            return None
        level = self.vwc[-1] if start_vwc is None else float(start_vwc)
        vpd_now = self.vpd[-1]
        out = np.empty(steps)
        for k in range(steps):
            vpd = vpd_now if vpd_future is None else vpd_future[k]
            level += self.rate_per_step(vpd)
            out[k] = level
        return out

    # -- decision (spec 6-7) ----------------------------------------------
    def decide(self):
        d = Decision()
        if self.count == 0:
            return d
        line = self.params.vwc_line(self.params.fc_warn_low)
        d.now = self.vwc[-1] < line
        self.fit()
        d.fitted = self.fitted
        fc = self.forecast()
        if fc is None:
            return d                              # no fit yet: only "now"
        d.vwc_pred_3h = round(float(fc[-1]), 2)
        below = np.nonzero(fc < line)[0]
        if below.size:
            d.warn = True
            d.t_cross_min = int(round((below[0] + 1) * self.params.step_minutes))
        return d

    # -- persistence across restarts ---------------------------------------
    def to_dict(self):
        return {"vwc": list(self.vwc), "vpd": list(self.vpd)}

    def load_dict(self, data):
        self.vwc.extend(data.get("vwc", [])[-CONTEXT:])
        self.vpd.extend(data.get("vpd", [])[-CONTEXT:])
        self.fitted = False
