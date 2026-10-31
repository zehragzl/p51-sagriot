"""Stand-in for the cloud service: answers every reading with a cloud_decision.

Until the real cloud models run (Docker service, see the technical plan),
this script lets the node test the full loop node -> broker -> cloud -> node.
It echoes seq and t_send_ms, so the node can log the round trip on its own
clock. The decision fields are placeholders (model "echo"), not a forecast.

Usage
  python cloud/fake_cloud.py                  # broker on localhost:1883
  python cloud/fake_cloud.py --host 192.168.1.10 --delay 0.2
  python cloud/fake_cloud.py --host mosquitto --user sagriot --password ...
"""
import argparse
import json
import time

import paho.mqtt.client as mqtt


def main():
    parser = argparse.ArgumentParser(description="SAgrIoT fake cloud (echo)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--delay", type=float, default=0.0,
                        help="extra seconds before answering, to imitate model time")
    args = parser.parse_args()

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"connected to {args.host}:{args.port} ({reason_code}), answering sagriot/+/reading")
        client.subscribe("sagriot/+/reading", qos=1)

    def on_message(client, userdata, message):
        try:
            reading = json.loads(message.payload)
        except json.JSONDecodeError:
            print(f"skipped, not JSON: {message.topic}")
            return
        node = message.topic.split("/")[1]
        if args.delay:
            time.sleep(args.delay)
        answer = {
            "v": 1, "node": node, "seq": reading.get("seq"), "ts": reading.get("ts"),
            "model": "echo", "now": False, "warn": False, "t_cross_min": -1,
            "vwc_pred_3h": None, "infer_us": 0,
            "t_send_ms": reading.get("t_send_ms"),   # echoed for the round trip
        }
        client.publish(f"sagriot/{node}/cloud_decision", json.dumps(answer), qos=1)
        print(f"{node} seq {reading.get('seq')}: answered")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sagriot-fake-cloud")
    if args.user:
        client.username_pw_set(args.user, args.password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
