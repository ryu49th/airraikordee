# Node 1 — ESP32-S3 in the unit
PMS5003, fan PWM + tach, 28BYJ-48 pan, MG90S louver, stop button, NTC, INMP441 wake word, USB JSON link to the Jetson.

`examples/pan_stepper.ino` = pan test with homing + limit switch.

`examples/voice_stream_test/` = voice command clip after the wake word (AIR-20). `VoiceStream.h` is header-only:
copy it next to the main sketch, call `onWake()` logic (wake event + `voice.startClip()`) when "hellofoon" fires,
pass every 16 kHz mic block to `voice.feed()`, every line from the Jetson to `voice.handleLine()`, and call
`voice.loop()` often. Needs Tools -> USB CDC On Boot: Enabled (native USB port) and PSRAM: OPI PSRAM. Test it
without the model: press BOOT, speak, and watch `voice.py` on the Jetson. INMP441 pins in the sketch
(SCK 41, WS 42, SD 40) are a proposal: change them to the real harness.
