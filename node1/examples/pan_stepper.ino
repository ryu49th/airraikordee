/*
 * AirRaiKorDee – head pan with 28BYJ-48 + ULN2003 on ESP32-S3 (Arduino-ESP32 core 2.x / 3.x)
 *
 * Wiring
 *   ULN2003 IN1..IN4  <- GPIO 4, 5, 6, 7
 *   ULN2003 "+"       <- 5 V from the servo/stepper buck (NOT the ESP32 5V/3V3 pin)
 *   ULN2003 "-"       <- GND  (common with the ESP32 GND!)
 *   28BYJ-48 5-pin plug -> ULN2003 white socket (keyed)
 *   Limit switch: COM -> GND, NO -> GPIO 15 (internal pull-up, pressed = LOW)
 *
 * Mechanics
 *   motor 28BYJ-48: 64 half-steps x 63.684 gearbox = 4075.8 half-steps / motor rev
 *   pinion 12T -> ring gear 89T = 7.4167 : 1
 *   => 30229 half-steps per head revolution = 83.97 half-steps per degree
 *   switch at the rear, tab under the head front -> the tab hits it when the head faces the rear (±180°)
 *
 * Serial test commands (115200, newline):
 *   home          find the switch, then go to 0°
 *   aim <deg>     go to an angle, -178 .. +178
 *   off           de-energise the coils
 *   pos           print the current angle
 */
#include <Arduino.h>

// ---------- pins & constants ----------
const int IN_PINS[4]   = {4, 5, 6, 7};
const int PIN_SWITCH   = 15;

const float HALFSTEPS_PER_MOTOR_REV = 4075.7728f;          // 64 * 63.68395
const float GEAR_RATIO              = 89.0f / 12.0f;
const float STEPS_PER_DEG           = HALFSTEPS_PER_MOTOR_REV * GEAR_RATIO / 360.0f;   // ≈ 83.97

const int   DIR_SIGN       = +1;      // flip to -1 if "aim 30" turns the head the wrong way
const float SOFT_LIMIT_DEG = 178.0f;  // firmware limit, the switch is the hard stop at 180°
const float HOME_ANGLE_DEG = -180.0f; // angle where the switch triggers when homing in the - direction

const uint32_t STEP_US_FAST  = 1200;  // ≈ 12 rpm motor ≈ 1.7 rpm head (raise if it skips steps)
const uint32_t STEP_US_SLOW  = 2500;  // start speed and homing speed
const uint32_t RAMP_STEPS    = 300;   // accelerate over this many steps

// half-step sequence for unipolar 28BYJ-48 (IN1..IN4)
const uint8_t SEQ[8][4] = {
  {1,0,0,0},{1,1,0,0},{0,1,0,0},{0,1,1,0},
  {0,0,1,0},{0,0,1,1},{0,0,0,1},{1,0,0,1}
};

// ---------- state ----------
volatile bool    g_switchHit = false;   // set in the ISR (E2)
volatile int32_t g_pos       = 0;       // half-steps, 0 = head facing front
volatile bool    g_homed     = false;
int              g_phase     = 0;

QueueHandle_t g_cmdQueue;               // target angles (float) for the pan task
const float CMD_HOME = 9999.0f;
const float CMD_OFF  = 8888.0f;

// ---------- interrupt: limit switch (E2) ----------
void IRAM_ATTR onSwitch() {
  g_switchHit = true;                   // the pan task stops on the next step (< 2.5 ms)
}

// ---------- low level ----------
void coils(const uint8_t *p) { for (int i = 0; i < 4; i++) digitalWrite(IN_PINS[i], p[i]); }
void coilsOff()             { for (int i = 0; i < 4; i++) digitalWrite(IN_PINS[i], LOW); }

void oneStep(int dir) {                 // dir = +1 / -1 in head coordinates
  g_phase = (g_phase + dir * DIR_SIGN + 8) & 7;
  coils(SEQ[g_phase]);
  g_pos += dir;
}

// move n steps in direction dir with a simple linear ramp; returns false if the switch stopped it
bool moveSteps(int32_t n, int dir, bool homing) {
  for (int32_t i = 0; i < n; i++) {
    if (g_switchHit && !homing) { coilsOff(); return false; }     // safety stop
    if (homing && g_switchHit) return true;
    uint32_t edge = min<uint32_t>(i, n - 1 - i);
    uint32_t us = homing ? STEP_US_SLOW
                         : (edge >= RAMP_STEPS ? STEP_US_FAST
                                               : STEP_US_SLOW - (STEP_US_SLOW - STEP_US_FAST) * edge / RAMP_STEPS);
    oneStep(dir);
    delayMicroseconds(us);
    if ((i & 31) == 31) vTaskDelay(1);  // let other tasks (UART, wake word) run
  }
  return true;
}

bool goToDeg(float deg) {
  if (!g_homed) { Serial.println("ERR not homed – send 'home' first"); return false; }
  deg = constrain(deg, -SOFT_LIMIT_DEG, SOFT_LIMIT_DEG);
  int32_t target = lroundf(deg * STEPS_PER_DEG);
  int32_t delta  = target - g_pos;
  bool ok = moveSteps(abs(delta), delta >= 0 ? +1 : -1, false);
  coilsOff();                            // gearbox holds the head, coils stay cool
  if (!ok) { g_homed = false; Serial.println("STOP limit switch hit – re-home"); }
  return ok;
}

bool home() {
  g_switchHit = (digitalRead(PIN_SWITCH) == LOW);
  if (g_switchHit) {                     // already on the switch: back off first
    g_switchHit = false;
    moveSteps(lroundf(20 * STEPS_PER_DEG), +1, true);
    g_switchHit = false;
  }
  // search in the - direction, at most one full head turn
  bool found = moveSteps(lroundf(370 * STEPS_PER_DEG), -1, true) && g_switchHit;
  coilsOff();
  if (!found) { Serial.println("ERR switch not found – check wiring/direction"); return false; }
  g_pos = lroundf(HOME_ANGLE_DEG * STEPS_PER_DEG);
  g_switchHit = false;
  moveSteps(lroundf(5 * STEPS_PER_DEG), +1, true);   // leave the switch
  g_switchHit = false;
  g_homed = true;
  return goToDeg(0);
}

// ---------- FreeRTOS task (E1) ----------
void panTask(void *) {
  float cmd;
  for (;;) {
    if (xQueueReceive(g_cmdQueue, &cmd, portMAX_DELAY) == pdTRUE) {
      if (cmd == CMD_HOME)      { Serial.println(home() ? "OK homed" : "ERR home"); }
      else if (cmd == CMD_OFF)  { coilsOff(); Serial.println("OK off"); }
      else                      { if (goToDeg(cmd)) Serial.printf("OK at %.1f deg\n", g_pos / STEPS_PER_DEG); }
    }
  }
}

void setup() {
  Serial.begin(115200);
  for (int p : IN_PINS) { pinMode(p, OUTPUT); digitalWrite(p, LOW); }
  pinMode(PIN_SWITCH, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_SWITCH), onSwitch, FALLING);

  g_cmdQueue = xQueueCreate(4, sizeof(float));
  xTaskCreatePinnedToCore(panTask, "pan", 4096, nullptr, 2, nullptr, 1);
  Serial.printf("pan ready: %.2f half-steps/deg. Commands: home | aim <deg> | off | pos\n", STEPS_PER_DEG);
}

void loop() {
  if (!Serial.available()) { delay(10); return; }
  String line = Serial.readStringUntil('\n'); line.trim();
  float v;
  if (line == "home")                  { v = CMD_HOME; xQueueSend(g_cmdQueue, &v, 0); }
  else if (line == "off")              { v = CMD_OFF;  xQueueSend(g_cmdQueue, &v, 0); }
  else if (line == "pos")              { Serial.printf("%.1f deg (homed=%d)\n", g_pos / STEPS_PER_DEG, g_homed); }
  else if (line.startsWith("aim "))    { v = line.substring(4).toFloat(); xQueueSend(g_cmdQueue, &v, 0); }
  else                                  Serial.println("? home | aim <deg> | off | pos");
}
