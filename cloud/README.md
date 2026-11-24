# Cloud

Runs on the cloud VM (now: Azure, Sweden Central, B2ats_v2, Ubuntu 24.04)
with Docker. Everything the VM needs is in this folder, so moving to a new
VM is: install Docker, copy the folder, create the two secret files,
`docker compose up -d`.

| File | What |
|---|---|
| `docker-compose.yml` | broker (Mosquitto) + cloud service |
| `mosquitto/mosquitto.conf` | broker config, password required |
| `mosquitto/passwd` | broker users (not committed, created on the VM) |
| `.env` | user/password for the cloud service (not committed) |
| `service.py` | cloud service: baseline decision + digital twin for every reading |
| `baseline.py` | driven-drying model, same decision as the node (float64) |
| `twin.py` | digital twin: state (😊 / 😟 / ⚠️), fault checks, what-ifs |
| `replay_check.py` | runs the twin offline over a recorded log (no broker) |
| `models.py` | extra models (Chronos, Derya's model) and the twin's model choice |
| `telegram_bot.py` | Telegram alerts with buttons; saves the farmer's feedback |
| `nodes.json.example` | per-node field capacity and cycle length |
| `tests/` | `python -m pytest tests -q` (from this folder) |
| `fake_cloud.py` | old stand-in service (model "echo"), kept for loop tests |

## Setup on a new VM

```bash
# from the Mac
scp -r cloud azureuser@<IP>:~/
# on the VM
cd ~/cloud
cp .env.example .env && nano .env                     # set the password
docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 \
  mosquitto_passwd -c -b /m/passwd sagriot '<password>'
sudo chown 1883:1883 mosquitto/passwd   # the broker runs as uid 1883
docker compose up -d
docker compose logs -f
```

Create `mosquitto/passwd` before the first `docker compose up`; otherwise
Docker creates an empty directory with that name.

Port 1883 must be open in the VM's network security group.
The connection is not encrypted yet (TLS is the next step).

## Cloud service (`service.py`)

For every `sagriot/<node>/reading` it publishes `sagriot/<node>/cloud_decision`
(model `baseline`, same `seq`) and `sagriot/<node>/twin` (retained); see
`docs/message_contract.md`. State (24 h buffer, last watering, held fault)
is saved in the `cloud-state` volume after every reading.

Run it on the Mac against the local broker, without the cloud:

```bash
pip install -r requirements.txt
python cloud/service.py --state data/cloud_state            # anonymous local broker
mosquitto_sub -h localhost -t 'sagriot/+/twin' -v           # watch the twin
```

Field capacity matters: the default 64.9 %VWC is the internship pot. For a
new pot, water it, wait 1-2 h for drainage, and use the plateau VWC
(`nodes.json`, then restart).

Check the twin on the internship record (needs `zehragzl/sagriot-dss`):

```bash
cd cloud
python replay_check.py ../../sagriot-dss/data/snapshot_0909.csv              # 5 watering cycles
python replay_check.py ../../sagriot-dss/data/snapshot_0909.csv --inject-drop # 9 Sept probe shift
SAGRIOT_DSS=../../sagriot-dss python -m pytest tests -q
```

## Telegram bot (`telegram_bot.py`)

Sends a message when the twin's state changes (😊 → 😟, → ⚠️, 😟 → 😊),
with buttons ✅ Suladım / ⏰ 1 saat ertele / ❌ Gerek yok (🔧 Kontrol ettim
for faults). Button presses go to `data/feedback.jsonl` and
`sagriot/<node>/feedback`. `/durum` shows the current state and what-ifs.

```bash
# Telegram: @BotFather -> /newbot -> copy the token
export TELEGRAM_TOKEN=...            # or put it in cloud/.env (git-ignored)
python cloud/telegram_bot.py         # next to service.py, same broker
# then send /start to the bot from your phone
```

## More models and the twin's choice (`models.py`)

```bash
pip install torch chronos-forecasting               # once, ~1 GB; first run downloads the model
python cloud/service.py --model chronos             # baseline + Chronos-Bolt tiny
python cloud/service.py --model chronos --model tinyml=derya_model:predict
python cloud/replay_check.py ../../sagriot-dss/data/snapshot_0909.csv --model chronos
```

Each model sends its own `cloud_decision`. The twin scores every forecast
once its 3 h have passed (only near the warning line, never across a
watering) and listens to the model with the lowest recent error; the
what-ifs stay with the baseline. On the internship record the baseline's
3 h error in that zone is 0.082 VWC points against 0.298 for "no change".

Chronos-Bolt tiny used 728 MB on the Raspberry Pi 5 (internship): it does
not fit next to the rest on a 1 GB VM; use a 2 GB machine.

A model for `--model name=module:function` is any
`predict(vwc_24h, vpd_24h) -> 36 VWC values` (288 inputs each, 5-min steps).

## Digital twin web panel (`gateway/twin_panel.py`)

A local page on the gateway (Mac now, Pi later): state, soil moisture against
the 70 % / 60 % lines, history, what-ifs and the model table; a 🔧 button
sends `twin_ack`. No external scripts, works offline.

```bash
python gateway/twin_panel.py          # then open http://localhost:8080
```
