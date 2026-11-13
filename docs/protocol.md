# Protocol (shared contract — change only with the whole team)

All messages are JSON. Every telemetry/command carries `node_id`, `seq` (uint32, per sender), `ts` (unix seconds).

### MQTT topics
| Topic | Direction | Payload |
|---|---|---|
| `ab/node1/telemetry` | bridge → all | `{node_id, seq, ts, pm1, pm25, pm10, fan_rpm, pan_deg, tilt_deg, button}` |
| `ab/node1/event` | bridge → all | `{node_id, seq, ts, type: "wake"\|"stop_pressed"\|"servo_timeout"}` |
| `ab/node1/cmd` | brain/voice → bridge | `{seq, ts, cmd: "aim", pan_deg, tilt_deg}` / `{cmd:"fan", pct}` / `{cmd:"stop"}` / `{cmd:"survey_step", pan_deg}` |
| `ab/node2/telemetry` | dock → all | `{node_id, seq, ts, docked, charging, voltage, current_a, temp_c, est_percent}` |
| `ab/node2/cmd` | brain → dock | `{seq, ts, charge_allowed: bool, reason}` |
| `ab/+/ack` | receiver → sender | `{ack_seq}` |
| `ab/gw/env` | sensors → all | `{ts, temp_c, rh, pressure_hpa}` |
| `ab/gw/vision` | vision → all | `{ts, persons: [{pan_err_deg, tilt_err_deg, dist_est_m, conf}], person_present}` |
| `ab/gw/cause` | ai → all | `{ts, cause, confidence, advice_th, snapshot_path}` |
| `ab/gw/state` | brain → all | `{ts, mode, fan_pct, target, pm25_corrected, alerts:[...]}` |

### UART (Node 1 ↔ bridge)
- Newline-delimited JSON, 115200 baud (USB-CDC ignores baud but keep it set).
- Commands from bridge need `{"ack":seq}` within 300 ms; retry up to 3 times, then publish `ab/gw/state` alert.
- Bridge must auto-reconnect if the serial port disappears (ESP32 reset / cable unplug).
- If Node 1 receives no command or heartbeat for 2 s it stops servos (handled in firmware; bridge sends heartbeat every 500 ms).

---

## Firebase Realtime Database (Jetson ↔ dashboard)

Source of truth: Farn's data contract (https://web.polishstack.com:36523/airraikordee-contract/, copied 13 Nov).
Change paths or fields only through a PR to this file, approved by Komet (gateway) and Kittipich, then Farn updates
the contract page and the rules.

One project (`airraikordee`, Realtime Database in Singapore) for every unit. Everything of one unit lives under
`devices/(deviceId)`, deviceId = `airraikordee-NN`. The Jetson writes with its device account
`device-(deviceId)@devices.airraikordee.example` (`signInWithPassword`, ID token valid 1 h, refreshed before it
expires or after a 401). The dashboard reads, and writes only `config` and `feedback`.

```
devices/(deviceId)/
  telemetry/node1/latest    J  overwritten ~1 s
  telemetry/node2/latest    J  overwritten ~1 s
  env/latest                J
  history/node1/(minuteTs)  J  one row per minute, key = ts - ts % 60 (unix seconds)
  state                     J
  cause/latest              J
  alerts/(id)               J  one entry per new alert
  health                    J  ~30 s
  feedback/(id)             O  "ไม่ใช่สาเหตุนี้"
  config                    O  Settings; the Jetson reads it every 5–10 s
users/(uid)/devices/(deviceId) = {"code": "AB12-CD34"}   claim (object, not a bare string)
users/(uid)/line, users/(uid)/linkcode, lineLinks/(LINE userId)   LINE linking
claims/(code) = {"deviceId": "...", "used": false}       created by hand in the console
```

| Path | Fields | Filled by uploader from |
|---|---|---|
| `telemetry/node1/latest` | `ts, pm25, pm25_corrected, pm10, fan_pct, fan_rpm, pan_deg, tilt_deg, person (bool), mode` | `ab/node1/telemetry`; `pm25_corrected`, `fan_pct`, `mode` from `ab/gw/state`; `person` = `ab/gw/vision.person_present` |
| `telemetry/node2/latest` | `ts, mode, voltage, current_a, temp_c, batt_temp_c, plugged, est_percent` | `ab/node2/telemetry` (`plugged` = `docked` if absent); `batt_temp_c` from `ab/node1/telemetry` |
| `env/latest` | `ts, temp_c, rh, pressure_hpa` | `ab/gw/env` |
| `history/node1/(minuteTs)` | `ts, pm25, temp_c, rh, fan_pct` | averages over the minute; `ts` = last sample |
| `state` | `mode, fan_pct, target, alerts` | `ab/gw/state`; `alerts` = list of type strings, e.g. `["pm25_high"]` |
| `cause/latest` | `ts, cause, confidence, source (rules\|cloud), advice_th` | `ab/gw/cause` (`snapshot_path` is never uploaded) |
| `alerts/(id)` | `ts, level, title, text, sent_to` | each new item in `state.alerts`, and each `ab/gw/alert` (`severity` → `level`, `msg` → `text`) |
| `health` | `node1_online, node2_online, cpu_temp_c, pending_rows, last_upload_ts, filter_hours, pms_hours` | `ab/gw/health` + the uploader's own `pending_rows`, `last_upload_ts`, and node online from telemetry age |
| `feedback/(id)` | `cause_ts, correct (bool), true_cause` | dashboard |
| `config` | `pm_alert, night_start, night_end, manual_fan_pct, fan_mode, filter_reset_ts` | dashboard → Jetson config listener (AIR-97) |

- Every record goes to the SQLite outbox first and is deleted only after HTTP 200 (E4). One `PATCH devices/(id).json`
  carries many paths.
- `ts` = unix seconds of the sender; the Jetson must sync NTP.
- Units: µg/m³ · °C · % · hPa · A · V, kept out of field names except `_c`, `_pct`, `_a`.
- No secrets in Git: the device password is sent privately; the web `apiKey` is not secret.

---

## Proposed MQTT additions (need team OK before merge)

The dashboard needs these. They add to the MQTT table above, they do not change it.

1. **`ab/node2/telemetry` + `mode`, `fault`.** CLAUDE.md says Node 2 reports `mode` (`IDLE` · `DOCKED` · `CHARGING` · `FULL` · `FAULT` · `FIELD_TEST`) in every message, but the table only has `docked` / `charging`. `fault` ∈ `null` · `over_current` · `over_temp` · `timer`.
2. **`ab/node1/telemetry` + `batt_temp_c`, `fan_pct`.** CLAUDE.md already uses `ab/node1/telemetry.batt_temp_c` for the charge rule. `fan_pct` lets health compare RPM against the commanded speed.
3. **`ab/gw/cause` + `id`, `source`.** The spec text already says `source: rules|cloud`. `id` links the "ไม่ใช่" feedback to one result.
4. **`ab/gw/state.alerts[]`**: strings (`"pm25_high"`) as in the contract, or `{type, severity, msg, since}`; the uploader stores only the type in `state.alerts` and turns each new one into an `alerts/(id)` record.
5. **`ab/gw/health`** is in the spec (§4 health.py, every 10 s) but not in the table. Payload = the `health` fields above.
6. **`ab/gw/config`** (retained): uploader publishes the current `config` so `decision.py` gets web changes.
7. ~~`ab/gw/ui_cmd`~~ dropped: the contract has no `ui_cmd` path.
8. **`ab/node2/cmd` + optional `field_test`, `reset_fault`**: e.g. `{seq, ts, charge_allowed, reason, field_test: true}` or `{seq, ts, charge_allowed, reason, reset_fault: true}`, so the web settings and buttons reach the box.

9. **`ab/gw/alert`**: one-off alerts from modules that are not the brain, payload
   `{ts, type, severity, msg, source}`. The uploader writes each one to `alerts/(id)` as `{ts, level, title, text}`. Today only `uart_bridge`
   uses it (`uart_fail` after 3 retries), so `decision.py` should not also add `uart_fail` to `state.alerts`.
10. **`ab/gw/uploader`**: `{ts, outbox_pending, last_upload_ts, last_error}` every 10 s, for `health.py`.
11. **UART heartbeat line** = `{"cmd":"hb","ts":..}` every 500 ms, no ACK. Every other command line carries the
    bridge's own `seq`, and Node 1 answers `{"ack":<seq>}`. The bridge then publishes `ab/node1/ack {ack_seq}`
    with the seq the MQTT sender used.
12. **Vision and dust-cause topics** (gateway-internal, used by Komet's modules):
    - `ab/gw/vision` + `surveying` (bool): `decision.py` should not send `aim` while it is true.
    - `ab/gw/vision/cmd`: `{"cmd": "snapshot", "tag": ...}` or `{"cmd": "survey", "reason": ...}` (from `decision.py` or voice).
    - `ab/gw/vision/snapshot`: `{ts, path, tag, person_present}` after each snapshot (path is local to the Jetson).
    - `ab/gw/vision/survey`: `{ts, snapshots: [paths], reason}` when a survey ends.
    - `ab/gw/cause/request`: `{ts, reason}` asks `cause_rules` for a fresh answer (survey end, voice "ทำไมฝุ่นเยอะ").
    - `ab/gw/cause` + `id`, `source`, `features` (rules), `reason`, and for cloud answers `rules_cause`, `rules_confidence`.
      The uploader sends only the contract fields to Firebase.
