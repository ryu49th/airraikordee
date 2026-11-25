import { initializeTestEnvironment, assertSucceeds, assertFails } from "@firebase/rules-unit-testing";
import { ref, set, get, update, push, remove } from "firebase/database";
import { readFileSync } from "fs";
import test from "node:test";

const DEV = "airraikordee-01", OTHER = "airraikordee-02";
const DEVMAIL = `device-${DEV}@devices.airraikordee.example`;
let env;
test.before(async () => {
  env = await initializeTestEnvironment({ projectId: "demo-ab", database: { host: "127.0.0.1", port: 9000, rules: readFileSync("rules.copy.json", "utf8") } });
  await env.withSecurityRulesDisabled(async (c) => {
    const d = c.database();
    await set(ref(d, "claims/AB12CD34"), { deviceId: DEV, used: false });
    await set(ref(d, "claims/ZZ99YY88"), { deviceId: OTHER, used: false });
    await set(ref(d, "users/owner1/devices/" + DEV), { code: "AB12CD34" });
    await set(ref(d, "claims/AB12CD34/used"), true);
    await set(ref(d, `devices/${DEV}/config`), { pm_alert: 35 });
    await set(ref(d, `devices/${DEV}/telemetry/node1/latest`), { ts: 1 });
    await set(ref(d, `devices/${DEV}/ui_cmd/k1`), { ts: 1, by: "owner1", cmd: "reset_fault" });
    await set(ref(d, `devices/${OTHER}/telemetry/node1/latest`), { ts: 1 });
  });
});
test.after(async () => env.cleanup());
const owner = () => env.authenticatedContext("owner1", { email: "o1@example.com" }).database();
const stranger = () => env.authenticatedContext("stranger", { email: "s@example.com" }).database();
const device = () => env.authenticatedContext("devuid", { email: DEVMAIL }).database();
const anon = () => env.unauthenticatedContext().database();

test("owner reads own unit", () => assertSucceeds(get(ref(owner(), `devices/${DEV}/telemetry/node1/latest`))));
test("owner cannot read another unit", () => assertFails(get(ref(owner(), `devices/${OTHER}/telemetry/node1/latest`))));
test("stranger denied read", () => assertFails(get(ref(stranger(), `devices/${DEV}/telemetry/node1/latest`))));
test("unauthenticated denied read", () => assertFails(get(ref(anon(), `devices/${DEV}/telemetry/node1/latest`))));
test("unauthenticated denied write", () => assertFails(set(ref(anon(), `devices/${DEV}/config/pm_alert`), 40)));
test("owner writes config (dashboard update)", () => assertSucceeds(update(ref(owner(), `devices/${DEV}/config`), { pm_alert: 40, fan_mode: "manual", manual_fan_pct: 50, night_start: "22:00", night_end: "06:00" })));
test("owner config out of range denied", () => assertFails(update(ref(owner(), `devices/${DEV}/config`), { pm_alert: 900 })));
test("owner config bad HH:MM denied", () => assertFails(update(ref(owner(), `devices/${DEV}/config`), { night_start: "25:00" })));
test("owner config unknown field denied", () => assertFails(update(ref(owner(), `devices/${DEV}/config`), { evil: 1 })));
test("stranger cannot write config", () => assertFails(update(ref(stranger(), `devices/${DEV}/config`), { pm_alert: 40 })));
test("owner cannot write telemetry", () => assertFails(set(ref(owner(), `devices/${DEV}/telemetry/node1/latest`), { ts: 5, pm25: 10 })));
test("owner cannot write alerts or health", async () => {
  await assertFails(set(ref(owner(), `devices/${DEV}/health`), { node1_online: true }));
  await assertFails(set(ref(owner(), `devices/${DEV}/alerts/k`), { ts: 1, level: "warn", title: "t", text: "x" }));
});
test("device writes telemetry via multi-path PATCH", () => assertSucceeds(update(ref(device(), `devices/${DEV}`), {
  "telemetry/node1/latest": { ts: 10, pm25: 17, pm25_corrected: 14.2, pm10: 22, fan_pct: 40, fan_rpm: 1180, pan_deg: 10, tilt_deg: 5, person: true, mode: "idle" },
  "env/latest": { ts: 10, temp_c: 29, rh: 70, pressure_hpa: 1008 },
  "history/node1/1794383700": { ts: 10, pm25: 17, temp_c: 29, rh: 70, fan_pct: 40 },
  "alerts/0001794383760001": { ts: 10, level: "warn", title: "PM2.5 สูง", text: "x", sent_to: ["discord"] },
  "health": { node1_online: true, node2_online: false, cpu_temp_c: 50, pending_rows: 0, last_upload_ts: 9, filter_hours: 1, pms_hours: 2 },
  "state": { mode: "idle", fan_pct: 25, target: { pan_deg: 1, tilt_deg: 2 }, alerts: ["pm_high"] },
  "cause/latest": { ts: 10, cause: "cooking", confidence: 0.7, source: "rules", advice_th: "x" }
})));
test("device wrong type denied", () => assertFails(update(ref(device(), `devices/${DEV}`), { "telemetry/node1/latest": { ts: "x" } })));
test("device cannot write another unit", () => assertFails(set(ref(device(), `devices/${OTHER}/telemetry/node1/latest`), { ts: 3 })));
test("device cannot write config", () => assertFails(update(ref(device(), `devices/${DEV}/config`), { pm_alert: 10 })));
test("device reads own unit, config, ui_cmd", async () => {
  await assertSucceeds(get(ref(device(), `devices/${DEV}/config`)));
  await assertSucceeds(get(ref(device(), `devices/${DEV}/ui_cmd`)));
});
test("ui_cmd: owner creates", () => assertSucceeds(set(ref(owner(), `devices/${DEV}/ui_cmd/k2`), { ts: 2, by: "owner1", cmd: "reset_filter" })));
test("ui_cmd: by must be own uid", () => assertFails(set(ref(owner(), `devices/${DEV}/ui_cmd/k3`), { ts: 2, by: "other", cmd: "reset_filter" })));
test("ui_cmd: unknown cmd denied", () => assertFails(set(ref(owner(), `devices/${DEV}/ui_cmd/k4`), { ts: 2, by: "owner1", cmd: "format_disk" })));
test("ui_cmd: owner cannot edit existing", () => assertFails(set(ref(owner(), `devices/${DEV}/ui_cmd/k1`), { ts: 9, by: "owner1", cmd: "survey_now" })));
test("ui_cmd: owner cannot delete existing", () => assertFails(remove(ref(owner(), `devices/${DEV}/ui_cmd/k1`))));
test("ui_cmd: device deletes after handling", () => assertSucceeds(remove(ref(device(), `devices/${DEV}/ui_cmd/k1`))));
test("feedback: owner creates (dashboard shape)", () => assertSucceeds(push(ref(owner(), `devices/${DEV}/feedback`), { cause_ts: 5, correct: false, true_cause: "cooking" })));
test("feedback: owner cannot overwrite existing", async () => {
  const r = push(ref(owner(), `devices/${DEV}/feedback`), { cause_ts: 5, correct: true }); await assertSucceeds(r);
  await assertFails(set(r, { cause_ts: 6, correct: false }));
});
test("claim: owner claims unit once", async () => {
  const d = env.authenticatedContext("owner2", { email: "o2@example.com" }).database();
  await assertSucceeds(get(ref(d, "claims/ZZ99YY88")));
  await assertSucceeds(set(ref(d, `users/owner2/devices/${OTHER}`), { code: "ZZ99YY88" }));
  await assertSucceeds(update(ref(d, "claims/ZZ99YY88"), { used: true }));
});
test("claim: second user with a used code denied", async () => {
  const d = env.authenticatedContext("owner3", { email: "o3@example.com" }).database();
  await assertFails(set(ref(d, `users/owner3/devices/${DEV}`), { code: "AB12CD34" }));
});
test("claim: wrong code for unit denied", async () => {
  const d = env.authenticatedContext("owner4", { email: "o4@example.com" }).database();
  await env.withSecurityRulesDisabled(async (c) => set(ref(c.database(), "claims/QQ11WW22"), { deviceId: "airraikordee-09", used: false }));
  await assertFails(set(ref(d, `users/owner4/devices/${DEV}`), { code: "QQ11WW22" }));
});
test("claims cannot be listed", () => assertFails(get(ref(stranger(), "claims"))));
test("users cannot read another user's devices", () => assertFails(get(ref(stranger(), "users/owner1/devices"))));
test("deviceSecrets: owner writes discord webhook, cannot read back", async () => {
  await assertSucceeds(set(ref(owner(), `deviceSecrets/${DEV}/discord`), "https://discord.com/api/webhooks/123/abc"));
  await assertFails(get(ref(owner(), `deviceSecrets/${DEV}/discord`)));
});
test("deviceSecrets: wrong prefix denied", () => assertFails(set(ref(owner(), `deviceSecrets/${DEV}/discord`), "https://evil.example/hook")));
test("deviceSecrets: stranger cannot write", () => assertFails(set(ref(stranger(), `deviceSecrets/${DEV}/discord`), "https://discord.com/api/webhooks/1/a")));
test("deviceSecrets: own device reads", () => assertSucceeds(get(ref(device(), `deviceSecrets/${DEV}/discord`))));
test("deviceSecrets: other unit's device denied", () => assertFails(get(ref(env.authenticatedContext("d2", { email: `device-${OTHER}@devices.airraikordee.example` }).database(), `deviceSecrets/${DEV}/discord`))));

// LINE linking (live rules had this before the repo file did)
test("owner writes and reads own link code", async () => {
  await assertSucceeds(set(ref(owner(), "users/owner1/linkcode"), "K7Q2M9"));
  await assertSucceeds(get(ref(owner(), "users/owner1/linkcode")));
});
test("another user cannot write or read someone else's link code", async () => {
  await assertFails(set(ref(stranger(), "users/owner1/linkcode"), "XXXXXX"));
  await assertFails(get(ref(stranger(), "users/owner1/linkcode")));
});
test("link code must be a short string", async () => {
  await assertFails(set(ref(owner(), "users/owner1/linkcode"), 12345));
  await assertFails(set(ref(owner(), "users/owner1/linkcode"), "x".repeat(65)));
});
test("signed-out cannot touch link codes", () => assertFails(set(ref(anon(), "users/owner1/linkcode"), "K7Q2M9")));
