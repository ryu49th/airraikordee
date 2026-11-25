/*
 * VoiceStream.h - send the spoken command after the wake word to the Jetson (AIR-20, gateway/voice/voice.py)
 *
 * Header-only: copy this file next to the main Node 1 sketch. Needs an ESP32-S3 with PSRAM (N16R8: Tools ->
 * PSRAM -> OPI PSRAM) and the native USB port as Serial (Tools -> USB CDC On Boot -> Enabled): 16 kHz x 16 bit
 * is 32 kB/s, too much for the 115200-baud UART port.
 *
 * What it does
 *   After the firmware sends the wake event, startClip() begins a clip. Every mic block goes through feed()
 *   (16 kHz, 16-bit mono - the same samples the wake-word model reads). loop() sends each 50 ms frame as soon
 *   as it is full, ends the clip after ~0.8 s of silence or 5 s, and keeps it in PSRAM until the Jetson
 *   answers audio_ack (or 10 s pass), so lost frames can be sent again.
 *
 * Lines it writes (one JSON object per line, the format in gateway/README.md -> "Voice commands"):
 *   {"audio":<sid>,"i":<k>,"pcm":"<base64 of 1600 bytes>"}
 *   {"audio":<sid>,"end":<frames>,"rate":16000,"reason":"silence"|"max"}
 * Line it reads (pass every line from the Jetson to handleLine(); it returns true for audio_ack):
 *   {"cmd":"audio_ack","sid":<sid>,"missing":[5,9],"seq":<n>,"ts":..}
 *   The caller still answers {"ack":<seq>} like for every other command.
 *
 * Serial is shared with telemetry: give begin() the same mutex the rest of the firmware takes around each
 * Serial line, so a telemetry line can never land in the middle of an audio line.
 */
#pragma once
#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

class VoiceStream {
 public:
  static const uint32_t RATE = 16000;
  static const size_t FRAME_SAMPLES = 800;          // 50 ms
  static const size_t MAX_SECONDS = 5;
  static const size_t MAX_FRAMES = MAX_SECONDS * RATE / FRAME_SAMPLES;   // 100
  static const uint16_t SILENCE_END_FRAMES = 16;    // 0.8 s of quiet after speech -> end
  static const uint16_t NO_SPEECH_FRAMES = 60;      // nothing said within 3 s -> end anyway
  static const uint32_t KEEP_MS = 10000;            // free the clip if no audio_ack comes

  // Mean |sample| of a 50 ms frame that counts as speech. Raise it if fan noise ends clips late,
  // lower it if quiet speakers get cut off. Print level() while talking to tune it.
  uint16_t speechLevel = 600;

  bool begin(Stream &out, SemaphoreHandle_t writeLock = nullptr) {
    out_ = &out;
    lock_ = writeLock;
    if (!buf_) buf_ = (int16_t *)ps_malloc(MAX_FRAMES * FRAME_SAMPLES * sizeof(int16_t));
    return buf_ != nullptr;
  }

  // Call right after sending the wake event. Returns the clip number (also used as "audio" in the lines).
  uint32_t startClip() {
    sid_++;
    recorded_ = 0;
    sent_ = 0;
    speech_ = false;
    quiet_ = 0;
    lastLevel_ = 0;
    state_ = CAPTURING;
    return sid_;
  }

  bool capturing() const { return state_ == CAPTURING; }
  bool busy() const { return state_ != IDLE; }
  uint16_t level() const { return lastLevel_; }

  // Mic samples (16 kHz mono int16). Safe to call from the I2S/mic task while loop() runs in another task.
  void feed(const int16_t *samples, size_t n) {
    if (state_ != CAPTURING || !buf_) return;
    size_t room = MAX_FRAMES * FRAME_SAMPLES - recorded_;
    if (n > room) n = room;
    memcpy(buf_ + recorded_, samples, n * sizeof(int16_t));
    __sync_synchronize();
    recorded_ += n;  // published after the copy, so loop() never reads a half-written frame
  }

  // Call often (every few ms) from the main loop or a task.
  void loop() {
    if (state_ == CAPTURING) {
      while (sent_ < recorded_ / FRAME_SAMPLES) {
        const int16_t *f = buf_ + sent_ * FRAME_SAMPLES;
        sendFrame(sent_);
        sent_++;
        if (endOfSpeech(f)) {
          finish("silence");
          return;
        }
      }
      if (sent_ >= MAX_FRAMES) finish("max");
    } else if (state_ == WAITING && millis() - endedAt_ > KEEP_MS) {
      state_ = IDLE;  // the Jetson never answered: drop the clip
    }
  }

  // Pass every line from the Jetson. Returns true if it was an audio_ack (the caller still sends {"ack":seq}).
  bool handleLine(const char *line) {
    if (!strstr(line, "\"audio_ack\"")) return false;
    long sid = numberAfter(line, "\"sid\"");
    if (state_ != WAITING || sid != (long)sid_) return true;
    const char *m = strstr(line, "\"missing\"");
    m = m ? strchr(m, '[') : nullptr;
    bool any = false;
    while (m && *m && *m != ']') {
      m++;
      while (*m == ' ') m++;
      if (*m >= '0' && *m <= '9') {
        long i = strtol(m, (char **)&m, 10);
        if (i >= 0 && (uint32_t)i < frames_) {
          sendFrame((uint32_t)i);
          any = true;
        }
      } else if (*m != ',' && *m != ']') {
        break;
      }
      while (*m == ' ') m++;
    }
    if (any) {
      sendEnd();
      endedAt_ = millis();
    } else {
      state_ = IDLE;  // nothing missing: the Jetson has the whole clip
    }
    return true;
  }

 private:
  enum State { IDLE, CAPTURING, WAITING };
  Stream *out_ = nullptr;
  SemaphoreHandle_t lock_ = nullptr;
  int16_t *buf_ = nullptr;
  volatile State state_ = IDLE;
  volatile size_t recorded_ = 0;
  uint32_t sid_ = 0, sent_ = 0, frames_ = 0, endedAt_ = 0;
  bool speech_ = false;
  uint16_t quiet_ = 0, lastLevel_ = 0;
  const char *reason_ = "silence";
  // {"audio":4294967295,"i":100,"pcm":"<2136>"}\n
  char line_[2200];

  bool endOfSpeech(const int16_t *f) {
    uint32_t sum = 0;
    for (size_t k = 0; k < FRAME_SAMPLES; k++) sum += (uint32_t)abs(f[k]);
    lastLevel_ = sum / FRAME_SAMPLES;
    if (lastLevel_ >= speechLevel) {
      speech_ = true;
      quiet_ = 0;
      return false;
    }
    quiet_++;
    return speech_ ? quiet_ >= SILENCE_END_FRAMES : sent_ >= NO_SPEECH_FRAMES;
  }

  void finish(const char *reason) {
    frames_ = sent_;
    reason_ = reason;
    state_ = WAITING;
    sendEnd();
    endedAt_ = millis();
  }

  void sendFrame(uint32_t i) {
    int n = snprintf(line_, sizeof(line_), "{\"audio\":%lu,\"i\":%lu,\"pcm\":\"", (unsigned long)sid_,
                     (unsigned long)i);
    n += base64(reinterpret_cast<const uint8_t *>(buf_ + i * FRAME_SAMPLES), FRAME_SAMPLES * 2, line_ + n);
    line_[n++] = '"';
    line_[n++] = '}';
    line_[n++] = '\n';
    write(line_, n);
  }

  void sendEnd() {
    int n = snprintf(line_, sizeof(line_), "{\"audio\":%lu,\"end\":%lu,\"rate\":%lu,\"reason\":\"%s\"}\n",
                     (unsigned long)sid_, (unsigned long)frames_, (unsigned long)RATE, reason_);
    write(line_, n);
  }

  void write(const char *s, size_t n) {
    if (!out_) return;
    if (lock_) xSemaphoreTake(lock_, portMAX_DELAY);
    out_->write(reinterpret_cast<const uint8_t *>(s), n);
    if (lock_) xSemaphoreGive(lock_);
  }

  static size_t base64(const uint8_t *in, size_t len, char *out) {
    static const char T[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t o = 0, i = 0;
    for (; i + 2 < len; i += 3) {
      uint32_t v = (in[i] << 16) | (in[i + 1] << 8) | in[i + 2];
      out[o++] = T[(v >> 18) & 63]; out[o++] = T[(v >> 12) & 63];
      out[o++] = T[(v >> 6) & 63];  out[o++] = T[v & 63];
    }
    if (i < len) {
      uint32_t v = in[i] << 16;
      if (i + 1 < len) v |= in[i + 1] << 8;
      out[o++] = T[(v >> 18) & 63];
      out[o++] = T[(v >> 12) & 63];
      out[o++] = (i + 1 < len) ? T[(v >> 6) & 63] : '=';
      out[o++] = '=';
    }
    return o;
  }

  static long numberAfter(const char *s, const char *key) {
    const char *p = strstr(s, key);
    if (!p) return -1;
    p = strchr(p, ':');
    return p ? strtol(p + 1, nullptr, 10) : -1;
  }
};
