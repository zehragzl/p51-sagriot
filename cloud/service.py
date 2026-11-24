"""Cloud service: answers every reading with the cloud's decision and the twin.

Replaces fake_cloud.py. For every `sagriot/<node>/reading`:

  1. the node's digital twin (twin.py) checks the reading for faults and,
     if it is usable, adds it to a 24 h buffer, the same as the node does;
  2. the baseline (baseline.py) decides: now / warn / t_cross_min;
  3. publishes
       sagriot/<node>/cloud_decision   model "baseline", same seq (QoS 1)
       sagriot/<node>/twin             state, reasons, what-ifs (QoS 1, retained)

Listens to `sagriot/<node>/twin_ack` (clears a held fault, probe checked)
and `sagriot/<node>/feedback` (a "watered" press marks a watering as
reported; the twin waits for the VWC rise to confirm it). Both republish
the twin state at once.

One buffer step = one node cycle, as on the node; set step_minutes to the
node's period so the forecast minutes are right (5 in the field, the 10 s
test period gives meaningless minutes but still a fair edge/cloud check).

State is saved to STATE_DIR/<node>.json after every reading, so a restart
does not throw away the 24 h of history the model needs.

Usage
  python cloud/service.py                                  # broker on localhost
  python cloud/service.py --host mosquitto --user sagriot --password ...
  python cloud/service.py --nodes cloud/nodes.json --state data/cloud_state
  python cloud/service.py --model chronos            # + Chronos-Bolt (needs torch)
  python cloud/service.py --model tinyml=derya_model:predict

nodes.json (optional), per node:
  {"node01": {"field_capacity": 64.9, "step_minutes": 5}}
"""
import argparse
import json
import os
import resource
import sys
from pathlib import Path

from baseline import Params
from models import load_model
from twin import Twin

DEFAULTS = {"field_capacity": 64.9, "step_minutes": 5.0}


class Service:
    """MQTT-free core: handle() takes one message and returns what to publish."""

    def __init__(self, node_config=None, state_dir=None, models=()):
        self.node_config = node_config or {}
        self.models = list(models)            # extra models, shared by all nodes
        self.state_dir = Path(state_dir) if state_dir else None
        self.twins = {}

    def twin(self, node):
        if node not in self.twins:
            cfg = {**DEFAULTS, **self.node_config.get("default", {}),
                   **self.node_config.get(node, {})}
            twin = Twin(Params(field_capacity=float(cfg["field_capacity"]),
                               step_minutes=float(cfg["step_minutes"])),
                        extra_models=self.models)
            path = self._state_path(node)
            if path and path.exists():
                try:
                    twin.load_dict(json.loads(path.read_text()))
                    print(f"{node}: state loaded, {twin.model.count} samples")
                except (OSError, ValueError) as e:
                    print(f"{node}: state file unreadable ({e}), starting empty")
            self.twins[node] = twin
        return self.twins[node]

    def _state_path(self, node):
        return self.state_dir / f"{node}.json" if self.state_dir else None

    def _save(self, node):
        path = self._state_path(node)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.twins[node].to_dict()))
        os.replace(tmp, path)                      # never leaves a half-written file

    def handle(self, topic, payload):
        """Returns a list of (topic, payload_str, retain)."""
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "sagriot":
            return []
        node, kind = parts[1], parts[2]

        if kind == "twin_ack":
            twin = self.twin(node)
            twin.ack_fault()
            self._save(node)
            return self._republish(node, twin)
        if kind == "feedback":
            try:
                action = json.loads(payload).get("action")
            except (ValueError, AttributeError):
                return []
            if action != "watered":
                return []
            twin = self.twin(node)
            twin.report_watering(json.loads(payload).get("time"))
            self._save(node)
            return self._republish(node, twin)
        if kind != "reading":
            return []
        try:
            reading = json.loads(payload)
        except (ValueError, TypeError):
            print(f"{node}: reading is not JSON, skipped")
            return []
        reading.setdefault("node", node)

        twin = self.twin(node)
        decision, state = twin.process(reading)
        self._save(node)

        out = []
        for model, d in twin.last_decisions.items():
            cloud_decision = {
                "v": 1, "node": node, "seq": reading.get("seq"), "ts": reading.get("ts"),
                "model": model, "now": d.now, "warn": d.warn,
                "t_cross_min": d.t_cross_min, "vwc_pred_3h": d.vwc_pred_3h,
                "infer_us": twin.last_infer_us.get(model),
                "t_send_ms": reading.get("t_send_ms"),   # echoed: node-side round trip
            }
            out.append((f"sagriot/{node}/cloud_decision", json.dumps(cloud_decision), False))
        out.append((f"sagriot/{node}/twin", json.dumps(state, ensure_ascii=False), True))
        return out

    def _republish(self, node, twin):
        state = twin.current_state()
        return [] if state is None else [
            (f"sagriot/{node}/twin", json.dumps(state, ensure_ascii=False), True)]


def max_rss_mb():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024   # bytes on macOS


def main():
    import paho.mqtt.client as mqtt

    parser = argparse.ArgumentParser(description="SAgrIoT cloud service (baseline + twin)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--user", default=os.environ.get("MQTT_USER"))
    parser.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    parser.add_argument("--nodes", default=None, help="JSON file with per-node settings")
    parser.add_argument("--state", default="data/cloud_state", help="state directory")
    parser.add_argument("--model", action="append", default=[],
                        help="extra model next to the baseline: 'chronos', "
                             "'chronos=amazon/chronos-bolt-small' or 'tinyml=module:function'")
    args = parser.parse_args()

    node_config = json.loads(Path(args.nodes).read_text()) if args.nodes else {}
    models = [load_model(spec) for spec in args.model]
    service = Service(node_config, args.state, models)
    if models:
        print("models: baseline + " + ", ".join(m.name for m in models))
    handled = 0

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"connected to {args.host}:{args.port} ({reason_code})")
        client.subscribe([("sagriot/+/reading", 1), ("sagriot/+/twin_ack", 1),
                          ("sagriot/+/feedback", 1)])

    def on_message(client, userdata, message):
        nonlocal handled
        payload = message.payload.decode("utf-8", errors="replace")
        results = service.handle(message.topic, payload)
        for topic, out, retain in results:
            client.publish(topic, out, qos=1, retain=retain)
        twin_msgs = [json.loads(out) for topic, out, _ in results if topic.endswith("/twin")]
        if twin_msgs:
            handled += 1
            s = twin_msgs[0]
            print(f"{s['node']} seq {s['seq']}: {s['emoji']} {s['label']} | "
                  f"samples {s['model']['samples']}/288 | model {s['selected_model']} | "
                  f"max RSS {max_rss_mb():.0f} MB"
                  + (f" | {'; '.join(s['reasons'])}" if s["reasons"] else ""))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sagriot-cloud")
    if args.user:
        client.username_pw_set(args.user, args.password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print(f"stopped after {handled} readings")


if __name__ == "__main__":
    main()
