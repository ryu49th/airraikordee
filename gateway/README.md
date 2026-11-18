# Gateway — Jetson Nano
Spec and build order: `JETSON_GATEWAY_SPEC.md`. Secrets: copy `.env.example` to `/etc/airraikordee/.env`.

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

Tests: `python3 -m pytest tests`

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
