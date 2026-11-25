// AirRaiKorDee Node 1 firmware - ESP32-S3 <-> Jetson uart_bridge (JSON lines over USB, 115200)
// Contract: docs/protocol.md + gateway/bridge/uart_bridge.py (branch gateway/bridge-uploader)
//
// Arduino IDE: Board "ESP32S3 Dev Module", PSRAM "OPI PSRAM", USB CDC On Boot "Enabled" (cable in the "USB" port)
// Libraries (Library Manager): ArduinoJson (v7), ESP32Servo
//
// Node 1 -> Jetson, one JSON object per line:
//   telemetry every 1 s : {"node_id":"node1","seq":N,"ts":<unix s>,"pm1","pm25","pm10","fan_rpm","fan_pct",
//                          "pan_deg","tilt_deg","button","batt_temp_c"}
//   events              : {"node_id":"node1","seq":N,"ts":..,"type":"stop_pressed"|"servo_timeout"}
//   ack                 : {"ack":<seq of the command>}            (within a few ms)
// Jetson -> Node 1:
//   {"cmd":"hb","ts":..}                    heartbeat every 0.5 s, no ack; also sets our clock
//   {"seq":N,"cmd":"fan","pct":0..100}
//   {"seq":N,"cmd":"aim","pan_deg":..,"tilt_deg":..}   pan 0 = front (+ right), tilt 0 = middle, +-20 (servo 90..130)
//   {"seq":N,"cmd":"survey_step","pan_deg":..}
//   {"seq":N,"cmd":"stop"}                  fan off, head and louver hold
// Safety: no line from the Jetson for 2 s -> head stops (event servo_timeout). Stop button -> everything stops,
// commands are ignored while it is held (they are still ACKed so the bridge does not resend).
// Head: no limit switch yet -> power on with the head facing FRONT (pan 0). Range +-PAN_LIMIT.
//
// Test without the Jetson: Serial Monitor 115200, "Newline", paste lines such as
//   {"cmd":"hb","ts":1763330000}
//   {"seq":1,"cmd":"fan","pct":50}
//   {"seq":2,"cmd":"aim","pan_deg":30,"tilt_deg":10}
// (the head stops again 2 s after your last line - that is the watchdog working)
#include <ArduinoJson.h>
#include <ESP32Servo.h>

// ---------- pins (agreed table) ----------
const int STEP_PINS[4] = {7, 5, 4, 6};   // IN1..IN4 (IN2/IN3 swapped to match the wiring, tested 24 Nov)
const int PIN_STOP = 3;            // button to GND
const int PIN_SERVO = 8;
const int PMS_RX = 9, PMS_TX = 46; // PMS TX -> 9
const int PIN_NTC = 10;            // 3V3 - 10k - pin - NTC - GND
const int FAN_PWM = 11, FAN_TACH = 12, PIN_RELAY = 13;
// INMP441 16/17/18 is reserved for the wake word (not in this sketch yet)

// ---------- settings ----------
const char *NODE_ID = "node1";
const bool RELAY_ACTIVE_HIGH = true;
const float STEPS_PER_HEAD_DEG = 2037.8864f * (89.0f / 12.0f) / 360.0f;   // ~42 full steps per head degree
const float PAN_LIMIT = 90;        // no limit switch yet; raise to 178 once it is fitted
const float TILT_LIMIT = 20;        // louver travel: servo 90..130 deg
const int STEP_PERIOD_MS = 2;      // 500 steps/s ~ 12 deg/s at the head
const uint32_t WATCHDOG_MS = 2000;
const uint32_t TELEMETRY_MS = 1000;
const float NTC_SERIES = 10000, NTC_R0 = 10000, NTC_BETA = 3950;

// ---------- shared state (guarded by stateMux where it matters) ----------
portMUX_TYPE stateMux = portMUX_INITIALIZER_UNLOCKED;
SemaphoreHandle_t txMutex;         // one writer at a time on Serial

volatile long panSteps = 0;        // current head position (motor steps, 0 = front)
volatile long panTargetSteps = 0;
volatile int tiltDeg = 0;
volatile int fanPct = 0;
volatile bool buttonDown = false;
volatile uint32_t lastRxMs = 0;
volatile bool watchdogTripped = false;
volatile uint32_t tachCount = 0;
volatile uint32_t fanRpm = 0;

double tsOffset = 0;               // unix seconds - millis()/1000, set by the heartbeat
bool timeSynced = false;
uint32_t txSeq = 0;
long lastCmdSeq = -1;

uint16_t pm1 = 0, pm25 = 0, pm10 = 0;
uint32_t pmsLastMs = 0;

Servo louver;

// ---------- helpers ----------
void sendJson(JsonDocument &doc) {
  xSemaphoreTake(txMutex, portMAX_DELAY);
  serializeJson(doc, Serial);
  Serial.print('\n');
  xSemaphoreGive(txMutex);
}

void addHeader(JsonDocument &doc) {
  doc["node_id"] = NODE_ID;
  doc["seq"] = ++txSeq;
  if (timeSynced) doc["ts"] = round((tsOffset + millis() / 1000.0) * 1000.0) / 1000.0;
  else doc["ts"] = nullptr;                      // protocol: unknown = null (uploader then uses the Jetson clock)
}

void sendEvent(const char *type) {
  JsonDocument doc;
  addHeader(doc);
  doc["type"] = type;
  sendJson(doc);
}

void sendAck(long seq) {
  JsonDocument doc;
  doc["ack"] = seq;
  sendJson(doc);
}

void setRelay(bool on) { digitalWrite(PIN_RELAY, (on == RELAY_ACTIVE_HIGH) ? HIGH : LOW); }

void setFan(int pct) {
  fanPct = constrain(pct, 0, 100);
  setRelay(fanPct > 0);                          // relay cuts fan power completely at 0 %
  ledcWrite(FAN_PWM, fanPct * 255 / 100);
}

void setTilt(float deg) {
  tiltDeg = (int)constrain(deg, -TILT_LIMIT, TILT_LIMIT);
  louver.write(110 + tiltDeg);                 // tilt -20..+20 -> servo 90..130, 0 = 110
}

void setPanTarget(float deg) {
  deg = constrain(deg, -PAN_LIMIT, PAN_LIMIT);
  portENTER_CRITICAL(&stateMux);
  panTargetSteps = lroundf(deg * STEPS_PER_HEAD_DEG);
  portEXIT_CRITICAL(&stateMux);
}

void holdHead() {
  portENTER_CRITICAL(&stateMux);
  panTargetSteps = panSteps;
  portEXIT_CRITICAL(&stateMux);
}

void haltAll() { setFan(0); holdHead(); }

float panDeg() { return panSteps / STEPS_PER_HEAD_DEG; }

// ---------- command handling ----------
void handleLine(const char *line) {
  JsonDocument doc;
  if (deserializeJson(doc, line)) return;        // not JSON: ignore
  lastRxMs = millis();
  const char *cmd = doc["cmd"] | "";

  if (strcmp(cmd, "hb") == 0) {                  // heartbeat: sync clock, no ack
    if (doc["ts"].is<double>()) {
      tsOffset = doc["ts"].as<double>() - millis() / 1000.0;
      timeSynced = true;
    }
    return;
  }

  bool hasSeq = doc["seq"].is<long>();
  long seq = hasSeq ? doc["seq"].as<long>() : -1;
  if (hasSeq) sendAck(seq);                      // ack first: the bridge waits only 300 ms
  if (hasSeq && seq == lastCmdSeq) return;       // resend of a command we already applied
  if (hasSeq) lastCmdSeq = seq;

  if (strcmp(cmd, "stop") == 0) { haltAll(); return; }
  if (buttonDown) return;                        // stop button held: ignore everything else

  if (strcmp(cmd, "fan") == 0) {
    setFan(doc["pct"] | 0);
  } else if (strcmp(cmd, "aim") == 0) {
    if (doc["pan_deg"].is<float>()) setPanTarget(doc["pan_deg"].as<float>());
    if (doc["tilt_deg"].is<float>()) setTilt(doc["tilt_deg"].as<float>());
  } else if (strcmp(cmd, "survey_step") == 0) {
    if (doc["pan_deg"].is<float>()) {
      float d = doc["pan_deg"].as<float>();
      if (d > 180) d -= 360;                     // 0..360 -> -180..180
      setPanTarget(d);
    }
  }
}

// ---------- tasks (FreeRTOS, E1) ----------
// Reads JSON lines from the Jetson.
void rxTask(void *) {
  static char buf[512];
  size_t n = 0;
  for (;;) {
    while (Serial.available()) {
      char c = Serial.read();
      if (c == '\n' || c == '\r') {
        if (n) { buf[n] = 0; handleLine(buf); n = 0; }
      } else if (n < sizeof(buf) - 1) {
        buf[n++] = c;
      } else {
        n = 0;                                   // line too long: drop it
      }
    }
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

// Steps the head toward its target; de-energises the coils when it arrives.
void motorTask(void *) {
  const uint8_t SEQ[4][4] = {{1,1,0,0},{0,1,1,0},{0,0,1,1},{1,0,0,1}};
  int idx = 0;
  bool energised = false;
  TickType_t last = xTaskGetTickCount();
  for (;;) {
    long diff = panTargetSteps - panSteps;
    if (diff != 0) {
      int dir = diff > 0 ? 1 : -1;
      idx = (idx + dir + 4) % 4;
      for (int p = 0; p < 4; p++) digitalWrite(STEP_PINS[p], SEQ[idx][p]);
      energised = true;
      portENTER_CRITICAL(&stateMux);
      panSteps += dir;
      portEXIT_CRITICAL(&stateMux);
      vTaskDelayUntil(&last, pdMS_TO_TICKS(STEP_PERIOD_MS));
    } else {
      if (energised) { for (int p = 0; p < 4; p++) digitalWrite(STEP_PINS[p], LOW); energised = false; }
      vTaskDelay(pdMS_TO_TICKS(10));
      last = xTaskGetTickCount();
    }
  }
}

// PMS5003 frames, stop button, fan RPM, Jetson watchdog.
void sensorTask(void *) {
  uint8_t frame[32];
  int n = 0;
  uint32_t lastRpmMs = millis();
  bool lastBtn = false;
  uint32_t btnChangeMs = 0;
  for (;;) {
    while (Serial1.available()) {
      uint8_t c = Serial1.read();
      if (n == 0 && c != 0x42) continue;
      if (n == 1 && c != 0x4D) { n = 0; continue; }
      frame[n++] = c;
      if (n == 32) {
        n = 0;
        uint16_t sum = 0;
        for (int i = 0; i < 30; i++) sum += frame[i];
        if (sum == ((frame[30] << 8) | frame[31])) {
          pm1 = (frame[10] << 8) | frame[11];
          pm25 = (frame[12] << 8) | frame[13];
          pm10 = (frame[14] << 8) | frame[15];
          pmsLastMs = millis();
        }
      }
    }

    // stop button, 30 ms debounce
    bool btn = digitalRead(PIN_STOP) == LOW;
    if (btn != lastBtn && millis() - btnChangeMs > 30) {
      btnChangeMs = millis();
      lastBtn = btn;
      buttonDown = btn;
      if (btn) { haltAll(); sendEvent("stop_pressed"); }
    }

    // fan RPM once a second (2 tach pulses per revolution)
    if (millis() - lastRpmMs >= 1000) {
      noInterrupts(); uint32_t c = tachCount; tachCount = 0; interrupts();
      fanRpm = c * 30;
      lastRpmMs = millis();
    }

    // watchdog: no line from the Jetson for 2 s -> stop the head
    bool silent = millis() - lastRxMs > WATCHDOG_MS;
    if (silent && !watchdogTripped) {
      watchdogTripped = true;
      holdHead();
      sendEvent("servo_timeout");
    } else if (!silent) {
      watchdogTripped = false;
    }
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}

void IRAM_ATTR onTach() { tachCount++; }

float readNtcC() {
  int mv = analogReadMilliVolts(PIN_NTC);
  if (mv > 3200 || mv < 50) return NAN;          // not connected / shorted
  float r = NTC_SERIES * mv / (3300.0f - mv);
  return 1.0f / (logf(r / NTC_R0) / NTC_BETA + 1.0f / 298.15f) - 273.15f;
}

// ---------- setup / loop ----------
void setup() {
  Serial.begin(115200);
  txMutex = xSemaphoreCreateMutex();

  for (int p = 0; p < 4; p++) { pinMode(STEP_PINS[p], OUTPUT); digitalWrite(STEP_PINS[p], LOW); }
  pinMode(PIN_STOP, INPUT_PULLUP);
  pinMode(PIN_RELAY, OUTPUT);
  setRelay(false);

  // Servo BEFORE the fan: ESP32Servo picks LEDC channel 0 on its own without asking the core. If the fan's
  // ledcAttach() took channel 0 first, the servo pin was put on the fan's 25 kHz channel and never moved.
  louver.setPeriodHertz(50);
  louver.attach(PIN_SERVO, 500, 2400);
  setTilt(0);

  ledcAttach(FAN_PWM, 25000, 8);              // core picks the next free channel + its own timer
  setFan(0);
  pinMode(FAN_TACH, INPUT_PULLUP);
  attachInterrupt(FAN_TACH, onTach, FALLING);

  Serial1.begin(9600, SERIAL_8N1, PMS_RX, PMS_TX);
  analogReadResolution(12);
  lastRxMs = millis();

  xTaskCreatePinnedToCore(motorTask, "motor", 3072, nullptr, 4, nullptr, 1);
  xTaskCreatePinnedToCore(rxTask, "rx", 6144, nullptr, 3, nullptr, 1);
  xTaskCreatePinnedToCore(sensorTask, "sensors", 6144, nullptr, 2, nullptr, 1);
  // core 0 stays free for the wake-word model later
}

void loop() {                                    // telemetry every second
  static uint32_t next = millis();
  if ((int32_t)(millis() - next) < 0) { delay(10); return; }
  next += TELEMETRY_MS;

  JsonDocument doc;
  addHeader(doc);
  if (pmsLastMs && millis() - pmsLastMs < 5000) {
    doc["pm1"] = pm1;
    doc["pm25"] = pm25;
    doc["pm10"] = pm10;
  } else {                                       // PMS5003 silent: null, never 0 or -1 (protocol.md)
    doc["pm1"] = nullptr;
    doc["pm25"] = nullptr;
    doc["pm10"] = nullptr;
  }
  doc["fan_rpm"] = fanRpm;
  doc["fan_pct"] = fanPct;
  doc["pan_deg"] = round(panDeg() * 10) / 10.0;
  doc["tilt_deg"] = tiltDeg;
  doc["button"] = (bool)buttonDown;
  float t = readNtcC();
  if (!isnan(t)) doc["batt_temp_c"] = round(t * 10) / 10.0;
  else doc["batt_temp_c"] = nullptr;
  sendJson(doc);
}
