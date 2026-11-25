// Fake data for demo mode (?demo=1 or no config.js). Same shape as the Firebase contract,
// so every screen can be built and checked without the Jetson or a Firebase project.

const DEVICE = "airraikordee-01";

function rnd(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

// PM2.5 for a given time: low at night, cooking peaks at 07:00, 12:00 and 18:30 (Bangkok time).
function pmAt(t, r) {
  const h = ((t / 3600 + 7) % 24 + 24) % 24;
  let v = 9 + 4 * Math.sin((h / 24) * 2 * Math.PI);
  for (const peak of [7, 12, 18.5]) v += 45 * Math.exp(-((h - peak) ** 2) / 0.15);
  return Math.max(2, v + (r() - 0.5) * 4);
}

function fanFor(pm) {
  const table = [[15, 25], [35, 45], [55, 70], [9999, 100]];
  return table.find(([upper]) => pm <= upper)[1];
}

export function demoSeed(nowS) {
  const r = rnd(42);
  const minute = nowS - (nowS % 60);
  const history = {};
  for (let i = 10080 - 1; i >= 1; i--) {
    const t = minute - i * 60;
    const pm = pmAt(t, r);
    history[t] = {
      ts: t + 59,
      pm25: Math.round(pm * 10) / 10,
      temp_c: Math.round((28 + 3 * Math.sin(((t / 3600 + 7 - 9) / 24) * 2 * Math.PI)) * 10) / 10,
      rh: Math.round(65 + 10 * Math.cos(((t / 3600 + 7) / 24) * 2 * Math.PI)),
      fan_pct: fanFor(pm),
    };
  }

  const tree = {
    users: { "demo-user": { devices: { [DEVICE]: { code: "AB12-CD34" } } } },
    claims: {
      "AB12-CD34": { deviceId: DEVICE, used: true },
      "EF56-GH78": { deviceId: "airraikordee-02", used: false },
    },
    devices: {
      [DEVICE]: {
        history: { node1: history },
        config: {
          pm_alert: 35,
          night_start: "22:00",
          night_end: "06:00",
          manual_fan_pct: null,
          fan_mode: "auto",
          filter_reset_ts: nowS - 13 * 86400,
        },
        cause: {
          latest: {
            ts: nowS - 600,
            cause: "cooking",
            confidence: 0.74,
            source: "cloud",
            advice_th: "น่าจะมาจากการทำอาหาร เปิดพัดลมดูดควันหรือปิดประตูครัว",
          },
        },
        alerts: {
          [String((nowS - 7200) * 1000) + "000"]: {
            ts: nowS - 7200, level: "warn", title: "PM2.5 สูง",
            text: "PM2.5 68 µg/m³ เกิน 10 นาที", sent_to: ["discord"],
          },
          [String((nowS - 3600) * 1000) + "001"]: {
            ts: nowS - 3600, level: "info", title: "ชาร์จเต็มแล้ว",
            text: "แบตเตอรี่ชาร์จเต็ม ถอดสายได้", sent_to: ["discord"],
          },
        },
      },
      "airraikordee-02": {
        telemetry: { node1: { latest: { ts: nowS, pm25: 12, pm25_corrected: 10.5, pm10: 15, fan_pct: 25,
          fan_rpm: 420, pan_deg: 0, tilt_deg: 0, person: false, mode: "idle" } } },
      },
    },
  };
  return { tree, deviceId: DEVICE };
}

// Stream new values every second, like the uploader does.
export function startDemo(store, deviceId) {
  const r = rnd(7);
  const base = "devices/" + deviceId + "/";
  let soc = 0.62;
  let pan = 40;
  let minuteAcc = [];
  let currentMinute = null;
  let pmHighSince = null;

  function tick() {
    const now = Math.floor(Date.now() / 1000);
    const pm = pmAt(now, r);
    const corrected = pm * 0.85;
    const fan = fanFor(corrected);
    const person = Math.floor(now / 90) % 3 !== 0;
    pan += person ? (r() - 0.5) * 6 : 0;
    pan = Math.max(-180, Math.min(180, pan));
    const mode = person ? "aim_person" : "survey";

    store._write(base + "telemetry/node1/latest", {
      ts: now,
      pm25: Math.round(pm * 10) / 10,
      pm25_corrected: Math.round(corrected * 10) / 10,
      pm10: Math.round(pm * 1.3 * 10) / 10,
      fan_pct: fan,
      fan_rpm: Math.round(fan * 16.5 + (r() - 0.5) * 20),
      pan_deg: Math.round(pan * 10) / 10,
      tilt_deg: 10,
      person,
      mode,
    });
    const temp = 28.6 + (r() - 0.5) * 0.2;
    const rh = 68 + (r() - 0.5);
    store._write(base + "env/latest", {
      ts: now, temp_c: Math.round(temp * 10) / 10, rh: Math.round(rh * 10) / 10, pressure_hpa: 1009.2,
    });

    const alerts = [];
    if (corrected > 35) {
      pmHighSince = pmHighSince || now;
      alerts.push({ type: "pm_high", level: "warn", title: "PM2.5 สูง",
        text: "PM2.5 " + Math.round(corrected) + " µg/m³", since: pmHighSince });
    } else {
      pmHighSince = null;
    }
    store._write(base + "state", {
      ts: now, mode, fan_pct: fan, target: { pan_deg: Math.round(pan), tilt_deg: 10 },
      pm25_corrected: Math.round(corrected * 10) / 10, alerts,
    });

    soc = Math.min(1, soc + 0.0004);
    const full = soc >= 1;
    store._write(base + "telemetry/node2/latest", {
      ts: now,
      mode: full ? "FULL" : "CHARGING",
      voltage: Math.round((11.1 + 1.45 * soc) * 100) / 100,
      current_a: full ? 1.2 : 3.0,
      temp_c: 41 + (r() - 0.5),
      batt_temp_c: 33 + (r() - 0.5),
      plugged: true,
      est_percent: Math.round(soc * 100),
    });

    if (now % 5 === 0) {
      store._write(base + "health", {
        node1_online: true, node2_online: true, cpu_temp_c: 52 + (r() - 0.5) * 2,
        pending_rows: 0, last_upload_ts: now, filter_hours: 312.5, pms_hours: 410,
      });
    }

    const minute = now - (now % 60);
    if (currentMinute !== null && minute !== currentMinute && minuteAcc.length) {
      const avg = (k) => minuteAcc.reduce((s, x) => s + x[k], 0) / minuteAcc.length;
      store._write(base + "history/node1/" + currentMinute, {
        ts: currentMinute + 59, pm25: Math.round(avg("pm25") * 10) / 10,
        temp_c: Math.round(avg("temp_c") * 10) / 10, rh: Math.round(avg("rh")), fan_pct: Math.round(avg("fan_pct")),
      });
      minuteAcc = [];
    }
    currentMinute = minute;
    minuteAcc.push({ pm25: pm, temp_c: temp, rh, fan_pct: fan });
  }

  // the fake Jetson handles dashboard buttons: it deletes each ui_cmd entry after a moment (= "received")
  const taken = new Set();
  store.listen(base + "ui_cmd", (cmds) => {
    for (const key of Object.keys(cmds || {})) {
      if (taken.has(key)) continue;
      taken.add(key);
      setTimeout(() => store._write(base + "ui_cmd/" + key, null), 1500);
    }
  });

  tick();
  return setInterval(tick, 1000);
}
