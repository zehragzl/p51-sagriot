"""Fake sensor node: publishes contract-format messages until the ESP32 is ready.

Sends one cycle every --period seconds (default 2 s instead of 5 min):
a reading (cloud mode) or a decision (edge
mode), with a rising seq and the node's millisecond counter in t_send_ms.
The values are synthetic; the point is to exercise the message path
node -> broker -> gateway logger, not the model.

Usage
  python tools/fake_node.py                         # edge mode, 20 cycles
  python tools/fake_node.py --mode cloud --cycles 50 --drop 0.1
"""
import argparse
import json
import math
import random
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


def vpd(t, rh):
    es = 0.6108 * math.exp(17.27 * t / (t + 237.3))
    return round(es - es * rh / 100.0, 3)


def main():
    parser = argparse.ArgumentParser(description="SAgrIoT fake node")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--node", default="node01")
    parser.add_argument("--mode", choices=["edge", "cloud"], default="edge")
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--period", type=float, default=2.0, help="seconds between cycles")
    parser.add_argument("--drop", type=float, default=0.0,
                        help="fraction of cycles to skip on purpose, to test seq-gap counting")
    args = parser.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.node)
    client.connect(args.host, args.port)
    client.loop_start()
    boot = time.monotonic()
    base = f"sagriot/{args.node}"

    status = {"v": 1, "node": args.node, "state": "boot", "mode": args.mode}
    client.publish(f"{base}/status", json.dumps(status), qos=1, retain=True)

    vwc = 50.0
    for seq in range(args.cycles):
        t = 22 + 4 * math.sin(seq / 10) + random.gauss(0, 0.2)
        rh = 65 - 10 * math.sin(seq / 10) + random.gauss(0, 1)
        vwc -= 0.05 + random.gauss(0, 0.01)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if random.random() < args.drop:
            print(f"seq {seq}: dropped on purpose")
            time.sleep(args.period)
            continue

        t_send_ms = int((time.monotonic() - boot) * 1000)
        if args.mode == "cloud":
            topic = f"{base}/reading"
            msg = {"v": 1, "node": args.node, "seq": seq, "ts": ts,
                   "vwc": round(vwc, 2), "soil_t": 21.0, "ec": 0.6,
                   "air_t": round(t, 2), "rh": round(rh, 1), "vpd": vpd(t, rh),
                   "batt_mv": 3900, "err": 0}
        else:
            topic = f"{base}/decision"
            msg = {"v": 1, "node": args.node, "seq": seq, "ts": ts,
                   "model": "baseline", "warn": vwc < 45.4, "t_cross_min": -1,
                   "vwc_pred_3h": round(vwc - 1.8, 2), "infer_us": 90,
                   "t_send_ms": t_send_ms}
        info = client.publish(topic, json.dumps(msg), qos=1)
        info.wait_for_publish()
        print(f"seq {seq}: sent {topic}")
        time.sleep(args.period)

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
