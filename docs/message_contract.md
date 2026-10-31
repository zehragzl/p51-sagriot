# Message Contract (v0.2, draft)

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
| `sagriot/<node>/config` | gateway → node | when the interval changes (retained) | 1 |

### Flow

Every cycle the node sends both its reading and its own decision, then may
sleep; it does not wait for the cloud. The gateway (Mac now, Pi later)
keeps the edge decision and forwards the reading to the cloud through a
Mosquitto bridge (`gateway/bridge.conf.example`). The cloud answers with
`cloud_decision` for the same `seq`; the gateway pairs the two
(`gateway/compare.py`) and measures the round trip on its own clock.

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
