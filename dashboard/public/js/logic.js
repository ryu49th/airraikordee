// Pure helpers (no DOM, no Firebase) so they can be tested with `node --test`.
// Field names follow the Firebase data contract (Farn, 13 Nov 2026).

export const STALE_S = 90;            // the unit counts as offline when its data is older than this
export const HISTORY_LIMITS = { "1h": 60, "24h": 1440, "7d": 10080 };   // rows of history/node1 (1 per minute)
export const MAX_CHART_POINTS = 600;

// Thai PM2.5 bands (Pollution Control Department, µg/m³).
const PM_BANDS = [
  { max: 15, key: "very_good", label: "ดีมาก" },
  { max: 25, key: "good", label: "ดี" },
  { max: 37.5, key: "moderate", label: "ปานกลาง" },
  { max: 75, key: "unhealthy_sensitive", label: "เริ่มมีผลต่อสุขภาพ" },
  { max: Infinity, key: "unhealthy", label: "มีผลต่อสุขภาพ" },
];

export function pmBand(value) {
  if (!isNum(value)) return { key: "unknown", label: "ไม่มีข้อมูล" };
  return PM_BANDS.find((b) => value <= b.max);
}

// PM2.5 to show: the humidity-corrected value when the gateway has one.
export function pmShown(node1) {
  if (!node1) return null;
  if (isNum(node1.pm25_corrected)) return node1.pm25_corrected;
  return isNum(node1.pm25) ? node1.pm25 : null;
}

export function isNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

export function fmt(v, digits = 0, unit = "") {
  if (!isNum(v)) return "–";
  return v.toFixed(digits) + (unit ? " " + unit : "");
}

export function ageSeconds(ts, nowS) {
  return isNum(ts) ? Math.max(0, nowS - ts) : null;
}

export function isStale(ts, nowS, limit = STALE_S) {
  const age = ageSeconds(ts, nowS);
  return age === null || age > limit;
}

export function ageText(ts, nowS) {
  const age = ageSeconds(ts, nowS);
  if (age === null) return "ยังไม่เคยได้ข้อมูล";
  if (age < 5) return "เมื่อสักครู่";
  if (age < 60) return Math.floor(age) + " วินาทีที่แล้ว";
  if (age < 3600) return Math.floor(age / 60) + " นาทีที่แล้ว";
  if (age < 86400) return Math.floor(age / 3600) + " ชั่วโมงที่แล้ว";
  return Math.floor(age / 86400) + " วันที่แล้ว";
}

export const CAUSES = {
  window_open: "เปิดหน้าต่าง",
  cooking: "ทำอาหาร",
  incense_candle: "ธูปหรือเทียน",
  smoking: "สูบบุหรี่",
  outdoor_infiltration: "ฝุ่นจากข้างนอก",
  unknown: "ยังไม่ทราบ",
};

export function causeLabel(cause) {
  return CAUSES[cause] || cause || "–";
}

export const UNIT_MODES = {
  aim_person: "หันหาคน",
  survey: "สำรวจห้อง",
  night: "โหมดกลางคืน",
  idle: "พัก",
};

export const BOX_MODES = {
  IDLE: { label: "ไม่ได้เสียบสาย", tone: "muted" },
  DOCKED: { label: "เสียบสายแล้ว รออนุญาตชาร์จ", tone: "info" },
  CHARGING: { label: "กำลังชาร์จ", tone: "ok" },
  FULL: { label: "ชาร์จเต็มแล้ว", tone: "ok" },
  FAULT: { label: "หยุดชาร์จเพราะผิดปกติ", tone: "bad" },
  FIELD_TEST: { label: "ทดสอบ 24 ชม. (ไม่มีแบต)", tone: "info" },
};

export function boxMode(mode) {
  return BOX_MODES[mode] || { label: mode || "–", tone: "muted" };
}

export const ALERT_TITLES = {
  pm_high: "PM2.5 สูง",
  node_offline: "อุปกรณ์ขาดการติดต่อ",
  uart_fail: "สั่งงานเครื่องไม่สำเร็จ",
  charge_cut_temp: "หยุดชาร์จเพราะร้อนเกิน",
  charge_fault: "ที่ชาร์จผิดปกติ",
  battery_low: "แบตเตอรี่ใกล้หมด",
  charge_full: "ชาร์จเต็มแล้ว",
  filter_clogged: "ไส้กรองเริ่มตัน",
  stop_pressed: "มีคนกดปุ่มหยุด",
};

// state.alerts may hold objects ({type, level, title, text, since}) or bare type strings
// (what the current uploader writes). Return objects either way.
export function normalizeStateAlerts(list) {
  if (!Array.isArray(list)) return [];
  return list
    .map((a) => {
      if (typeof a === "string") return { type: a, level: "warn", title: ALERT_TITLES[a] || a, text: "" };
      if (a && typeof a === "object") {
        return {
          type: a.type || "",
          level: a.level || a.severity || "warn",
          title: a.title || ALERT_TITLES[a.type] || a.type || "แจ้งเตือน",
          text: a.text || a.msg || "",
          since: a.since,
        };
      }
      return null;
    })
    .filter(Boolean);
}

// alerts/{key} -> newest first
export function alertList(obj) {
  if (!obj || typeof obj !== "object") return [];
  return Object.keys(obj)
    .sort()
    .reverse()
    .map((key) => Object.assign({ key }, obj[key]));
}

// history/node1/{minuteTs} -> [{t, v}] sorted by time, null values dropped
export function historySeries(rows, field) {
  if (!rows || typeof rows !== "object") return [];
  return Object.keys(rows)
    .map(Number)
    .filter(isNum)
    .sort((a, b) => a - b)
    .map((t) => ({ t, v: rows[t] ? rows[t][field] : null }))
    .filter((p) => isNum(p.v));
}

// Average neighbouring points so a long range stays fast to draw.
export function downsample(points, maxPoints = MAX_CHART_POINTS) {
  if (points.length <= maxPoints) return points;
  const size = Math.ceil(points.length / maxPoints);
  const out = [];
  for (let i = 0; i < points.length; i += size) {
    const chunk = points.slice(i, i + size);
    const v = chunk.reduce((s, p) => s + p.v, 0) / chunk.length;
    out.push({ t: chunk[chunk.length - 1].t, v });
  }
  return out;
}

export function stats(points) {
  if (!points.length) return null;
  let min = Infinity, max = -Infinity, sum = 0;
  for (const p of points) {
    min = Math.min(min, p.v);
    max = Math.max(max, p.v);
    sum += p.v;
  }
  return { min, max, avg: sum / points.length };
}

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

// Settings form -> config fields allowed by the contract. Returns {ok, values, errors}.
export function validateConfig(form) {
  const errors = {};
  const values = {};

  const pm = Number(form.pm_alert);
  if (!Number.isInteger(pm) || pm < 5 || pm > 500) errors.pm_alert = "ใส่เลขเต็ม 5 ถึง 500";
  else values.pm_alert = pm;

  if (form.fan_mode !== "auto" && form.fan_mode !== "manual") errors.fan_mode = "เลือกอัตโนมัติหรือกำหนดเอง";
  else values.fan_mode = form.fan_mode;

  if (form.fan_mode === "manual") {
    const pct = Number(form.manual_fan_pct);
    if (!Number.isInteger(pct) || pct < 0 || pct > 100) errors.manual_fan_pct = "ใส่ 0 ถึง 100";
    else values.manual_fan_pct = pct;
  } else {
    values.manual_fan_pct = null;
  }

  for (const k of ["night_start", "night_end"]) {
    if (!HHMM.test(form[k] || "")) errors[k] = "รูปแบบ HH:MM";
    else values[k] = form[k];
  }

  return { ok: Object.keys(errors).length === 0, values, errors };
}

// Claim codes look like AB12-CD34; accept lower case and missing dash.
export function normalizeClaimCode(text) {
  const raw = String(text || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (raw.length !== 8) return null;
  return raw.slice(0, 4) + "-" + raw.slice(4);
}

export function feedbackRecord(cause, correct, trueCause) {
  const rec = { cause_ts: cause.ts, correct: Boolean(correct) };
  if (!correct && trueCause) rec.true_cause = trueCause;
  return rec;
}
