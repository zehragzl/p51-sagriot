"""The node's copy of Derya's model (firmware/components/tinyml) matches the cloud's."""
import csv
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "cloud"))

import derya_model                     # noqa: E402
import export_tinyml                   # noqa: E402

TEST_DIR = REPO / "firmware/components/tinyml/test"


def test_weights_file_is_up_to_date():
    """Fails when derya_model.npz changed but tools/export_tinyml.py was not rerun."""
    text, size, err = export_tinyml.render()
    assert export_tinyml.OUT.read_text() == text
    assert size <= 50 * 1024 and err < 0.01


@pytest.mark.skipif(shutil.which("gcc") is None or shutil.which("make") is None,
                    reason="needs gcc and make")
def test_c_forecast_matches_python(tmp_path):
    subprocess.run(["make", "-s", "-C", str(TEST_DIR)], check=True)
    rng = np.random.default_rng(1)
    n = 400
    vwc = (55 - 0.03 * np.arange(n) + rng.normal(0, 0.05, n)).astype(np.float32)
    t = 22 + 4 * np.sin(np.arange(n) / 288 * 2 * np.pi)
    rh = np.full(n, 60.0)
    data = tmp_path / "d.csv"
    with data.open("w") as f:
        f.write("vwc,air_temp,air_humidity\n")
        for row in zip(vwc, t, rh):
            f.write("%.6f,%.6f,%.6f\n" % row)
    out = subprocess.run([str(TEST_DIR / "test_tinyml"), str(data), "64.9"],
                         capture_output=True, text=True, check=True).stdout
    rows = list(csv.DictReader(out.splitlines()))
    assert len(rows) == n - 287

    es = 0.6108 * np.exp(17.27 * t / (t + 237.3))
    vpd = np.round(es - es * rh / 100, 3)
    worst = 0.0
    for r in rows:
        end = int(r["index"])
        py = derya_model.predict(vwc[end - 287:end + 1].astype(float), vpd[end - 287:end + 1], fc=64.9)
        c = np.array([float(r[f"f{j}"]) for j in range(1, 37)])
        worst = max(worst, np.abs(c - py).max())
    assert worst < 0.05                # VWC points; the quantization costs ~0.02
