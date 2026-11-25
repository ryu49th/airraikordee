# Gateway — Jetson Nano
Spec and build order: `JETSON_GATEWAY_SPEC.md`. Secrets: copy `.env.example` to `/etc/airraikordee/.env`.

## Layout
| Folder | What | Status |
|---|---|---|
| `common/` | `config.py` (config.yaml + .env), `protocol.py` (topics, JSON, seq), `mqtt_client.py`, `logging_setup.py`, `service.py` | done |
| `bridge/uart_bridge.py` | Node 1 USB serial <-> MQTT, ACK/retry, heartbeat, auto-reconnect | done |
| `cloud/uploader.py` | MQTT -> SQLite outbox -> Firebase REST under `devices/<id>`, device-account login, backoff (E4) | done (uplink) |
| `cloud/config_listener.py` | Firebase `devices/<id>/config` (dashboard Settings) -> `ab/gw/config` (retained) + manual fan to Node 1 (AIR-97) | done |
| `health/health.py` | node on/offline (30 s), CPU temp / RAM / disk, filter and PMS hours, fan RPM ratio -> `ab/gw/health` every 10 s (AIR-21) | done |
| `tools/sim_node1.py`, `tools/sim_node2.py` | fake nodes, see below | done |
| `vision/vision.py` | webcam + detectNet → `ab/gw/vision` (pan/tilt error, distance) 5 Hz; snapshots; 8-direction survey | done, needs the camera |
| `ai/cause_rules.py` | local dust-cause rules → `ab/gw/cause` (source rules) | done |
| `ai/cause_cloud.py` | blurred survey photos → Claude (`claude-opus-5-5`, JSON answer) when the rules are unsure | done, needs `AI_API_KEY` |
| `voice/voice.py` | wake word -> INMP441 clip from Node 1 -> Google STT -> command (keywords, Claude fallback) -> Google TTS on the speaker (AIR-20) | done, needs the keys + Node 1 audio frames |
| `notify/discord.py` | alerts (`ab/gw/alert`, `ab/gw/state`) -> Discord webhook, one per type per 15 min, blurred photo on PM alerts, 429 retry_after (AIR-52) | done, needs `DISCORD_WEBHOOK_URL` |
| `brain/decision.py` | PM humidity correction + fan table, modes (night / survey / aim_person / idle), aiming, `charge_allowed` for Node 2, state alerts -> `ab/gw/state` (AIR-13/14/15) | done, tested with the simulators |
| `sensors/bme280_reader.py` | BME280 on I2C bus 1 -> `ab/gw/env {temp_c, rh, pressure_hpa}` every 2 s, Bosch compensation, reopens the bus after errors (AIR-12) | done, needs the sensor |
| `udev/99-airraikordee.rules` | stable `/dev/airraikordee-node1` name for Node 1 | done |
| `tests/` | pytest, no hardware needed | |

## Setup on the Jetson (JetPack 4.6, Python 3.6)
```bash
sudo apt install mosquitto mosquitto-clients python3-pip
pip3 install -r requirements.txt
sudo mkdir -p /etc/airraikordee /var/lib/airraikordee /var/log/airraikordee
sudo cp .env.example /etc/airraikordee/.env    # then fill in the real values
sudo cp udev/99-airraikordee.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger
```

## Run at boot (systemd, AIR-22)
```bash
sudo systemd/install.sh            # as the user who owns this folder (or: sudo systemd/install.sh eiclite)
systemctl status 'airraikordee@*' --no-pager
journalctl -u airraikordee@brain.decision -f
sudo systemctl restart airraikordee.target      # after `git pull`
```
- One template unit, `airraikordee@<module>.service`, runs `python3 -m <module>` from this folder with
  `Restart=always` (3 s). `airraikordee.target` starts them all at boot.
- `systemd/modules.txt` lists the modules that run; remove a line, or `sudo systemctl disable
  airraikordee@<module>`, to leave one off (for example `voice.voice` until the keys are in `.env`).
- The installer adds the user to `dialout`, `i2c`, `video` and `audio`, creates `/var/lib/airraikordee` and
  `/var/log/airraikordee`, and warns if the user cannot read `/etc/airraikordee/.env`.
- The MQTT broker keeps its own crontab start (below); the modules reconnect by themselves when it comes up.

## MQTT broker (port 1884)
The gateway uses its own Mosquitto on **port 1884**, run as user `eiclite` without sudo
(the system broker on 1883 has a login the team does not know):
- config, password file, data and log in `~/airraikordee-mqtt/` (`mosquitto.conf`, `passwd`, `mosquitto.log`)
- login: user `gateway`; the password is only in `gateway/.env` (`MQTT_USER`, `MQTT_PASS`, `MQTT_PORT=1884`)
- started by the user crontab: `@reboot` and a watchdog every minute (`crontab -l | grep airraikordee-mqtt`)
- Node 2 (Wi-Fi) must connect to the Jetson's IP on port 1884 with the same login
- add a user: `mosquitto_passwd ~/airraikordee-mqtt/passwd <name>` then `kill -HUP $(cat ~/airraikordee-mqtt/mosquitto.pid)`

## Run (from this folder)
```bash
python3 -m bridge.uart_bridge
python3 -m cloud.uploader
python3 -m brain.decision
python3 -m sensors.bme280_reader
python3 -m cloud.config_listener
python3 -m health.health
python3 -m vision.vision
python3 -m ai.cause_rules
python3 -m ai.cause_cloud
python3 -m voice.voice
python3 -m notify.discord
python3 -m pytest tests
```
On a laptop the logs and the outbox go to `gateway/logs/` and `gateway/data/` (both git-ignored).

## Serial link to Node 1 (firmware must match)
- One JSON object per line, 115200 baud.
- Node 1 sends telemetry (`{"node_id":"node1","seq":..,"ts":..,"pm25":..}`) and events (same plus `"type"`).
- Every command line from the bridge carries the bridge's `seq`; Node 1 answers `{"ack":<that seq>}` within 300 ms.
  No answer: the same line is resent up to 3 times, then a `uart_fail` alert goes to `ab/gw/alert`.
- Heartbeat line every 500 ms: `{"cmd":"hb","ts":..}` (no ACK). Stop servos after 2 s without any line.
- When Node 1 ACKs, the bridge publishes `ab/node1/ack {ack_seq}` with the seq the sender used on MQTT.

## Voice commands (voice.py, AIR-20)
1. Node 1 hears the wake word and sends the `wake` event; the Jetson plays a chime.
2. Node 1 records until ~0.8 s of silence or 5 s and streams the clip as **audio lines** on the same serial link:
   ```
   {"audio":12,"i":0,"pcm":"<base64, 50 ms of 16 kHz 16-bit mono = 1600 bytes>"}
   ...
   {"audio":12,"end":64,"rate":16000,"reason":"silence"}
   ```
   `audio` is a clip number Node 1 picks per wake. These lines carry no `seq` and get no `ack`; the bridge
   forwards them on `ab/node1/audio`. 32 kB/s needs the ESP32-S3's **native USB (USB CDC)** port.
   Firmware side: `node1/examples/voice_stream_test/VoiceStream.h` (drop-in, compiled for the ESP32-S3).
3. voice.py answers on `ab/node1/cmd` with `{"cmd":"audio_ack","sid":12,"missing":[5,9]}`: Node 1 resends those
   frames and the `end` line again (up to 3 rounds). `missing: []` means done, free the clip (keep it in PSRAM
   until then, or 10 s at most).
4. Google Speech-to-Text (th-TH) -> keyword rules -> Claude if no keyword matched -> action -> Google
   Text-to-Speech on the USB speaker.

| Said | Action |
|---|---|
| "หันมาทางนี้" | `aim` to the nearest person in `ab/gw/vision` (current pan/tilt + the person's error) |
| "ฟอกแรงขึ้น" / "เบาลง" | `fan` ± 25 % from the current `fan_pct` |
| "ทำไมฝุ่นเยอะ" | says the advice of a cause from the last 10 min, else asks `ab/gw/cause/request` and a survey, waits up to 20 s |
| "หยุด" | `stop` |

- `.env`: `STT_API_KEY` / `TTS_API_KEY` (one Google Cloud API key with both APIs enabled), `AI_API_KEY` (Claude).
- Every fixed reply and dust-cause advice is cached as a WAV in `data_dir/tts_cache/`. Run
  `python3 -m voice.voice --prepare` once while online; after that the unit can still say "no internet" and the
  dust advice offline. `--say "ข้อความ"` speaks one sentence, `--wav file.wav` runs one recording through STT.
- Speaker: find the card with `aplay -l`, then set `voice.player: aplay -q -D plughw:<card>,0` in `config.yaml`.
- In night mode (`ab/gw/state.mode == "night"`) there is no chime and no spoken reply; the command still runs.
- Each command publishes `ab/gw/voice {ts, sid, text, intent, source, reply, ok, latency_s}`
  (`latency_s` = wake -> reply, for the metrics table).
- Manual fan from the dashboard (config_listener) re-sends its speed every 60 s, so a voice fan change lasts
  until then in manual mode; in auto mode it lasts until `decision.py` (not written yet) picks a new speed.
- Try it without the ESP32: `python3 -m tools.sim_node1 --voice-wav hello.wav` (16 kHz mono), run voice.py, press
  `w`. `--drop-audio 0.1` drops frames to test the resend.

## Decision (brain/decision.py, AIR-13/14/15)
Once a second, from the latest messages:
- **PM:** `pm25_corrected = pm25 / (1 + k * max(0, rh - 60) / 40)` (`rh` from `ab/gw/env`; without the BME280
  reader there is no correction). Fan % from `decision.fan_table`, capped at `night_fan_max` at night, sent on
  change and every 60 s. With `fan_mode: manual` on the dashboard, config_listener drives the fan instead.
- **Mode** (first match): `night` (dashboard night hours; fan capped, head keeps its aim) → `survey` (vision.py is
  taking its 8 photos; it starts surveys itself) → `aim_person` (someone seen in the last 10 min; P-control with
  3° deadband and 10° max step) → `idle` (head back to the room centre once).
- **Charging (rule 4.3):** `charge_allowed` = docked AND a person seen (or `{"cmd":"confirm_present"}` on
  `ab/gw/ui_cmd`) in the last 15 min AND battery temperature known and < 45 °C. Sent to `ab/node2/cmd` on change
  and every 30 s, because the box stops by itself 60 s after the last one.
- **Alerts** in `ab/gw/state.alerts`: `pm_high` (above `pm_alert` for 10 min, ends below 90 %), `charge_cut_temp`,
  `charge_fault`, `battery_low` (< 25 %), `charge_full`, `filter_clogged` (RPM ratio < 0.7 for 10 min),
  `stop_pressed`. Nothing goes to Node 1 while the stop button is held.
- Try it: `tools.sim_node1` + `tools.sim_node2` (without `--auto-allow`) + `brain.decision`; press `d` in sim_node1
  and watch the fan rise, and charging stops 15 min after the last person (no vision running = nobody seen).

## Discord alerts (notify/discord.py, AIR-52)
- Put the webhook in `.env`: Discord channel -> Edit Channel -> Integrations -> Webhooks -> New Webhook ->
  Copy Webhook URL -> `DISCORD_WEBHOOK_URL=...` (the URL is the password: never in Git, chat or screenshots).
  Check it with `python3 -m notify.discord --test`.
- Sends every one-off alert on `ab/gw/alert` and every alert type that newly appears in `ab/gw/state.alerts`, as an
  embed coloured by level (info blue, warn yellow, crit red). One message per alert type per 15 min
  (`notify.discord.cooldown_min`); the rest are counted in the stats and still go to Firebase through the uploader.
- `pm_high` gets the newest survey photo (at most 15 min old) with faces blurred on the Jetson; without OpenCV or
  the face model no photo is sent at all.
- HTTP 429 waits Discord's `retry_after`; other failures retry with backoff, up to 5 tries.
- Alert sources: `uart_fail` (uart_bridge), `node_offline` (health.py, once when a node that was online goes
  silent for 30 s) and the state alerts from `brain/decision.py` (`pm_high`, charge alerts, ...).

## Firebase (uploader)
Paths and fields follow Farn's data contract (`docs/protocol.md` → "Firebase Realtime Database").
Everything goes under `devices/<DEVICE_ID>/` with one PATCH per batch. The Jetson signs in as
`device-<DEVICE_ID>@devices.airraikordee.example`, so `.env` needs `FIREBASE_DB_URL`, `FIREBASE_API_KEY`,
`DEVICE_ID` and `DEVICE_PASSWORD` (the password comes from Farn privately, never in Git).
`*/latest` and `state` are overwritten at most every 2 s, `history/node1/<minute>` gets one averaged row per
minute, `health` every 10 s, `cause/latest` and `alerts/<id>` on every event.
Reading `config` back down is `cloud/config_listener.py` (below).

## Settings from the dashboard (config_listener)
Polls `devices/<DEVICE_ID>/config` every 5 s with the same device account and `.env` values as the uploader.
- Checks each field (`pm_alert` 5–500, `night_start` / `night_end` "HH:MM", `fan_mode` auto/manual,
  `manual_fan_pct` 0–100, `filter_reset_ts`); a missing or invalid one falls back to `config.yaml` (`decision:`).
- Publishes the settings in use on **`ab/gw/config`, retained** (`{ts, source, pm_alert, night_start, night_end,
  fan_mode, manual_fan_pct, filter_reset_ts}`, `source` = firebase / cache / defaults), so `decision.py` and
  `health.py` get them even if they start later.
- `fan_mode: manual` -> `ab/node1/cmd {"cmd":"fan","pct":manual_fan_pct}` on every change and again every 60 s.
  In `auto` it sends nothing: picking the speed from PM is `decision.py`'s job (AIR-13, not written yet).
- Firebase unreachable: keeps the last settings, polls slower (up to 60 s), and keeps them across a restart
  through `data_dir/config_cache.json`.
Try it without hardware: run `tools.sim_node1` and the listener, set the fan to manual on the dashboard, and the
sim's `fan_pct` follows within ~5 s.

## Dashboard buttons (ui_cmd)
config_listener also reads `devices/<DEVICE_ID>/ui_cmd` on every poll (5 s), publishes each entry on
`ab/gw/ui_cmd {ts, cmd, source: "app", channel?}` and deletes it from Firebase (the web shows "received" when it is
gone). Unknown commands and entries older than 10 min are deleted without effect.

| `cmd` | Handled by | Effect |
|---|---|---|
| `confirm_present` | decision.py | counts as "person seen" for the 15-min charge rule (does not aim the head) |
| `reset_fault` | decision.py | `ab/node2/cmd {reset_fault: true}`; Node 2 clears FAULT if the cause is gone |
| `reset_filter` | health.py | `filter_hours` = 0, new RPM baseline |
| `aim_person` | decision.py | turns straight to the nearest person the camera sees |
| `survey_now` | decision.py | `ab/gw/vision/cmd {cmd: survey}` |
| `test_notify` | notify/discord.py | one test message now, ignoring the cooldown |

## Dust-cause accuracy (ai/feedback.py)
The "ใช่ / ไม่ใช่" buttons on the dashboard write `devices/<id>/feedback`. The config_listener process copies each
new entry every 60 s into `data_dir/cause_log.db` together with the cause the user saw (it records every
`ab/gw/cause`). `python3 -m tools.accuracy_report` prints the table for the report (rated / correct per source,
and which causes were mistaken for which).

## Health (health.py)
- A node is **offline after 30 s** without any message; `ab/gw/health` goes out every 10 s and at once on a change,
  so the uploader and the dashboard see it within ~30 s (the uploader writes `health` to Firebase every
  `uploader.health_every_s`, 10 s, so the dashboard shows a dead node within ~40 s).
- `cpu_temp_c` from `/sys/class/thermal` (`CPU-therm` on the Nano; the fake `PMIC-Die` zone is skipped),
  `ram_pct` from `/proc/meminfo`, `disk_pct` of `data_dir`; a warning is logged above 85 %.
- `filter_hours` counts while Node 1 is online and the fan turns; `pms_hours` while PM readings arrive. A newer
  `filter_reset_ts` in `ab/gw/config` (the dashboard's "new filter" button) resets `filter_hours`.
- `fan_rpm_ratio` = RPM now / RPM at the same fan % during the first hour of the filter. A falling ratio means the
  filter is clogging (the `filter_clogged` alert itself belongs to `decision.py`, AIR-15).
- Counters are saved to `data_dir/health_state.json` every minute.
- Logs: every module rotates its own file (5 MB x 3). For the broker log see `logrotate/airraikordee-mqtt.conf`.

## Simulators (no hardware needed)

`tools/sim_node1.py` stands in for Node 1 + `uart_bridge`, `tools/sim_node2.py` for the charging box.
Both speak the topics in `docs/protocol.md`, answer commands with `ab/<node>/ack`, and print a status line every few messages.

```bash
pip3 install -r requirements.txt
brew install mosquitto && mosquitto -v      # laptop only; the Jetson already runs Mosquitto
```

Run each in its own terminal, from `gateway/`:

```bash
python3 -m tools.sim_node1
python3 -m tools.sim_node2 --auto-allow
mosquitto_sub -v -t 'ab/#'                  # watch everything
mosquitto_pub -t ab/node1/cmd -m '{"seq":1,"cmd":"fan","pct":80}'
```

MQTT host, user and password come from `.env` (`MQTT_HOST`, `MQTT_USER`, `MQTT_PASS`) or `--host / --user / --password`.

| Option | What it does |
|---|---|
| `--speed 60` | simulated time runs 60× faster (charge the battery in minutes) |
| `--seed 1` | repeatable random values |
| `sim_node1 --dust-every 0` | no random dust events (use key `d`) |
| `sim_node1 --drop-tel 0.1 --drop-ack 0.2` | lose messages on purpose to test seq gaps and retry |
| `sim_node2 --auto-allow` | allow charging by itself until `decision.py` sends `charge_allowed` |
| `sim_node2 --batt-temp 30` | fixed battery temperature when sim_node1 is not running |
| `sim_node2 --unplugged --no-battery --soc 0.2` | starting state |

Keys while running (letter + Enter, `?` for help):
- **sim_node1:** `d` dust event, `w` wake word, `s` stop button, `t` servo timeout, `c` clogged filter, `h` hot battery
- **sim_node2:** `p` plug cable, `b` battery in/out, `a` auto-allow, `f` field_test flag, `o` over-current, `h` hot box, `r` reset FAULT

Both sims also send the fields proposed in `docs/protocol.md` (`batt_temp_c`, `fan_pct`, `mode`, `fault`).
Remove them if the team rejects the proposal.

With sim_node1 running, do not also run `uart_bridge` (both would answer `ab/node1/cmd`).

### Known limits (the real Node 2 firmware must handle these)

The sim models the charge rules in CLAUDE.md, and two of them do not work as written once the unit is running
while it charges. The sim hides both, so passing tests do not prove the real box is right.

1. **FULL can't be detected from the INA226 alone.** The rule is "V ≥ 12.55 V and I < 150 mA for 60 s", but the
   INA226 sits before the unit, so it measures charge current **plus** the unit's load (≈1.2 A for Jetson, fan and
   motors). With the unit on, I never drops below 150 mA and the box never reaches FULL. The sim cheats: it compares
   `charge_a` (charge current only, `sim_node2.py` line ~205), which the real box cannot see.
   Options: subtract the load reported by Node 1, read pack current on the unit side, or detect FULL by the
   current settling (no change for several minutes) instead of a fixed threshold.
2. **The 4 h timer can trip on a normal charge.** The XL4015 limit (3 A) is shared with the load, so only
   ≈1.8 A reaches the 3S2P pack (≈7 Ah). A charge from empty needs ≈3.9 h in CC plus the CV tail, so it can hit
   the 4 h timer and latch FAULT. `test_charge_timer_fault` only shows that the timer works.
   Options: scale the timer to the measured charge current, or raise it to ≈6 h and rely on the temperature and
   over-current stops.

Also: `est_percent` is `null` while the cable is unplugged, because the box only sees voltage through the cable.
Battery level when unplugged has to come from Node 1.
