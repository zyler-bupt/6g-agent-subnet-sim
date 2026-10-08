/* TCANet testbed console: live view of the prototype, fed by /api/stream (SSE). */
"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
// Dependency colors, validated (dataviz checks) on the paper sheet and on the aubergine screens.
const DEP_SHEET = { e1: "#00859f", e2: "#7353d4", e3: "#a87200" };
const DEP_SCREEN = { e1: "#16a19a", e2: "#8e74ec", e3: "#b98300" };
const GW_SCREEN = { G1: "#16a19a", G2: "#8e74ec", G3: "#b98300", G4: "#e0607e" };
const LAYER = { transport: "#d05a14", network: "#2f6fde", physical: "#c2417a" };
const DEP_OFFSET = { e1: -8, e2: 0, e3: 8 };
const INK = "#172033", INK2 = "#4a5568", BAD = "#c93a3a";
const SCR = { ink: "#efe6ec", muted: "#b79fb0", rule: "#4a1d3e", ok: "#5fd394", bad: "#ff8a8a", warn: "#f2c14e" };

// Fixed geometry of the paper Fig. 1 world (viewBox 1200 x 470).
const GW_POS = { G1: [330, 215], G2: [600, 85], G3: [600, 350], G4: [870, 215] };
const EP_POS = { a1: [105, 215], a2: [395, 420], a3: [1095, 215], a4: [805, 420] };
const EP_ICON = { a1: "drone", a2: "camera", a3: "brain", a4: "ambulance" };
const EP_NAME = { a1: "drone", a2: "roadside camera", a3: "edge AI", a4: "rescue vehicle" };
const LINK_BEND = { L4: 34, L5: 34 };
const CTRL_POS = [135, 55];

const S = {
  scenario: null, subnet: { version: 0, paths: {}, bindings: {} }, logs: [], flows: {}, access: {},
  agentLogs: { application: [], transport: [], network: [], physical: [] }, faults: [], status: null,
  clockOffset: 0, lastEvent: 0, view: null, tour: null, verifyGw: "G1",
};

const $ = (id) => document.getElementById(id);
const now = () => Date.now() / 1000 + S.clockOffset;
const el = (tag, attrs = {}, parent) => {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
};
const icon = (name, x, y, size, color, parent) => {
  const g = el("g", { transform: `translate(${x - size / 2},${y - size / 2}) scale(${size / 24})`,
    fill: "none", stroke: color, "stroke-width": 1.7, "stroke-linecap": "round", "stroke-linejoin": "round" }, parent);
  g.innerHTML = (window.TCANET_ICONS || {})[name] || "";
  return g;
};
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const sentence = (s) => { const t = String(s).trim(); return t ? t[0].toUpperCase() + t.slice(1) + (/[.…]$/.test(t) ? "" : ".") : ""; };

/* ------------------------------------------------------------------ data */
function ingest(event) {
  S.lastEvent = Date.now();
  if (event.type === "snapshot") {
    Object.assign(S, { scenario: event.scenario, subnet: event.subnet, logs: event.logs, flows: event.flows,
      access: event.access, agentLogs: event.agent_logs, faults: event.faults });
    setStatus(event.status);
    buildTopology();
    renderCtrlLog();
    for (const layer of Object.keys(S.agentLogs)) renderAgentLog(layer);
    renderWorkflow();
    return;
  }
  if (event.type === "status") return setStatus(event);
  const p = event.payload, ts = event.ts;
  switch (event.topic) {
    case "report.flow": push(S.flows, p.dep_id, [ts, p.rx_mbps, p.owd_ms, p.loss]); break;
    case "report.access": push(S.access, p.gateway, [ts, p.capacity_mbps, p.snr_db]); break;
    case "ctrl.subnet": S.subnet = p; break;
    case "ctrl.log": S.logs.push({ ts, ...p }); trim(S.logs, 200); appendCtrlLog({ ts, ...p }); renderWorkflow(); break;
    case "agent.log": {
      const list = S.agentLogs[p.role];
      if (list) { list.push({ ts, ...p }); trim(list, 80); appendAgentLog(p.role, { ts, ...p }); }
      break;
    }
    case "ops.fault": S.faults.push({ ts, ...p }); trim(S.faults, 40); break;
    case "ctrl.metrics": if (S.status) S.status.metrics[p.kind] = p; break;
  }
  if (event.caption) setCaption(event.caption);
}
const push = (store, key, row) => { (store[key] = store[key] || []).push(row); trim(store[key], 400); };
const trim = (list, n) => { if (list.length > n) list.splice(0, list.length - n); };

function setCaption(text) {  // headline (what happened) + the controller's steps as sentences
  const [head, ...steps] = String(text).split(" → ");
  if (setCaption.last === text) return;
  setCaption.last = text;
  $("caption").innerHTML = `<p class="head">${esc(sentence(head))}</p>` +
    (steps.length ? `<ol>${steps.map((s) => `<li>${esc(sentence(s))}</li>`).join("")}</ol>` : "");
}

function setStatus(st) {
  S.status = st;
  S.clockOffset = st.now - Date.now() / 1000;
  setCaption(st.caption);
  const mode = $("mode");
  mode.textContent = st.mode === "sim" ? "Simulated data plane" : "Measured on Linux network namespaces";
  mode.className = "mode " + (st.mode === "sim" ? "sim" : "measured");
  $("k-version").textContent = st.version ? "v" + st.version : "none";
  $("k-agents").textContent = `${st.online} of ${st.total}`;
  $("k-agents").style.color = st.online < st.total ? BAD : "";
  const f = st.metrics.formation, r = st.metrics.recovery;
  $("k-form").textContent = f ? (f.latency_ms / 1000).toFixed(2) + " s" : "–";
  $("k-rec").textContent = r ? (r.latency_ms / 1000).toFixed(2) + " s" : "–";
  $("k-mod").textContent = r ? Math.round(r.ratio * 100) + "%" : "–";
  renderWorkflow();
}

function connect() {
  const source = new EventSource("/api/stream");
  source.onmessage = (msg) => ingest(JSON.parse(msg.data));
  source.onerror = () => $("live").classList.remove("on");
}

/* ------------------------------------------------- Algorithm 1 progress */
function renderWorkflow() {
  const items = [...document.querySelectorAll("#workflow li")];
  const state = items.map(() => "idle");
  let start = -1;
  S.logs.forEach((e, i) => {
    if ((e.step === "formation" && e.title.startsWith("T_m")) || e.step === "event" || e.step === "withdraw") start = i;
  });
  if (start >= 0 && S.logs[start].step !== "withdraw") {
    for (const e of S.logs.slice(start)) {
      if (e.step === "formation" || e.step === "scope") state[0] = "done";
      else if (e.step === "select") {
        if (/minimize/.test(e.title)) { state[1] = state[2] = state[3] = "done"; state[4] = state[5] = "idle"; }
        else state[2] = "failed";
      } else if (e.step === "apply") state[4] = "done";
      else if (e.step === "assess") state[5] = e.level === "ok" ? "done" : "failed";
      else if (e.step === "commit" && e.level === "ok") state.fill("done");
    }
    if (S.status && S.status.reconfiguring) {
      const next = state.findIndex((s, i) => s === "idle" && state.slice(0, i).every((p) => p !== "idle"));
      if (next >= 0) state[next] = "active";
    }
  }
  items.forEach((li, i) => { li.className = state[i] === "idle" ? "" : state[i]; });
}

/* -------------------------------------------------------------- schematic */
function linkFor(a, b) { return (S.scenario?.links || []).find((l) => l.src === a && l.dst === b); }

function segment(a, b, offset, bend) {
  const [x1, y1] = a, [x2, y2] = b;
  const len = Math.hypot(x2 - x1, y2 - y1) || 1;
  const nx = -(y2 - y1) / len, ny = (x2 - x1) / len;
  const p0 = [x1 + nx * offset, y1 + ny * offset], p2 = [x2 + nx * offset, y2 + ny * offset];
  const c = [(x1 + x2) / 2 + nx * (2 * bend + offset), (y1 + y2) / 2 + ny * (2 * bend + offset)];
  const mid = [(p0[0] + 2 * c[0] + p2[0]) / 4, (p0[1] + 2 * c[1] + p2[1]) / 4];
  return { p0, c, p2, mid, n: [nx, ny] };
}

function buildTopology() {
  const svg = $("topo");
  svg.innerHTML = "";
  if (!S.scenario) return;
  const base = el("g", {}, svg), flows = el("g", {}, svg), nodes = el("g", {}, svg);
  const ctrl = el("g", { class: "ctrl-node", id: "ctrl-node" }, nodes);
  el("rect", { x: CTRL_POS[0] - 100, y: CTRL_POS[1] - 22, width: 200, height: 44, rx: 6 }, ctrl);
  icon("server-cog", CTRL_POS[0] - 74, CTRL_POS[1], 24, INK, ctrl);
  el("text", { x: CTRL_POS[0] - 54, y: CTRL_POS[1] + 5 }, ctrl).textContent = "TCANet controller";
  for (const gw of S.scenario.gateways) {
    el("line", { class: "ctrl-line", x1: CTRL_POS[0], y1: CTRL_POS[1] + 22, x2: GW_POS[gw][0], y2: GW_POS[gw][1] }, base);
  }
  for (const link of S.scenario.links) {
    const seg = segment(GW_POS[link.src], GW_POS[link.dst], 0, LINK_BEND[link.id] || 0);
    el("path", { class: "link-base", id: "lk-" + link.id, d: `M${seg.p0} Q${seg.c} ${seg.p2}` }, base);
    el("text", { class: "link-label", id: "lt-" + link.id, "text-anchor": "middle",
      x: seg.mid[0] + seg.n[0] * 18, y: seg.mid[1] + seg.n[1] * 18 + 4 }, base).textContent = link.id;
  }
  for (const ep of S.scenario.endpoints) {
    const [x, y] = EP_POS[ep.id], [gx, gy] = GW_POS[ep.gateway];
    el("line", { class: "access", x1: x, y1: y, x2: gx, y2: gy }, base);
    const g = el("g", { class: "ep", id: "ep-" + ep.id }, nodes);
    el("circle", { cx: x, cy: y, r: 27 }, g);
    icon(EP_ICON[ep.id], x, y, 30, INK, g);
    el("text", { x, y: y + (y > 380 ? -36 : 46), "text-anchor": "middle" }, g).textContent = `${ep.id} ${EP_NAME[ep.id]}`;
  }
  for (const gw of S.scenario.gateways) {
    const [x, y] = GW_POS[gw];
    const g = el("g", { class: "gw", id: "gw-" + gw }, nodes);
    el("rect", { x: x - 48, y: y - 26, width: 96, height: 52, rx: 8 }, g);
    icon("router", x - 20, y, 28, INK, g);
    el("text", { x: x + 6, y: y + 6 }, g).textContent = gw;
    ["transport", "network", "physical"].forEach((role, i) => {
      const a = el("g", { class: "ag", id: `ag-${role}-${gw}` }, g);
      el("circle", { cx: x - 22 + i * 22, cy: y + 39, r: 9, fill: LAYER[role], stroke: LAYER[role], "stroke-width": 1.5 }, a);
      el("text", { x: x - 22 + i * 22, y: y + 42.5, "text-anchor": "middle" }, a).textContent = role[0].toUpperCase();
      el("title", {}, a).textContent = `${role}-${gw}`;
    });
    const x1 = el("g", { id: "gx-" + gw, visibility: "hidden" }, g);
    el("line", { x1: x - 30, y1: y - 30, x2: x + 30, y2: y + 30, class: "x" }, x1);
    el("line", { x1: x + 30, y1: y - 30, x2: x - 30, y2: y + 30, class: "x" }, x1);
  }
  for (const dep of S.scenario.deps) el("path", { class: "flow", id: "fl-" + dep.id, stroke: DEP_SHEET[dep.id], d: "" }, flows);
  $("legend").innerHTML = S.scenario.deps.map((d) =>
    `<span><i style="background:${DEP_SHEET[d.id]}"></i>${d.id}: ${EP_NAME[d.src]} to ${EP_NAME[d.dst]}, ${d.demand} Mbps</span>`
  ).join("") + `<span>T, N, P: each gateway's Trans, Net and Phy agents (hollow red when stopped)</span>` +
    `<span><i style="background:${BAD}"></i>failed link or broken path</span>`;
  buildDag();
}

function depPath(dep, gws) {
  if (!gws.length) return "";
  const off = DEP_OFFSET[dep.id] || 0;
  let d = `M${EP_POS[dep.src]}`;
  for (let i = 0; i + 1 < gws.length; i++) {
    const link = linkFor(gws[i], gws[i + 1]);
    const seg = segment(GW_POS[gws[i]], GW_POS[gws[i + 1]], off, link ? (LINK_BEND[link.id] || 0) : 0);
    d += ` L${seg.p0} Q${seg.c} ${seg.p2}`;
  }
  if (gws.length === 1) d += ` L${GW_POS[gws[0]]}`;
  return d + ` L${EP_POS[dep.dst]}`;
}

function renderTopology() {
  if (!S.scenario || !S.status) return;
  const st = S.status, failed = new Set(st.failed_gateways || []), affected = new Set(st.affected || []);
  for (const link of S.scenario.links) {
    const info = st.links[link.id] || { up: true, util: 0 };
    const down = !info.up || failed.has(link.src) || failed.has(link.dst);
    $("lk-" + link.id).setAttribute("class", "link-base" + (down ? " down" : ""));
    const label = $("lt-" + link.id);
    label.setAttribute("class", "link-label" + (down ? " down" : ""));
    label.textContent = down ? `${link.id} down` : `${link.id} ${Math.round(info.util * 100)}%`;
  }
  for (const gw of S.scenario.gateways) {
    $("gw-" + gw).setAttribute("class", "gw" + (failed.has(gw) ? " failed" : ""));
    $("gx-" + gw).setAttribute("visibility", failed.has(gw) ? "visible" : "hidden");
    for (const role of ["transport", "network", "physical"]) {
      const alive = st.agents[`${role}-${gw}`], badge = $(`ag-${role}-${gw}`);
      badge.setAttribute("class", "ag" + (alive ? "" : " dead"));
      const c = badge.querySelector("circle");
      c.setAttribute("fill", alive ? LAYER[role] : "#fff");
      c.setAttribute("stroke", alive ? LAYER[role] : BAD);
    }
  }
  const down = new Set(Object.entries(st.links).filter(([, v]) => !v.up).map(([k]) => k));
  for (const dep of S.scenario.deps) {
    const gws = (S.subnet.paths || {})[dep.id] || [];
    const path = $("fl-" + dep.id);
    path.setAttribute("d", st.version ? depPath(dep, gws) : "");
    const broken = gws.some((g) => failed.has(g)) ||
      gws.slice(0, -1).some((g, i) => { const l = linkFor(g, gws[i + 1]); return l && down.has(l.id); });
    path.setAttribute("class", "flow" + (broken ? " broken" : "") + (affected.has(dep.id) && !broken ? " affected" : ""));
  }
}

/* ------------------------------------------------- task graph (screen) */
const DAG_POS = { a1: [70, 60], a2: [70, 190], a3: [275, 125], a4: [455, 125] };
function buildDag() {
  const svg = $("dag");
  svg.innerHTML = "";
  el("defs", {}, svg).innerHTML =
    `<marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${SCR.muted}"/></marker>`;
  for (const dep of S.scenario.deps) {
    const [x1, y1] = DAG_POS[dep.src], [x2, y2] = DAG_POS[dep.dst];
    const len = Math.hypot(x2 - x1, y2 - y1), ux = (x2 - x1) / len, uy = (y2 - y1) / len;
    el("line", { id: "de-" + dep.id, x1: x1 + ux * 32, y1: y1 + uy * 32, x2: x2 - ux * 34, y2: y2 - uy * 34,
      stroke: SCR.muted, "stroke-width": 3, "marker-end": "url(#arr)" }, svg);
    el("text", { id: "dl-" + dep.id, x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 10, "text-anchor": "middle",
      fill: SCR.ink, "font-size": 13, "font-family": "Avenir Next, Ubuntu, sans-serif" }, svg).textContent = dep.id;
  }
  for (const ep of S.scenario.endpoints) {
    const [x, y] = DAG_POS[ep.id];
    el("circle", { cx: x, cy: y, r: 28, fill: "#3d0a2d", stroke: SCR.muted, "stroke-width": 1.5 }, svg);
    icon(EP_ICON[ep.id], x, y, 28, SCR.ink, svg);
    el("text", { x, y: y + 46, "text-anchor": "middle", fill: SCR.muted, "font-size": 13,
      "font-family": "Avenir Next, Ubuntu, sans-serif" }, svg).textContent = `${ep.id} ${EP_NAME[ep.id]}`;
  }
}

function renderDag() {
  if (!S.scenario || !S.status) return;
  const st = S.status, affected = new Set(st.affected || []);
  for (const dep of S.scenario.deps) {
    const q = st.qos[dep.id], color = q === "ok" ? SCR.ok : q === "violated" ? SCR.bad : SCR.muted;
    const line = $("de-" + dep.id);
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-dasharray", affected.has(dep.id) ? "6 5" : "");
    const d = st.demand[dep.id], rate = d ? d.rate : dep.demand;
    const mark = q === "ok" ? "meets QoS" : q === "violated" ? "violates QoS" : "idle";
    const label = $("dl-" + dep.id);
    label.textContent = `${dep.id} ${rate} Mbps, ${mark}`;
    label.setAttribute("fill", q === "inactive" ? SCR.muted : color);
  }
  if (S.view !== "app") return;
  const qos = S.scenario.qos;
  const rows = S.scenario.deps.map((dep) => {
    const latest = st.latest[dep.id] || {}, q = st.qos[dep.id];
    const path = ((S.subnet.paths || {})[dep.id] || []).join("–") || "none";
    const phi = ((S.subnet.bindings || {})[dep.id] || []).map((a) => (a ? a.replace(/^(\w)\w*-/, "$1@") : "none")).join(", ") || "none";
    const meas = latest.rx === undefined ? "no samples" :
      `${latest.rx.toFixed(1)} Mbps, ${latest.owd == null ? "no delay sample" : latest.owd.toFixed(1) + " ms"}, ${latest.loss == null ? "–" : (latest.loss * 100).toFixed(1) + "% loss"}`;
    const color = q === "ok" ? SCR.ok : q === "violated" ? SCR.bad : SCR.muted;
    const word = q === "ok" ? "met" : q === "violated" ? "violated" : "idle";
    return `<tr><td><span class="dot" style="background:${DEP_SCREEN[dep.id]}"></span>${dep.id}</td><td>${path}</td><td>${phi}</td><td>${meas}</td><td style="color:${color}">${word}</td></tr>`;
  });
  $("dep-table").innerHTML = `<tr><th>Dependency</th><th>Gateway path</th><th>Bound agents (T, N, P)</th><th>Measured now</th>` +
    `<th>Hard QoS (≥ ${qos.min_mbps} Mbps, ≤ ${qos.max_delay_ms} ms, ≤ ${qos.max_loss * 100}% loss)</th></tr>` + rows.join("");
}

/* ----------------------------------------------------- charts (screen) */
function markers() {
  const out = [];
  for (const f of S.faults) {
    const label = { gateway: `${f.target} ${f.up ? "up" : "down"}`, link: `${f.target} ${f.up ? "up" : "down"}`,
      degrade: `${f.target} ${f.loss_pct}% loss`, kill: `${f.target} stopped`, demand: `${f.dep_id} to ${f.mbps} Mbps` }[f.op];
    if (label) out.push({ t: f.ts, label, color: SCR.muted });
  }
  for (const e of S.logs) {
    if (e.step === "event") out.push({ t: e.ts, label: "detected", color: SCR.bad });
    else if (e.step === "rollback" && e.title.startsWith("Rollback")) out.push({ t: e.ts, label: "rollback", color: SCR.warn });
    else if (e.step === "commit" && e.level === "ok") out.push({ t: e.ts, label: e.title.replace("Commit ", ""), color: SCR.ok });
  }
  return out;
}

function fitCanvas(canvas) {
  const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  if (r.width < 10 || r.height < 10) return null;
  const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: r.width, h: r.height };
}

function drawChart(canvas, opts) {
  const fit = fitCanvas(canvas);
  if (!fit) return;
  const { ctx, w, h } = fit, win = opts.window || 60, t1 = now(), t0 = t1 - win;
  const m = { l: 34, r: 74, t: 20, b: 18 }, pw = w - m.l - m.r, ph = h - m.t - m.b;
  ctx.clearRect(0, 0, w, h);
  let ymax = opts.yMin || 1;
  for (const s of opts.series) for (const [t, v] of s.pts) if (t >= t0 && v != null && isFinite(v)) ymax = Math.max(ymax, v);
  ymax = niceMax(ymax * 1.12);
  const X = (t) => m.l + ((t - t0) / win) * pw, Y = (v) => m.t + ph - (v / ymax) * ph;
  ctx.font = "11px Avenir Next, Ubuntu, sans-serif";
  ctx.fillStyle = SCR.muted;
  ctx.fillText(opts.title, m.l, 12);
  ctx.strokeStyle = SCR.rule; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = (ymax / 4) * i, y = Y(v);
    ctx.beginPath(); ctx.moveTo(m.l, y); ctx.lineTo(m.l + pw, y); ctx.stroke();
    ctx.fillText(fmt(v), 2, y + 4);
  }
  for (let s = 0; s <= win; s += win / 4) ctx.fillText(s === win ? "now" : `−${win - s} s`, X(t0 + s) - 12, h - 4);
  let row = 0;
  for (const mk of opts.markers || []) {
    if (mk.t < t0) continue;
    const x = X(mk.t);
    ctx.strokeStyle = mk.color; ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(x, m.t); ctx.lineTo(x, m.t + ph); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = mk.color; ctx.fillText(mk.label, x + 3, m.t + 10 + (row++ % 3) * 12);
  }
  const ends = [];
  for (const s of opts.series) {
    ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.beginPath();
    let pen = false, prev = null, last = null;
    for (const [t, v] of s.pts) {
      if (t < t0 - 2) continue;
      const gap = prev !== null && t - prev > (opts.gap || 1.6);
      if (v == null || !isFinite(v) || gap) pen = false;
      if (v != null && isFinite(v)) {
        if (pen) ctx.lineTo(X(t), Y(v)); else ctx.moveTo(X(t), Y(v));
        pen = true; last = [t, v];
      }
      prev = t;
    }
    ctx.stroke();
    if (last) ends.push({ s, v: last[1], y: Math.min(Math.max(Y(last[1]), m.t + 6), m.t + ph) });
  }
  // direct labels at the line ends (colored key, light text), spread so they never overlap
  ends.sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 13);
  const overflow = ends.length ? ends[ends.length - 1].y - (m.t + ph + 4) : 0;
  for (const e of ends) {
    const y = overflow > 0 ? e.y - overflow : e.y;
    ctx.fillStyle = e.s.color; ctx.fillRect(m.l + pw + 6, y - 4, 8, 8);
    ctx.fillStyle = SCR.ink; ctx.fillText(`${e.s.label} ${fmt(e.v)}`, m.l + pw + 18, y + 4);
  }
}
const niceMax = (v) => { const p = Math.pow(10, Math.floor(Math.log10(v))); return Math.ceil((v / p) * 2) / 2 * p; };
const fmt = (v) => (Math.abs(v) >= 10 ? v.toFixed(0) : v.toFixed(1));

function renderCharts() {
  const mk = markers(), deps = S.scenario ? S.scenario.deps.map((d) => d.id) : [];
  const flow = (i) => deps.map((d) => ({ label: d, color: DEP_SCREEN[d], pts: (S.flows[d] || []).map((r) => [r[0], r[i]]) }));
  drawChart($("chart-rx"), { title: "Goodput per dependency, Mbps", series: flow(1), markers: mk, yMin: 20 });
  const gws = S.scenario ? S.scenario.gateways : [];
  const acc = (i) => gws.map((g) => ({ label: g, color: GW_SCREEN[g], pts: (S.access[g] || []).map((r) => [r[0], r[i]]) }));
  drawChart($("chart-cap"), { title: "Access capacity per gateway, Mbps", series: acc(1), yMin: 60, gap: 3 });
  if (S.view === "net") {
    drawChart($("chart-owd"), { title: "One-way delay per dependency, ms", series: flow(2), markers: mk, yMin: 40 });
    renderLinkBars();
  }
  if (S.view === "phy") drawChart($("chart-snr"), { title: "Radio SNR per gateway, dB", series: acc(2), yMin: 25, gap: 3 });
}

function renderLinkBars() {
  if (!S.status) return;
  $("link-bars").innerHTML = "<h5>Gateway link utilization, reported by NetAgents</h5>" +
    Object.entries(S.status.links).map(([id, v]) => v.up
      ? `<span>${id}</span><div class="track"><div class="fill" style="width:${Math.min(100, v.util * 100)}%"></div></div><span>${Math.round(v.util * 100)}%</span>`
      : `<span>${id}</span><div class="track"></div><span class="down">down</span>`).join("");
}

/* ------------------------------------------------------------------- logs */
function renderCtrlLog() {
  const box = $("ctrl-log");
  box.innerHTML = "";
  for (const e of S.logs.slice(-150)) appendCtrlLog(e, true);
  box.scrollTop = box.scrollHeight;
}
function appendCtrlLog(e, bulk) {
  const box = $("ctrl-log");
  const div = document.createElement("div");
  div.className = `e lv-${e.level} ${e.step}`;
  div.innerHTML = `<span class="step st-${e.step}">${esc(e.step)}</span><span class="t">${esc(e.title)}</span>` +
    (e.lines && e.lines.length ? `<div class="lines">${e.lines.map(esc).join("\n")}</div>` : "");
  box.appendChild(div);
  while (box.childElementCount > 160) box.removeChild(box.firstChild);
  if (!bulk) box.scrollTop = box.scrollHeight;
}
const LAYER_TITLE = { application: "AppAgents", transport: "TransAgents", network: "NetAgents", physical: "PhyAgents" };
function renderAgentLog(layer) {
  const box = $("log-" + layer);
  if (!box) return;
  box.innerHTML = `<h5>${LAYER_TITLE[layer]} log</h5>`;
  for (const e of S.agentLogs[layer] || []) appendAgentLog(layer, e);
}
function appendAgentLog(layer, e) {
  const box = $("log-" + layer);
  if (!box) return;
  const div = document.createElement("div");
  div.className = e.level || "";
  div.innerHTML = `<span class="who">${esc(e.agent_id)}</span> ${esc(e.text)}`;
  box.appendChild(div);
  while (box.childElementCount > 90) box.removeChild(box.children[1]);
  box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------- views & leaders */
const PANELS = { controller: "p-controller", app: "p-app", net: "p-net", phy: "p-phy" };
function setView(view) {
  S.view = view;
  $("stage").classList.toggle("zoom", !!view);
  let t = 1;
  for (const [name, id] of Object.entries(PANELS)) {
    const p = $(id);
    p.classList.toggle("focus", name === view);
    p.style.gridArea = view ? (name === view ? "focus" : "t" + t++) : "";
  }
  requestAnimationFrame(() => { renderAll(); drawConnectors(); });
}

function anchor(node, stageRect, edge) {
  const r = node.getBoundingClientRect();
  const y = edge === "bottom" ? r.bottom : r.top - 2;
  return [r.left + r.width / 2 - stageRect.left, y - stageRect.top];
}
function drawConnectors() {
  const svg = $("connectors");
  svg.innerHTML = "";
  if (S.view || !S.scenario) return;
  const sr = $("stage").getBoundingClientRect();
  const targets = [["p-controller", "ctrl-node"], ["p-app", "ep-a3"], ["p-net", "gw-G2"], ["p-phy", "gw-G4"]];
  for (const [from, to] of targets) {
    const a = $(from).querySelector(".label"), b = $(to);
    if (!a || !b) continue;
    const [x1, y1] = anchor(a, sr, "bottom"), [x2, y2] = anchor(b, sr, "top");
    const my = (y1 + y2) / 2;
    el("path", { d: `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}` }, svg);
    el("circle", { cx: x2, cy: y2, r: 3 }, svg);
  }
}

/* ---------------------------------------------------------------- actions */
const ACTIONS = {
  submit: { op: "submit" },
  demand: { op: "demand", dep: "e1", mbps: 25 },
  "fail-g2": { op: "fail_gateway", gateway: "G2" },
  rollback: { op: "hidden_loss_then_fail", link: "L3", gateway: "G2" },
  "kill-phy": { op: "kill", agent: "physical-G4" },
  reset: { op: "reset" },
};
async function act(button) {
  const key = button.dataset.op;
  if (key === "verify") return openVerify();
  if (key === "tour") return toggleTour();
  button.classList.add("busy");
  try {
    const res = await fetch("/api/action", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ACTIONS[key]) });
    const body = await res.json();
    toast(body.ok ? `Sent: ${button.textContent.trim()}` : `Not sent: ${body.error}`, !body.ok);
  } catch (err) {
    toast(`Not sent: the testbed did not answer (${err.message})`, true);
  } finally {
    button.classList.remove("busy");
  }
}
function toast(text, err) {
  const t = $("toast");
  t.textContent = text;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.className = ""), 2600);
}
function toggleTour() {
  const btn = $("btn-tour");
  if (S.tour) { clearInterval(S.tour); S.tour = null; btn.setAttribute("aria-pressed", "false"); return; }
  const order = ["controller", "app", "net", "phy", null];
  let i = order.indexOf(S.view);
  btn.setAttribute("aria-pressed", "true");
  S.tour = setInterval(() => { i = (i + 1) % order.length; setView(order[i]); }, 8000);
}

async function openVerify() {
  $("verify").hidden = false;
  const gws = S.scenario ? S.scenario.gateways : ["G1", "G2", "G3", "G4"];
  $("v-tabs").innerHTML = gws.map((g) => `<button class="${g === S.verifyGw ? "on" : ""}" data-gw="${g}">${g}</button>`).join(" ");
  const v = await (await fetch(`/api/verify?gateway=${S.verifyGw}`)).json();
  $("v-expected").textContent = v.expected.length
    ? `# subnet v${v.version}, forwarding entries at ${v.gateway}\n` + v.expected.join("\n")
    : `No forwarding entries at ${v.gateway} in subnet ${v.version ? "v" + v.version : "(not formed)"}.`;
  if (v.kernel) {
    $("v-kernel-title").textContent = `Linux kernel on ${v.gateway}, read live`;
    $("v-kernel").textContent = Object.entries(v.kernel).map(([cmd, out]) => `$ ${cmd}\n${out || "(empty)"}`).join("\n\n");
  } else {
    $("v-kernel-title").textContent = "Linux kernel";
    $("v-kernel").textContent = "This run uses the simulated data plane, so there is no kernel state to read.\nStart the testbed on the Ubuntu host to see live ip rule and ip route output here.";
  }
}

/* ------------------------------------------------------------------- main */
function renderAll() {
  $("live").classList.toggle("on", Date.now() - S.lastEvent < 3000);
  renderTopology();
  renderDag();
  renderCharts();
}

function init() {
  for (const [name, id] of Object.entries(PANELS)) {
    const p = $(id);
    p.addEventListener("click", () => { if (S.view !== name) setView(name); });
    p.addEventListener("keydown", (e) => { if (e.key === "Enter" && S.view !== name) setView(name); });
  }
  document.querySelectorAll("#ops button").forEach((b) => b.addEventListener("click", () => act(b)));
  $("v-close").addEventListener("click", () => ($("verify").hidden = true));
  $("v-tabs").addEventListener("click", (e) => { if (e.target.dataset.gw) { S.verifyGw = e.target.dataset.gw; openVerify(); } });
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    const k = e.key.toLowerCase();
    if (k === "1") setView("controller"); else if (k === "2") setView("app");
    else if (k === "3") setView("net"); else if (k === "4") setView("phy");
    else if (k === "0" || k === "escape") { $("verify").hidden = true; setView(null); }
    else if (k === "h") { document.body.classList.toggle("bar-hidden"); requestAnimationFrame(drawConnectors); }
    else if (k === "t") toggleTour();
  });
  window.addEventListener("resize", () => { renderAll(); drawConnectors(); });
  connect();
  const start = location.hash.slice(1);  // e.g. /#net opens that agent's screen
  if (PANELS[start]) setTimeout(() => setView(start), 0);
  setInterval(renderAll, 300);
  setInterval(drawConnectors, 1000);
}

document.addEventListener("DOMContentLoaded", init);
