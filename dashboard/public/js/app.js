import * as L from "./logic.js";
import { firebaseStore, demoStore } from "./store.js";
import { demoSeed, startDemo } from "./demo.js";
import { renderChart } from "./chart.js";

const $ = (id) => document.getElementById(id);
const nowS = () => Math.floor(Date.now() / 1000);

const ui = {
  store: null,
  user: null,
  deviceId: null,
  unsub: [],
  historyUnsub: null,
  range: "1h",
  metric: "pm25",
  data: { node1: null, node2: null, env: null, state: null, cause: null, health: null, config: null, history: null },
  configDirty: false,
};

// ------------------------------------------------------------ start

async function boot() {
  const params = new URLSearchParams(location.search);
  let config = null;
  if (!params.has("demo")) {
    try {
      const mod = await import("../config.js");
      config = mod.firebaseConfig;
      ui.devLogin = mod.devLogin && mod.devLogin.email && mod.devLogin.password ? mod.devLogin : null;
    } catch (e) {
      config = null;   // no config.js yet -> demo mode
    }
  }

  if (config) {
    ui.store = await firebaseStore(config);
  } else {
    const seed = demoSeed(nowS());
    ui.store = demoStore(seed);
    startDemo(ui.store, seed.deviceId);
    $("demo-badge").hidden = false;
  }

  wireStatic();
  ui.store.onAuth(onAuth);
  setInterval(refreshAges, 1000);
}

function show(viewId) {
  for (const id of ["loading", "view-login", "view-claim"]) $(id).hidden = id !== viewId;
  const dash = viewId === "dash";
  $("tabs").hidden = !dash;
  if (!dash) document.querySelectorAll(".tab").forEach((t) => (t.hidden = true));
  else showTab();
}

async function onAuth(user) {
  stopDevice();
  ui.user = user;
  if (!user && ui.devLogin && !ui.autoTried) {
    // Development only: config.js (git-ignored) holds a test owner login, so the page opens straight to the data.
    ui.autoTried = true;
    show("loading");
    $("loading").textContent = "กำลังเข้าสู่ระบบอัตโนมัติ (บัญชีทดสอบใน config.js)…";
    try {
      await ui.store.signIn(ui.devLogin.email, ui.devLogin.password);
      return;   // onAuth runs again with the user
    } catch (err) {
      setText("login-error", "เข้าสู่ระบบอัตโนมัติไม่ได้: " + errorText(err));
    }
  }
  if (!user) {
    $("device-select").hidden = true;
    show("view-login");
    return;
  }
  $("account-email").textContent = "เข้าสู่ระบบเป็น " + (user.email || user.uid);
  let mine = null;
  try {
    mine = await ui.store.get("users/" + user.uid + "/devices");
  } catch (e) {
    showLoadError(e);
    return;
  }
  const ids = Object.keys(mine || {}).sort();
  if (!ids.length) {
    $("claim-cancel").hidden = true;
    show("view-claim");
    return;
  }
  fillDeviceSelect(ids);
  let saved = null;
  try { saved = localStorage.getItem("deviceId"); } catch (e) { /* storage blocked */ }
  startDevice(ids.includes(saved) ? saved : ids[0]);
}

function showLoadError(e) {
  show("loading");
  $("loading").textContent = "โหลดข้อมูลไม่ได้: " + errorText(e);
}

function fillDeviceSelect(ids) {
  const sel = $("device-select");
  sel.textContent = "";
  for (const id of ids) {
    const o = document.createElement("option");
    o.value = id;
    o.textContent = id;
    sel.appendChild(o);
  }
  sel.hidden = ids.length < 2;
}

// ------------------------------------------------------------ device subscriptions

function startDevice(id) {
  stopDevice();
  ui.deviceId = id;
  $("device-select").value = id;
  try { localStorage.setItem("deviceId", id); } catch (e) { /* storage blocked */ }
  ui.data = { node1: null, node2: null, env: null, state: null, cause: null, health: null, config: null, history: null };
  ui.configDirty = false;
  const base = "devices/" + id + "/";
  const on = (path, key, render) =>
    ui.unsub.push(ui.store.listen(base + path, (v) => { ui.data[key] = v; render(); }, onListenError));

  on("telemetry/node1/latest", "node1", renderOverview);
  on("env/latest", "env", renderOverview);
  on("state", "state", renderOverview);
  on("cause/latest", "cause", renderCause);
  on("telemetry/node2/latest", "node2", () => { renderCharge(); renderOverview(); });
  on("health", "health", renderHealth);
  on("config", "config", () => { renderConfig(); renderHistory(); renderHealth(); });
  ui.unsub.push(ui.store.listenLast(base + "alerts", 50, renderAlerts, onListenError));
  subscribeHistory();
  show("dash");
}

function stopDevice() {
  for (const u of ui.unsub) if (typeof u === "function") u();
  ui.unsub = [];
  if (ui.historyUnsub) ui.historyUnsub();
  ui.historyUnsub = null;
}

function subscribeHistory() {
  if (ui.historyUnsub) ui.historyUnsub();
  ui.data.history = null;
  renderHistory();
  ui.historyUnsub = ui.store.listenLast(
    "devices/" + ui.deviceId + "/history/node1",
    L.HISTORY_LIMITS[ui.range],
    (v) => { ui.data.history = v; renderHistory(); },
    onListenError,
  );
}

function onListenError(e) {
  const b = $("offline-banner");
  b.hidden = false;
  b.textContent = "อ่านข้อมูลไม่ได้ (" + errorText(e) + ") ลองออกจากระบบแล้วเข้าใหม่";
}

// ------------------------------------------------------------ render

function setText(id, text) {
  $(id).textContent = text;
}

function renderOverview() {
  const n1 = ui.data.node1 || {};
  const env = ui.data.env || {};
  const st = ui.data.state || {};
  const pm = L.pmShown(ui.data.node1);
  const band = L.pmBand(pm);

  setText("pm-value", L.fmt(pm, 0));
  const bandEl = $("pm-band");
  bandEl.textContent = band.label;
  $("pm-card").dataset.band = band.key;
  setText("pm-raw", L.isNum(n1.pm25_corrected) && L.isNum(n1.pm25)
    ? "ค่าดิบจากเซนเซอร์ " + L.fmt(n1.pm25, 0) + " · ปรับตามความชื้นแล้ว"
    : "");

  setText("env-temp", L.fmt(env.temp_c, 1, "°C"));
  setText("env-rh", L.fmt(env.rh, 0, "%"));
  setText("env-pressure", L.fmt(env.pressure_hpa, 0, "hPa"));
  setText("pm10", L.fmt(n1.pm10, 0, "µg/m³"));

  const mode = n1.mode || st.mode;
  setText("unit-mode", L.UNIT_MODES[mode] || mode || "–");
  const fanPct = L.isNum(n1.fan_pct) ? n1.fan_pct : st.fan_pct;
  setText("fan", L.isNum(fanPct) ? fanPct + "%" + (L.isNum(n1.fan_rpm) ? " · " + n1.fan_rpm + " rpm" : "") : "–");
  setText("person", n1.person === true ? "มี" : n1.person === false ? "ไม่มี" : "–");
  setText("aim", L.isNum(n1.pan_deg) ? L.fmt(n1.pan_deg, 0) + "° / ก้ม-เงย " + L.fmt(n1.tilt_deg, 0) + "°" : "–");
  const n2 = ui.data.node2 || {};
  setText("battery-mini", L.isNum(n2.est_percent) ? n2.est_percent + "% · " + L.boxMode(n2.mode).label : "–");

  const list = $("active-alerts");
  list.textContent = "";
  const active = L.normalizeStateAlerts(st.alerts);
  if (!active.length) list.appendChild(li("ไม่มี", "muted"));
  for (const a of active) list.appendChild(alertItem(a));

  refreshAges();
}

function li(text, cls) {
  const n = document.createElement("li");
  n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

function alertItem(a) {
  const n = document.createElement("li");
  n.className = "alert " + (a.level || "warn");
  const head = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = a.title || L.ALERT_TITLES[a.type] || a.type || "แจ้งเตือน";
  head.appendChild(title);
  const when = a.ts || a.since;
  if (L.isNum(when)) {
    const t = document.createElement("span");
    t.className = "muted small";
    t.textContent = " · " + new Date(when * 1000).toLocaleString("th-TH", { timeZone: "Asia/Bangkok" });
    head.appendChild(t);
  }
  n.appendChild(head);
  if (a.text) n.appendChild(Object.assign(document.createElement("div"), { textContent: a.text }));
  if (Array.isArray(a.sent_to) && a.sent_to.length) {
    n.appendChild(Object.assign(document.createElement("div"), {
      className: "muted small", textContent: "ส่งไปที่ " + a.sent_to.join(", "),
    }));
  }
  return n;
}

let feedbackFor = null;   // cause ts the user already answered

function renderCause() {
  const c = ui.data.cause;
  if (!c) {
    setText("cause-label", "ยังไม่มีผล");
    setText("cause-conf", "");
    setText("cause-advice", "");
    setText("cause-meta", "");
    $("cause-actions").hidden = true;
    return;
  }
  setText("cause-label", L.causeLabel(c.cause));
  setText("cause-conf", L.isNum(c.confidence) ? "มั่นใจ " + Math.round(c.confidence * 100) + "%" : "");
  setText("cause-advice", c.advice_th || "");
  const src = c.source === "cloud" ? "AI บนคลาวด์" : c.source === "rules" ? "กฎในเครื่อง" : "";
  setText("cause-meta", (src ? "วิเคราะห์โดย " + src + " · " : "") + L.ageText(c.ts, nowS()));
  const answered = feedbackFor === c.ts;
  $("cause-actions").hidden = answered;
  $("cause-fix").hidden = true;
  $("cause-thanks").hidden = !answered;
}

function renderCharge() {
  const n2 = ui.data.node2;
  const m = L.boxMode(n2 && n2.mode);
  const pill = $("box-mode");
  pill.textContent = n2 ? m.label : "ยังไม่มีข้อมูล";
  pill.dataset.tone = m.tone;
  setText("box-plugged", !n2 ? "" : n2.plugged ? "สายเสียบอยู่" : "ไม่ได้เสียบสาย");
  const pct = n2 && L.isNum(n2.est_percent) ? n2.est_percent : null;
  setText("batt-pct", pct === null ? "–" : String(pct));
  $("batt-bar").style.width = (pct || 0) + "%";
  $("batt-bar").dataset.low = pct !== null && pct < 25 ? "1" : "";
  setText("box-v", L.fmt(n2 && n2.voltage, 2, "V"));
  setText("box-i", L.fmt(n2 && n2.current_a, 2, "A"));
  setText("box-batt-temp", L.fmt(n2 && n2.batt_temp_c, 1, "°C"));
  setText("box-temp", L.fmt(n2 && n2.temp_c, 1, "°C"));
  const fault = $("box-fault");
  fault.hidden = !(n2 && n2.mode === "FAULT");
  if (!fault.hidden) {
    const why = { over_current: "กระแสเกิน", over_temp: "ร้อนเกิน", timer: "ชาร์จนานเกิน 4 ชั่วโมง" }[n2.fault];
    fault.textContent = "หยุดชาร์จ" + (why ? "เพราะ" + why : "") + " แก้สาเหตุก่อน แล้วกดรีเซ็ตกล่องชาร์จ";
  }
  $("btn-reset-fault").hidden = fault.hidden;
  refreshAges();
}

function renderAlerts(obj) {
  const list = $("alert-list");
  list.textContent = "";
  const items = L.alertList(obj);
  if (!items.length) list.appendChild(li("ยังไม่มีแจ้งเตือน", "muted"));
  for (const a of items) list.appendChild(alertItem(a));
}

function onlineText(flag) {
  return flag === true ? "● ออนไลน์" : flag === false ? "● ออฟไลน์" : "–";
}

function renderHealth() {
  const h = ui.data.health || {};
  const n1 = $("h-node1"), n2 = $("h-node2");
  n1.textContent = onlineText(h.node1_online);
  n1.dataset.state = h.node1_online === false ? "bad" : h.node1_online ? "ok" : "";
  n2.textContent = onlineText(h.node2_online);
  n2.dataset.state = h.node2_online === false ? "bad" : h.node2_online ? "ok" : "";
  setText("h-cpu", L.fmt(h.cpu_temp_c, 0, "°C"));
  setText("h-pending", L.isNum(h.pending_rows) ? h.pending_rows + " แถว" : "–");
  setText("h-upload", L.ageText(h.last_upload_ts, nowS()));
  setText("h-filter", L.fmt(h.filter_hours, 0, "ชั่วโมง"));
  setText("h-pms", L.fmt(h.pms_hours, 0, "ชั่วโมง"));
  const reset = ui.data.config && ui.data.config.filter_reset_ts;
  setText("h-filter-reset", L.isNum(reset)
    ? new Date(reset * 1000).toLocaleDateString("th-TH", { timeZone: "Asia/Bangkok", dateStyle: "medium" })
    : "–");
}

const METRICS = {
  pm25: { label: "PM2.5", unit: "µg/m³", digits: 1 },
  temp_c: { label: "อุณหภูมิ", unit: "°C", digits: 1, zeroBased: false },
  rh: { label: "ความชื้น", unit: "%", digits: 0 },
  fan_pct: { label: "พัดลม", unit: "%", digits: 0 },
};

function renderHistory() {
  if ($("tab-history").hidden) return;
  const box = $("chart");
  if (ui.data.history === null) {
    box.textContent = "กำลังโหลด…";
    return;
  }
  const meta = METRICS[ui.metric];
  const all = L.historySeries(ui.data.history, ui.metric);
  const points = L.downsample(all);
  const cfg = ui.data.config || {};
  renderChart(box, points, {
    unit: meta.unit,
    label: meta.label,
    zeroBased: meta.zeroBased,
    threshold: ui.metric === "pm25" && L.isNum(cfg.pm_alert) ? cfg.pm_alert : null,
  });
  const s = L.stats(all);
  const dl = $("history-stats");
  dl.textContent = "";
  if (s) {
    for (const [k, v] of [["ต่ำสุด", s.min], ["เฉลี่ย", s.avg], ["สูงสุด", s.max]]) {
      const d = document.createElement("div");
      d.appendChild(Object.assign(document.createElement("dt"), { textContent: k }));
      d.appendChild(Object.assign(document.createElement("dd"), { textContent: L.fmt(v, meta.digits, meta.unit) }));
      dl.appendChild(d);
    }
  }
  const expected = L.HISTORY_LIMITS[ui.range];
  setText("history-note", all.length < expected * 0.9
    ? "มีข้อมูล " + all.length + " จาก " + expected + " นาที (ช่วงที่เครื่องปิดหรือเน็ตหลุดจะเว้นว่าง)"
    : "");
}

function renderConfig() {
  if (ui.configDirty) return;   // do not overwrite what the user is typing
  const c = ui.data.config || {};
  const f = $("config-form");
  f.pm_alert.value = L.isNum(c.pm_alert) ? c.pm_alert : 35;
  const mode = c.fan_mode === "manual" ? "manual" : "auto";
  f.querySelector(`input[name=fan_mode][value=${mode}]`).checked = true;
  f.manual_fan_pct.value = L.isNum(c.manual_fan_pct) ? c.manual_fan_pct : 50;
  f.night_start.value = c.night_start || "22:00";
  f.night_end.value = c.night_end || "06:00";
  syncManualFan();
}

function syncManualFan() {
  const f = $("config-form");
  const manual = f.querySelector("input[name=fan_mode]:checked")?.value === "manual";
  $("manual-fan-row").hidden = !manual;
  $("manual-fan-out").textContent = f.manual_fan_pct.value;
}

function refreshAges() {
  if (!ui.deviceId) return;
  const now = nowS();
  const n1 = ui.data.node1;
  setText("pm-updated", n1 ? "อัปเดต " + L.ageText(n1.ts, now) : "");
  const n2 = ui.data.node2;
  setText("box-updated", n2 ? "อัปเดต " + L.ageText(n2.ts, now) : "");

  const stale = L.isStale(n1 && n1.ts, now);
  const conn = $("conn");
  conn.dataset.state = stale ? "bad" : "ok";
  conn.textContent = stale ? "ออฟไลน์" : "ออนไลน์";
  const banner = $("offline-banner");
  if (stale) {
    banner.hidden = false;
    banner.textContent = n1
      ? "ไม่ได้รับข้อมูลจากเครื่องมา " + L.ageText(n1.ts, now).replace("ที่แล้ว", "") + " ค่าที่เห็นอาจไม่ใช่ค่าปัจจุบัน"
      : "ยังไม่มีข้อมูลจากเครื่องนี้ ตรวจว่าเครื่องเปิดและต่อเน็ตแล้ว";
  } else if (banner.textContent.startsWith("ไม่ได้รับ") || banner.textContent.startsWith("ยังไม่มี")) {
    banner.hidden = true;
  }
  if (ui.data.health) setText("h-upload", L.ageText(ui.data.health.last_upload_ts, now));
}

// ------------------------------------------------------------ tabs

function currentTab() {
  const t = location.hash.replace("#", "");
  return document.getElementById("tab-" + t) ? t : "overview";
}

function showTab() {
  const t = currentTab();
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== "tab-" + t));
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === t));
  if (t === "history") renderHistory();
}

// ------------------------------------------------------------ actions

function errorText(e) {
  const code = (e && (e.code || e.message)) || String(e);
  const map = {
    "auth/invalid-credential": "อีเมลหรือรหัสผ่านไม่ถูกต้อง",
    "auth/wrong-password": "อีเมลหรือรหัสผ่านไม่ถูกต้อง",
    "auth/user-not-found": "ยังไม่มีบัญชีนี้ กดสมัครใหม่",
    "auth/email-already-in-use": "อีเมลนี้มีบัญชีแล้ว กดเข้าสู่ระบบ",
    "auth/weak-password": "รหัสผ่านต้องยาวอย่างน้อย 6 ตัว",
    "auth/too-many-requests": "ลองผิดหลายครั้ง รอสักครู่แล้วลองใหม่",
    "PERMISSION_DENIED": "ไม่มีสิทธิ์",
  };
  for (const k in map) if (String(code).includes(k)) return map[k];
  return String(code);
}

function busy(btn, on) {
  btn.disabled = on;
}

function wireStatic() {
  window.addEventListener("hashchange", () => { if (!$("tabs").hidden) showTab(); });

  const login = $("login-form");
  login.addEventListener("submit", async (e) => {
    e.preventDefault();
    setText("login-error", "");
    const btn = login.querySelector("button[type=submit]");
    busy(btn, true);
    try {
      await ui.store.signIn(login.email.value.trim(), login.password.value);
    } catch (err) {
      setText("login-error", errorText(err));
    }
    busy(btn, false);
  });
  $("signup-btn").addEventListener("click", async () => {
    setText("login-error", "");
    if (!login.reportValidity()) return;
    try {
      await ui.store.signUp(login.email.value.trim(), login.password.value);
    } catch (err) {
      setText("login-error", errorText(err));
    }
  });

  document.querySelectorAll("[data-action=sign-out]").forEach((b) =>
    b.addEventListener("click", () => ui.store.signOut()));

  $("device-select").addEventListener("change", (e) => startDevice(e.target.value));

  // claim: read claims/(code) for the deviceId, write users/(uid)/devices/(id) = {code}, then mark the code used
  const claim = $("claim-form");
  claim.addEventListener("submit", async (e) => {
    e.preventDefault();
    setText("claim-error", "");
    const code = L.normalizeClaimCode(claim.code.value);
    if (!code) {
      setText("claim-error", "รหัสต้องมี 8 ตัวอักษร เช่น AB12-CD34");
      return;
    }
    const btn = claim.querySelector("button[type=submit]");
    busy(btn, true);
    try {
      const c = await ui.store.get("claims/" + code);
      if (!c || !c.deviceId) throw new Error("ไม่พบรหัสนี้");
      if (c.used === true) throw new Error("รหัสนี้ถูกใช้ไปแล้ว");
      if (c.used !== false) {
        // e.g. "false" typed with quotes in the console is a string, which the rules will not accept
        throw new Error("ค่า used ของรหัสนี้ใน Firebase เป็น " + JSON.stringify(c.used) + " ต้องเป็น false (boolean ไม่มีเครื่องหมายคำพูด)");
      }
      await ui.store.set("users/" + ui.user.uid + "/devices/" + c.deviceId, { code });
      await ui.store.update("claims/" + code, { used: true });
      claim.reset();
      try { localStorage.setItem("deviceId", c.deviceId); } catch (e2) { /* storage blocked */ }
      await onAuth(ui.user);   // reload the device list and open the new unit
    } catch (err) {
      setText("claim-error", errorText(err));
    }
    busy(btn, false);
  });
  $("claim-cancel").addEventListener("click", () => startDevice(ui.deviceId));
  $("add-device").addEventListener("click", () => {
    stopDevice();
    $("claim-cancel").hidden = false;
    show("view-claim");
  });

  // feedback on the dust cause
  const causeSel = $("cause-fix").true_cause;
  for (const [k, v] of Object.entries(L.CAUSES)) {
    const o = document.createElement("option");
    o.value = k;
    o.textContent = v;
    causeSel.appendChild(o);
  }
  async function sendFeedback(correct, trueCause) {
    const c = ui.data.cause;
    if (!c) return;
    try {
      await ui.store.push("devices/" + ui.deviceId + "/feedback", L.feedbackRecord(c, correct, trueCause));
      feedbackFor = c.ts;
      renderCause();
    } catch (err) {
      alert("ส่งไม่สำเร็จ: " + errorText(err));
    }
  }
  $("cause-yes").addEventListener("click", () => sendFeedback(true));
  $("cause-no").addEventListener("click", () => {
    $("cause-actions").hidden = true;
    $("cause-fix").hidden = false;
  });
  $("cause-fix").addEventListener("submit", (e) => {
    e.preventDefault();
    sendFeedback(false, causeSel.value);
  });

  // history controls
  $("range-seg").addEventListener("click", (e) => {
    const r = e.target.dataset.range;
    if (!r || r === ui.range) return;
    ui.range = r;
    segOn("range-seg", e.target);
    subscribeHistory();
  });
  $("metric-seg").addEventListener("click", (e) => {
    const m = e.target.dataset.metric;
    if (!m) return;
    ui.metric = m;
    segOn("metric-seg", e.target);
    renderHistory();
  });

  // settings
  const form = $("config-form");
  form.addEventListener("input", () => {
    ui.configDirty = true;
    syncManualFan();
    setText("config-status", "ยังไม่ได้บันทึก");
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    form.querySelectorAll("[data-err]").forEach((n) => (n.textContent = ""));
    const res = L.validateConfig({
      pm_alert: form.pm_alert.value,
      fan_mode: form.querySelector("input[name=fan_mode]:checked")?.value,
      manual_fan_pct: form.manual_fan_pct.value,
      night_start: form.night_start.value,
      night_end: form.night_end.value,
    });
    if (!res.ok) {
      for (const k in res.errors) {
        const n = form.querySelector(`[data-err=${k}]`);
        if (n) n.textContent = res.errors[k];
      }
      setText("config-status", "ยังบันทึกไม่ได้ ตรวจช่องที่เป็นสีแดง");
      return;
    }
    const btn = form.querySelector("button[type=submit]");
    busy(btn, true);
    try {
      await ui.store.update("devices/" + ui.deviceId + "/config", res.values);
      ui.configDirty = false;
      setText("config-status", "บันทึกแล้ว เครื่องจะใช้ค่าใหม่ภายในไม่กี่วินาที");
      renderConfig();
    } catch (err) {
      setText("config-status", "บันทึกไม่สำเร็จ: " + errorText(err));
    }
    busy(btn, false);
  });

  document.querySelectorAll("button[data-cmd]").forEach((btn) => {
    btn.addEventListener("click", () => sendCmd(btn));
  });

  $("filter-reset").addEventListener("click", async () => {
    if (!confirm("ยืนยันว่าเปลี่ยนไส้กรองใหม่แล้ว?")) return;
    try {
      await ui.store.update("devices/" + ui.deviceId + "/config", { filter_reset_ts: nowS() });
      setText("filter-status", "บันทึกแล้ว");
    } catch (err) {
      setText("filter-status", "บันทึกไม่สำเร็จ: " + errorText(err));
    }
  });
}

// Write devices/(id)/ui_cmd/(push id); the Jetson deletes it when handled, so "gone" = received.
async function sendCmd(btn) {
  const cmd = btn.dataset.cmd;
  const status = btn.dataset.status;
  if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  const path = "devices/" + ui.deviceId + "/ui_cmd";
  busy(btn, true);
  setText(status, "กำลังส่ง…");
  let ref;
  try {
    ref = await ui.store.push(path, L.uiCmdRecord(cmd, ui.user.uid, nowS()));
  } catch (err) {
    setText(status, "ส่งไม่สำเร็จ: " + errorText(err));
    busy(btn, false);
    return;
  }
  setText(status, "ส่งแล้ว รอเครื่องรับ…");
  let seen = false;
  let finished = false;
  let off = null;
  const finish = (text) => {
    if (finished) return;
    finished = true;
    setText(status, text);
    busy(btn, false);
    if (off) off();
  };
  const timer = setTimeout(() => finish("เครื่องยังไม่รับคำสั่ง (ออฟไลน์อยู่หรือเปล่า?) คำสั่งจะค้างไว้ 10 นาที"),
    L.UI_CMD_TIMEOUT_S * 1000);
  off = ui.store.listen(path + "/" + ref.key, (v) => {
    if (v !== null) { seen = true; return; }
    if (seen) { clearTimeout(timer); finish("เครื่องรับคำสั่งแล้ว"); }
  }, (err) => { clearTimeout(timer); finish("ติดตามสถานะไม่ได้: " + errorText(err)); });
  if (finished && off) off();
}

function segOn(groupId, btn) {
  $(groupId).querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
}

boot().catch((e) => {
  $("loading").hidden = false;
  $("loading").textContent = "เปิดแอปไม่ได้: " + errorText(e);
});
