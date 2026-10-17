# Baseline Port Specification (v0.1)

What the C port on the ESP32 must reproduce, taken from the internship
reference code (`zehragzl/sagriot-dss`). Every number below is read from that
code, with the source location in brackets.

## 1. Time base

| Item | Value | Source |
|---|---|---|
| Step | 5 min | `advise.py: STEP_MINUTES = 5` |
| Context (history used for the fit) | 24 h = 288 samples | `CONTEXT_HOURS = 24` |
| Horizon (forecast length) | 3 h = 36 steps | `HORIZON = 3 * 60 // STEP_MINUTES` |
| Forecast issued every | 30 min in the benchmark | `replay(stride_minutes=30)` |

## 2. Inputs per sample

- `soil_vwc` (%) from the RS485 probe
- `air_temp` (°C) and `air_humidity` (%) from the SHT31

## 3. VPD

FAO-56 / Tetens form, **rounded to 3 decimals** [`features.py: compute_vpd`]:

```
es  = 0.6108 * exp(17.27 * T / (T + 237.3))
ea  = es * RH / 100
vpd = round(es - ea, 3)
```

## 4. Drying model (DrivenDrying, drivers = vpd only)

Source: `forecasters.py: class DrivenDrying`, configured as
`driven_drying_vpd` (`decay=None`, `level_term=False`, `ridge=1e-3`,
`non_increasing=True`).

### 4.1 Fit

From the last 288 samples:

```
delta[i] = vwc[i+1] - vwc[i]            i = 0..286   (287 values)
x[i]     = vpd[i+1]                     aligned with the change it drove
model:   delta = a * x + c
```

Ridge least squares, penalty on `a` only (not on the intercept `c`):

```
Sxx = Σ x²   Sx = Σ x   Sxd = Σ x·delta   Sd = Σ delta   n = 287
p   = 1e-3 * (Sxx + n) / 2              (ridge * trace(gram) / 2)

| Sxx + p   Sx | | a |   | Sxd |
| Sx        n  | | c | = | Sd  |
```

Solve the 2×2 system directly (Cramer's rule). Python falls back to
`lstsq` if the matrix is singular; the C port returns "no forecast" in that
case and logs it.

### 4.2 Forecast

For step k = 1..36:

```
rate_k = a * vpd_future[k] + c
rate_k = min(rate_k, 0)                 soil cannot gain water without input
vwc_k  = vwc_now + Σ_{j<=k} rate_j
```

The standard-deviation band (`sigma`) is only used for quantile output and is
**not** needed for the decision.

## 5. Future VPD (open design choice)

In the reference system, future air temperature and humidity come from
**Chronos-tiny** [`config.py: FORECASTERS`], and future VPD is computed from
them step by step. Chronos cannot run on the ESP32, so the port needs another
source:

| Option | Rule | Extra memory |
|---|---|---|
| A. Persistence | `vpd_future[k] = vpd_now` | none |
| B. Seasonal naive | `vpd_future[k] = vpd 24 h earlier at the same time` = the oldest 36 entries of the 288-sample buffer | none |
| C. Cloud-assisted | future VPD sent down from the cloud | none, but needs a connection |

Plan: implement A and B in C, compare A, B and the Chronos reference in Python
on the recorded logs (events caught, false alarms, timing error), pick one for
the node. This comparison is itself a result: what on-device-only costs.

**Consequence for success criterion 1:** the reference for the "≥ 99% same
decisions" check is the Python model run with the **same** future-VPD option
as the C port, not the Chronos-driven pipeline.

## 6. Decision rules (tomato)

Field capacity (FC): `VWC_FIELD_CAPACITY = 64.9` [`config.py`] for the
internship pot and probe only.

**Other sites (AGC greenhouses):** FC is not given, so it is estimated from
the data, per greenhouse:

- Primary: median VWC 1–2 h after each irrigation event ends (the level the
  substrate settles at once free water has drained).
- Fallback (if irrigation events are hard to detect): 95th percentile of VWC.

The estimate is reported as a limitation ("field capacity estimated from data,
not measured"). Field capacity, `warn_low` and the step length are parameters
of the C code, not constants. The dry-event definition must match the one
Derya uses for success criterion 3 (to confirm with her).

```
soil_fc = round(vwc / FC * 100, 1)
```

Thresholds [`plants.py`, tomato, default stage]:

| Channel | crit_low | warn_low | warn_high | crit_high |
|---|---|---|---|---|
| soil_fc | 60 | 70 | – | – |
| vpd | 0.3 | 0.5 | 0.8 | 1.0 |

Classification is **strict** [`rules.py: classify`]: `value < warn_low` is
WARN_LOW, `value > warn_high` is WARN_HIGH. So `soil_fc = 70.0` is NORMAL and
`69.9` is WARN_LOW.

| Rule | Fires when | Action |
|---|---|---|
| Rule 2, Irrigation | `soil_fc < 70` (WARN_LOW or CRIT_LOW) | irrigate now |
| Rule 1, Water stress | `vpd > 0.8` **and** `soil_fc < 70` | irrigate and/or lower VPD |

## 7. Warning

- **Now:** the rules run on the current sample.
- **Upcoming:** the rules run on each of the 36 forecast steps; the first step
  where a rule fires gives `when_minutes = k * 5`.
- A warning is issued whenever a rule fires **anywhere in the 3 h horizon**,
  not only when it is less than 1 h away. The "about an hour ahead" figure in
  the presentation describes the typical lead time, not the rule.
- To verify: whether `advise()` drops an upcoming rule that is already active
  now (see `advise.py` around line 255–300).

## 8. Memory estimate

| Buffer | Size |
|---|---|
| vwc, 288 × float32 | 1152 B |
| vpd, 288 × float32 | 1152 B |
| Total | ≈ 2.3 KB, kept in RTC memory (`RTC_DATA_ATTR`) across deep sleep |

If the node computes VPD on wake and stores it, temperature and humidity do
not need to be buffered.

## 9. Changes this causes elsewhere

- `docs/message_contract.md`: the `warn` field should mean "a rule fires
  within the 3 h horizon"; `t_cross_min` carries `when_minutes`.
- Precision: Python uses float64, the ESP32 float32. Disagreements near the
  70 % FC line are expected; they will be listed case by case, and Python will
  also be run in float32 to separate porting errors from rounding.
