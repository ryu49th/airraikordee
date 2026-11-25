// Small SVG line chart (no library). points = [{t: unix seconds, v: number}] sorted by t.

const NS = "http://www.w3.org/2000/svg";
const W = 640, H = 240, PAD = { l: 44, r: 12, t: 12, b: 28 };

function el(name, attrs, parent) {
  const n = document.createElementNS(NS, name);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function timeLabel(t, longRange) {
  const d = new Date(t * 1000);
  const opts = longRange
    ? { day: "numeric", month: "short", timeZone: "Asia/Bangkok" }
    : { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Bangkok" };
  return d.toLocaleString("th-TH", opts);
}

export function renderChart(box, points, opt = {}) {
  box.textContent = "";
  if (!points.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "ยังไม่มีข้อมูลในช่วงนี้";
    box.appendChild(p);
    return;
  }
  const t0 = points[0].t, t1 = points[points.length - 1].t || t0 + 1;
  let vMax = Math.max(...points.map((p) => p.v));
  if (opt.threshold) vMax = Math.max(vMax, opt.threshold * 1.1);
  const vMin = opt.zeroBased === false ? Math.min(...points.map((p) => p.v)) : 0;
  const top = niceMax(vMax - vMin) + vMin;
  const x = (t) => PAD.l + ((t - t0) / Math.max(1, t1 - t0)) * (W - PAD.l - PAD.r);
  const y = (v) => H - PAD.b - ((v - vMin) / Math.max(1e-9, top - vMin)) * (H - PAD.t - PAD.b);

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img",
    "aria-label": opt.label || "กราฟ" }, box);

  for (let i = 0; i <= 4; i++) {
    const v = vMin + ((top - vMin) * i) / 4;
    el("line", { x1: PAD.l, x2: W - PAD.r, y1: y(v), y2: y(v), class: "grid" }, svg);
    const tx = el("text", { x: PAD.l - 6, y: y(v) + 4, class: "axis", "text-anchor": "end" }, svg);
    tx.textContent = Math.round(v * 10) / 10;
  }
  const long = t1 - t0 > 2 * 86400;
  for (let i = 0; i <= 4; i++) {
    const t = t0 + ((t1 - t0) * i) / 4;
    const tx = el("text", { x: x(t), y: H - 8, class: "axis",
      "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" }, svg);
    tx.textContent = timeLabel(t, long);
  }

  if (opt.threshold) {
    el("line", { x1: PAD.l, x2: W - PAD.r, y1: y(opt.threshold), y2: y(opt.threshold), class: "threshold" }, svg);
    const tx = el("text", { x: W - PAD.r, y: y(opt.threshold) - 4, class: "axis threshold-label", "text-anchor": "end" }, svg);
    tx.textContent = "เกณฑ์แจ้งเตือน " + opt.threshold;
  }

  // Break the line where rows are missing (an outage), instead of drawing across the gap.
  const gap = Math.max(180, ((t1 - t0) / points.length) * 5);
  let d = "";
  points.forEach((p, i) => {
    const move = i === 0 || p.t - points[i - 1].t > gap;
    d += (move ? "M" : "L") + x(p.t).toFixed(1) + " " + y(p.v).toFixed(1);
  });
  el("path", { d, class: "line" }, svg);

  const dot = el("circle", { r: 4, class: "dot", visibility: "hidden" }, svg);
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.hidden = true;
  box.appendChild(tip);

  svg.addEventListener("pointermove", (e) => {
    const rect = svg.getBoundingClientRect();
    const t = t0 + (((e.clientX - rect.left) / rect.width) * W - PAD.l) / (W - PAD.l - PAD.r) * (t1 - t0);
    let best = points[0];
    for (const p of points) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
    dot.setAttribute("cx", x(best.t));
    dot.setAttribute("cy", y(best.v));
    dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.textContent = timeLabel(best.t, false) + " · " + (Math.round(best.v * 10) / 10) + (opt.unit ? " " + opt.unit : "");
    tip.style.left = Math.min(rect.width - 120, Math.max(0, (x(best.t) / W) * rect.width - 60)) + "px";
  });
  svg.addEventListener("pointerleave", () => {
    dot.setAttribute("visibility", "hidden");
    tip.hidden = true;
  });
}
