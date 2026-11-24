"""Baseline (driven drying) vs Chronos-tiny vs persistence on the school greenhouse log.

Same windows for every model: 24 h history (288 samples at 5 min), 3 h ahead
(36 samples), one origin per hour, horizons that contain an irrigation are
skipped (no model is told about irrigation). Signal-level metrics only;
decision-level scoring needs more threshold crossings than this log has (1).

Usage (on a machine with internet; first run downloads chronos-t5-tiny, ~35 MB)
  pip install pandas numpy torch chronos-forecasting
  python experiments/twin_school_check/compare_models.py ~/bitirme/data/okul_serasi/lab_log.csv
  python experiments/twin_school_check/compare_models.py LOG --no-chronos   # baseline only
"""
import argparse
import time

import numpy as np
import pandas as pd

CONTEXT, HORIZON, STEP = 288, 36, 12   # 24 h, 3 h, 1 h at 5 min
IRRIGATION_JUMP = 0.5                   # %VWC rise per 5 min that marks irrigation
START = "2026-10-06 17:00"              # continuous part of the log


def load(path):
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df.timestamp, utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    df = df.apply(pd.to_numeric, errors="coerce")
    r = df.resample("5min").mean()
    r.index = r.index.tz_convert("Europe/Istanbul")
    t = r.air_temp
    r["vpd"] = 0.6108 * np.exp(17.27 * t / (t + 237.3)) * (1 - r.air_humidity / 100)
    return r.loc[START:]


def baseline_forecast(hv, hp):
    """2-coefficient driven drying: step = a*VPD + c, fitted on non-irrigation steps."""
    steps, vpd = np.diff(hv), hp[1:]
    m = steps <= IRRIGATION_JUMP
    A = np.c_[vpd[m], np.ones(m.sum())]
    (a, c), *_ = np.linalg.lstsq(A, steps[m], rcond=None)
    return hv[-1] + np.cumsum(a * hp[-1] + c * np.ones(HORIZON))   # VPD persistence


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--no-chronos", action="store_true")
    ap.add_argument("--csv", default=None, help="write per-window errors")
    args = ap.parse_args()

    r = load(args.log)
    v, vpd = r.soil_vwc.values, r.vpd.values
    irr = np.r_[False, np.diff(v) > IRRIGATION_JUMP]

    origins = []
    for t in range(CONTEXT, len(v) - HORIZON, STEP):
        if np.isnan(v[t - CONTEXT:t + HORIZON + 1]).any() or np.isnan(vpd[t - CONTEXT:t + 1]).any():
            continue
        if irr[t + 1:t + HORIZON + 1].any():
            continue
        origins.append(t)
    print(f"{len(origins)} windows, {r.index[origins[0]]:%d %b} – {r.index[origins[-1]]:%d %b}")

    truth = np.array([v[t + 1:t + HORIZON + 1] for t in origins])
    preds = {"persistence": np.array([np.full(HORIZON, v[t]) for t in origins])}

    t0 = time.perf_counter()
    preds["baseline"] = np.array([baseline_forecast(v[t - CONTEXT:t + 1], vpd[t - CONTEXT:t + 1])
                                  for t in origins])
    ms = {"baseline": 1000 * (time.perf_counter() - t0) / len(origins)}

    if not args.no_chronos:
        import torch
        from chronos import ChronosPipeline
        pipe = ChronosPipeline.from_pretrained("amazon/chronos-t5-tiny", device_map="cpu",
                                               torch_dtype=torch.float32)
        out, t0 = [], time.perf_counter()
        for i in range(0, len(origins), 32):
            ctx = [torch.tensor(v[t - CONTEXT:t + 1], dtype=torch.float32) for t in origins[i:i + 32]]
            q = pipe.predict(ctx, prediction_length=HORIZON)        # [batch, samples, H]
            out.append(np.median(q.numpy(), axis=1))
        preds["chronos"] = np.concatenate(out)
        ms["chronos"] = 1000 * (time.perf_counter() - t0) / len(origins)

    print(f"\n{'model':12s} {'MAE 0-3h':>9s} {'err @3h':>8s} {'skill':>6s} {'ms/win':>7s}")
    base = np.abs(preds["persistence"] - truth).mean()
    for name, p in preds.items():
        mae = np.abs(p - truth).mean()
        e3 = np.abs(p[:, -1] - truth[:, -1]).mean()
        print(f"{name:12s} {mae:9.3f} {e3:8.3f} {1 - mae / base:6.1%} {ms.get(name, 0):7.2f}")
    print("\nMAE in %VWC points; skill = 1 - MAE/MAE(persistence).")

    if args.csv:
        rows = {"origin": [r.index[t] for t in origins]}
        for name, p in preds.items():
            rows[f"mae_{name}"] = np.abs(p - truth).mean(axis=1)
        pd.DataFrame(rows).to_csv(args.csv, index=False)
        print(f"written: {args.csv}")


if __name__ == "__main__":
    main()
