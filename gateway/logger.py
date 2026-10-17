"""Gateway logger: stores every node and cloud message in SQLite.

First version of the Pi gateway service (docs/message_contract.md).
Subscribes to sagriot/#, keeps every raw message, and parses readings
and decisions into their own tables with the time they arrived, so the
delivery rate (seq gaps) and latency can be computed later.

Usage
  python gateway/logger.py                      # broker on localhost:1883
  python gateway/logger.py --host 192.168.1.10 --db data/gateway.db
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

import paho.mqtt.client as mqtt

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw (
    received_ms INTEGER NOT NULL,
    topic       TEXT    NOT NULL,
    payload     TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    node TEXT, seq INTEGER, ts TEXT,
    vwc REAL, soil_t REAL, ec REAL, air_t REAL, rh REAL, vpd REAL,
    batt_mv INTEGER, err INTEGER,
    received_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    source TEXT NOT NULL,              -- 'edge' (node) or 'cloud'
    node TEXT, seq INTEGER, ts TEXT, model TEXT,
    warn INTEGER, t_cross_min INTEGER, vwc_pred_3h REAL,
    infer_us INTEGER, t_send_ms INTEGER,
    received_ms INTEGER NOT NULL
);
"""

READING_FIELDS = ["node", "seq", "ts", "vwc", "soil_t", "ec", "air_t", "rh", "vpd",
                  "batt_mv", "err"]
DECISION_FIELDS = ["node", "seq", "ts", "model", "warn", "t_cross_min", "vwc_pred_3h",
                   "infer_us", "t_send_ms"]


def now_ms():
    return int(time.time() * 1000)


def store(db, topic, payload, received_ms):
    db.execute("INSERT INTO raw VALUES (?, ?, ?)", (received_ms, topic, payload))
    parts = topic.split("/")                 # sagriot/<node>/<kind>
    if len(parts) != 3:
        return "raw only"
    kind = parts[2]
    try:
        msg = json.loads(payload)
    except json.JSONDecodeError:
        return "not JSON, raw only"

    if kind == "reading":
        db.execute(f"INSERT INTO readings VALUES ({','.join('?' * 12)})",
                   [msg.get(f) for f in READING_FIELDS] + [received_ms])
    elif kind in ("decision", "cloud_decision"):
        source = "edge" if kind == "decision" else "cloud"
        db.execute(f"INSERT INTO decisions VALUES ({','.join('?' * 11)})",
                   [source] + [msg.get(f) for f in DECISION_FIELDS] + [received_ms])
    return kind


def main():
    parser = argparse.ArgumentParser(description="SAgrIoT gateway logger")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--db", default="data/gateway.db")
    args = parser.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"connected to {args.host}:{args.port} ({reason_code}), listening on sagriot/#")
        client.subscribe("sagriot/#", qos=1)

    def on_message(client, userdata, message):
        received = now_ms()
        payload = message.payload.decode("utf-8", errors="replace")
        kind = store(db, message.topic, payload, received)
        db.commit()
        print(f"{received}  {message.topic:32s} {kind}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sagriot-gateway")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        db.close()


if __name__ == "__main__":
    main()
