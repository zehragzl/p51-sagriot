"""Forecast models that run next to the baseline, and the twin's model choice.

Every model answers the same question from the same 24 h window:
"what will VWC be over the next 3 h (36 steps)?". The same decision rule
(baseline.decision_from_forecast: soil_fc < 70 %) turns each forecast into
now / warn / t_cross_min, so differences come from the model alone.

Models
  baseline   the driven-drying model (baseline.py); always there
  chronos    Chronos-Bolt (amazon/chronos-bolt-tiny by default), VWC history
             only; needs `pip install torch chronos-forecasting`. The
             internship measured 728 MB and 33.6 ms on a Raspberry Pi 5.
  any other  a function `predict(vwc_24h, vpd_24h) -> 36 values`, loaded
             from "module:function" (Derya's model goes here as "tinyml").

ModelSelector: the twin listens to the model that has been right lately.
Each forecast is scored when its 36 steps have really happened (mean
absolute error over the 3 h, in VWC points). The model with the lowest
average error over its last 288 scored forecasts is used for the twin's
state. The same scoring learns each extra model's bias on this site, and
the twin subtracts it from that model's next forecasts (no retraining). Forecasts whose 3 h contain a watering are dropped (no model can
see a watering coming). Only forecasts made in the decision zone (soil between 60 and 85 %
of field capacity) are scored, because that is where a forecast decides
anything; a challenger has to be at least 10 % better to take
over, so the choice does not flip back and forth. Until every candidate
has 12 scores, the baseline is used. The what-ifs always come from the baseline: only the physical model
can answer "what if I water now"; Chronos only sees the VWC series.
"""
from collections import deque
import importlib
import inspect
import math

import numpy as np

from baseline import CONTEXT, HORIZON


class ChronosModel:
    bias_correct = True

    def __init__(self, model_id="amazon/chronos-bolt-tiny", name="chronos"):
        self.name = name
        self.model_id = model_id
        self._pipeline = None
        self.error = None

    def _load(self):
        if self._pipeline is None and self.error is None:
            try:
                import torch
                from chronos import BaseChronosPipeline
                self._pipeline = BaseChronosPipeline.from_pretrained(
                    self.model_id, device_map="cpu", torch_dtype=torch.float32)
                self._torch = torch
            except Exception as e:                 # missing package, no network, ...
                self.error = f"{type(e).__name__}: {e}"
                print(f"{self.name}: disabled ({self.error})")
        return self._pipeline

    def predict(self, vwc, vpd, fc=None):          # fc unused: VWC series only
        pipeline = self._load()
        if pipeline is None:
            return None
        series = self._torch.tensor(np.asarray(vwc, dtype=np.float32))
        try:
            _, mean = pipeline.predict_quantiles(inputs=series, prediction_length=HORIZON,
                                                 quantile_levels=[0.5])
        except TypeError:                          # older chronos-forecasting
            _, mean = pipeline.predict_quantiles(context=series, prediction_length=HORIZON,
                                                 quantile_levels=[0.5])
        mean = mean.detach().cpu().numpy() if hasattr(mean, "detach") else np.asarray(mean)
        return np.asarray(mean, dtype=float).reshape(-1)[:HORIZON]


class FunctionModel:
    """Wraps predict(vwc_24h, vpd_24h) -> 36 values, e.g. Derya's model."""

    def __init__(self, name, func, bias_correct=True):
        self.name = name
        self.func = func
        self.bias_correct = bias_correct      # False for references (persistence)
        try:                                 # does the model want the field capacity?
            self.takes_fc = "fc" in inspect.signature(func).parameters
        except (TypeError, ValueError):
            self.takes_fc = False

    def predict(self, vwc, vpd, fc=None):
        args = (np.asarray(vwc, dtype=float), np.asarray(vpd, dtype=float))
        out = self.func(*args, fc=fc) if self.takes_fc and fc else self.func(*args)
        if out is None:
            return None
        out = np.asarray(out, dtype=float).reshape(-1)
        return out[:HORIZON] if out.size >= HORIZON else None


def persistence(vwc, vpd, fc=None):
    """Reference: VWC stays where it is. Any model should beat it."""
    return np.full(HORIZON, float(vwc[-1]))


def load_model(spec):
    """'chronos', 'chronos=amazon/chronos-bolt-small', 'persistence' or
    'tinyml=module:function'."""
    name, _, target = spec.partition("=")
    if name == "chronos":
        return ChronosModel(target or "amazon/chronos-bolt-tiny")
    if name == "persistence" and not target:
        return FunctionModel("persistence", persistence, bias_correct=False)
    if not target or ":" not in target:
        raise ValueError(f"model spec '{spec}': use name=module:function")
    module, func = target.split(":", 1)
    return FunctionModel(name, getattr(importlib.import_module(module), func))


class ModelSelector:
    """Scores every model's forecasts and picks the one the twin follows.

    It also learns each model's bias: the mean of (raw forecast - what
    happened) per horizon step over the last `window` scored forecasts.
    bias() is subtracted from a model's next forecasts (twin.py), so a
    model that is always a little too wet or too dry on this site is
    corrected without retraining. Scores are kept for the forecasts as
    used (after correction); the bias is learned from the raw ones.
    """

    def __init__(self, names, default="baseline", window=CONTEXT, min_scores=12,
                 margin=0.10):
        self.names = list(names)
        self.default = default
        self.window = window
        self.min_scores = min_scores
        self.margin = margin
        self.selected = default
        self.step = 0
        # [step made, model, forecast used, raw forecast, |errors| so far, raw residuals so far]
        self.pending = deque()
        self.scores = {n: deque(maxlen=window) for n in self.names}
        self.residuals = {n: deque(maxlen=window) for n in self.names}
        self._res_sum = {n: np.zeros(HORIZON) for n in self.names}

    def _add_residual(self, name, res):
        d = self.residuals[name]
        if len(d) == d.maxlen:
            self._res_sum[name] -= np.asarray(d[0], dtype=float)
        d.append(res)
        self._res_sum[name] += np.asarray(res, dtype=float)

    def update(self, actual_vwc, forecasts, relevant=True, raw=None):
        """Call once per valid reading, after the forecasts for it are made.

        actual_vwc scores the older forecasts; forecasts {name: array|None}
        (as used, after any bias correction) and raw {name: array|None}
        (before it; defaults to forecasts) are stored to be scored over the
        next 36 steps, but only when `relevant`: the twin passes True only
        when the soil is near the warning line, where the forecast changes
        the decision. Far above it (just after watering) a flat "no change"
        guess scores well and says nothing about the next crossing.
        """
        raw = raw or forecasts
        self.step += 1
        keep = deque()
        for made, name, fc, rfc, errs, res in self.pending:
            k = self.step - made              # 1 .. HORIZON
            errs.append(abs(fc[k - 1] - actual_vwc))
            res.append(rfc[k - 1] - actual_vwc)
            if k == HORIZON:
                self.scores[name].append(sum(errs) / len(errs))
                self._add_residual(name, res)
            else:
                keep.append((made, name, fc, rfc, errs, res))
        self.pending = keep
        for name, fc in (forecasts.items() if relevant else ()):
            rfc = raw.get(name, fc)
            if (fc is not None and len(fc) >= HORIZON and np.all(np.isfinite(fc))
                    and rfc is not None and len(rfc) >= HORIZON):
                self.pending.append((self.step, name, [float(v) for v in fc[:HORIZON]],
                                     [float(v) for v in rfc[:HORIZON]], [], []))
        self._choose()

    def discard_pending(self):
        """Watering happened: no model forecasts it, so open forecasts are not scored."""
        self.pending.clear()

    def bias(self, name):
        """Mean raw residual per horizon step (36 values), or None until min_scores."""
        r = self.residuals.get(name)
        if not r or len(r) < self.min_scores:
            return None
        return self._res_sum[name] / len(r)

    def mae(self, name):
        s = self.scores.get(name)
        return float(np.mean(s)) if s else None

    def _choose(self):
        ready = {n: self.mae(n) for n in self.names
                 if len(self.scores[n]) >= self.min_scores}
        if len(ready) < len(self.names) or len(ready) < 2:
            self.selected = self.default if self.default in self.names else self.names[0]
            return
        best = min(ready, key=ready.get)
        current = ready.get(self.selected, math.inf)
        if best != self.selected and ready[best] < current * (1 - self.margin):
            self.selected = best

    def summary(self):
        out = {}
        for n in self.names:
            b = self.bias(n)
            out[n] = {"mae_3h": None if self.mae(n) is None else round(self.mae(n), 3),
                      "scored": len(self.scores[n]),
                      "bias_3h": None if b is None else round(float(b[-1]), 3)}
        return out

    # -- persistence -----------------------------------------------------------
    def to_dict(self):
        return {"selected": self.selected, "step": self.step,
                "pending": [list(p) for p in self.pending],
                "scores": {n: list(s) for n, s in self.scores.items()},
                "residuals": {n: [list(r) for r in rs] for n, rs in self.residuals.items()}}

    def load_dict(self, data):
        self.step = data.get("step", 0)
        if data.get("selected") in self.names:
            self.selected = data["selected"]
        self.pending = deque(tuple(p) for p in data.get("pending", [])
                             if len(p) == 6 and p[1] in self.names)   # older files: dropped
        for n, s in data.get("scores", {}).items():
            if n in self.scores:
                self.scores[n].extend(s[-self.window:])
        for n, rs in data.get("residuals", {}).items():
            if n in self.residuals:
                for r in rs[-self.window:]:
                    self._add_residual(n, r)
