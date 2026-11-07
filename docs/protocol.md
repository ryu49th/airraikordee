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

