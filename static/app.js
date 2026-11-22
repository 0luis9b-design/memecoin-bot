"use strict";
// Memecoin-Bot Dashboard – alles Spielgeld. Coin-Namen kommen von fremden Quellen und werden immer escaped.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (v, d = 2) => (v == null || isNaN(v) ? "–" : (v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d }));
const sgn = (v, d = 2) => (v == null ? "–" : (v >= 0 ? "+" : "−") + "$" + Math.abs(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (v, d = 1) => (v == null || isNaN(v) ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(d) + " %");
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const big = (v) => (v >= 1e6 ? (v / 1e6).toFixed(2) + " Mio" : v >= 1e3 ? (v / 1e3).toFixed(1) + "k" : Math.round(v || 0));
const tm = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) : "–");
const dt = (ts) => (ts ? new Date(ts * 1000).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "–");
const ago = (ts) => { if (!ts) return "–"; const s = Date.now() / 1000 - ts; return s < 60 ? Math.round(s) + " s" : s < 3600 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h"; };
const price = (p) => (!p ? "–" : p >= 1 ? p.toFixed(4) : p.toPrecision(4));
const short = (a) => (a ? esc(a.slice(0, 4) + "…" + a.slice(-4)) : "");
const AGENT_COLORS = { launchfeed: "#ff9f43", shadow: "#8695a3", scanner: "#4cc3ff", rugcheck: "#ff4d5e", analyst: "#a78bfa", sizer: "#f5c84c", executor: "#22d36b", risk: "#ff9f43", logger: "#e6edf3", scout: "#4cc3ff", copyfeed: "#22d36b", learning: "#f5c84c" };

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts, headers: { "X-Requested-With": "memebot", "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

let tab = "overview", eqHours = 168, accounts = [], timer = null;

// ------------------------------------------------------------------ Tabs
$$("#tabs button").forEach((b) => b.addEventListener("click", () => {
  tab = b.dataset.tab;
  $$("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + tab));
  refresh();
}));
$$("#eqRange button").forEach((b) => b.addEventListener("click", () => {
  eqHours = +b.dataset.h;
  $$("#eqRange button").forEach((x) => x.classList.toggle("on", x === b));
  loadEquity();
}));
$("#eqTests").addEventListener("change", loadEquity);
let shDays = 7;
$$("#shDays button").forEach((b) => b.addEventListener("click", () => {
  shDays = +b.dataset.d;
  $$("#shDays button").forEach((x) => x.classList.toggle("on", x === b));
  loadShadow();
}));
const fmtVal = (v) => (v !== null && typeof v === "object" ? Object.entries(v).map(([k, x]) => `${k} ${x}`).join(", ") : v);
["#trAcc", "#trStatus"].forEach((s) => $(s).addEventListener("change", loadTrades));
["#lgAgent", "#lgAction"].forEach((s) => $(s).addEventListener("change", loadLog));
let qT; $("#lgQ").addEventListener("input", () => { clearTimeout(qT); qT = setTimeout(loadLog, 300); });
$("#modalClose").addEventListener("click", () => $("#modal").classList.add("hide"));
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("#modal").classList.add("hide"); });
document.addEventListener("click", (e) => {
  const tr = e.target.closest("[data-pos]");
  if (tr) openTrade(+tr.dataset.pos);
  const pb = e.target.closest("[data-prop]");
  if (pb) decideProposal(+pb.dataset.prop, pb.dataset.accept === "1");
});

// ------------------------------------------------------------------ Übersicht
async function loadOverview() {
  const o = await api("/api/overview");
  accounts = o.accounts;
  const age = o.state.last_cycle ? Date.now() / 1000 - o.state.last_cycle : null;
  $("#live").className = "dot " + (age != null && age < 60 ? "ok" : "bad");
  const up = (Date.now() / 1000 - o.state.started) / 3600;
  $("#status").textContent = `läuft seit ${up.toFixed(1)} h · Takt ${age != null ? Math.round(age) + " s" : "–"} · SOL ${usd(o.sol_usd, 0)} · Telegram: ${o.telegram}` + (o.auth ? "" : " · ⚠ ohne Login");
  const pb = $("#propBadge"); pb.textContent = o.proposals; pb.classList.toggle("hide", !o.proposals);

  const t = o.ticker.map((x) => `<span>$${esc(x.symbol)} <b class="${cls(x.ch_m5)}">${pct(x.ch_m5, 1)}</b></span>`).join("");
  $("#ticker .track").innerHTML = t ? t + t : `<span class="muted">noch keine heißen Coins</span>`;

  $("#accCards").innerHTML = o.accounts.filter((a) => a.kind === "main").map((a) => `
    <div class="card acc">
      <div class="row between"><h2>${esc(a.name)}</h2><span class="small muted">Start ${usd(a.start, 0)}</span></div>
      <div class="big ${a.pnl > 0 ? "pos" : ""}">${usd(a.equity)}</div>
      <div class="chg ${cls(a.pnl)}">${sgn(a.pnl)} (${pct(a.pnl_pct)}) <span class="mult ${a.multiple < 1 ? "neg" : ""}">${a.multiple.toFixed(2)}x</span></div>
      <div class="stats">
        <div><b>W/L</b>${a.wins}/${a.losses}</div><div><b>Treffer</b>${a.winrate ?? "–"} %</div><div><b>Trades</b>${a.trades}</div>
        <div><b>offen</b>${a.open}</div><div><b>Max DD</b>−${a.max_drawdown} %</div><div><b>PF</b>${a.profit_factor ?? "–"}</div>
        <div><b>Bester</b>${a.best ? "$" + esc(a.best.symbol) + " " + sgn(a.best.pnl) : "–"}</div>
      </div>
    </div>`).join("");

  const R = o.regime;
  $("#heatBox").innerHTML = R.heat == null ? `<div class="empty">wird in 5 min gemessen</div>` :
    `<div class="heat ${esc(R.label)}">${Math.round(R.heat)}</div><div><b>${esc(R.label)}</b></div>
     <div class="small muted">${Object.entries(R.parts || {}).map(([k, v]) => `${esc(k.replace(/_/g, " "))}: ${v.wert ?? "–"}`).join("<br>")}</div>`;
  $("#pipeline").innerHTML = o.agents.map((g) => `
    <div class="agent" style="--c:${AGENT_COLORS[g.id] || "#4cc3ff"}">
      <div class="n">${esc(g.name)}</div><div class="v">${(g.wert ?? 0).toLocaleString("de-DE")}</div>
      <div class="z">${esc(g.einheit)} · ${esc(g.zeile)}</div>${g.fehler ? `<div class="err" title="${esc(g.fehler)}">${esc(g.fehler)}</div>` : ""}
    </div>`).join("");
  $("#skips").textContent = o.skips.length ? "Häufigste Ausschlussgründe (Hauptkonto Scanner): " + o.skips.map(([k, n]) => `${k} ${n}×`).join(" · ") : "";

  $("#openPos").innerHTML = o.open.length ? `<div class="tbl"><table><tr><th>Coin</th><th>Konto</th><th>Einsatz</th><th>Wert</th><th>G/V</th><th>seit</th></tr>` +
    o.open.map((p) => `<tr class="click" data-pos="${p.id}"><td><b>$${esc(p.symbol)}</b> <span class="tag ${p.status}">${p.status === "pending" ? "Order" : "offen"}</span></td>
      <td class="small">${esc(p.konto)}</td><td>${usd(p.cost)}</td><td>${p.status === "open" ? usd(p.value + p.proceeds) : "–"}</td>
      <td class="${cls(p.pnl)}">${p.status === "open" ? pct(p.pnl_pct, 0) : "–"}</td><td class="small muted">${ago(p.opened_at || p.created)}</td></tr>`).join("") + `</table></div>`
    : `<div class="empty">Keine offenen Positionen.</div>`;

  $("#why").innerHTML = o.why ? whyHtml(o.why) : `<div class="empty">Noch kein abgeschlossener Trade im Hauptkonto.</div>`;

  $("#activity").innerHTML = o.activity.length ? `<div class="feed">` + o.activity.map((a) => `
    <div class="it"><div class="t">${tm(a.ts)}</div><div><span class="tag ${a.action}">${a.action}</span> <b>$${esc(a.symbol || "")}</b>
      <span class="small muted">${esc(a.konto || "")}${a.kind === "test" ? " (Test)" : ""}</span><div class="r">${esc(a.reason)}</div></div></div>`).join("") + `</div>`
    : `<div class="empty">Noch keine Trades in den Hauptkonten – der Bot sucht. Trades der Testkonten stehen unter „Trades“.</div>`;
  if (!$("#trAcc").options.length) fillAccSelect();
}

function whyHtml(w) {
  const s = w.signal || {};
  const v = s.werte || {};
  const checks = (s.checks || []).map(([n, ok, val]) => `<span class="${ok ? "" : "f"}">${ok ? "✓" : "✗"} ${esc(n)} (${esc(val)})</span>`).join("");
  return `<div class="row between"><div style="font-size:22px;font-weight:800">$${esc(w.symbol)}</div><div class="${cls(w.pnl)}" style="font:700 20px var(--mono)">${sgn(w.pnl)}</div></div>
    <div class="small muted">${esc(w.konto)} · gehalten ${w.opened_at && w.closed_at ? Math.round((w.closed_at - w.opened_at) / 60) + " min" : "–"} · Punktzahl ${Math.round(w.score || 0)}</div>
    <div class="kv" style="margin-top:8px">
      <div>Auslöser</div><div>${esc(w.reason_in)}</div>
      <div>Werte</div><div class="mono small">MC ${big(v.mcap)} $ · Liq ${big(v.liq || v.reserve * 2)} $ · Alter ${Math.round(v.age_min || 0)} min</div>
      <div>Einstieg → Ausstieg</div><div class="mono small">${price(w.entry)} → ${price(w.last)} (${pct(w.pnl_pct, 0)})</div>
      <div>Ausstieg</div><div>${esc(w.reason_out)}</div>
    </div>
    <div class="checks" style="margin-top:8px">${checks}</div>
    <div style="margin-top:8px"><button class="act" data-pos="${w.id}">Details & Chart</button></div>`;
}

// ------------------------------------------------------------------ Kontostand-Chart
async function loadHeat() {
  const r = await api("/api/regime?hours=48");
  drawLines($("#heatChart"), [{ name: "Hitze", pts: r.points, color: "#ff9f43", width: 2.2, base: 30, unit: "" }]);
}

async function loadEquity() {
  const data = await api("/api/equity?hours=" + eqHours);
  const showTests = $("#eqTests").checked;
  const series = Object.values(data).filter((s) => s.kind === "main" || showTests);
  drawLines($("#eqChart"), series.map((s, i) => ({
    name: s.name, pts: s.points, color: s.kind === "main" ? (s.strategy === "scanner" ? "#22d36b" : "#4cc3ff") : ["#a78bfa", "#f5c84c", "#ff9f43", "#ff4d5e", "#8695a3", "#e6edf3", "#6ee7b7"][i % 7],
    width: s.kind === "main" ? 2.2 : 1, base: s.start,
  })));
}

function setupCanvas(cv) {
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const h = +cv.getAttribute("height");
  cv.width = r.width * dpr; cv.height = h * dpr; cv.style.height = h + "px";
  const c = cv.getContext("2d"); c.scale(dpr, dpr);
  return { c, w: r.width, h };
}

function drawLines(cv, series) {
  const { c, w, h } = setupCanvas(cv);
  const all = series.flatMap((s) => s.pts);
  if (!all.length) { c.fillStyle = "#8695a3"; c.fillText("Noch keine Daten", 10, 20); return; }
  const x0 = Math.min(...all.map((p) => p[0])), x1 = Math.max(...all.map((p) => p[0]));
  let y0 = Math.min(...all.map((p) => p[1]), ...series.map((s) => s.base)), y1 = Math.max(...all.map((p) => p[1]), ...series.map((s) => s.base));
  const pad = (y1 - y0) * 0.1 || 10; y0 -= pad; y1 += pad;
  const L = 52, R = 8, T = 8, B = 20;
  const X = (t) => L + (x1 === x0 ? 0 : (t - x0) / (x1 - x0)) * (w - L - R);
  const Y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (h - T - B);
  c.font = "11px ui-monospace,monospace"; c.fillStyle = "#8695a3"; c.strokeStyle = "#1f2830"; c.lineWidth = 1;
  for (let i = 0; i <= 4; i++) { const v = y0 + (y1 - y0) * i / 4, y = Y(v); c.beginPath(); c.moveTo(L, y); c.lineTo(w - R, y); c.stroke(); c.fillText((series[0] && series[0].unit !== undefined ? "" : "$") + Math.round(v), 2, y + 4); }
  const nx = w < 520 ? 1 : 3;
  for (let i = 0; i <= nx; i++) { const t = x0 + (x1 - x0) * i / nx, lab = dt(t), lw = c.measureText(lab).width; c.fillText(lab, Math.max(L, Math.min(w - R - lw, X(t) - lw / 2)), h - 5); }
  series.forEach((s) => {
    if (s.base) { c.setLineDash([3, 4]); c.strokeStyle = "#33404c"; c.beginPath(); c.moveTo(L, Y(s.base)); c.lineTo(w - R, Y(s.base)); c.stroke(); c.setLineDash([]); }
    c.strokeStyle = s.color; c.lineWidth = s.width; c.beginPath();
    s.pts.forEach((p, i) => (i ? c.lineTo(X(p[0]), Y(p[1])) : c.moveTo(X(p[0]), Y(p[1]))));
    c.stroke();
    const last = s.pts[s.pts.length - 1];
    if (last && s.width > 2) { c.fillStyle = s.color; c.beginPath(); c.arc(X(last[0]), Y(last[1]), 3.5, 0, 7); c.fill(); }
  });
  let lx = L + 6; series.filter((s) => s.width > 2).forEach((s) => { c.fillStyle = s.color; c.fillText("● " + s.name, lx, T + 12); lx += c.measureText("● " + s.name).width + 14; });
}

function drawCandles(cv, candles, marks) {
  const { c, w, h } = setupCanvas(cv);
  if (!candles.length) { c.fillStyle = "#8695a3"; c.font = "13px sans-serif"; c.fillText("Keine Kerzen verfügbar", 10, 20); return; }
  const lo = Math.min(...candles.map((k) => k[3]), ...marks.map((m) => m.price)), hi = Math.max(...candles.map((k) => k[2]), ...marks.map((m) => m.price));
  const L = 8, R = 70, T = 10, B = 20, n = candles.length;
  const x0 = candles[0][0], x1 = candles[n - 1][0] + 60;
  const X = (t) => L + (t - x0) / (x1 - x0) * (w - L - R);
  const Y = (v) => T + (1 - (Math.log(v) - Math.log(lo)) / ((Math.log(hi) - Math.log(lo)) || 1)) * (h - T - B);
  const bw = Math.max(1, (w - L - R) / n * 0.7);
  c.font = "11px ui-monospace,monospace";
  for (let i = 0; i <= 4; i++) { const v = Math.exp(Math.log(lo) + (Math.log(hi) - Math.log(lo)) * i / 4), y = Y(v); c.strokeStyle = "#1f2830"; c.beginPath(); c.moveTo(L, y); c.lineTo(w - R, y); c.stroke(); c.fillStyle = "#8695a3"; c.fillText(price(v), w - R + 4, y + 4); }
  candles.forEach((k) => {
    const up = k[4] >= k[1]; c.strokeStyle = c.fillStyle = up ? "#22d36b" : "#ff4d5e";
    const x = X(k[0] + 30); c.beginPath(); c.moveTo(x, Y(k[2])); c.lineTo(x, Y(k[3])); c.stroke();
    const y1 = Y(Math.max(k[1], k[4])), y2 = Y(Math.min(k[1], k[4])); c.fillRect(x - bw / 2, y1, bw, Math.max(1, y2 - y1));
  });
  marks.forEach((m) => {
    if (m.ts < x0 || m.ts > x1) return;
    const x = X(m.ts), y = Y(m.price); c.fillStyle = m.side === "buy" ? "#4cc3ff" : "#f5c84c";
    c.beginPath(); c.arc(x, y, 6, 0, 7); c.fill(); c.fillStyle = "#000"; c.font = "bold 9px sans-serif"; c.fillText(m.side === "buy" ? "K" : "V", x - 3, y + 3);
  });
  c.fillStyle = "#8695a3"; c.font = "11px ui-monospace,monospace"; c.fillText(tm(x0), L, h - 5); c.fillText(tm(x1), w - R - 40, h - 5);
}

// ------------------------------------------------------------------ Trade-Details
async function openTrade(id) {
  $("#modal").classList.remove("hide");
  $("#modalBody").innerHTML = `<div class="empty">lade …</div>`;
  const t = await api("/api/trade/" + id);
  const s = t.signal || {};
  const parts = s.punkte ? Object.entries(s.punkte).map(([k, p]) => `<tr><td>${esc(k)}</td><td style="width:40%"><div class="bar"><i style="width:${Math.round(p.wert * 100)}%"></i></div></td><td class="mono">${p.punkte}</td><td class="small muted">${esc(p.text)}</td></tr>`).join("") : "";
  const checks = (s.checks || []).map(([n, ok, val]) => `<span class="${ok ? "" : "f"}">${ok ? "✓" : "✗"} ${esc(n)} (${esc(val)})</span>`).join("");
  const copy = s.copy ? `<div class="kv" style="margin:8px 0"><div>Wallet</div><div class="mono">${short(s.copy.wallet)} · Score ${s.copy.wallet_score}</div><div>Preis Wallet</div><div class="mono">${price(s.copy.wallet_preis)} (wir ${pct(s.copy.aufpreis_pct, 0)})</div><div>Wallets im Cluster</div><div>${s.copy.wallets_im_cluster}</div></div>` : "";
  $("#modalBody").innerHTML = `
    <div class="row between wrap gap"><div><div style="font-size:24px;font-weight:800">$${esc(t.symbol)} <span class="tag ${t.status}">${t.status}</span></div>
      <div class="small muted">${esc(t.konto)} · ${esc(t.dex)} · <span class="mono">${short(t.mint)}</span> · <a href="https://dexscreener.com/solana/${encodeURIComponent(t.pool)}" target="_blank" rel="noopener" style="color:var(--blue)">DexScreener</a></div></div>
      <div class="${cls(t.pnl)}" style="font:800 26px var(--mono)">${sgn(t.pnl)} <span style="font-size:15px">${pct(t.pnl_pct, 0)}</span></div></div>
    <canvas id="tradeChart" height="260" style="margin:10px 0;height:260px"></canvas>
    <div class="kv"><div>Auslöser</div><div>${esc(t.reason_in)}</div><div>Ausstieg</div><div>${esc(t.reason_out || "läuft noch")}</div>
      <div>Einsatz</div><div>${usd(t.cost)} · Punktzahl ${Math.round(t.score || 0)}</div><div>Erlös + Wert</div><div>${usd(t.proceeds + t.value)}</div></div>
    ${copy}
    <h2 style="margin-top:14px">Rug-Check</h2><div class="checks">${checks || '<span class="small muted">–</span>'}</div>
    ${parts ? `<h2 style="margin-top:14px">Punktzahl des Analysten</h2><table>${parts}</table>` : ""}
    <h2 style="margin-top:14px">Ausführungen</h2><div class="tbl"><table><tr><th>Zeit</th><th>Art</th><th>Kurs</th><th>effektiv</th><th>$</th><th>Gebühr</th><th>Notiz</th></tr>
      ${t.fills.map((f) => `<tr><td>${tm(f.ts)}</td><td>${f.side === "buy" ? "Kauf" : "Verkauf"}</td><td class="mono">${price(f.market_price)}</td><td class="mono">${price(f.eff_price)}</td><td>${usd(f.usd)}</td><td>${usd(f.fee)}</td><td class="small muted">${esc(f.note)}</td></tr>`).join("")}</table></div>
    <h2 style="margin-top:14px">Entscheidungen zu diesem Coin</h2><pre class="log">${t.decisions.map((d) => `${tm(d.ts)} ${d.agent.padEnd(9)} ${d.action.padEnd(8)} ${esc(d.reason)}`).join("\n")}</pre>`;
  try {
    const ch = await api("/api/chart/" + id);
    drawCandles($("#tradeChart"), ch.candles || [], t.fills.filter((f) => f.market_price > 0).map((f) => ({ ts: f.ts, price: f.market_price, side: f.side })));
  } catch (e) { drawCandles($("#tradeChart"), [], []); }
}

// ------------------------------------------------------------------ Trades
function fillAccSelect() {
  $("#trAcc").innerHTML = `<option value="">alle Konten</option>` + accounts.map((a) => `<option value="${a.id}">${esc(a.name)}${a.kind === "test" ? " (Test)" : ""}</option>`).join("");
}
async function loadTrades() {
  if (!accounts.length) { const o = await api("/api/overview"); accounts = o.accounts; fillAccSelect(); }
  const rows = await api(`/api/trades?account=${$("#trAcc").value}&status=${$("#trStatus").value}`);
  const closed = rows.filter((r) => r.status === "closed");
  const sum = closed.reduce((a, r) => a + r.pnl, 0);
  $("#trSum").textContent = `${rows.length} Einträge · ${closed.length} geschlossen · Summe ${sgn(sum)}`;
  $("#trades").innerHTML = rows.length ? `<div class="tbl"><table><tr><th>Zeit</th><th>Coin</th><th>Konto</th><th>Status</th><th>Einsatz</th><th>G/V</th><th>Grund</th></tr>` +
    rows.map((r) => `<tr class="click" data-pos="${r.id}"><td class="small">${dt(r.created)}</td><td><b>$${esc(r.symbol)}</b></td><td class="small">${esc(r.konto)}</td>
      <td><span class="tag ${r.status}">${r.status}</span></td><td>${usd(r.cost)}</td><td class="${cls(r.pnl)}">${r.status === "cancelled" ? "–" : sgn(r.pnl) + " " + pct(r.pnl_pct, 0)}</td>
      <td class="small muted">${esc(r.reason_out || r.reason_in)}</td></tr>`).join("") + `</table></div>` : `<div class="empty">Noch keine Trades.</div>`;
}

// ------------------------------------------------------------------ Log
async function loadLog() {
  const q = new URLSearchParams({ agent: $("#lgAgent").value, action: $("#lgAction").value, q: $("#lgQ").value });
  const rows = await api("/api/log?" + q);
  $("#logBox").innerHTML = rows.map((d) => `<span class="muted">${dt(d.ts)}</span> <span style="color:${AGENT_COLORS[d.agent] || "#8695a3"}">${esc(d.agent.padEnd(9))}</span> <span class="tag ${d.action}">${d.action}</span> ${d.symbol ? "<b>$" + esc(d.symbol) + "</b> " : ""}${esc(d.reason)}`).join("\n") || "Noch keine Einträge.";
}

// ------------------------------------------------------------------ Wallets
async function loadWallets() {
  const w = await api("/api/wallets");
  $("#walletInfo").textContent = `Scout: ${w.scout.coins_gesammelt} Coins gesammelt, ${w.scout.trades} Trades, ${w.scout.wallets_bewertet} Wallets bewertet · beobachtet ab ${w.min_tokens} Coins und Score ≥ ${w.min_score} · Copy-Feed: ${w.feed.abgefragt} Abfragen` + (w.feed.fehler ? ` · letzter Fehler: ${w.feed.fehler}` : "");
  $("#wallets").innerHTML = w.wallets.length ? `<div class="tbl"><table><tr><th>Wallet</th><th>Score</th><th>Coins</th><th>Treffer</th><th>Fehlgriffe</th><th>G/V (geschätzt)</th><th>unsere Kopien</th><th></th></tr>` +
    w.wallets.slice(0, 120).map((x) => `<tr><td class="mono"><a href="https://solscan.io/account/${encodeURIComponent(x.address)}" target="_blank" rel="noopener" style="color:var(--blue)">${short(x.address)}</a></td>
      <td class="mono">${(x.score ?? 0).toFixed(2)}</td><td>${x.tokens}</td><td class="pos">${x.hits}</td><td class="neg">${x.misses}</td><td class="${cls(x.pnl_usd)}">${sgn(x.pnl_usd, 0)}</td>
      <td class="${cls(x.copy_pnl)}">${x.copy_trades ? `${x.copy_wins}/${x.copy_trades} · ${sgn(x.copy_pnl)}` : "–"}</td>
      <td>${x.tracked ? '<span class="tag open">beobachtet</span>' : x.note ? `<span class="tag CANCEL">${esc(x.note)}</span>` : ""}</td></tr>`).join("") + `</table></div>`
    : `<div class="empty">Der Scout sammelt noch Daten. Eine Wallet braucht mindestens ${w.min_tokens} bewertete Coins (Ergebnis frühestens 30 min nach dem Kauf).</div>`;
  $("#copyFeed").innerHTML = w.recent.length ? `<div class="feed">` + w.recent.map((t) => `<div class="it"><div class="t">${tm(t.ts)}</div><div><span class="tag ${t.kind === "buy" ? "ENTER" : "EXIT"}">${t.kind === "buy" ? "KAUF" : "VERKAUF"}</span>
    <b>$${esc(t.symbol || short(t.mint))}</b> <span class="small muted">${short(t.wallet)} · ${usd(t.usd)}</span></div></div>`).join("") + `</div>` : `<div class="empty">Noch keine Käufe der beobachteten Wallets gesehen.</div>`;
}

// ------------------------------------------------------------------ Lernen
async function loadLearning() {
  const L = await api("/api/learning");
  $("#learnInfo").textContent = `Vergleich ab ${L.min_trades} abgeschlossenen Trades · Vorschlag ab ${L.min_edge} %-Punkten Vorsprung · letzter Lauf: ${L.learner.letzter_lauf ? dt(L.learner.letzter_lauf) : "noch keiner"} · Testkonten ersetzt: ${L.learner.ersetzt}`;
  const open = L.proposals.filter((p) => p.status === "offen");
  $("#proposals").innerHTML = (open.length ? open : L.proposals.slice(0, 5)).map((p) => `
    <div class="card" style="background:var(--card2)"><div class="row between wrap gap"><b>${esc(p.stats.testkonto)} → Hauptkonto</b><span class="tag ${p.status === "offen" ? "PROPOSAL" : p.status === "angenommen" ? "PARTIAL" : "CANCEL"}">${esc(p.status)}</span></div>
      <div class="small muted">${dt(p.ts)} · ${p.stats.trades} Trades · Rendite Test ${pct(p.stats.rendite_test)} vs. Haupt ${pct(p.stats.rendite_haupt)} · PF ${p.stats.pf_test ?? "–"} vs. ${p.stats.pf_haupt ?? "–"} · Max DD ${p.stats.dd_test} % vs. ${p.stats.dd_haupt} %</div>
      <ul>${Object.entries(p.changes).map(([k, [a, b]]) => `<li>${esc(L.labels[k] || k)}: <span class="mono">${esc(fmtVal(a) ?? "Standard")} → ${esc(fmtVal(b))}</span></li>`).join("")}</ul>
      ${p.status === "offen" ? `<button class="act" data-prop="${p.id}" data-accept="1">Übernehmen</button> <button class="act no" data-prop="${p.id}" data-accept="0">Ablehnen</button>` : ""}</div>`).join("")
    || `<div class="empty">Noch keine Vorschläge. Ein Testkonto braucht mindestens ${L.min_trades} Trades und muss klar besser sein.</div>`;
  const mains = Object.fromEntries(L.accounts.filter((a) => a.kind === "main").map((a) => [a.strategy, a]));
  $("#variants").innerHTML = `<div class="tbl"><table><tr><th>Konto</th><th>Kontostand</th><th>Rendite</th><th>Trades</th><th>Treffer</th><th>PF</th><th>Max DD</th><th>Abweichende Regeln</th></tr>` +
    L.accounts.map((a) => {
      const m = mains[a.strategy];
      const diff = a.kind === "main" ? "Hauptkonto" : Object.entries(a.params).filter(([k, v]) => m && m.params[k] !== v).map(([k, v]) => `${L.labels[k] || k}: ${fmtVal(v)}`).join(" · ");
      return `<tr style="${a.active ? "" : "opacity:.45"}"><td><b>${esc(a.name)}</b>${a.active ? "" : " <span class='tag'>abgeschaltet</span>"}${a.kind === "main" || a.generation || !a.active ? `<div class="small muted">${esc(a.note || "")}</div>` : ""}</td>
        <td>${usd(a.equity)}</td><td class="${cls(a.pnl_pct)}">${pct(a.pnl_pct)}</td><td>${a.trades}</td><td>${a.winrate ?? "–"} %</td><td>${a.profit_factor ?? "–"}</td><td>−${a.max_drawdown} %</td><td class="small muted">${esc(diff)}</td></tr>`;
    }).join("") + `</table></div>`;
}
async function decideProposal(id, accept) {
  if (!confirm(accept ? "Regeln des Testkontos auf das Hauptkonto übernehmen?" : "Vorschlag ablehnen?")) return;
  try { await api("/api/proposal/" + id, { method: "POST", body: JSON.stringify({ accept }) }); } catch (e) { alert("Fehler: " + e.message); }
  loadLearning();
}

// ------------------------------------------------------------------ Auswertung
const VERDICT_CLS = { "eher zu streng": "PROPOSAL", "schützt gut": "PARTIAL", "Vergleichswert": "ENTER" };
async function loadShadow() {
  const S = await api("/api/shadow?days=" + shDays);
  $("#shInfo").innerHTML = `Jede Ablehnung des Hauptkontos wird ${S.hours} h weiterverfolgt. <b>Gewinner</b> = zwischendurch mindestens +50 %, <b>Rug</b> = am Ende −80 % oder Liquidität weg.
    „gekauft“ ist der Vergleich: so liefen die Coins, die der Bot gekauft hat. Eine Regel, die mehr Gewinner und nicht mehr Rugs blockiert, ist eher zu streng.
    Fällt ein Coin durch mehrere Regeln, zählt nur die erste – die Zahlen sind ein Hinweis, kein Beweis. Erfasst: ${S.stats.erfasst}, ausgewertet: ${S.stats.ausgewertet}.`;
  const names = { scanner: "Scanner", copy: "Copy-Trading" };
  $("#shadowTables").innerHTML = Object.entries(S.report).map(([strat, r]) => `
    <h2 style="margin-top:14px">${esc(names[strat] || strat)}</h2>
    <div class="tbl"><table><tr><th>Regel</th><th>Coins</th><th>läuft</th><th>Gewinner</th><th>Rugs</th><th>Verlierer</th><th>Ø bestes Plus</th><th>Ø Ende</th><th>Einschätzung</th></tr>
    ${r.regeln.map((i) => `<tr style="${i.rule === "gekauft" ? "background:#0f1d16" : ""}"><td><b>${esc(i.name)}</b>${i.regel ? `<div class="small muted">${esc(i.regel)}</div>` : ""}</td>
      <td>${i.n}</td><td class="muted">${i.offen || 0}</td><td class="pos">${i.n ? i.gewinner_pct + " %" : "–"}</td><td class="neg">${i.n ? i.rug_pct + " %" : "–"}</td>
      <td>${i.n ? i.verlierer_pct + " %" : "–"}</td><td>${i.n ? pct(i.avg_bestes, 0) : "–"}</td><td class="${cls(i.avg_ende)}">${i.n ? pct(i.avg_ende, 0) : "–"}</td>
      <td><span class="tag ${VERDICT_CLS[i.einschaetzung] || ""}">${esc(i.einschaetzung)}</span></td></tr>`).join("")}</table></div>`).join("")
    || `<div class="empty">Noch keine Ablehnungen erfasst.</div>`;
  const G = S.gewichte;
  const rows = Object.keys(G.aktuell).map((k) => { const b = G.bestandteile[k] || {}; return `<tr><td><b>${esc(k)}</b></td><td>${G.aktuell[k]}</td>
    <td class="mono">${b.gewinner ?? "–"}</td><td class="mono">${b.verlierer ?? "–"}</td><td class="mono ${cls(b.unterschied)}">${b.unterschied != null ? (b.unterschied >= 0 ? "+" : "") + b.unterschied : "–"}</td>
    <td>${G.neu ? `<b>${G.neu[k]}</b>` : "–"}</td></tr>`; }).join("");
  $("#weights").innerHTML = `<div class="small muted" style="margin-bottom:8px">${G.beispiele} Beispiele (${G.gewinner} Gewinner, ${G.verlierer} Verlierer/Rugs) aus gekauften und wegen Punktzahl abgelehnten Coins.
    ${G.neu ? "Neue Gewichte berechnet – der Lern-Agent testet sie im Testkonto „Scanner · Gewichte“. Das Hauptkonto ändert sich nur nach deiner Freigabe." : `Neue Gewichte ab ${G.noetig} Beispielen.`}
    Werte 0–1: wie stark der Bestandteil im Schnitt bei Gewinnern bzw. Verlierern ausgeprägt war.</div>
    <div class="tbl"><table><tr><th>Bestandteil</th><th>Gewicht jetzt</th><th>Ø Gewinner</th><th>Ø Verlierer</th><th>Unterschied</th><th>gelernt</th></tr>${rows}</table></div>`;
  const H = S.uhrzeit, maxAbs = Math.max(1, ...H.map((h) => Math.abs(h.pnl)));
  $("#hours").innerHTML = `<div class="small muted" style="margin-bottom:6px">Balken = Gewinn/Verlust aller abgeschlossenen Trades (alle Konten), die in dieser Stunde eröffnet wurden. Darunter: Trades · Treffer.</div>
    <div class="hbar">${H.map((h) => `<div class="${h.pnl > 0 ? "pos" : h.pnl < 0 ? "neg" : ""}" style="height:${Math.max(2, Math.abs(h.pnl) / maxAbs * 90)}px" title="${h.stunde} Uhr: ${sgn(h.pnl)} · ${h.trades} Trades · ${h.trades ? Math.round(h.gewinne / h.trades * 100) : 0} % Treffer · Schatten ${h.schatten_n ? Math.round(h.schatten_gewinner / h.schatten_n * 100) + " % Gewinner" : "–"}"></div>`).join("")}</div>
    <div class="row small muted" style="justify-content:space-between;margin-top:4px">${[0, 3, 6, 9, 12, 15, 18, 21, 23].map((h) => `<span>${h}</span>`).join("")}</div>
    <div class="tbl" style="margin-top:8px"><table><tr><th>Uhrzeit</th><th>Trades</th><th>Treffer</th><th>G/V</th><th>Schatten: Käufe → Gewinner</th></tr>
    ${H.filter((h) => h.trades || h.schatten_n).map((h) => `<tr><td>${h.stunde}–${h.stunde + 1} Uhr</td><td>${h.trades}</td><td>${h.trades ? Math.round(h.gewinne / h.trades * 100) + " %" : "–"}</td>
      <td class="${cls(h.pnl)}">${sgn(h.pnl)}</td><td>${h.schatten_n ? `${h.schatten_n} → ${Math.round(h.schatten_gewinner / h.schatten_n * 100)} %` : "–"}</td></tr>`).join("")}</table></div>`;
  const L = S.launches, c = S.launch_counts;
  $("#launchInfo").innerHTML = `<div class="kv"><div>Verbindung</div><div>${L.verbunden ? '<span class="pos">● live</span>' : '<span class="neg">● getrennt</span>'} · Neustarts ${L.neustarts}${L.fehler ? ` · <span class="neg">${esc(L.fehler)}</span>` : ""}</div>
    <div>Neue Coins</div><div>${L.neue_coins} seit Start · zuletzt ${L.pro_min}/min · letzte Nachricht vor ${ago(L.letzte_nachricht)}</div>
    <div>Nachprüfung</div><div>${c.neu || 0} warten · ${c.aufgenommen || 0} lebendig → Beobachtungsliste · ${c.verworfen || 0} verworfen (letzte 3 Tage)</div>
    <div>Migrationen</div><div>${L.migrationen} gesehen (Wechsel von der Pump.fun-Kurve zu PumpSwap)</div></div>
    <div class="small muted" style="margin-top:6px">Kostenloser PumpPortal-Stream. Er liefert nur neue Coins und Migrationen; Trades einzelner Coins kosten dort Gebühren und werden nicht genutzt.</div>`;
}

// ------------------------------------------------------------------ Backtest
$("#btRun").addEventListener("click", async () => {
  try { await api("/api/backtest/run", { method: "POST", body: "{}" }); } catch (e) { alert("Läuft schon oder Fehler: " + e.message); }
  setTimeout(loadBacktest, 1500);
});
const mstat = (m) => `<td class="${cls(m.rendite)}">${pct(m.rendite)}</td><td>${m.trades}</td><td>${m.treffer ?? "–"} %</td><td>${m.pf ?? "–"}</td><td>${m.max_dd ? "−" + m.max_dd : 0} %</td>`;
async function loadBacktest() {
  const B = await api("/api/backtest");
  const last = B.runs[0];
  $("#btRun").disabled = B.running;
  $("#btInfo").innerHTML = `Spielt die gespeicherten Kurs-Aufnahmen der letzten ${B.hours} h mit vielen Regelsätzen erneut ab – mit denselben Regeln, Gebühren und Verzögerungen wie live.
    Ausgewählt wird auf den ersten 70 % der Zeit (<b>Train</b>), geprüft auf den letzten 30 % (<b>Test</b>). <b>Robust</b> = besser als das Hauptkonto in beiden Zeiträumen,
    im Test mind. +5 %-Punkte und Profit-Faktor ≥ 1. Der beste robuste Regelsatz wird ein Testkonto „Scanner · Replay“ (höchstens eins alle 2 Tage); ins Hauptkonto kommt nichts ohne deine Freigabe.
    Läuft automatisch alle ${B.every_h} h. Grenzen: Rug-Check mit dem letzten bekannten Bericht, Ausstiege nur an den aufgenommenen Punkten, nur Scanner-Strategie.
    <br>Aufnahmen mit allen Werten: ${B.snapshots.toLocaleString("de-DE")} · ${B.running ? `<b>läuft: ${esc(B.progress)}</b>` : last ? `letzter Lauf ${dt(last.ts)} (${esc(last.status)})` : "noch kein Lauf"}`;
  const R = B.results;
  $("#btResults").innerHTML = R.length ? `<div class="tbl"><table><tr><th>Regelsatz</th><th colspan="5">Train</th><th colspan="5">Test</th><th>vs. Haupt</th><th>Änderungen</th></tr>
    <tr><th></th><th>Rendite</th><th>Trades</th><th>Treffer</th><th>PF</th><th>Max DD</th><th>Rendite</th><th>Trades</th><th>Treffer</th><th>PF</th><th>Max DD</th><th>Test</th><th></th></tr>
    ${R.map((r, i) => `<tr style="${i === 0 ? "background:#0f1d16" : ""}"><td><b>${esc(r.name)}</b> ${r.robust && r.art === "neu" ? '<span class="tag PARTIAL">robust</span>' : ""}${r.art === "live" && i ? ' <span class="tag">läuft live</span>' : ""}</td>
      ${mstat(r.train)}${mstat(r.test)}<td class="${cls(r.besser_im_test)}">${i ? pct(r.besser_im_test) : "–"}</td>
      <td class="small muted">${Object.entries(r.aenderungen).map(([k, [a, b]]) => `${esc(B.labels[k] || k)}: ${esc(fmtVal(a) ?? "–")}→${esc(fmtVal(b))}`).join(" · ") || "–"}</td></tr>`).join("")}</table></div>`
    : `<div class="empty">Noch kein Ergebnis. Der erste automatische Lauf startet, sobald genug Aufnahmen da sind – oder starte ihn oben von Hand.</div>`;
  $("#btRuns").innerHTML = B.runs.map((r) => `<div class="small"><span class="muted">${dt(r.ts)}</span> <b>${esc(r.status)}</b> ·
    ${r.summary.fehler ? esc(r.summary.fehler) : `${r.summary.regelsaetze} Regelsätze · ${r.summary.coins} Coins · ${r.summary.kandidaten} Kandidaten · ${r.summary.stunden} h · ${r.summary.dauer_s} s · ${r.summary.robust} robust${r.summary.testkonto ? " · neues Testkonto " + esc(r.summary.testkonto) : ""}`}</div>`).join("") || `<div class="empty">–</div>`;
  if (B.running) { clearTimeout(timer); timer = setTimeout(refresh, 5000); }
}

// ------------------------------------------------------------------ System
async function loadSystem() {
  const s = await api("/api/system");
  $("#srcs").innerHTML = `<div class="tbl"><table><tr><th>Quelle</th><th>ok</th><th>Fehler</th><th>frei/min</th><th>Antwortzeit</th><th>letzter Fehler</th></tr>` +
    Object.entries(s.sources).map(([k, v]) => `<tr><td><b>${esc(k)}</b></td><td>${v.ok}</td><td class="${v.fehler ? "neg" : ""}">${v.fehler}</td><td>${v.frei_pro_min}/${v.limit_pro_min}</td><td>${Math.round(v.ms)} ms</td><td class="small muted">${esc(v.letzter_fehler)}</td></tr>`).join("") + `</table></div>`;
  $("#sysInfo").innerHTML = `<div class="kv"><div>Version</div><div>${esc(s.version)}</div><div>Takte</div><div>${s.state.cycles} (zuletzt ${s.state.cycle_ms ?? "–"} ms)</div>
    <div>Beobachtet</div><div>${s.watch} Coins, davon ${s.hot} heiß</div><div>Gebühren</div><div>${s.sim.gebuehr_pct} % je Trade + ${usd(s.sim.tx_kosten)} Netzwerk + ${s.sim.slip_zuschlag_pct} % Sandwich-Zuschlag</div>
    <div>Verzögerung</div><div>Orders werden frühestens nach ${s.sim.verzoegerung_s} s zum dann gültigen Kurs ausgeführt</div>
    <div>Telegram</div><div>${esc(s.telegram.status)} · gesendet ${s.telegram.gesendet}, unterdrückt ${s.telegram.unterdrueckt}</div>
    <div>Launch-Feed</div><div>${s.launches.verbunden ? "live" : "getrennt"} · ${s.launches.neue_coins} neue Coins${s.launches.fehler ? " · " + esc(s.launches.fehler) : ""}</div>
    <div>Login</div><div>${s.auth ? "aktiv" : "⚠ nicht gesetzt"}</div><div>Letzter Fehler</div><div class="small">${esc(s.state.last_error || "–")}</div></div>`;
  $("#events").innerHTML = s.events.length ? s.events.map((e) => `<div class="small"><span class="muted">${dt(e.ts)}</span> <b>${esc(e.level)}</b> ${esc(e.title)} <span class="muted">${esc(e.body)}</span></div>`).join("") : `<div class="empty">Keine Ereignisse.</div>`;
}

// ------------------------------------------------------------------ Aktualisieren
async function refresh() {
  clearTimeout(timer);
  try {
    if (tab === "overview") { await loadOverview(); await loadEquity(); await loadHeat(); }
    else if (tab === "backtest") await loadBacktest();
    else if (tab === "trades") await loadTrades();
    else if (tab === "log") await loadLog();
    else if (tab === "wallets") await loadWallets();
    else if (tab === "learning") await loadLearning();
    else if (tab === "shadow") await loadShadow();
    else if (tab === "system") await loadSystem();
  } catch (e) {
    $("#status").textContent = "Verbindung gestört: " + e.message;
    $("#live").className = "dot bad";
  }
  timer = setTimeout(refresh, tab === "overview" ? 10000 : 20000);
}
window.addEventListener("resize", () => { if (tab === "overview") loadEquity(); });
refresh();
