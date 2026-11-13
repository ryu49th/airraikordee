# Gateway — Jetson Nano
Spec and build order: `JETSON_GATEWAY_SPEC.md`. Secrets: copy `.env.example` to `/etc/airraikordee/.env`.

## Layout
| Folder | What | Status |
|---|---|---|
| `common/` | `config.py` (config.yaml + .env), `protocol.py` (topics, JSON, seq), `mqtt_client.py`, `logging_setup.py`, `service.py` | done |
| `bridge/uart_bridge.py` | Node 1 USB serial <-> MQTT, ACK/retry, heartbeat, auto-reconnect | done |
| `cloud/uploader.py` | MQTT -> SQLite outbox -> Firebase REST, backoff (E4), paths from `docs/protocol.md` | done (uplink) |
| `tools/sim_node1.py`, `tools/sim_node2.py` | fake nodes, see below | done |
| `sensors/ vision/ ai/ voice/ brain/ notify/ health/` | other modules | todo |
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

## Run (from this folder)
```bash
python3 -m bridge.uart_bridge
python3 -m cloud.uploader
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
Paths and records follow `docs/protocol.md` → "Firebase Realtime Database". `node_id` / `seq` are dropped,
`telemetry/*`, `env`, `state` are overwritten at most every 5 s, `health` every 30 s, `cause/latest` +
`cause/history/{id}` and `alerts/{key}` on every event, and `history/m1` / `history/m15` hold 1-min / 15-min
averages (old keys deleted once a day). The REST call uses `?auth=FIREBASE_TOKEN` from `.env`.
Not done yet: reading `config`, `ui_cmd`, `feedback` back down (AIR-97).

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
