# Jetson Nano gateway — implementation spec (AirRaiKorDee)

Read CLAUDE.md first for the overall project. This file defines the gateway software only.

## 0. Platform constraints (important)

- Jetson Nano dev kit, **JetPack 4.6 / Ubuntu 18.04 / Python 3.6**. Write Python 3.6-compatible code
  (no dataclasses, no walrus, no `list[int]` type hints). Pin library versions that still support 3.6.
- Person detection: use **jetson-inference `detectNet` (SSD-Mobilenet-v2, TensorRT)** — runs natively on
  JetPack 4.6 with the GPU. Do not use Ultralytics (needs Python ≥3.8).
- Cloud calls (Firebase, Discord, AI, speech) go over **HTTPS with `requests`** (REST APIs), not heavy SDKs.
- Secrets live in `/etc/airraikordee/.env` (never in Git). Commit only `.env.example`.

## 1. Hardware connections

| Device | Connection | Linux device |
|---|---|---|
| Node 1 (ESP32-S3) | USB cable (USB-CDC serial) | `/dev/ttyACM0` (use udev rule → `/dev/airraikordee-node1`) |
| Node 2 (dock, ESP32-C3) | Wi-Fi → local MQTT | broker on Jetson `:1883` |
| USB webcam (+ its mic) | USB | `/dev/video0`, ALSA mic |
| BME280 | I2C bus 1 (header pins 3/5), addr 0x76 | `smbus2` |
| USB speaker | USB | ALSA |

Note: the webcam mic is used only to record the spoken command **after** Node 1's on-chip wake word fires.
It is not counted as a sensor.

## 2. Process layout

All modules talk through the **local Mosquitto broker** (decoupled, easy to test). One systemd service per module,
`Restart=always`. Mosquitto itself also runs as a systemd service.

```
airraikordee/
  common/   config.py  mqtt_client.py  protocol.py  logging_setup.py
  bridge/   uart_bridge.py        # Node 1 UART <-> MQTT, seq/ACK/retry
  sensors/  bme280_reader.py      # publishes humidity/temp/pressure
  vision/   vision.py             # camera, detectNet, aiming, person-present flag, snapshots
  ai/       cause_rules.py        # local dust-cause rules (PM pattern, humidity, outdoor PM)
            cause_cloud.py        # Cloud AI on blurred snapshot, fallback to rules
  voice/    voice.py              # on wake: audio from ESP32 (INMP441), STT, intent -> command, TTS reply
  brain/    decision.py           # modes, fan speed, aim/survey, charge permission, alerts
  cloud/    uploader.py           # Firebase REST + SQLite store-and-forward
  notify/   discord.py            # alerts with rate limiting + photo
  health/   health.py             # heartbeats, node timeouts, self-checks
  tools/    sim_node1.py  sim_node2.py   # fake nodes for testing without hardware
  tests/
  systemd/  *.service
  .env.example  config.yaml  requirements.txt  README.md
```

## 3. Message protocol

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

## 4. Module behaviour

### decision.py (the "brain")
- **Humidity compensation:** `pm25_corrected = pm25 / (1 + k * max(0, rh - 60) / 40)`; `k` in config (calibrate later).
- **Modes:**
  - `aim_person` (default when person_present): P-controller on pan/tilt error → `ab/node1/cmd aim`, deadband 3°, max step 10°/update.
  - `survey`: every 30 min or on PM rise > 15 µg/m³ in 5 min → rotate 0→360° in 45° steps, request snapshot each step, trigger `cause_rules` then (if needed) `cause_cloud`.
  - `night` (22:00–06:00, config): fan ≤ 35 %, no speech, keep last aim.
  - `idle`: no person 10 min → fan by PM only, head faces room centre.
- **Fan speed:** lookup on `pm25_corrected` (≤15→25 %, ≤35→45 %, ≤55→70 %, >55→100 %), clamp by mode.
- **Charge permission (Node 2):** `charge_allowed = docked AND person_seen_within(15 min) AND temp_c < 45`. Publish on every change and every 60 s. The dock independently cuts charging on over-temp/over-current.
- **Alerts:** PM > 55 for 10 min, node offline > 60 s, charging cut for temperature, battery < 25 %.

### vision.py
- Capture 640×480 via OpenCV/GStreamer at ~10 fps; run detectNet every frame (or every 2nd if slow).
- Convert bbox centre to pan/tilt error using camera FOV from config. Estimate distance from bbox height.
- Publish `ab/gw/vision` at 5 Hz. Save snapshots on request to `/var/lib/airraikordee/snapshots/` (keep last 200).
- Privacy: snapshots stay local unless sent to the cause AI; blur faces before upload (OpenCV Haar or detectNet face model).

### cause_ai/ (two parts: local rules on the Jetson + Cloud AI)
The heavy image reasoning runs in the **cloud**; the Jetson decides when to ask, protects privacy and keeps a
working answer when the internet is down. Both publish the same `ab/gw/cause` message (`source: rules|cloud`).
- `cause_rules.py` (local, always runs first, < 50 ms):
  - inputs: last 30 min PM series, BME280 humidity, outdoor PM (public API, URL in config, cached).
  - rules (thresholds in config): fast spike (> 15 µg/m³ in 5 min) **with** humidity rise → `cooking`;
    fast spike, humidity flat → `incense_candle` / `smoking`; slow rise tracking outdoor PM → `window_open` /
    `outdoor_infiltration`; else `unknown`. Output `{cause, confidence, features}`.
- `cause_cloud.py` (only when online and the rule confidence < 0.8, max once per 10 min):
  - snapshot(s) from the survey → **blur faces on the Jetson before upload** → send image + rule features to a
    cloud vision-language model over HTTPS; **force JSON output** with
    `cause ∈ {window_open, cooking, incense_candle, smoking, outdoor_infiltration, unknown}`, `confidence 0–1`,
    `advice` (one short sentence for TTS).
  - timeout 10 s; on any failure keep the rule result. Never upload an unblurred image. API key only in `.env`.
- Log both answers + the user's correction (app/voice "ไม่ใช่") to SQLite → accuracy table for the report.

### voice.py
- On `ab/node1/event type=wake`: play short chime; the ESP32-S3 streams the INMP441 audio after the wake word as
  binary frames over the USB link (seq + ACK, ends on ~0.8 s silence or 5 s) → WAV → cloud STT.
- Intent parse (keyword rules first, LLM fallback): "หันมาทางนี้" → aim at nearest person; "ฟอกแรงขึ้น/เบาลง" → fan ±25 %;
  "ทำไมฝุ่นเยอะ" → trigger survey + speak cause; "หยุด" → stop.
- Reply with Thai TTS (cloud) and play on USB speaker. Offline: play pre-recorded Thai WAVs for basic replies.

### uploader.py (E4 store-and-forward)
- Subscribe to all telemetry/state/cause topics; write every record to SQLite `outbox` first.
- Background worker posts batches to Firebase Realtime Database REST (`PATCH/POST` with auth token) over HTTPS;
  delete rows only after HTTP 200. Exponential backoff 1 s → 5 min. Survives restarts (data on disk).
- Expose counts (pending rows, last success) on `ab/gw/state` for the dashboard.

### discord.py
- Discord **webhook** over HTTPS (POST JSON; `multipart/form-data` to attach the snapshot). Same alert max once per
  15 min. Include snapshot for cause alerts. Handle HTTP 429 by waiting `retry_after`. Optional later: a bot for
  commands from the phone ("/status", "/fan 3").

### health.py
- Track last-seen per node; mark offline after 30 s; publish heartbeat `ab/gw/health` every 10 s.
- Log CPU temp, RAM, disk; warn if disk > 85 %. Log rotation via logrotate.

## 5. Config and secrets
- `config.yaml`: topics, thresholds, mode times, camera FOV, PID gains, outdoor API URL, Firebase DB URL.
- `.env`: FIREBASE_TOKEN, DISCORD_WEBHOOK_URL (the URL itself is the secret), AI_API_KEY, STT/TTS keys.
- `.gitignore` must contain `.env`, `*.db`, `snapshots/`.

## 6. Metrics hooks (needed for the report)
Log to SQLite `metrics` table with timestamps:
- packet delivery: gaps in `seq` per node; latency: Node 1 `ts` vs receive time (sync clocks with NTP / time msg),
- PM-spike → fan-change latency, wake → action latency, aim error (deg),
- uptime per service (systemd restarts count), cloud upload backlog over time.

## 7. Testing
- `tools/sim_node1.py` and `tools/sim_node2.py` publish realistic fake data (also usable as simulated nodes).
- Unit tests: protocol encode/decode, ACK/retry, humidity compensation, fan table, charge permission, store-and-forward
  (simulate HTTP failures). Use pytest (version compatible with Python 3.6).
- Failure tests to script: kill each service, unplug Node 1 USB, stop Wi-Fi, stop broker, fill disk warning.

## 8. Build order (stop after each step for review)
1. Repo skeleton, config loading, logging, MQTT client wrapper, simulators.
2. uart_bridge with ACK/retry + reconnect (test with sim/ real ESP32).
3. bme280_reader, decision.py (fan + charge permission) using simulators.
4. uploader (Firebase + SQLite outbox) and discord.
5. vision.py (detectNet + aiming), then survey mode.
6. cause_rules.py, cause_cloud.py, then voice.py.
7. health.py, systemd units, install script, README.
