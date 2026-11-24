"""Runs the digital twin offline over a recorded log and prints what it said.

No MQTT, no broker: the same Twin class the cloud service uses is fed one
5-min sample at a time, as if the node had sent it. Use it to check the
twin on the internship record before trusting it live, and to make the
state timeline for the presentation.

Usage
  python cloud/replay_check.py ../sagriot-dss/data/snapshot_0909.csv
  python cloud/replay_check.py LOG.csv --fc 23.5 --csv data/twin_timeline.csv
  python cloud/replay_check.py LOG.csv --inject-drop    # adds the 9 Sept probe shift
  python cloud/replay_check.py LOG.csv --model chronos --model persistence

The log needs the internship columns: timestamp, soil_vwc, air_temp,
air_humidity (soil_temp optional). It is resampled to 5 min like the
reference (advise.load_recent: mean, gaps of up to 3 steps interpolated).
"""
import argparse
import csv

import pandas as pd

from baseline import Params
from models import load_model
from twin import Twin


def load_log(path):
    frame = pd.read_csv(path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    cols = [c for c in ("soil_vwc", "air_temp", "air_humidity", "soil_temp") if c in frame]
    frame = frame.set_index("timestamp")[cols].apply(pd.to_numeric, errors="coerce")
    return frame.resample("5min").mean().interpolate(limit=3, limit_area="inside")


def inject_probe_shift(frame, drop_to=43.0, steps=3, hold_steps=24):
    """Appends the 9 Sept event as it was logged: VWC fell from 55.8 to 43 in
    12 min when the probe was moved (sagriot-dss/data/events.csv). The real
    record stops just before it, so it is added after the last sample."""
    last = frame.iloc[-1]
    start = float(last["soil_vwc"])
    rows, index = [], []
    for k in range(1, steps + hold_steps + 1):
        row = last.copy()
        row["soil_vwc"] = start + (drop_to - start) * min(k, steps) / steps
        rows.append(row)
        index.append(frame.index[-1] + pd.Timedelta(minutes=5 * k))
    return pd.concat([frame, pd.DataFrame(rows, index=index)]), index[0]


def run(frame, params, node="replay", models=()):
    """Feeds the frame to a fresh twin; returns one state dict per sample."""
    twin = Twin(params, extra_models=list(models))
    out = []
    for seq, (ts, row) in enumerate(frame.iterrows()):
        reading = {"node": node, "seq": seq, "ts": ts.isoformat(),
                   "vwc": _num(row.get("soil_vwc")), "air_t": _num(row.get("air_temp")),
                   "rh": _num(row.get("air_humidity")), "soil_t": _num(row.get("soil_temp")),
                   "err": 0}
        _, state = twin.process(reading)
        out.append(state)
    return out


def _num(v):
    return None if v is None or pd.isna(v) else float(v)


def transitions(states):
    """(ts, from, to, reasons) every time the state changes."""
    changes, prev = [], None
    for s in states:
        if s["state"] != prev:
            changes.append((s["ts"], prev, s["state"], s["reasons"], s["soil_fc"]))
            prev = s["state"]
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("log")
    parser.add_argument("--fc", type=float, default=64.9, help="field capacity, %%VWC")
    parser.add_argument("--inject-drop", action="store_true",
                        help="append the 9 Sept probe displacement after the record")
    parser.add_argument("--csv", default=None, help="write the full timeline here")
    parser.add_argument("--model", action="append", default=[],
                        help="extra model (chronos, persistence, name=module:function)")
    args = parser.parse_args()

    frame = load_log(args.log)
    if args.inject_drop:
        frame, at = inject_probe_shift(frame)
        print(f"injected probe shift from {at}")
    states = run(frame, Params(field_capacity=args.fc),
                 models=[load_model(m) for m in args.model])

    counts = {k: sum(s["state"] == k for s in states) for k in ("happy", "thirsty", "fault")}
    print(f"{len(states)} samples | " + " | ".join(f"{k}: {v}" for k, v in counts.items()))
    for ts, a, b, reasons, fc in transitions(states):
        why = f"  ({'; '.join(reasons)})" if reasons else ""
        print(f"{ts}  {a or '-':7s} -> {b:7s}  soil_fc={fc}{why}")

    last = states[-1]
    if len(last["models"]) > 1:
        print("model choice at the end: " + last["selected_model"])
        for name, m in last["models"].items():
            print(f"  {name:12s} 3 h MAE {m.get('mae_3h')} VWC points over {m.get('scored')} forecasts")
        picks = {}
        for s in states:
            picks[s["selected_model"]] = picks.get(s["selected_model"], 0) + 1
        print("  samples per chosen model: " + ", ".join(f"{k} {v}" for k, v in picks.items()))

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "state", "vwc", "soil_fc", "now", "warn", "t_cross_min",
                        "no_water_warn_min", "water_now_warn_min", "reasons"])
            for s in states:
                wi = s["what_if"] or {}
                w.writerow([s["ts"], s["state"], s["vwc"], s["soil_fc"],
                            s["decision"]["now"], s["decision"]["warn"],
                            s["decision"]["t_cross_min"],
                            (wi.get("no_water") or {}).get("warn_in_min"),
                            (wi.get("water_now") or {}).get("warn_in_min"),
                            "; ".join(s["reasons"])])
        print(f"written: {args.csv}")


if __name__ == "__main__":
    main()
