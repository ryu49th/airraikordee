# Protocol

### Wire format

- **Transport:** MQTT 3.1.1 to the Mosquitto broker on the Jetson, port 1884 (the team's user-level broker; `MQTT_PORT` in `.env` wins; the system broker on 1883 needs a login the team does not have), username and password from `.env`, keepalive 30 s, clean session. Nodes connect over Wi-Fi (Node 2) or through the UART bridge (Node 1). The Node 2 firmware example (`node2/include/secrets.example.h`) and the simulators still default to 1883: set the port to 1884.
- **Payload encoding:** one JSON object per message, UTF-8, compact (no spaces), Thai text written as UTF-8 (not `\uXXXX` escapes). The gateway never sends `NaN` or `Infinity`; a bad reading is `null`. No binary payloads.
- **QoS and retain:** the gateway publishes with QoS 1 (the vision module with QoS 0), retain off, and subscribes with QoS 1. The simulated nodes publish telemetry with QoS 0 and events and acks with QoS 1. Only `ab/gw/config` is retained (see MQTT additions).
- **Common fields:** every telemetry and command carries `node_id`, `seq` and `ts`.

Wire types used below. All of them are JSON types; the width is what the sender stores before it writes the number.

| Type | On the wire | Meaning |
|---|---|---|
| `string` | JSON string | UTF-8 text |
| `integer` | JSON number without a decimal point | whole number; unit and range in the comment |
| `float` | JSON number, decimals as shown | unit in the comment; decimals are what the sender rounds to |
| `boolean` | `true` / `false` | |
| `unix_seconds` | JSON number | seconds since 1970-01-01 UTC. |
| `uint32` | JSON integer 0 to 4294967295 | only `seq`; wraps to 0 |
| `null` | `null` | value unknown or missing; never `-1` or `0` |

### MQTT topics
| Topic | Direction | Payload |
|---|---|---|
| `ab/node1/telemetry` | bridge → all | `Node1Telemetry` |
| `ab/node1/event` | bridge → all | `Node1Event` |
| `ab/node1/cmd` | brain/voice → bridge | `Node1Cmd` |
| `ab/node2/telemetry` | dock → all | `Node2Telemetry` |
| `ab/node2/cmd` | brain → dock | `Node2Cmd` |
| `ab/node1/ack`, `ab/node2/ack` | receiver → sender | `Ack` |
| `ab/gw/env` | sensors → all | `GwEnv` |
| `ab/gw/vision` | vision → all | `GwVision` |
| `ab/gw/cause` | ai → all | `GwCause` |
| `ab/gw/state` | brain → all | `GwState` |

```ts
interface Node1Telemetry {
  node_id: string;          // "node1"
  seq: uint32;
  ts: unix_seconds;
  pm1: integer;             // µg/m³
  pm25: integer;            // µg/m³
  pm10: integer;            // µg/m³
  fan_rpm: integer;         // rpm, 0 to about 1700
  pan_deg: float;           // degrees, -180 to 180, 1 decimal
  tilt_deg: float;          // degrees, -30 to 30, 1 decimal
  button: boolean;          // true while the stop button is pressed
}

interface Node1Event {
  node_id: string;          // "node1"
  seq: uint32;
  ts: unix_seconds;
  type: "wake" | "stop_pressed" | "servo_timeout";   // JSON string
}

interface Node1CmdBase { seq: uint32; ts: unix_seconds }
type Node1Cmd = Node1CmdBase & (
  | { cmd: "aim"; pan_deg: float; tilt_deg: float }  // degrees
  | { cmd: "fan"; pct: integer }                     // 0 to 100
  | { cmd: "stop" }
  | { cmd: "survey_step"; pan_deg: float }           // degrees
);

interface Node2Telemetry {
  node_id: string;          // "node2"
  seq: uint32;
  ts: unix_seconds;
  docked: boolean;
  charging: boolean;
  voltage: float;           // V, 2 decimals
  current_a: float;         // A, 2 decimals
  temp_c: float;            // °C, 1 decimal
  est_percent: integer | null;   // 0 to 100; null while the cable is unplugged
}

interface Node2Cmd {
  seq: uint32;
  ts: unix_seconds;
  charge_allowed: boolean;
  reason: string;
}

interface Ack {
  ack_seq: uint32;          // the seq being acknowledged
  ts?: unix_seconds;        // present on acks the UART bridge publishes
}

interface GwEnv {
  ts: unix_seconds;
  temp_c: float;            // °C
  rh: float;                // % relative humidity
  pressure_hpa: float;      // hPa
}

interface GwVision {
  ts: unix_seconds;
  persons: {                // nearest first
    pan_err_deg: float;     // degrees, 1 decimal; > 0 = person is to the right
    tilt_err_deg: float;    // degrees, 1 decimal; > 0 = person is above the centre
    dist_est_m: float | null;   // metres, 2 decimals
    conf: float;            // 0 to 1, 2 decimals
  }[];
  person_present: boolean;
}

interface GwCause {
  ts: unix_seconds;
  cause: string;            // see CauseRecord
  confidence: float;        // 0 to 1, 2 decimals
  advice_th: string;
  snapshot_path: string;    // local Jetson path
}

interface GwState {
  ts: unix_seconds;
  mode: Mode;               // JSON string
  fan_pct: integer;         // 0 to 100
  target: { pan_deg: float; tilt_deg: float };   // degrees
  pm25_corrected: float;    // µg/m³
  alerts: StateAlert[];
}
```

```json
{
  "node_id": "node1",
  "seq": 4021,
  "ts": 1794383760,
  "pm1": 12,
  "pm25": 17,
  "pm10": 22,
  "fan_rpm": 1180,
  "pan_deg": 112.0,
  "tilt_deg": 10.0,
  "button": false
}
```

### UART (Node 1 ↔ bridge)
- Newline-delimited JSON, 115200 baud (USB-CDC ignores baud but keep it set), UTF-8.
- Commands from bridge need `{"ack": <seq>}` (`seq` = the command's `seq`, uint32) within 300 ms; retry up to 3 times, then publish `ab/gw/state` alert.
- Bridge must auto-reconnect if the serial port disappears (ESP32 reset / cable unplug).
- If Node 1 receives no command or heartbeat for 2 s it stops servos (handled in firmware). The bridge sends a heartbeat every 500 ms: `{"cmd": "hb", "ts": <unix_seconds>}`.

---

## Firebase Realtime Database (Jetson ↔ dashboard)

Read first: the dashboard does not use MQTT. It reads and writes Firebase only, and `uploader.py` on the Jetson copies MQTT data up and dashboard writes back down.
One Firebase project holds many units. Everything a unit owns lives under `devices/(deviceId)/`, with deviceId = `airraikordee-NN` (it never appears in MQTT topics).

General rules:
- Payloads keep the MQTT field names and units. `node_id` is dropped because the path already names the unit, and `seq` is dropped because it only orders MQTT messages. `ts` stays the sender's unix seconds (the Jetson keeps NTP so charts do not shift). The uploader sends only the fields listed under Records; the rest stays on the Jetson.
- Every record goes to the SQLite outbox first (E4) and is deleted only after HTTP 200. After a network outage, old records are sent with their original `ts`.
- The uploader sends many paths in one multi-path `PATCH devices/(deviceId).json`. "overwrite" / "PUT" below means the value at that path is replaced. Keys the uploader creates (`alerts`, `history`, `cause/history`) are made from `ts`, so a retried batch never adds a duplicate.
- Missing values are `null`, never `-1` or `0`.
- No secrets under `devices/(deviceId)/` (the owner can read everything there): the device password and API keys stay in the Jetson `.env`. The one exception is a notification webhook: the owner sets it from the dashboard into `deviceSecrets/(deviceId)/(channel)`, which the owner can write but not read back (see Notification channels). Snapshots never go to Firebase.

### Paths (all under `devices/(deviceId)/` unless noted)

| Path | Written by | Read by | How | Rate | Payload |
|---|---|---|---|---|---|
| `telemetry/node1/latest` | uploader | dashboard | overwrite | 1 s | Node 1 record (fields below) |
| `telemetry/node2/latest` | uploader | dashboard | overwrite | 1 s | Node 2 record (fields below) |
| `env/latest` | uploader | dashboard | overwrite | 1 s | last `ab/gw/env` |
| `state` | uploader | dashboard | overwrite | 1 s | last `ab/gw/state` |
| `history/node1/{minuteTs}` | uploader | dashboard | PUT | 1 min | 1-minute averages (retention 48 h is planned, nothing deletes old rows yet) |
| `history/node1_m15/{ts}` | uploader | dashboard | PUT | 15 min | planned, no code yet: 15-minute averages, kept 30 days |
| `cause/latest` | uploader | dashboard | overwrite | each result | cause record |
| `cause/history/{id}` | uploader | dashboard | PUT | each result | planned, no code yet: cause record |
| `alerts/{key}` | uploader | dashboard | PUT, new key | when an alert starts | alert record |
| `health` | uploader | dashboard | overwrite | 10 s (`uploader.health_every_s`; the code default is 30 s) | health record |
| `feedback/{push_id}` | owner (dashboard) | uploader | POST | button press | "ไม่ใช่" on a dust cause (the dashboard writes it; no gateway code reads it yet) |
| `config` | owner (dashboard) | uploader | PATCH | when saved | settings |
| `ui_cmd/{push_id}` | owner (dashboard) | uploader | POST | button press | one-shot command (planned: no dashboard button and no gateway code yet) |


Outside `devices/`:

| Path | Written by | Payload |
|---|---|---|
| `users/(uid)/devices/(deviceId)` | owner | `UserDevice`, proof the owner claimed the unit |
| `claims/(code)` | admin, then owner sets `used` | `Claim`, one-time claim code |
| `deviceSecrets/(deviceId)/(channel)` | owner (write only) | one secret string per notification channel, e.g. `discord`; read by the unit's own device account only |

```ts
interface UserDevice { code: string }
interface Claim { deviceId: string; used: boolean }
```

Claim order: the dashboard first writes `users/(uid)/devices/(deviceId)` (the rule checks `used` is still `false`),
then sets `claims/(code)/used = true`. The other order is denied, and nobody can list `claims`.

### Accounts and access

Two kinds of Firebase Auth account sign in:

| Account | Who | Sign-in | Can the owner change it |
|---|---|---|---|
| Owner | the end user, one account per person | the owner's own choice: email and password, a changed password, or another provider such as Google | yes, it is the owner's account |
| Device | one per unit, `device-(deviceId)@devices.airraikordee.example` | `signInWithPassword` from the Jetson, password in the Jetson `.env` | no, the owner never gets the password, and nobody changes it after the unit is sold |

The rules do not look at how an owner signs in. They check that `users/(uid)/devices/(deviceId)` exists for the signed-in `uid`. A device account is recognised by its email.

Rules (`dashboard/database.rules.json`; this file is not in the repo yet, it lives in Farn's card AIR-49 and is added when he publishes the rules):
- Device account: writes the paths the uploader writes, and reads its own unit, including `config`, `ui_cmd`, `feedback` and `deviceSecrets/(deviceId)`.
- Owner who claimed the unit: reads everything under `devices/(deviceId)/`; writes `config` and `feedback`; creates `ui_cmd` entries (cannot change an existing one; `by` must be the owner's `uid`; `cmd` must be one of the names below); writes `deviceSecrets/(deviceId)/(channel)`, whose value must start with `https://discord.com/api/webhooks/` and be at most 300 characters.
- Another unit's account, another owner, and anyone signed out read nothing (E5).

The Jetson refreshes its 1-hour ID token before it expires and retries once after HTTP 401. The dashboard signs in, reads `users/(uid)/devices` to find its deviceId, then listens to the smallest branch each screen needs.

`cloud/config_listener.py` (PR #5) polls `config` every `config_listener.poll_s` = 5 s (AIR-97 asks for 5 to 10 s), so a change on the web reaches the unit within about 5 s; it keeps the last good settings if Firebase is unreachable. Polling `ui_cmd` and `feedback` (every 3 s, skipping `feedback` keys already copied) is planned: no code reads them yet.

### Records

Field types use the wire types from the top of this file. Firebase stores JSON, so `integer` and `float` are both a JSON number there; `ts` is always a whole number (the uploader rounds it down).

**`telemetry/node1/latest`**: from `ab/node1/telemetry` (`pm25` and `pm10` are integers from the sensor on MQTT and a JSON number in Firebase; `pm1` stays on the Jetson, the uploader does not copy it); `pm25_corrected`, `fan_pct` and `mode` (= `state.mode`) from `ab/gw/state`; `person` = `ab/gw/vision.person_present`.
```ts
interface Node1Record {
  ts: unix_seconds;
  pm25: float | null;
  pm25_corrected: float | null;
  pm10: float | null;
  fan_pct: integer | null;
  fan_rpm: integer | null;
  pan_deg: float | null;
  tilt_deg: float | null;
  person: boolean | null;
  mode: Mode | null;
}
```

**`telemetry/node2/latest`**: from `ab/node2/telemetry`; `mode` / `fault` are the Node 2 values (MQTT addition 1); `plugged` = `docked`; `batt_temp_c` from `ab/node1/telemetry` (MQTT addition 2).
```ts
interface Node2Record {
  ts: unix_seconds;
  mode: "IDLE" | "DOCKED" | "CHARGING" | "FULL" | "FAULT" | "FIELD_TEST" | null;
  fault: "over_current" | "over_temp" | "timer" | null;   // planned: the uploader does not copy `fault` yet (the simulator sends it)
  voltage: float | null;
  current_a: float | null;
  temp_c: float | null;
  batt_temp_c: float | null;
  plugged: boolean | null;
  est_percent: integer | null;
}
```

**`env/latest`** = `ab/gw/env`:
```ts
interface EnvRecord {
  ts: unix_seconds;
  temp_c: float | null;
  rh: float | null;
  pressure_hpa: float | null;
}
```

**`state`**: from `ab/gw/state`. The uploader (PR #8) writes only four of its fields, and `alerts` as a list of type strings. `ts`, `pm25_corrected` and the alert objects stay in MQTT `GwState`; the alert details go to `alerts/{key}`. The dashboard accepts strings and objects. The outbox counts are `pending_rows` and `last_upload_ts` in `health`. Nothing publishes `ab/gw/state` yet (`brain/` is empty, no `decision.py`), so this path stays empty until it exists.
```ts
type Mode = "aim_person" | "survey" | "night" | "idle";   // names in the gateway spec, §4 decision.py (planned)

interface StateRecord {   // what the uploader writes
  mode: Mode;
  fan_pct: integer;
  target: { pan_deg: float; tilt_deg: float };
  alerts: AlertType[];
}

interface StateAlert {    // item of `GwState.alerts` on MQTT only
  type: AlertType;
  level: "info" | "warn" | "crit";
  title: string;
  text: string;
  since: unix_seconds;
}
```
```json
{
  "mode": "aim_person",
  "fan_pct": 45,
  "target": { "pan_deg": 112, "tilt_deg": 10 },
  "alerts": ["pm_high"]
}
```

**Cause record** (`cause/latest` is written by the uploader; `cause/history/{id}` is planned, no code yet). The cause modules also publish `features`, `reason`, `rules_cause` and `rules_confidence` on MQTT; they stay on the Jetson:
```ts
interface CauseRecord {
  id: string;               // "c" + ts. On MQTT only: the uploader does not copy `id` yet
  ts: unix_seconds;
  cause: "window_open" | "cooking" | "incense_candle" | "smoking" | "outdoor_infiltration" | "unknown";
  confidence: float;       // 0 to 1
  source: "rules" | "cloud";
  advice_th: string;
  pm25_corrected: float;    // planned: neither the cause modules nor the uploader send it
}
```
```json
{
  "id": "c1794383760",
  "ts": 1794383760,
  "cause": "cooking",
  "confidence": 0.74,
  "source": "cloud",
  "advice_th": "น่าจะมาจากการทำอาหาร เปิดพัดลมดูดควันหรือปิดประตูครัว",
  "pm25_corrected": 62.0
}
```
`snapshot_path` is a local Jetson path and is not uploaded (privacy).

**Alert record** (`alerts/{key}`): one entry when an item first appears in `state.alerts` (`ts` = its `since`), and one per `ab/gw/alert` message (MQTT addition 9). `{key}` is `ts` in milliseconds (13 digits) plus a 3-digit counter, so keys sort by time.
```ts
interface AlertRecord {
  ts: unix_seconds;
  type: AlertType;          // planned: the uploader (PR #8) does not copy `type` into the record yet
  level: "info" | "warn" | "crit";
  title: string;
  text: string;
  sent_to: string[];        // planned: no notify module yet, so nothing sets it (see Notification channels)
}
```
```json
{
  "ts": 1794383760,
  "type": "pm_high",
  "level": "warn",
  "title": "PM2.5 สูง",
  "text": "PM2.5 68 µg/m³ เกิน 10 นาที",
  "sent_to": ["discord"]
}
```

```ts
type AlertType = "pm_high" | "node_offline" | "uart_fail" | "charge_cut_temp" | "charge_fault"
               | "battery_low" | "charge_full" | "filter_clogged" | "stop_pressed";
```

| `type` | `level` | When |
|---|---|---|
| `pm_high` | warn | `pm25_corrected` > `config.pm_alert` for `config.pm_alert_min` minutes |
| `node_offline` | crit | `node*_online` turns false (node not seen for 30 s, see Offline rules) |
| `uart_fail` | warn | a Node 1 command failed after 3 retries |
| `charge_cut_temp` | crit | charging stopped for battery or charger temperature |
| `charge_fault` | crit | Node 2 latched FAULT (over-current, timer) |
| `battery_low` | warn | `est_percent` < 25 |
| `charge_full` | info | Node 2 reached FULL |
| `filter_clogged` | warn | fan RPM well below the filter baseline at the same `fan_pct` |
| `stop_pressed` | info | stop button pressed on the unit |

**`health`**:
```ts
interface HealthRecord {   // no `ts`: the uploader writes none (PR #4 relies on telemetry/node1/latest.ts)
  node1_online: boolean;
  node1_last_seen: unix_seconds;   // on MQTT only: the uploader does not forward it yet
  node2_online: boolean;
  node2_last_seen: unix_seconds;   // on MQTT only: not forwarded yet
  cpu_temp_c: float;
  ram_pct: integer;                // on MQTT only: not forwarded yet
  disk_pct: integer;               // on MQTT only: not forwarded yet
  pending_rows: integer;     // added by the uploader
  last_upload_ts: unix_seconds;   // added by the uploader
  filter_hours: float;
  pms_hours: float;
  fan_rpm_ratio: float;            // on MQTT only: not forwarded yet
}
```
```json
{
  "node1_online": true,
  "node1_last_seen": 1794383759,
  "node2_online": true,
  "node2_last_seen": 1794383757,
  "cpu_temp_c": 52.0,
  "ram_pct": 61,
  "disk_pct": 40,
  "pending_rows": 0,
  "last_upload_ts": 1794383758,
  "filter_hours": 312.5,
  "pms_hours": 410.0,
  "fan_rpm_ratio": 0.97
}
```
- `health.py` publishes `ts`, `node*_online`, `node*_last_seen`, `cpu_temp_c`, `ram_pct`, `disk_pct`, `filter_hours`, `pms_hours` and `fan_rpm_ratio` on `ab/gw/health`. The uploader copies `node*_online`, `cpu_temp_c`, `filter_hours` and `pms_hours` and adds `pending_rows` and `last_upload_ts`; the rest is not forwarded yet (PR #6 vs PR #8).
- `node*_online` follows `health.py` (`health.node_offline_s` = 30 s without data). `health.py` only sets the flag: no module sends the `node_offline` alert yet (planned; the PR #8 bridge test says it is `health.py`'s job). `node*_last_seen` is the unix time of the last message from that node, so the dashboard can say "Node 2 offline for 3 min". The `node_offline` alert fires at the same moment `node*_online` turns false (30 s), never later.
- `fan_rpm_ratio` is the RPM now divided by the RPM with a fresh filter at the same `fan_pct`. A falling ratio means the filter is clogging.
- The dashboard shows the whole unit as offline when `telemetry/node1/latest.ts` is older than 90 s (the gateway-silent rule below; PR #4 reads this path).

**Offline rules** (one value per question):

| Rule | Value | Who decides | What the web shows |
|---|---|---|---|
| One node is offline | 30 s without a message from that node | `health.py` (sets `node*_online` = false) | that node offline, "offline for N min" from `node*_last_seen` |
| `node_offline` alert | the same moment: `node*_online` turns false (30 s) | the gateway, from the same flag | an alert in the list |
| Whole unit / gateway silent | `telemetry/node1/latest.ts` older than 90 s | the dashboard, from its own clock | the whole unit offline |

The alert can never arrive after the web shows a node offline, because both follow the same `node*_online` flag. The 90 s rule is a separate check: the unit sends telemetry every 1 s and `health` every 10 s, so 90 s of silence (9 missed health heartbeats) means the whole unit or gateway is down, not that one node is late.

**History, two time frames** (`history/node1/{minuteTs}` and `history/node1_m15/{ts}`): key = unix seconds at the start of the bucket (`ts − ts % 60` for 1 min, `ts − ts % 900` for 15 min), 10 digits, so keys sort by time. A repeated write replaces the row instead of adding one. Values are averages over the bucket; `ts` = the last sample in it.
```ts
interface HistoryRow {
  ts: unix_seconds;
  pm25: float | null;
  pm25_corrected: float | null;   // planned: the uploader averages pm25, temp_c, rh, fan_pct only
  temp_c: float | null;
  rh: float | null;
  fan_pct: integer | null;
}
```
```json
{
  "1794383760": {
    "ts": 1794383790,
    "pm25": 17.0,
    "temp_c": 29.1,
    "rh": 71,
    "fan_pct": 40
  }
}
```
Charts read: 1 h = `limitToLast(60)` of `history/node1`, 24 h = `limitToLast(1440)`, 7 d = `limitToLast(10080)` of `history/node1` (what PR #4 does today; it downloads about 10k rows, so the 15-minute history below is the planned fix: 7 d = `limitToLast(672)` of `history/node1_m15`). After an outage there are gaps, so a chart that must end exactly at "now − window" uses `orderByKey().startAt(String(fromSec))` instead. Planned, no code yet: the uploader deletes keys older than the retention (48 h for 1 min, 30 days for 15 min) once a day, and a 1-minute row older than 48 h survives only inside its 15-minute average.

**`config`** (written by the owner). `config_listener.py` applies six fields today (`pm_alert`, `night_start`, `night_end`, `fan_mode`, `manual_fan_pct`, `filter_reset_ts`) and republishes them on `ab/gw/config`; a missing or invalid field falls back to `config.yaml`. The other fields below are planned (they need `decision.py`, which does not exist yet):
```ts
interface Config {
  pm_alert: integer;                 // µg/m³, 5 to 500; default 35 (`decision.pm_alert` in config.yaml; the gateway spec text still says 55)
  pm_alert_min: integer;             // minutes (planned)
  alert_cooldown_min: integer;       // planned
  night_enabled: boolean;            // planned
  night_start: string;              // "HH:MM", default 22:00
  night_end: string;                // "HH:MM", default 06:00
  night_fan_max_pct: integer;       // planned (config.yaml calls it night_fan_max: 35)
  manual_fan_pct: integer | null;    // 0 to 100
  fan_mode: "auto" | "manual";
  fan_table: [pm25_upper: integer, fan_pct: integer][];   // planned
  field_test: boolean;              // planned
  filter_reset_ts: unix_seconds;
  updated_ts: unix_seconds;
  updated_by: string;               // uid
  notify?: Record<string, NotifyChannel>;   // planned
}
```
```json
{
  "pm_alert": 35,
  "pm_alert_min": 10,
  "alert_cooldown_min": 15,
  "night_enabled": true,
  "night_start": "22:00",
  "night_end": "06:00",
  "night_fan_max_pct": 35,
  "manual_fan_pct": null,
  "fan_mode": "auto",
  "fan_table": [[15, 25], [35, 45], [55, 70], [100000, 100]],
  "field_test": false,
  "filter_reset_ts": 1730000000,
  "updated_ts": 1794383760,
  "updated_by": "<uid>"
}
```
- `pm_alert` / `pm_alert_min`: `pm_high` fires when `pm25_corrected` stays above `pm_alert` for `pm_alert_min` minutes.
- `alert_cooldown_min`: one alert per `type` in this window. Discord uses the same `type` list and the same limit.
- `night_*`: while night mode is on, the fan stays at or below `night_fan_max_pct`.
- `manual_fan_pct` is only used when `fan_mode` is `manual`; `manual` with `null` = `auto`.
- `fan_table`: `[pm25_corrected upper bound, fan_pct]`, sorted ascending (same table as `decision.py`).
- `field_test`: the 24-hour test flag for Node 2 (see CLAUDE.md).

### Notification channels

Each channel (a name such as `discord`) has two parts, so a new channel needs no new paths:

| Part | Path | Written by | Read by |
|---|---|---|---|
| Secret | `deviceSecrets/(deviceId)/(channel)` | owner, from the dashboard, any time | the unit's device account only (the owner cannot read it back) |
| Status and options | `config.notify.(channel)` | owner | dashboard, uploader |

The secret sits outside `devices/(deviceId)/` on purpose: the owner can read everything under `devices/(deviceId)/`, and a database rule can only add read access to a child, never take it away.

```ts
interface NotifyChannel {
  enabled: boolean;
  set_ts: unix_seconds;      // unix seconds when the owner last saved the secret
  hint: string;        // last 4 characters of the secret
}
```
```json
{
  "discord": { "enabled": true, "set_ts": 1794383760, "hint": "a1B2" }
}
```
- `enabled`: the unit sends alerts to this channel only when `true` and a secret exists.
- `set_ts`: the uploader re-reads the secret when this changes, so a new webhook works within seconds without a restart.
- `hint`: lets the dashboard show "set, ends with a1B2" without reading the secret.

Owner flow on the dashboard (Settings, notifications): paste the webhook URL, Save (writes `deviceSecrets/(deviceId)/discord`, then `config.notify.discord` with `enabled`, `set_ts`, `hint`), Send test (`ui_cmd` `test_notify`), Remove (deletes the secret, sets `enabled: false`). The dashboard never shows the full URL after saving.

`deviceSecrets/(deviceId)/discord` is a string that starts with `https://discord.com/api/webhooks/`. The uploader falls back to `DISCORD_WEBHOOK_URL` in the Jetson `.env` when no secret exists (bench and development).

**`ui_cmd/{push_id}`**: the uploader handles it, then **deletes** it (an unknown `cmd` is deleted without effect). The dashboard treats "gone" as "received".
```ts
interface UiCmd {
  ts: unix_seconds;
  by: string;          // uid
  cmd: "confirm_present" | "reset_fault" | "reset_filter" | "aim_person" | "survey_now" | "test_notify";
  channel?: string;    // test_notify only
}
```
```json
{ "ts": 1794383760, "by": "<uid>", "cmd": "confirm_present" }
```

| `cmd` | Effect |
|---|---|
| `confirm_present` | counts as "person seen" for 15 min from its `ts` (the app confirm in the charge rule) |
| `reset_fault` | clears a Node 2 FAULT if its cause is gone |
| `reset_filter` | new filter: `filter_hours` = 0, record a new RPM baseline |
| `aim_person` | turn to the nearest person now (same as the voice command) |
| `survey_now` | run a survey and update the dust cause now |
| `test_notify` | send a test message to one channel: `{ "cmd": "test_notify", "channel": "discord" }` |

**`feedback/{push_id}`**: the "ไม่ใช่" button. The uploader copies it into the SQLite accuracy table and leaves it in Firebase.
```ts
interface Feedback {
  ts: unix_seconds;
  by: string;              // uid
  cause_id: string;
  cause_ts: unix_seconds;
  correct: boolean;
  true_cause?: string;     // optional
}
```
```json
{
  "ts": 1794383760,
  "by": "<uid>",
  "cause_id": "c1794383760",
  "cause_ts": 1794383760,
  "correct": false,
  "true_cause": "incense_candle"
}
```

---

## MQTT additions for the dashboard

These add to the MQTT table above and leave it unchanged.

1. **`ab/node2/telemetry` + `mode`, `fault`.** `mode` ∈ `IDLE` · `DOCKED` · `CHARGING` · `FULL` · `FAULT` · `FIELD_TEST`; `fault` ∈ `null` · `over_current` · `over_temp` · `timer`.
2. **`ab/node1/telemetry` + `batt_temp_c`, `fan_pct`.** `batt_temp_c` feeds the charge rule. `fan_pct` lets health compare RPM against the commanded speed.
3. **`ab/gw/cause` + `id`, `source`.** `source` ∈ `rules` · `cloud`. `id` links the "ไม่ใช่" feedback to one result.
4. **`ab/gw/state.alerts[]` item** = `StateAlert` (types in the table above).
5. **`ab/gw/health`**, every 10 s: `HealthRecord` without `pending_rows` and `last_upload_ts` (the uploader adds those and sets `ts`).
6. **`ab/gw/config`** (retained): `config_listener.py` publishes the settings in use (QoS 1, retained) so `decision.py` / `health.py` get web changes.
7. **`ab/gw/ui_cmd`** *(planned, no code yet)*: the uploader publishes each `ui_cmd` record; `decision.py` / `health.py` act on it.
8. **`ab/node2/cmd` + optional `field_test`, `reset_fault`**, so the web settings and buttons reach the box (only the Node 2 simulator handles them today):
   ```ts
   type Node2CmdExtra = { field_test?: boolean; reset_fault?: boolean };   // sent with Node2Cmd
   ```
9. **`ab/gw/alert`**: one-off alerts from modules other than the brain, `{ ts, type, level, title, text }`; the uploader writes each to `alerts/{key}`. The UART bridge still sends the older names `severity` and `msg`; the uploader maps them to `level` and `text`. Only the UART bridge uses it (`uart_fail`), so `decision.py` must not also put `uart_fail` in `state.alerts`.
10. **`ab/gw/uploader`**, every 10 s, for `health.py`:
    ```ts
    interface GwUploader {
      ts: unix_seconds;
      outbox_pending: integer;
      last_upload_ts: unix_seconds;
      last_error: string | null;
    }
    ```

---

## Implementation status (checked against PRs #1 to #8 and main on 25 Nov 2026)

The code wins; this table lists what the doc describes and whether code exists. "PR" is the PR whose branch contains it.

| Item | State | Where |
|---|---|---|
| MQTT topics, QoS, retain, `seq`/`ts` rules | in code | `gateway/common` (PR #8), simulators (#2) |
| UART bridge: 300 ms ack, 3 retries, 500 ms heartbeat, `uart_fail` alert | in code | `bridge/uart_bridge.py` (PR #8) |
| Uploader: outbox, multi-path PATCH, token refresh, rates 1 s, minute history, alert keys | in code | `cloud/uploader.py` (PR #8) |
| `health.py`: 30 s offline flag, 10 s `ab/gw/health`, filter hours, RPM ratio | in code | PR #6 |
| `config_listener.py`: six config fields, retained `ab/gw/config`, manual fan resend | in code | PR #5 |
| `ab/gw/uploader` stats every 10 s | in code | PR #8 |
| Cause modules (rules, cloud), vision, `ab/gw/cause`, `ab/gw/vision` | in code | PR #8 |
| Dashboard: sign-in, claim, overview, history, alerts, health, settings (six config fields), demo mode | in code | PR #4 |
| `decision.py` (`ab/gw/state`, `ab/node1/cmd`, `ab/node2/cmd`, modes, `pm_high` and other alerts, `fan_table`, night mode, charge rule) | planned, no code | `gateway/brain/` is empty |
| `node_offline` alert | planned: the flag exists, nothing sends the alert | `health.py` |
| Notification channels (Discord, `deviceSecrets`, `config.notify`) | planned, no code | `gateway/notify/` is empty |
| `ui_cmd` handling, `ab/gw/ui_cmd`, dashboard buttons | planned, no code | |
| `feedback` copy to the SQLite accuracy table | planned, no code | |
| `cause/history`, `history/node1_m15`, retention deletes | planned, no code | |
| Health fields `node*_last_seen`, `ram_pct`, `disk_pct`, `fan_rpm_ratio` in Firebase | on MQTT only | uploader `HEALTH_FIELDS` |
| `database.rules.json` | not in the repo | Farn's card AIR-49 |

Still open (needs Farn): whether the web needs 1 s latest writes or 2 to 5 s, and whether `pm_alert` stays at 35 (the gateway spec still says 55).
