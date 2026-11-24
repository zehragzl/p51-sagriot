# Message Contract (v0.3, draft)

All node, gateway and cloud code follows this contract.
Every payload carries a version field `v`. Change the version when a field changes.

## Topics

`<node>` = node id, e.g. `node01`

| Topic | Direction | Sent when | QoS |
|---|---|---|---|
| `sagriot/<node>/reading` | node → gateway → cloud | every cycle; buffered batch on reconnect | 1 |
| `sagriot/<node>/decision` | node → gateway | every cycle (stays on the gateway) | 1 |
| `sagriot/<node>/status` | node → gateway → cloud | on boot and on errors (retained) | 1 |
| `sagriot/<node>/cloud_decision` | cloud → gateway | after the cloud models run on a reading | 1 |
| `sagriot/<node>/config` | cloud (twin) → gateway → node | when the send interval changes (retained) | 1 |
| `sagriot/<node>/twin` | cloud → gateway | after every reading: digital twin state (retained) | 1 |
| `sagriot/<node>/twin_ack` | gateway → cloud | a person checked the pot: clears a held fault | 1 |

### Flow

Every cycle the node sends both its reading and its own decision, then may
sleep; it does not wait for the cloud. The gateway (Mac now, Pi later)
keeps the edge decision and forwards the reading to the cloud through a
Mosquitto bridge (`gateway/bridge.conf.example`). The cloud answers with
`cloud_decision` for the same `seq`; the gateway pairs the two
(`gateway/compare.py`) and measures the round trip on its own clock.

### Twin payload (v0.3, `cloud/twin.py`)

Sent by the cloud service after every reading, retained, so a panel that
connects later sees the latest state at once.

```json
{"v":1, "node":"node01", "seq":1042, "ts":"2026-11-03T10:15:00Z",
 "state":"thirsty", "emoji":"😟", "label":"susamış", "reasons":[],
 "vwc":44.1, "soil_fc":67.9, "vpd":1.21, "air_t":24.1, "rh":58.2, "soil_t":21.8,
 "decision":{"now":true, "warn":true, "t_cross_min":5, "vwc_pred_3h":42.0},
 "what_if":{"horizon_h":120,
            "no_water":{"warn_in_min":0, "crit_in_min":1685, "vwc_end":34.6},
            "water_now":{"vwc_after":64.9, "warn_in_min":5190}},
 "selected_model":"baseline",
 "models":{"baseline":{"now":true, "warn":true, "t_cross_min":5, "vwc_pred_3h":42.0,
                       "mae_3h":0.08, "scored":288},
           "chronos":{"now":true, "warn":true, "t_cross_min":10, "vwc_pred_3h":42.6,
                      "mae_3h":0.11, "scored":288}},
 "last_irrigation_ts":"2026-11-01T09:20:00Z",
 "model":{"fitted":true, "samples":288, "needed":288, "a":-0.012, "c":-0.004},
 "params":{"field_capacity":64.9, "step_minutes":5}}
```

- `state`: `happy` (😊 mutlu), `thirsty` (😟 susamış: `now` or `warn`),
  `fault` (⚠️ arıza: wins over the others; `reasons` says why).
- `what_if` is `null` until the model has 24 h of readings. A `null`
  minute value means "not within `horizon_h`"; `0` means "already below".
- `water_now` assumes watering brings the soil back to field capacity.
- `forecast`: the followed model's 3 h forecast (36 VWC values, `null`
  before 24 h). `what_if.*.curve`: the next 24 h every `curve_step_min`
  minutes, for the panel. `drying_per_day`: VWC points lost per day at
  the current VPD. `params.warn_vwc` / `crit_vwc`: the 70 % / 60 % FC lines.
- `watering`: `status` is `pending` after a Telegram "Suladım" (feedback
  `watered`), `confirmed` when the VWC rise is seen, `unconfirmed` (a fault)
  if no rise within 30 min; `null` otherwise. The cloud also listens to
  `sagriot/<node>/feedback` for this.
- `models`: one entry per model the cloud runs (`--model`), each with its
  decision and its recent 3 h error; `selected_model` is the one the state
  comes from (`cloud/models.py`, ModelSelector). `decision` is that model's.
  The what-ifs always come from the baseline.
- `cloud_decision` is sent once per model per reading; `model` tells them
  apart (`gateway/compare.py --model chronos`).
- A sudden-drop fault stays until the pot is watered or a `twin_ack`
  arrives (any payload).

### Feedback (`cloud/telegram_bot.py`)

`sagriot/<node>/feedback` (bot → cloud, QoS 1) carries every Telegram button
press, with the state the farmer saw; the bot also appends it to
`feedback.jsonl`. This is the user-feedback data for closing the loop.

```json
{"time":"2026-11-24T10:44:53+00:00", "node":"node01", "action":"watered",
 "state":"thirsty", "seq":1042, "vwc":44.1, "soil_fc":67.9,
 "decision":{"now":true, "warn":true, "t_cross_min":5, "vwc_pred_3h":42.0}}
```

`action`: `watered` (✅ Suladım), `not_needed` (❌ Gerek yok), `snooze`
(⏰ remind in 1 h), `checked` (🔧 probe checked; the bot also sends `twin_ack`).

## Reading payload

```json
{"v":1, "node":"node01", "seq":1042, "ts":"2026-11-03T10:15:00Z",
 "vwc":31.4, "soil_t":21.8, "ec":0.62, "air_t":24.1, "rh":58.2,
 "vpd":1.26, "batt_mv":3910, "err":0}
```

## Decision payload

One message per model (`baseline`, `tinyml`).

```json
{"v":1, "node":"node01", "seq":1042, "ts":"2026-11-03T10:15:00Z",
 "model":"baseline", "now":false, "warn":true, "t_cross_min":48, "vwc_pred_3h":24.9,
 "infer_us":90, "t_send_ms":1730}
```

## Config payload

Published by the digital twin, retained, read by the node when it wakes
(docs/digital_twin_plan.md, section 4b).

```json
{"v":1, "send_every":3}
```

- `send_every`: send once every N measurement cycles (the node still measures
  and decides every cycle and keeps the samples in RTC memory). Default 1 if no
  config has been received. The node sends at once whenever its own decision
  has `warn` or `now`, whatever `send_every` says.

## Field notes

- `seq`: increases by 1 every cycle; the Pi uses gaps to count lost cycles.
- `ts`: node clock (UTC), synced by NTP when Wi-Fi is up.
- `t_send_ms`: node millisecond counter at publish time (node-side timing).
- `now`: true when the current sample is already below the irrigation line
  (Rule 2 fires now).
- `warn`: true when a rule fires anywhere in the 3 h forecast horizon
  (docs/baseline_port_spec.md, section 7).
- `ts` is `null` until the node clock has been set by SNTP.
- Missing values are sent as `null`.
- `t_cross_min`: minutes until the predicted crossing (-1 if none within 3 h).

## Units

| Field | Unit |
|---|---|
| `vwc` | % volumetric water content |
| `soil_t`, `air_t` | °C |
| `ec` | dS/m (bulk EC) |
| `rh` | % |
| `vpd` | kPa |
| `batt_mv` | mV |

## Open points

- Final probe fields depend on Rüya's driver.
- Cycle interval (5, 10 or 15 min) not decided yet.
- Decision content (which fields, which models) to be agreed by the team.
- `seq` restarts at 0 on reboot; add a per-boot id (or keep seq in RTC memory
  across deep sleep) so cycles stay unique. Until then the gateway pairs by
  seq + arrival time.
