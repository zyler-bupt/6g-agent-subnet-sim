"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
const state = { scenario: "tier1", trace: null, step: 0, nodes: {}, edges: {}, playing: false, timer: null };

const el = (id) => document.getElementById(id);

function nodeBox(kind) {
  if (kind === "controller") return { w: 360, h: 46 };
  if (kind === "gateway") return { w: 150, h: 34 };
  return { w: 104, h: 34 };
}

// point on a node's rectangle border, heading toward (tx, ty)
function borderPoint(node, tx, ty) {
  const { w, h } = nodeBox(node.kind);
  const dx = tx - node.x, dy = ty - node.y;
  if (dx === 0 && dy === 0) return { x: node.x, y: node.y };
  const sx = dx !== 0 ? (w / 2) / Math.abs(dx) : Infinity;
  const sy = dy !== 0 ? (h / 2) / Math.abs(dy) : Infinity;
  const s = Math.min(sx, sy);
  return { x: node.x + dx * s, y: node.y + dy * s };
}

function renderGraph(trace) {
  const edgeLayer = el("edge-layer");
  const nodeLayer = el("node-layer");
  edgeLayer.innerHTML = "";
  nodeLayer.innerHTML = "";
  state.nodes = {};
  state.edges = {};
  const byId = {};
  trace.nodes.forEach((n) => (byId[n.id] = n));

  // edges first (so nodes render on top)
  trace.edges.forEach((e) => {
    const a = byId[e.source], b = byId[e.target];
    if (!a || !b) return;
    const p1 = borderPoint(a, b.x, b.y);
    const p2 = borderPoint(b, a.x, a.y);
    const path = document.createElementNS(SVGNS, "path");
    let d;
    if (e.kind === "biz") {
      const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2 - 46; // bow upward
      d = `M ${p1.x} ${p1.y} Q ${mx} ${my} ${p2.x} ${p2.y}`;
    } else {
      d = `M ${p1.x} ${p1.y} L ${p2.x} ${p2.y}`;
    }
    path.setAttribute("d", d);
    path.setAttribute("class", "edge " + e.kind);
    path.dataset.kind = e.kind;
    edgeLayer.appendChild(path);
    state.edges[e.id] = path;
  });

  // nodes
  trace.nodes.forEach((n) => {
    const { w, h } = nodeBox(n.kind);
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", "node " + n.kind + (n.standby ? " standby-node" : ""));
    g.dataset.id = n.id;
    const rect = document.createElementNS(SVGNS, "rect");
    rect.setAttribute("x", n.x - w / 2);
    rect.setAttribute("y", n.y - h / 2);
    rect.setAttribute("width", w);
    rect.setAttribute("height", h);
    rect.setAttribute("rx", 8);
    g.appendChild(rect);
    const text = document.createElementNS(SVGNS, "text");
    text.setAttribute("x", n.x);
    text.setAttribute("y", n.y + 1);
    text.textContent = n.short || n.label;
    g.appendChild(text);
    const title = document.createElementNS(SVGNS, "title");
    title.textContent = n.label + "  [" + n.id + "]";
    g.appendChild(title);
    nodeLayer.appendChild(g);
    state.nodes[n.id] = g;
  });
}

function setClasses(map, baseGetter, id, extras) {
  const elem = map[id];
  if (!elem) return;
  elem.setAttribute("class", baseGetter(elem) + extras);
}

function applyStep(i) {
  const trace = state.trace;
  if (!trace) return;
  state.step = Math.max(0, Math.min(i, trace.steps.length - 1));
  const s = trace.steps[state.step];

  const active = new Set(s.active_nodes);
  const focus = new Set(s.focus_nodes);
  const failed = new Set(s.failed_nodes);
  const changed = new Set(s.changed_nodes);
  const aEdges = new Set(s.active_edges);
  const fEdges = new Set(s.focus_edges);
  const cEdges = new Set(s.changed_edges);

  // nodes
  trace.nodes.forEach((n) => {
    const g = state.nodes[n.id];
    let cls = "node " + n.kind;
    if (n.standby) cls += " standby";
    if (active.has(n.id)) cls += " active";
    if (focus.has(n.id)) cls += " focus";
    if (failed.has(n.id)) cls += " failed";
    if (changed.has(n.id)) cls += " changed";
    g.setAttribute("class", cls);
  });

  // edges
  trace.edges.forEach((e) => {
    const p = state.edges[e.id];
    if (!p) return;
    let cls = "edge " + e.kind;
    let marker = "url(#arrow)";
    if (aEdges.has(e.id)) { cls += " active"; marker = "url(#arrow-active)"; }
    if (fEdges.has(e.id)) cls += " focus";
    if (cEdges.has(e.id)) { cls += " changed"; marker = "url(#arrow-changed)"; }
    p.setAttribute("class", cls);
    p.setAttribute("marker-end", marker);
  });

  // sidebar
  el("step-counter").textContent = `${state.step + 1} / ${trace.steps.length}`;
  el("step-kind").textContent = s.kind;
  el("step-title").textContent = s.title;
  el("step-detail").textContent = s.detail;

  // risk
  const riskWrap = el("risk-wrap");
  if (s.risk === null || s.risk === undefined) {
    riskWrap.hidden = true;
  } else {
    riskWrap.hidden = false;
    const thr = s.threshold || trace.threshold || 1.0;
    const scaleMax = thr * 2;
    const pct = Math.max(0, Math.min(s.risk / scaleMax, 1)) * 100;
    const fill = el("risk-fill");
    fill.style.width = pct + "%";
    fill.classList.toggle("over", s.risk > thr);
    el("risk-tau").style.left = "50%";
    el("risk-val").textContent = s.risk.toFixed(3);
    el("risk-thr").textContent = thr.toFixed(2);
  }

  // badges
  const badges = el("badges");
  badges.innerHTML = "";
  (s.badges || []).forEach(([label, value]) => {
    const b = document.createElement("div");
    b.className = "badge";
    b.innerHTML = `<b>${label}</b>${value}`;
    badges.appendChild(b);
  });

  // compare
  const cmp = el("compare");
  if (s.compare) {
    const c = s.compare;
    cmp.hidden = false;
    cmp.innerHTML = `
      <table>
        <tr><th>指标</th><th>最小调整</th><th>全量重建</th></tr>
        <tr><td>策略</td><td class="min">${c.minimal.strategy}</td><td class="full">${c.full.strategy}</td></tr>
        <tr><td>变更 A/E/GW</td><td class="min">${c.minimal.changed}</td><td class="full">${c.full.changed}</td></tr>
        <tr><td>业务中断</td><td class="min">${c.minimal.interruption} ms</td><td class="full">${c.full.interruption} ms</td></tr>
        <tr><td>QoS</td><td class="min">${c.minimal.qos ? "满足 ✓" : "✗"}</td><td class="full">${c.full.qos ? "满足 ✓" : "✗"}</td></tr>
        <tr><td>中断降幅</td><td class="win" colspan="2">省 ${c.saved_ms} ms （-${c.drop_pct}%）</td></tr>
      </table>`;
  } else {
    cmp.hidden = true;
  }
}

function stopPlay() {
  state.playing = false;
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  el("btn-play").textContent = "⏵ 播放";
}

function togglePlay() {
  if (state.playing) { stopPlay(); return; }
  if (state.step >= state.trace.steps.length - 1) applyStep(0);
  state.playing = true;
  el("btn-play").textContent = "⏸ 暂停";
  state.timer = setInterval(() => {
    if (state.step >= state.trace.steps.length - 1) { stopPlay(); return; }
    applyStep(state.step + 1);
  }, 1600);
}

async function loadTrace(key) {
  stopPlay();
  state.scenario = key;
  const res = await fetch(`/api/trace?scenario=${encodeURIComponent(key)}`);
  state.trace = await res.json();
  renderGraph(state.trace);
  applyStep(0);
  document.querySelectorAll(".scenarios button").forEach((b) =>
    b.classList.toggle("on", b.dataset.key === key));
}

async function loadScenarios() {
  const res = await fetch("/api/scenarios");
  const list = await res.json();
  const wrap = el("scenarios");
  wrap.innerHTML = "";
  list.forEach((sc) => {
    const b = document.createElement("button");
    b.dataset.key = sc.key;
    b.textContent = `${sc.key.toUpperCase()} · ${sc.name}`;
    b.title = sc.trigger + " → " + sc.expect;
    b.onclick = () => loadTrace(sc.key);
    wrap.appendChild(b);
  });
}

el("btn-next").onclick = () => { stopPlay(); applyStep(state.step + 1); };
el("btn-prev").onclick = () => { stopPlay(); applyStep(state.step - 1); };
el("btn-reset").onclick = () => { stopPlay(); applyStep(0); };
el("btn-play").onclick = togglePlay;
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") { stopPlay(); applyStep(state.step + 1); }
  if (e.key === "ArrowLeft") { stopPlay(); applyStep(state.step - 1); }
  if (e.key === " ") { e.preventDefault(); togglePlay(); }
});

(async function init() {
  await loadScenarios();
  await loadTrace("tier1");
})();
