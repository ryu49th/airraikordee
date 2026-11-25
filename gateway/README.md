# Gateway — Jetson Nano
Spec and build order: `JETSON_GATEWAY_SPEC.md`. Secrets: copy `.env.example` to `/etc/airraikordee/.env`.

## Layout
| Folder | What | Status |
|---|---|---|
| `common/` | `config.py` (config.yaml + .env), `protocol.py` (topics, JSON, seq), `mqtt_client.py`, `logging_setup.py`, `service.py` | done |
| `bridge/uart_bridge.py` | Node 1 USB serial <-> MQTT, ACK/retry, heartbeat, auto-reconnect | done |
| `cloud/uploader.py` | MQTT -> SQLite outbox -> Firebase REST under `devices/<id>`, device-account login, backoff (E4) | done (uplink) |
| `tools/sim_node1.py`, `tools/sim_node2.py` | fake nodes, see below | done |
| `vision/vision.py` | webcam + detectNet → `ab/gw/vision` (pan/tilt error, distance) 5 Hz; snapshots; 8-direction survey | done, needs the camera |
| `ai/cause_rules.py` | local dust-cause rules → `ab/gw/cause` (source rules) | done |
| `ai/cause_cloud.py` | blurred survey photos → Claude (`claude-opus-5-5`, JSON answer) when the rules are unsure | done, needs `AI_API_KEY` |
| `sensors/ voice/ brain/ notify/ health/` | other modules | todo |
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
python3 -m vision.vision
python3 -m ai.cause_rules
python3 -m ai.cause_cloud
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

## Firebase (uploader)
Paths and fields follow Farn's data contract (`docs/protocol.md` → "Firebase Realtime Database").
Everything goes under `devices/<DEVICE_ID>/` with one PATCH per batch. The Jetson signs in as
`device-<DEVICE_ID>@devices.airraikordee.example`, so `.env` needs `FIREBASE_DB_URL`, `FIREBASE_API_KEY`,
`DEVICE_ID` and `DEVICE_PASSWORD` (the password comes from Farn privately, never in Git).
`*/latest` and `state` are overwritten at most every 2 s, `history/node1/<minute>` gets one averaged row per
minute, `health` every 30 s, `cause/latest` and `alerts/<id>` on every event.
Not done here: reading `config` back down (AIR-97).

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
