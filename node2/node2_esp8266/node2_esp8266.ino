// AirRaiKorDee Node 2 - charging box firmware, ESP8266 port for a bare ESP-12F module
// Same MQTT contract, safety rules and serial commands as node2_firmware.ino (ESP32-C3).
// Differences: no FreeRTOS on ESP8266 -> the charge state machine and the LED run as
// non-blocking 100 ms steps from loop(); the 0.2 s probe pulse no longer blocks.
//
// Arduino IDE: Board "Generic ESP8266 Module" (esp8266 core 3.x), Flash Size 4MB, Upload Speed 115200,
//   Reset Method "dtr (aka nodemcu)" if your USB-serial adapter has DTR/RTS wired, else "no dtr (aka ck)".
// Libraries: PubSubClient, ArduinoJson v7, OneWire, DallasTemperature
// Secrets: copy secrets.example.h to secrets.h and fill it in. Never commit secrets.h (E5).
//   ChulaWiFi (WPA2-Enterprise): also define WIFI_EAP_IDENTITY / WIFI_EAP_PASSWORD there (see wifiBegin()).
//
// Upload without auto-reset: hold FLASH (GPIO0 -> GND), tap RESET, release FLASH, then click Upload.
// After boot the FLASH button (GPIO0) is the FAULT reset button.
//
// Serial commands (115200, Newline): allow | deny | field on | field off | reset | status
#include <ESP8266WiFi.h>
#include <Wire.h>
#include <time.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include "secrets.h"
#ifdef WIFI_EAP_IDENTITY
extern "C" {
#include "user_interface.h"
#include "wpa2_enterprise.h"
}
#endif

// ---------- pins (ESP-12F GPIO numbers) ----------
// Keep GPIO0, GPIO2, GPIO15 free: they set the boot mode.
const int PIN_MOS = 13;     // MOSFET module PWM/TRIG input, HIGH = on (add 10k to GND so it stays off at boot)
const int PIN_ALERT = 14;   // INA226 ALERT, open drain, active LOW (10k-47k pull-up to 3V3)
const int PIN_DS18B20 = 12; // 4.7k pull-up to 3V3
const int PIN_SDA = 4;
const int PIN_SCL = 5;
const int PIN_LED = 16;     // status LED through 330R to GND (GPIO16 has no interrupt, fine for an LED)
const int PIN_BTN = 0;      // FLASH button, used as FAULT reset after boot

// ---------- settings ----------
const float R_SHUNT = 0.010;           // R010 on the red INA226 module
const float OVER_CURRENT_A = 2.7;      // ALERT threshold
const float PROBE_DOCKED_A = 0.03;     // probe current that means "a unit is on the cable"
const float UNPLUG_A = 0.02;           // current that means "cable pulled" while the MOSFET is on
const float CV_MIN_V = 12.45;          // XL4015 is in constant-voltage (end of charge) above this
const float FULL_I = 0.07;             // unit switched off: full when the current drops below this
const float SETTLE_BAND_A = 0.05;      // unit running: full when the current stops falling (see isFull())
const uint32_t SETTLE_WINDOW_MS = 10UL * 60 * 1000;
const uint32_t FULL_HOLD_MS = 60000;
const float BOX_HOT_C = 60.0, BATT_HOT_C = 45.0;
const float CHARGE_TIMER_H = 8.0;      // CLAUDE.md says 4 h, but with the unit running only part of the
                                       // XL4015 current reaches the 7 Ah pack (see gateway README "Known limits")
const uint32_t ALLOW_TIMEOUT_MS = 60000;
const uint32_t BATT_TEMP_STALE_MS = 10000;
const bool REQUIRE_BATT_TEMP = false;   // false only for bench tests before the NTC is wired
const uint32_t FULL_RECHECK_MS = 30UL * 60 * 1000;   // FULL: try a top-up every 30 min
const uint32_t TELEMETRY_MS = 2000;
const uint8_t INA_ADDR = 0x44;
const float VBUS_SCALE = 0.9559;        // calibrate: meter V / INA V

// ---------- topics ----------
const char *NODE_ID = "node2";
const char *T_TEL = "ab/node2/telemetry";
const char *T_CMD = "ab/node2/cmd";
const char *T_ACK = "ab/node2/ack";
const char *T_NODE1 = "ab/node1/telemetry";

// ---------- state ----------
enum Mode { IDLE, DOCKED, CHARGING, FULL, FAULT, FIELD_TEST };
const char *MODE_NAME[] = {"IDLE", "DOCKED", "CHARGING", "FULL", "FAULT", "FIELD_TEST"};

volatile Mode mode = IDLE;
volatile const char *faultName = nullptr;      // "over_current" | "over_temp" | "timer"
volatile bool mosOn = false;
volatile bool alertFired = false;
volatile bool docked = false;
volatile float vBus = 0, iBus = 0, boxTemp = NAN;
DeviceAddress dsAddr;
bool dsOk = false;
uint32_t lastGoodTemp = 0;

// inputs from MQTT / serial (written by the network task, read by the charge task)
volatile bool allowed = false;
volatile uint32_t allowedAtMs = 0;
volatile bool autoAllow = false;
volatile bool fieldTest = false;
volatile bool resetRequest = false;
volatile float battTemp = NAN;
volatile uint32_t battTempAtMs = 0;
long lastCmdSeq = -1;

uint32_t txSeq = 0;
double tsOffset = 0;            // from command ts when NTP is not available
bool tsFromCmd = false;

WiFiClient net;
PubSubClient mqtt(net);
OneWire oneWire(PIN_DS18B20);
DallasTemperature ds(&oneWire);

// ---------- MOSFET + ALERT ISR (E2) ----------
void IRAM_ATTR onAlert() {
  GPOC = (1 << PIN_MOS);    // direct register write: MOSFET off first (GPIO0-15 only), then tell the loop
  mosOn = false;
  alertFired = true;
}
void setMos(bool on) { mosOn = on; digitalWrite(PIN_MOS, on ? HIGH : LOW); }


// ---------- INA226 ----------
uint16_t inaRead(uint8_t reg) {
  Wire.beginTransmission(INA_ADDR); Wire.write(reg);
  if (Wire.endTransmission(false)) return 0xFFFF;
  Wire.requestFrom(INA_ADDR, (uint8_t)2);
  if (Wire.available() < 2) return 0xFFFF;
  uint16_t hi = Wire.read();
  return (hi << 8) | Wire.read();
}
void inaWrite(uint8_t reg, uint16_t v) {
  Wire.beginTransmission(INA_ADDR); Wire.write(reg); Wire.write(v >> 8); Wire.write(v & 0xFF);
  Wire.endTransmission();
}
bool inaInit() {
  if (inaRead(0xFE) != 0x5449) return false;
  inaWrite(0x00, 0x4327);                                   // average 4, 1.1 ms bus + shunt, continuous
  inaWrite(0x07, (uint16_t)(OVER_CURRENT_A * R_SHUNT / 2.5e-6));   // shunt over-voltage limit (LSB 2.5 uV)
  inaWrite(0x06, 0x8001);                                   // SOL alert, latched until 0x06 is read
  inaRead(0x06);
  return true;
}
void inaSample() {
  vBus = inaRead(0x02) * 1.25e-3f * VBUS_SCALE;
  iBus = (int16_t)inaRead(0x01) * 2.5e-6f / R_SHUNT;
}

// ---------- helpers ----------
bool allowedNow() { return (allowed || autoAllow) && (autoAllow || millis() - allowedAtMs < ALLOW_TIMEOUT_MS); }
bool battTempKnown() { return !isnan(battTemp) && millis() - battTempAtMs < BATT_TEMP_STALE_MS; }
bool battHot() { return battTempKnown() && battTemp >= BATT_HOT_C; }
bool boxHot() { return !isnan(boxTemp) && boxTemp >= BOX_HOT_C; }
bool canCharge() {
  if (!allowedNow() || boxHot() || battHot()) return false;
  if (REQUIRE_BATT_TEMP && !battTempKnown()) return false;
  return true;
}

void setMode(Mode m, const char *fault = nullptr) {
  if (m == mode && fault == faultName) return;
  Serial.printf("[node2] %s -> %s%s%s\n", MODE_NAME[mode], MODE_NAME[m], fault ? " " : "", fault ? fault : "");
  mode = m;
  faultName = (m == FAULT) ? fault : nullptr;
  if (m != CHARGING && m != FIELD_TEST) setMos(false);
}

double nowTs() {
  time_t t = time(nullptr);
  if (t > 1700000000) return (double)t;                     // NTP
  if (tsFromCmd) return tsOffset + millis() / 1000.0;       // clock from the last command
  return -1;
}

// ---------- charge state machine (non-blocking step, called every 100 ms) ----------
const int NS = SETTLE_WINDOW_MS / 30000 + 1;
float samples[NS];
int nSamples = 0;
uint32_t lastSample = 0, lastProbe = 0, chargeStart = 0, lowSince = 0, unplugSince = 0, fullAt = 0, tempReqAt = 0;
int probeMisses = 0;
bool probing = false;
uint32_t mosTestUntil = 0;
uint32_t probeStart = 0;

void chargeStep() {
  uint32_t now = millis();

  // box temperature, once a second, non-blocking
  if (now - tempReqAt >= 1000) {
    float t = dsOk ? ds.getTempC(dsAddr) : DEVICE_DISCONNECTED_C;
    bool good = t != DEVICE_DISCONNECTED_C && t != 85.0f && t > -40 && t < 125;
    if (good) { boxTemp = t; lastGoodTemp = now; }
    else {
      if (!dsOk) dsOk = ds.getAddress(dsAddr, 0);   // sensor came back
      if (now - lastGoodTemp > 10000) boxTemp = NAN; // null only after 10 s of failed reads
    }
    ds.requestTemperatures(); tempReqAt = now;
  }

  inaSample();

    if (mosTestUntil) {
    if ((int32_t)(millis() - mosTestUntil) >= 0 || alertFired || iBus > OVER_CURRENT_A || boxHot()) {
      mosTestUntil = 0; setMos(false); Serial.println("[mos] test end, MOSFET OFF");
    } else {
      static uint32_t lp = 0;
      if (millis() - lp >= 1000) { lp = millis(); Serial.printf("[mos] I=%.3f A  V=%.2f V\n", (float)iBus, (float)vBus); }
      return;
    }
  }

  // ALERT fired in the ISR: the MOSFET is already off
  if (alertFired) {
    alertFired = false;
    inaRead(0x06);                                        // clear the latched alert
    probing = false;
    if (mode == CHARGING || mode == FIELD_TEST) setMode(FAULT, "over_current");
  }

  if (resetRequest) {
    resetRequest = false;
    if (mode == FAULT) {
      if (boxHot() || battHot()) Serial.println("[node2] fault not reset: still too hot");
      else { setMode(IDLE); probeMisses = 0; }
    }
  }

  // probe pulse: 0.2 s on every 2 s while the MOSFET is normally off
  if (mode == IDLE || mode == DOCKED || mode == FULL) {
    if (!probing && now - lastProbe >= 2000) {
      lastProbe = now; probeStart = now; probing = true;
      setMos(true);
    } else if (probing && now - probeStart >= 200) {
      probing = false;
      bool seen = iBus > PROBE_DOCKED_A;                  // sampled above, MOSFET on for >= 0.2 s
      Serial.printf("[probe] I=%.3f A  V=%.2f V\n", (float)iBus, (float)vBus);
      setMos(false);
      if (seen) { probeMisses = 0; docked = true; }
      else if (++probeMisses >= 2) docked = false;        // two misses in a row = unplugged
    }
  } else probing = false;
  if (probing) return;                                    // decide nothing in the middle of a probe

  switch (mode) {
    case IDLE:
      if (docked) setMode(DOCKED);
      break;

    case DOCKED:
      if (!docked) setMode(IDLE);
      else if (fieldTest) { setMode(FIELD_TEST); setMos(true); unplugSince = 0; }
      else if (canCharge()) {
        setMode(CHARGING); setMos(true);
        chargeStart = now; lowSince = 0; unplugSince = 0; nSamples = 0; lastSample = now;
      }
      break;

    case CHARGING: {
      if (iBus > OVER_CURRENT_A) { setMode(FAULT, "over_current"); break; }
      if (boxHot() || battHot()) { setMode(FAULT, "over_temp"); break; }
      if (now - chargeStart > (uint32_t)(CHARGE_TIMER_H * 3600000.0f)) { setMode(FAULT, "timer"); break; }
      if (!canCharge()) { setMode(DOCKED); break; }
      if (iBus < UNPLUG_A) { if (!unplugSince) unplugSince = now; if (now - unplugSince > 3000) { docked = false; setMode(IDLE); break; } }
      else unplugSince = 0;
      if (vBus >= CV_MIN_V && iBus < FULL_I) { if (!lowSince) lowSince = now; }
      else lowSince = 0;
      bool full = lowSince && now - lowSince >= FULL_HOLD_MS;
      if (vBus >= CV_MIN_V && now - lastSample >= 30000) {
        lastSample = now;
        if (nSamples < NS) samples[nSamples++] = iBus;
        else { memmove(samples, samples + 1, sizeof(float) * (NS - 1)); samples[NS - 1] = iBus; }
        if (nSamples == NS) {
          float lo = samples[0], hi = samples[0];
          for (int k = 1; k < NS; k++) { lo = min(lo, samples[k]); hi = max(hi, samples[k]); }
          if (hi - lo < SETTLE_BAND_A) full = true;
        }
      } else if (vBus < CV_MIN_V) nSamples = 0;
      if (full) { setMode(FULL); fullAt = now; docked = true; }
      break;
    }

    case FULL:
      if (!docked) setMode(IDLE);
      else if (now - fullAt > FULL_RECHECK_MS && canCharge()) {
        setMode(CHARGING); setMos(true);
        chargeStart = now; lowSince = 0; unplugSince = 0; nSamples = 0; lastSample = now;
      }
      break;

    case FIELD_TEST:
      if (iBus > OVER_CURRENT_A) { setMode(FAULT, "over_current"); break; }
      if (boxHot()) { setMode(FAULT, "over_temp"); break; }
      if (!fieldTest) { setMode(DOCKED); break; }
      if (iBus < UNPLUG_A) { if (!unplugSince) unplugSince = now; if (now - unplugSince > 3000) { docked = false; setMode(IDLE); } }
      else unplugSince = 0;
      break;

    case FAULT:
      setMos(false);                                      // latched off
      break;
  }

  // FLASH button = fault reset (active LOW)
  static bool btnWas = false;
  bool btn = digitalRead(PIN_BTN) == LOW;
  if (btn && !btnWas) resetRequest = true;
  btnWas = btn;
}

// ---------- status LED (100 ms step) ----------
void ledStep() {
  static uint32_t t = 0; t++;
  bool on;
  switch (mode) {
    case IDLE:       on = false; break;
    case DOCKED:     on = (t % 20) == 0; break;           // short blink every 2 s
    case CHARGING:   on = (t % 10) < 7; break;            // mostly on
    case FULL:       on = true; break;                    // solid
    case FIELD_TEST: on = (t % 10) < 5; break;            // slow blink
    default:         on = (t % 2) == 0; break;            // FAULT: fast blink
  }
  digitalWrite(PIN_LED, on);
}

// ---------- MQTT ----------
void onMessage(char *topic, byte *payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;

  if (strcmp(topic, T_NODE1) == 0) {                         // battery temperature from Node 1
    if (doc["batt_temp_c"].is<float>()) { battTemp = doc["batt_temp_c"].as<float>(); battTempAtMs = millis(); }
    return;
  }
  if (strcmp(topic, T_CMD) != 0) return;

  if (doc["ts"].is<double>()) { tsOffset = doc["ts"].as<double>() - millis() / 1000.0; tsFromCmd = true; }
  bool hasSeq = doc["seq"].is<long>();
  long seq = hasSeq ? doc["seq"].as<long>() : -1;
  if (hasSeq) {
    JsonDocument ack; ack["ack_seq"] = seq;
    char buf[64]; size_t n = serializeJson(ack, buf);
    mqtt.publish(T_ACK, (const uint8_t *)buf, n, false);
    if (seq == lastCmdSeq) return;                           // resend: ack again, do not apply twice
    lastCmdSeq = seq;
  }
  if (doc["charge_allowed"].is<bool>()) { allowed = doc["charge_allowed"].as<bool>(); allowedAtMs = millis(); }
  if (doc["field_test"].is<bool>()) fieldTest = doc["field_test"].as<bool>();
  if (doc["reset_fault"] | false) resetRequest = true;
  Serial.printf("[node2] cmd seq %ld allowed=%d (%s)\n", seq, (int)allowed, (const char *)(doc["reason"] | ""));
}

// ChulaWiFi and other WPA2-Enterprise (PEAP + MSCHAPv2) networks: define WIFI_EAP_IDENTITY and
// WIFI_EAP_PASSWORD in secrets.h (WIFI_PASS is then unused). No CA certificate is loaded, so the
// server is not verified - fine for the demo, but the Chula password could be phished by a fake AP.
void wifiBegin() {
#ifdef WIFI_EAP_IDENTITY
  struct station_config cfg;
  memset(&cfg, 0, sizeof(cfg));
  strncpy((char *)cfg.ssid, WIFI_SSID, sizeof(cfg.ssid));
  wifi_station_set_config_current(&cfg);                    // SSID only, no PSK
  wifi_station_clear_cert_key();
  wifi_station_clear_enterprise_ca_cert();
  wifi_station_set_wpa2_enterprise_auth(1);
  wifi_station_set_enterprise_identity((uint8 *)WIFI_EAP_IDENTITY, strlen(WIFI_EAP_IDENTITY));
  wifi_station_set_enterprise_username((uint8 *)WIFI_EAP_IDENTITY, strlen(WIFI_EAP_IDENTITY));
  wifi_station_set_enterprise_password((uint8 *)WIFI_EAP_PASSWORD, strlen(WIFI_EAP_PASSWORD));
  wifi_station_connect();
#else
  WiFi.begin(WIFI_SSID, WIFI_PASS);
#endif
}

void ensureConnected() {
  static uint32_t lastTry = 0;
  static bool ipShown = false;
  if (WiFi.status() == WL_CONNECTED && !ipShown) {
    ipShown = true;
    Serial.printf("[node2] Wi-Fi OK: my IP %s, gateway %s, RSSI %d dBm -> MQTT %s:%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.gatewayIP().toString().c_str(), WiFi.RSSI(),
                  MQTT_HOST, MQTT_PORT);
  }
  if (WiFi.status() != WL_CONNECTED) {
    ipShown = false;
    if (millis() - lastTry > 10000) {
      lastTry = millis();
      Serial.printf("[node2] Wi-Fi connecting to %s...\n", WIFI_SSID);
      WiFi.disconnect();
      wifiBegin();
    }
    return;
  }
  if (!mqtt.connected() && millis() - lastTry > 5000) {
    lastTry = millis();
    if (mqtt.connect("airraikordee-node2", MQTT_USER, MQTT_PASS)) {
      mqtt.subscribe(T_CMD, 1);
      mqtt.subscribe(T_NODE1, 0);
      Serial.println("[node2] MQTT connected");
    } else {
      Serial.printf("[node2] MQTT failed, state %d\n", mqtt.state());
    }
  }
}

void publishTelemetry() {
  JsonDocument doc;
  doc["node_id"] = NODE_ID;
  doc["seq"] = ++txSeq;
  double ts = nowTs();
  if (ts > 0) doc["ts"] = (uint32_t)ts; else doc["ts"] = nullptr;
  bool on = mosOn;
  doc["docked"] = (bool)docked;
  doc["charging"] = mode == CHARGING;
  doc["voltage"] = on ? round(vBus * 100) / 100.0 : 0.0;     // only meaningful while the MOSFET is on
  doc["current_a"] = on ? round(max(0.0f, (float)iBus) * 100) / 100.0 : 0.0;
  if (isnan(boxTemp)) doc["temp_c"] = nullptr; else doc["temp_c"] = round(boxTemp * 10) / 10.0;
  if (mode == FULL) doc["est_percent"] = 100; else doc["est_percent"] = nullptr;   // no gauge on the box side
  doc["mode"] = MODE_NAME[mode];
  if (faultName) doc["fault"] = (const char *)faultName; else doc["fault"] = nullptr;

  char buf[320];
  size_t n = serializeJson(doc, buf);
  if (mqtt.connected()) mqtt.publish(T_TEL, (const uint8_t *)buf, n, false);
  Serial.println(buf);
}

void handleSerial() {
  if (!Serial.available()) return;
  String c = Serial.readStringUntil('\n'); c.trim();
  if (c == "allow") { autoAllow = true; Serial.println("auto-allow ON (bench only)"); }
  else if (c == "deny") { autoAllow = false; allowed = false; Serial.println("charging not allowed"); }
  else if (c == "field on") { fieldTest = true; Serial.println("field_test ON"); }
  else if (c == "field off") { fieldTest = false; Serial.println("field_test OFF"); }
  else if (c == "reset") resetRequest = true;
  else if (c == "status") publishTelemetry();
  else if (c == "mos") { mosTestUntil = millis() + 20000; probing = false; setMos(true); Serial.println("[mos] MOSFET ON for 20 s"); }
  else Serial.println("commands: allow | deny | field on | field off | reset | status");
}

// ---------- setup / loop ----------
void setup() {
  pinMode(PIN_MOS, OUTPUT); digitalWrite(PIN_MOS, LOW);    // charger OFF before anything else
  pinMode(PIN_LED, OUTPUT); digitalWrite(PIN_LED, LOW);
  pinMode(PIN_ALERT, INPUT_PULLUP);
  pinMode(PIN_BTN, INPUT_PULLUP);
  Serial.begin(115200);
  delay(500);
  Serial.println("\nAirRaiKorDee Node 2 (charging box, ESP8266)");

  Wire.begin(PIN_SDA, PIN_SCL);
  if (!inaInit()) {
    Serial.println("INA226 not found - charging blocked. Check 3V3/GND/SDA(4)/SCL(5).");
    mode = FAULT; faultName = "over_current";                // fail safe: never charge blind
  }
  attachInterrupt(digitalPinToInterrupt(PIN_ALERT), onAlert, FALLING);
  ds.begin();
  dsOk = ds.getAddress(dsAddr, 0);                 // remember the sensor, no bus search every second
  if (dsOk) ds.setResolution(dsAddr, 12);
  ds.setWaitForConversion(false);
  ds.requestTemperatures(); tempReqAt = millis();

  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  configTime(0, 0, "pool.ntp.org", "time.google.com");     // unix time in UTC
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(512);
  mqtt.setCallback(onMessage);
}

void loop() {
  static uint32_t lastStep = 0, lastTel = 0;
  if (millis() - lastStep >= 100) { lastStep = millis(); chargeStep(); ledStep(); }   // safety logic first
  ensureConnected();
  mqtt.loop();
  handleSerial();
  if (millis() - lastTel >= TELEMETRY_MS) { lastTel = millis(); publishTelemetry(); }
  delay(5);                                                // yield to the Wi-Fi stack
}
