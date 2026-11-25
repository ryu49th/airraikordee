/*
 * AirRaiKorDee Node 1 - voice clip test (AIR-20): INMP441 -> VoiceStream -> USB -> Jetson voice.py
 *
 * Board: ESP32-S3 DevKitC-1 N16R8, Arduino-ESP32 core 3.x
 *   Tools -> USB CDC On Boot: Enabled   (plug into the "USB" port, not "UART")
 *   Tools -> PSRAM: OPI PSRAM
 *   Tools -> Flash Size: 16MB
 *
 * Wiring (INMP441)          change the pins below to match the real harness
 *   SCK -> GPIO 41   WS -> GPIO 42   SD -> GPIO 40   L/R -> GND (left channel)   VDD -> 3V3   GND -> GND
 *
 * Test without the wake-word model: press the BOOT button (GPIO 0), or send {"cmd":"test_wake"} from the
 * Jetson. The sketch sends the wake event, streams what the mic hears until you stop talking, and answers
 * audio_ack. Jetson side: uart_bridge + voice.py (gateway/README.md -> "Voice commands").
 *
 * In the real firmware: the wake-word code calls onWake() when "hellofoon" is detected, and the mic loop that
 * feeds the Edge Impulse model also calls voice.feed() with the same 16 kHz samples (one I2S reader only:
 * the legacy and the new I2S drivers cannot run together).
 */
#include <Arduino.h>
#include <ESP_I2S.h>
#include "VoiceStream.h"

#if !ARDUINO_USB_CDC_ON_BOOT
#error "Set Tools -> USB CDC On Boot -> Enabled: audio needs the native USB port"
#endif

const int8_t PIN_I2S_SCK = 41;
const int8_t PIN_I2S_WS = 42;
const int8_t PIN_I2S_SD = 40;
const int8_t PIN_WAKE_BUTTON = 0;   // BOOT button, for testing
const int MIC_SHIFT = 14;           // INMP441 24-bit in a 32-bit slot -> int16; lower = louder

I2SClass i2s;
VoiceStream voice;
SemaphoreHandle_t serialLock;
uint32_t txSeq = 0;
double clockOffset = 0;  // Jetson time - millis()/1000, learnt from the heartbeat

void writeLine(const char *s) {
  xSemaphoreTake(serialLock, portMAX_DELAY);
  Serial.print(s);
  Serial.print('\n');
  xSemaphoreGive(serialLock);
}

double nowTs() { return clockOffset + millis() / 1000.0; }

void onWake() {
  if (voice.busy()) return;  // still sending or waiting for the last clip
  char line[120];
  snprintf(line, sizeof(line), "{\"node_id\":\"node1\",\"seq\":%lu,\"ts\":%.3f,\"type\":\"wake\"}",
           (unsigned long)++txSeq, nowTs());
  writeLine(line);
  voice.startClip();
}

// Mic task: reads I2S and hands the samples to the voice clip (and, in the real firmware, to the wake-word model).
void micTask(void *) {
  static int32_t raw[256];
  static int16_t pcm[256];
  for (;;) {
    size_t got = i2s.readBytes((char *)raw, sizeof(raw)) / sizeof(int32_t);
    for (size_t k = 0; k < got; k++) {
      int32_t v = raw[k] >> MIC_SHIFT;
      pcm[k] = (int16_t)constrain(v, -32768, 32767);
    }
    if (got) voice.feed(pcm, got);
    // ei_feed(pcm, got);  // the Edge Impulse continuous classifier would read the same samples here
  }
}

void handleLine(const char *line) {
  if (strstr(line, "\"cmd\":\"hb\"")) {  // heartbeat: learn the Jetson clock, no ACK
    const char *t = strstr(line, "\"ts\":");
    if (t) clockOffset = atof(t + 5) - millis() / 1000.0;
    return;
  }
  const char *s = strstr(line, "\"seq\":");
  if (s) {  // every command with a seq gets {"ack":seq} within 300 ms
    char ack[40];
    snprintf(ack, sizeof(ack), "{\"ack\":%ld}", strtol(s + 6, nullptr, 10));
    writeLine(ack);
  }
  if (voice.handleLine(line)) return;
  if (strstr(line, "\"test_wake\"")) onWake();
}

void setup() {
  Serial.setTxBufferSize(8192);  // room for a few 2 kB audio lines; set before begin()
  Serial.begin(115200);          // the baud rate is ignored on native USB
  Serial.setTxTimeoutMs(20);     // do not stall for long if the Jetson stops reading (lost frames are resent)
  serialLock = xSemaphoreCreateMutex();
  pinMode(PIN_WAKE_BUTTON, INPUT_PULLUP);
  if (!voice.begin(Serial, serialLock)) {
    for (;;) {
      writeLine("{\"node_id\":\"node1\",\"type\":\"error\",\"msg\":\"no PSRAM: Tools -> PSRAM -> OPI PSRAM\"}");
      delay(2000);
    }
  }
  i2s.setPins(PIN_I2S_SCK, PIN_I2S_WS, -1, PIN_I2S_SD);
  if (!i2s.begin(I2S_MODE_STD, VoiceStream::RATE, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO, I2S_STD_SLOT_LEFT)) {
    writeLine("{\"node_id\":\"node1\",\"type\":\"error\",\"msg\":\"I2S init failed\"}");
  }
  xTaskCreatePinnedToCore(micTask, "mic", 4096, nullptr, 5, nullptr, 0);
}

void loop() {
  static char rx[512];
  static size_t n = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      rx[n] = 0;
      if (n) handleLine(rx);
      n = 0;
    } else if (n < sizeof(rx) - 1) {
      rx[n++] = c;
    }
  }

  static uint32_t pressedAt = 0;
  if (digitalRead(PIN_WAKE_BUTTON) == LOW) {
    if (!pressedAt) pressedAt = millis();
  } else if (pressedAt) {
    if (millis() - pressedAt > 30) onWake();  // debounced press = fake wake word
    pressedAt = 0;
  }

  voice.loop();
  delay(2);
}
