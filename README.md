# p51-sagriot

Edge AI decision-making for low-cost agricultural IoT sensor nodes
(GTÜ graduation project P51). A battery-powered ESP32-S3 node reads the soil
and the air, forecasts the next 3 h and warns before the plant runs dry.
The question: should that decision be made on the node (edge) or in the cloud?

```
ESP32-S3 node  --MQTT-->  Raspberry Pi gateway  -->  cloud
 firmware/                 gateway/                  cloud/
```

## Where things are

| Folder | What | Who |
|---|---|---|
| `docs/` | Integration plan (who delivers what, interfaces, timeline), message contract (MQTT topics and payloads), baseline port spec | all |
| `firmware/` | The node: one ESP-IDF project | |
| `firmware/main/` | The cycle: read, decide, send | Zehra |
| `firmware/components/baseline/` | Physics model ("driven drying") in C, with a PC test in `test/` | Zehra |
| `firmware/components/net/` | Wi-Fi, MQTT, clock | Zehra |
| `firmware/components/replay/` | Recorded 21-day log, used instead of sensors for now | Zehra |
| `firmware/components/sensors/` | Sensor drivers | Rüya |
| `firmware/components/tinyml/` | Learned model on the node | Derya |
| `gateway/` | Runs on the Pi: `logger.py` (MQTT → SQLite), `gateway_stats.py` (delivery report) | Zehra |
| `cloud/` | Cloud service. For now `fake_cloud.py`, which only echoes | Zehra |
| `experiments/` | One-off measurements with their results: `baseline_bench`, `tinyml_budget` | |
| `tools/` | PC-only helpers: `fake_node.py`, `compare_baseline.py` | |
| `data/` | Local databases and logs (not committed) | |

## Run the end-to-end test

Python side (once): `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`

1. Broker on the Mac: `brew services start mosquitto`. Its config needs `listener 1883 0.0.0.0` and `allow_anonymous true` so the node can connect (home network only)
2. `python gateway/logger.py`
3. `python cloud/fake_cloud.py`
4. Node: copy `firmware/sdkconfig.local.example` to `firmware/sdkconfig.local`, fill in Wi-Fi (2.4 GHz) and the Mac's IP, then in `firmware/`:
   `idf.py set-target esp32s3` and `idf.py -p <port> flash monitor`
5. `python gateway/gateway_stats.py`

## Working together

- Work in your own folder and on your own branch; merge to `main` when it builds.
- Never commit `firmware/sdkconfig.local` (it holds the Wi-Fi password; it is in `.gitignore`).
