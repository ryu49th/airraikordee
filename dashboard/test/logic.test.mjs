import test from "node:test";
import assert from "node:assert/strict";
import * as L from "../public/js/logic.js";

test("PM2.5 bands follow the Thai thresholds", () => {
  assert.equal(L.pmBand(15).key, "very_good");
  assert.equal(L.pmBand(15.1).key, "good");
  assert.equal(L.pmBand(37.5).key, "moderate");
  assert.equal(L.pmBand(75).key, "unhealthy_sensitive");
  assert.equal(L.pmBand(76).key, "unhealthy");
  assert.equal(L.pmBand(null).key, "unknown");
});

test("corrected PM2.5 wins over the raw value", () => {
  assert.equal(L.pmShown({ pm25: 20, pm25_corrected: 17 }), 17);
  assert.equal(L.pmShown({ pm25: 20, pm25_corrected: null }), 20);
  assert.equal(L.pmShown(null), null);
});

test("data older than 90 s counts as offline", () => {
  assert.equal(L.isStale(1000, 1090), false);
  assert.equal(L.isStale(1000, 1091), true);
  assert.equal(L.isStale(undefined, 1000), true);
  assert.equal(L.ageText(1000, 1030), "30 วินาทีที่แล้ว");
  assert.equal(L.ageText(null, 1000), "ยังไม่เคยได้ข้อมูล");
});

test("state.alerts accepts objects and bare type strings", () => {
  const out = L.normalizeStateAlerts(["pm_high", { type: "battery_low", level: "warn", text: "เหลือ 20%" }, null]);
  assert.equal(out.length, 2);
  assert.equal(out[0].title, "PM2.5 สูง");
  assert.equal(out[1].title, "แบตเตอรี่ใกล้หมด");
  assert.deepEqual(L.normalizeStateAlerts(undefined), []);
});

test("alerts list is newest first", () => {
  const list = L.alertList({ "1794383760000000": { ts: 1 }, "1794383800000001": { ts: 2 } });
  assert.deepEqual(list.map((a) => a.ts), [2, 1]);
});

test("history rows become a sorted series and skip nulls", () => {
  const s = L.historySeries({ 120: { pm25: 3 }, 60: { pm25: 2 }, 180: { pm25: null } }, "pm25");
  assert.deepEqual(s, [{ t: 60, v: 2 }, { t: 120, v: 3 }]);
});

test("downsample keeps at most the limit and averages", () => {
  const pts = Array.from({ length: 10 }, (_, i) => ({ t: i, v: i }));
  const out = L.downsample(pts, 5);
  assert.equal(out.length, 5);
  assert.deepEqual(out[0], { t: 1, v: 0.5 });
  assert.equal(L.downsample(pts, 20), pts);
});

test("config form only produces contract fields", () => {
  const ok = L.validateConfig({ pm_alert: "35", fan_mode: "auto", manual_fan_pct: "70", night_start: "22:00", night_end: "06:00" });
  assert.equal(ok.ok, true);
  assert.deepEqual(ok.values, { pm_alert: 35, fan_mode: "auto", manual_fan_pct: null, night_start: "22:00", night_end: "06:00" });
  const manual = L.validateConfig({ pm_alert: "50", fan_mode: "manual", manual_fan_pct: "70", night_start: "23:30", night_end: "05:00" });
  assert.equal(manual.values.manual_fan_pct, 70);
  const bad = L.validateConfig({ pm_alert: "2", fan_mode: "x", night_start: "25:00", night_end: "" });
  assert.equal(bad.ok, false);
  assert.deepEqual(Object.keys(bad.errors).sort(), ["fan_mode", "night_end", "night_start", "pm_alert"]);
});

test("claim codes are normalised", () => {
  assert.equal(L.normalizeClaimCode("ab12cd34"), "AB12-CD34");
  assert.equal(L.normalizeClaimCode(" AB12-CD34 "), "AB12-CD34");
  assert.equal(L.normalizeClaimCode("AB12"), null);
});

test("feedback has only the contract fields", () => {
  assert.deepEqual(L.feedbackRecord({ ts: 5 }, false, "cooking"), { cause_ts: 5, correct: false, true_cause: "cooking" });
  assert.deepEqual(L.feedbackRecord({ ts: 5 }, true, "cooking"), { cause_ts: 5, correct: true });
});

test("ui_cmd records match the contract", () => {
  assert.deepEqual(L.uiCmdRecord("confirm_present", "u1", 1794383760),
    { ts: 1794383760, by: "u1", cmd: "confirm_present" });
  assert.deepEqual(L.uiCmdRecord("test_notify", "u1", 5), { ts: 5, by: "u1", cmd: "test_notify", channel: "discord" });
  assert.throws(() => L.uiCmdRecord("format_disk", "u1", 5));
});
