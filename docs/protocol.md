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

The dashboard never talks MQTT. It only reads and writes Firebase. `uploader.py` on the Jetson copies MQTT data up
and copies dashboard writes back down.

General rules:
- Payloads are the **MQTT payloads above with `node_id` and `seq` removed**. Same field names, same units, `ts` stays unix **seconds**.
- One unit only, so the tree starts at the root (no device id level).
- Every record goes to the SQLite outbox first (E4). After a network outage, old records are sent with their original `ts`.
- Missing values are `null`, never `-1` or `0`.

### Paths

| Path | Written by | Read by | How | Rate | Payload |
|---|---|---|---|---|---|
| `telemetry/node1` | uploader | dashboard | overwrite (PUT) | 5 s | last `ab/node1/telemetry` |
| `telemetry/node2` | uploader | dashboard | overwrite | 5 s | last `ab/node2/telemetry` |
| `env` | uploader | dashboard | overwrite | 5 s | last `ab/gw/env` |
| `state` | uploader | dashboard | overwrite | 5 s | last `ab/gw/state` (incl. outbox counts, see below) |
| `cause/latest` | uploader | dashboard | overwrite | each result | cause record |
| `cause/history/{id}` | uploader | dashboard | PUT | each result | cause record |
| `alerts/{push_id}` | uploader | dashboard | POST | when an alert starts | alert record |
| `health` | uploader | dashboard | overwrite | 30 s | health record |
| `history/m1/{ts}` | uploader | dashboard | PUT | 1 min | 1-minute averages, kept 48 h |
| `history/m15/{ts}` | uploader | dashboard | PUT | 15 min | 15-minute averages, kept 30 days |
| `config` | dashboard | uploader | PATCH | when saved | settings |
| `ui_cmd/{push_id}` | dashboard | uploader | POST | button press | one-shot command |
| `feedback/{push_id}` | dashboard | uploader | POST | button press | "ไม่ใช่" on a dust cause |

Write access: only the Jetson's Firebase account may write the paths the uploader writes. Any signed-in user may
write `config`, `ui_cmd` and `feedback`. Nobody signed out can read anything (E5). The rules file lives in
`dashboard/database.rules.json`.

The uploader polls `config` and `ui_cmd` every 3 s, so a change on the web reaches the unit within a few seconds.

### Records

**`state`** = `ab/gw/state` plus the counts the spec says the uploader exposes:
```json
{"ts": 1794383760, "mode": "aim_person", "fan_pct": 45, "target": {"pan_deg": 112, "tilt_deg": 10},
 "pm25_corrected": 21.4, "alerts": [{"type": "pm_high", "severity": "warn", "msg": "PM2.5 68 µg/m³ เกิน 10 นาที", "since": 1794383160}],
 "outbox_pending": 0, "last_upload_ts": 1794383758}
```
`mode` ∈ `aim_person` · `survey` · `night` · `idle`.

**Cause record** (`cause/latest` and `cause/history/{id}`):
```json
{"id": "c1794383760", "ts": 1794383760, "cause": "cooking", "confidence": 0.74, "source": "cloud",
 "advice_th": "น่าจะมาจากการทำอาหาร เปิดพัดลมดูดควันหรือปิดประตูครัว", "pm25_corrected": 62.0}
```
- `id` = `"c"` + `ts`.
- `cause` ∈ `window_open` · `cooking` · `incense_candle` · `smoking` · `outdoor_infiltration` · `unknown`.
- `source` ∈ `rules` · `cloud`.
- `snapshot_path` is a local Jetson path, so it is **not** uploaded. Snapshots never go to Firebase (privacy).

**Alert record** (`alerts/{push_id}`): one entry when an item first appears in `state.alerts`.
```json
{"ts": 1794383760, "type": "pm_high", "severity": "warn", "msg": "PM2.5 68 µg/m³ เกิน 10 นาที", "discord_sent": true}
```

| `type` | `severity` | When |
|---|---|---|
| `pm_high` | warn | `pm25_corrected` > `config.alert.pm_high` for `config.alert.pm_high_min` minutes |
| `node_offline` | crit | node not seen for 60 s |
| `uart_fail` | warn | a Node 1 command failed after 3 retries |
| `charge_cut_temp` | crit | charging stopped for battery or charger temperature |
| `charge_fault` | crit | Node 2 latched FAULT (over-current, timer) |
| `battery_low` | warn | `est_percent` < 25 |
| `charge_full` | info | Node 2 reached FULL ("ชาร์จเต็มแล้ว") |
| `filter_clogged` | warn | fan RPM well below the filter baseline at the same `fan_pct` |
| `stop_pressed` | info | stop button pressed on the unit |

Discord uses the same `type` list and the same rate limit (one per type per `config.alert.cooldown_min`).

**`health`**:
```json
{"ts": 1794383760,
 "nodes": {"node1": {"online": true, "last_seen": 1794383759},
           "node2": {"online": true, "last_seen": 1794383757}},
 "cpu_temp_c": 52.0, "ram_pct": 61, "disk_pct": 40,
 "filter_hours": 312.5, "pms_hours": 410.0, "fan_rpm_ratio": 0.97}
```
- `online` follows `health.py` (offline after 30 s without data).
- `fan_rpm_ratio` = RPM now ÷ RPM with a fresh filter at the same `fan_pct`. Falling ratio = filter clogging.
- The dashboard shows the **whole unit offline** when `health.ts` is older than 90 s.

**`history/m1/{ts}` and `history/m15/{ts}`** — key = unix seconds at the start of the bucket (10 digits, so key order = time order):
```json
"1794383760": {"pm25": 17.0, "pm25_corrected": 14.2, "temp_c": 29.1, "rh": 71, "fan_pct": 40}
```
Values are averages over the bucket. Charts read: 1 h = last 60 keys of `m1`, 24 h = last 1440 keys of `m1`,
7 d = last 672 keys of `m15` (`orderByKey().startAt(String(fromSec))`). The uploader deletes keys older than the
retention once a day.

**`config`** (written by the dashboard, used by `decision.py`; any missing or invalid field falls back to `config.yaml`):
```json
{"fan_table": [[15, 25], [35, 45], [55, 70], [9999, 100]],
 "night": {"enabled": true, "start": "22:00", "end": "06:00", "fan_max_pct": 35},
 "fan_manual_pct": null,
 "alert": {"pm_high": 55, "pm_high_min": 10, "cooldown_min": 15},
 "field_test": false,
 "updated_ts": 1794383760, "updated_by": "<uid>"}
```
- `fan_table`: `[pm25_corrected upper bound, fan_pct]`, sorted ascending (same table as `decision.py`).
- `fan_manual_pct`: `null` = automatic, a number 0–100 = fixed fan speed.
- `field_test`: the 24-hour test flag for Node 2 (see CLAUDE.md).

**`ui_cmd/{push_id}`** — the uploader handles it, then **deletes** it. The dashboard treats "gone" as "received".
```json
{"ts": 1794383760, "by": "<uid>", "cmd": "confirm_present"}
```

| `cmd` | Effect |
|---|---|
| `confirm_present` | counts as "person seen" for 15 min (the app confirm in the charge rule) |
| `reset_fault` | clears a Node 2 FAULT if its cause is gone |
| `reset_filter` | new filter: `filter_hours` = 0, record a new RPM baseline |
| `aim_person` | turn to the nearest person now (same as the voice command) |
| `survey_now` | run a survey and update the dust cause now |

**`feedback/{push_id}`** — the "ไม่ใช่" button. The uploader copies it into the SQLite accuracy table and leaves it in Firebase.
```json
{"ts": 1794383760, "by": "<uid>", "cause_id": "c1794383760", "correct": false, "actual": "incense_candle"}
```
`actual` is optional.

---

## Proposed MQTT additions (need team OK before merge)

The dashboard needs these. They add to the MQTT table above, they do not change it.

1. **`ab/node2/telemetry` + `mode`, `fault`.** CLAUDE.md says Node 2 reports `mode` (`IDLE` · `DOCKED` · `CHARGING` · `FULL` · `FAULT` · `FIELD_TEST`) in every message, but the table only has `docked` / `charging`. `fault` ∈ `null` · `over_current` · `over_temp` · `timer`.
2. **`ab/node1/telemetry` + `batt_temp_c`, `fan_pct`.** CLAUDE.md already uses `ab/node1/telemetry.batt_temp_c` for the charge rule. `fan_pct` lets health compare RPM against the commanded speed.
3. **`ab/gw/cause` + `id`, `source`.** The spec text already says `source: rules|cloud`. `id` links the "ไม่ใช่" feedback to one result.
4. **`ab/gw/state.alerts[]` item format** = `{type, severity, msg, since}` (types listed above).
5. **`ab/gw/health`** is in the spec (§4 health.py, every 10 s) but not in the table. Payload = the `health` record above.
6. **`ab/gw/config`** (retained): uploader publishes the current `config` so `decision.py` gets web changes.
7. **`ab/gw/ui_cmd`**: uploader publishes each `ui_cmd` record; `decision.py` / `health.py` act on it.
8. **`ab/node2/cmd` + optional `field_test`, `reset_fault`**: e.g. `{seq, ts, charge_allowed, reason, field_test: true}` or `{seq, ts, charge_allowed, reason, reset_fault: true}`, so the web settings and buttons reach the box.

