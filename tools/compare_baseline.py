"""Compare the C baseline (firmware/components/baseline) with the Python reference.

Runs the internship code (zehragzl/sagriot-dss) and the C port on the same
recorded log and reports how often their decisions agree (success criterion 1).

Steps
  1. Load the log exactly as the reference does (advise.load_recent:
     5-min mean resample, interpolate gaps of up to 3 steps).
  2. Write vwc, air_temp, air_humidity to a CSV and run the C host test on it.
  3. For every 24 h window with no missing values, run the reference
     DrivenDrying(("vpd",)) with persistence future VPD and apply the
     Irrigation rule (soil_fc < 70, strict) to the current sample and to
     the 36 forecast steps.
  4. Compare irrigate_now and when_minutes window by window.

Usage
  python tools/compare_baseline.py ../sagriot-dss data/snapshot_0909.csv [--vpd seasonal]

  The first argument is the sagriot-dss checkout, the second the log inside it.
  Build the C test first:  make -C firmware/components/baseline/test
"""
import argparse
import csv
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
C_TEST = REPO / "firmware/components/baseline/test/test_baseline"
CONTEXT, HORIZON, STEP = 288, 36, 5
WARN_LOW = 70.0


def reference_decision(vwc_window, vpd_window, field_capacity, DrivenDrying, mode):
    """Python decision for one window: (irrigate_now, when_minutes)."""
    model = DrivenDrying(("vpd",))
    if mode == "seasonal":
        future = vpd_window[:HORIZON].copy()             # same time 24 h earlier
    else:
        future = np.full(HORIZON, vpd_window[-1])        # persistence
    forecast = model.predict(vwc_window, HORIZON, {"vpd": vpd_window}, {"vpd": future})

    def soil_fc(v):
        return round(v / field_capacity * 100, 1)

    now = soil_fc(vwc_window[-1]) < WARN_LOW
    when = next(((k + 1) * STEP for k, v in enumerate(forecast) if soil_fc(v) < WARN_LOW), -1)
    return now, when


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("dss", type=Path, help="path to the sagriot-dss checkout")
    parser.add_argument("log", help="log file, relative to the checkout")
    parser.add_argument("--field-capacity", type=float, default=None,
                        help="default: VWC_FIELD_CAPACITY from the reference config")
    parser.add_argument("--vpd", choices=["persistence", "seasonal"], default="persistence",
                        help="future VPD source (spec section 5)")
    args = parser.parse_args()

    sys.path.insert(0, str(args.dss))
    from sagriot.advise import load_recent
    from sagriot.config import VWC_FIELD_CAPACITY
    from sagriot.features import compute_vpd
    from sagriot.forecasters import DrivenDrying

    fc = args.field_capacity or VWC_FIELD_CAPACITY
    frame = load_recent(str(args.dss / args.log), hours=100000)
    frame = frame[["soil_vwc", "air_temp", "air_humidity"]]
    print(f"log: {len(frame)} samples at {STEP} min, field capacity {fc}, future VPD: {args.vpd}")

    # 1-2. Feed the same samples to the C code. NaN rows are written as nan
    # and the windows that contain them are left out of the comparison.
    if not C_TEST.exists():
        sys.exit(f"build the C test first: make -C {C_TEST.parent}")
    with tempfile.TemporaryDirectory() as tmp:
        data_csv = Path(tmp) / "data.csv"
        frame.to_csv(data_csv, index=False, header=["vwc", "air_temp", "air_humidity"],
                     float_format="%.6f", na_rep="nan")
        out = subprocess.run([str(C_TEST), str(data_csv), str(fc), args.vpd],
                             capture_output=True, text=True, check=True).stdout
    c_rows = {int(r["index"]): r for r in csv.DictReader(out.splitlines())}

    # 3-4. Reference decisions and comparison.
    vwc = frame["soil_vwc"].to_numpy(dtype=float)
    vpd = np.array([compute_vpd(t, h) if np.isfinite(t) and np.isfinite(h) else np.nan
                    for t, h in zip(frame["air_temp"], frame["air_humidity"])])

    compared = agree = warns = 0
    mismatches = []
    for end in range(CONTEXT - 1, len(frame)):
        w_vwc = vwc[end - CONTEXT + 1:end + 1]
        w_vpd = vpd[end - CONTEXT + 1:end + 1]
        if not (np.isfinite(w_vwc).all() and np.isfinite(w_vpd).all()) or end not in c_rows:
            continue
        now, when = reference_decision(w_vwc, w_vpd, fc, DrivenDrying, args.vpd)
        c = c_rows[end]
        c_now, c_when = bool(int(c["irrigate_now"])), int(c["when_minutes"])
        compared += 1
        warns += when != -1
        if now == c_now and when == c_when:
            agree += 1
        else:
            mismatches.append((end, now, c_now, when, c_when))

    if compared == 0:
        sys.exit("no complete 24 h window to compare")
    print(f"windows compared: {compared}  (with a warning: {warns})")
    print(f"agreement: {agree}/{compared} = {100 * agree / compared:.2f} %  (target >= 99 %)")
    for end, now, c_now, when, c_when in mismatches[:20]:
        print(f"  mismatch at sample {end}: python now={now} when={when}  |  C now={c_now} when={c_when}")
    if len(mismatches) > 20:
        print(f"  ... {len(mismatches) - 20} more")


if __name__ == "__main__":
    main()
