"use strict";

// Kalshi perps: 1 contract = 0.0001 BTC, so contract price x 10,000 = implied BTC price.
const CONTRACTS_PER_BTC = 10000;
const FUNDING_PERIOD_MS = 8 * 3600 * 1000;
const BOOK_LEVELS = 12;

const state = {
  unit: "btc",
  iv: 15,
  cfg: null,
  snap: null,
  snapAt: 0,
  serverOffset: 0,
  candles: [],
  lastPrice: null,
  error: null,
};

const $ = (id) => document.getElementById(id);
const num = (x) => (x === null || x === undefined || x === "" ? null : Number(x));

function toUnit(p) {
  p = num(p);
  if (p === null) return null;
  return state.unit === "btc" ? p * CONTRACTS_PER_BTC : p;
}
function fmtPx(p) {
  const v = toUnit(p);
  if (v === null) return "—";
  return state.unit === "btc"
    ? "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 })
    : "$" + v.toFixed(4);
}
function fmtPxDelta(d) {
  const v = toUnit(Math.abs(d));
  if (v === null) return "—";
  return state.unit === "btc" ? "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 }) : "$" + v.toFixed(4);
}
function fmtUsdCompact(x) {
  x = num(x);
  if (x === null) return "—";
  return "$" + Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(x);
}
function fmtCount(x) {
  x = num(x);
  if (x === null) return "—";
  return Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(x);
}
function fmtSize(x) {
  return num(x).toLocaleString("en-US", { maximumFractionDigits: 2 });
}
function pct(x, dp = 4) {
  return x === null ? "—" : (x * 100).toFixed(dp) + "%";
}
function setText(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  if (cls !== undefined) el.className = el.className.replace(/\b(up|down)\b/g, "").trim() + (cls ? " " + cls : "");
}

async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  const body = await r.json();
  if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
  return body;
}

/* ---------------- config / header ---------------- */

async function loadConfig() {
  state.cfg = await getJSON("/api/config");
  const c = state.cfg;
  $("ticker").textContent = c.ticker;
  document.title = `${c.ticker} · BTC Perps Desk`;
  const env = $("envBadge");
  env.textContent = c.env;
  env.classList.toggle("prod", c.env === "prod");
  $("envFoot").textContent = c.env;
  const mode = $("modeBadge");
  if (c.live_trading_enabled) {
    mode.textContent = "LIVE TRADING ENABLED";
    mode.className = "badge live";
  }
}

function renderStatus() {
  const b = $("statusBadge");
  const label = b.querySelector("span");
  const stale = Date.now() - state.snapAt > 6000;
  b.classList.remove("ok", "bad");
  if (state.error && stale) {
    b.classList.add("bad");
    label.textContent = "API error";
    b.title = state.error;
  } else if (!state.snap) {
    label.textContent = "Connecting";
  } else if (stale) {
    b.classList.add("bad");
    label.textContent = "Stale";
  } else {
    const s = state.snap.status;
    const open = s.exchange_active && s.trading_active;
    b.classList.add(open ? "ok" : "bad");
    label.textContent = open ? "Trading open" : s.exchange_active ? "Trading paused" : "Exchange down";
    b.title = "";
  }
}

/* ---------------- quotes ---------------- */

function renderQuotes() {
  const s = state.snap;
  if (!s) return;
  const m = s.market;
  const last = num(m.price);

  const lastEl = $("qLast");
  lastEl.textContent = fmtPx(last);
  if (state.lastPrice !== null && last !== state.lastPrice) {
    const cls = last > state.lastPrice ? "flash-up" : "flash-down";
    lastEl.classList.remove("flash-up", "flash-down");
    void lastEl.offsetWidth;
    lastEl.classList.add(cls);
  }
  state.lastPrice = last;

  if (state.candles.length) {
    const open24 = num(state.candles[0].o);
    const d = last - open24;
    const sign = d >= 0 ? "+" : "−";
    setText("qChange", `${sign}${fmtPxDelta(d)} (${sign}${Math.abs((d / open24) * 100).toFixed(2)}%) 24h`, d >= 0 ? "up" : "down");
  }

  const bids = s.book.bids, asks = s.book.asks;
  const bb = bids.length ? bids[0] : null, ba = asks.length ? asks[0] : null;
  $("qBid").textContent = fmtPx(bb ? bb[0] : m.bid);
  $("qAsk").textContent = fmtPx(ba ? ba[0] : m.ask);
  $("qBidSz").textContent = bb ? `${fmtSize(bb[1])} contracts` : "";
  $("qAskSz").textContent = ba ? `${fmtSize(ba[1])} contracts` : "";

  const spread = num(s.book.spread), mid = num(s.book.mid);
  $("qSpread").textContent = spread === null ? "—" : fmtPxDelta(spread);
  $("qSpreadBps").textContent = spread === null || !mid ? "" : `${((spread / mid) * 1e4).toFixed(1)} bps`;

  const mark = m.settlement_mark_price && m.settlement_mark_price.price;
  const index = m.reference_price && m.reference_price.price;
  $("qMark").textContent = fmtPx(mark);
  $("qIndex").textContent = fmtPx(index);
  if (index && mid) {
    const basis = ((mid - num(index)) / num(index)) * 1e4;
    setText("qBasis", `basis ${basis >= 0 ? "+" : ""}${basis.toFixed(1)} bps`, basis >= 0 ? "up" : "down");
  }

  $("qVol").textContent = fmtUsdCompact(m.volume_24h_notional_value_dollars);
  $("qVolC").textContent = `${fmtCount(m.volume_24h)} contracts`;
  $("qOI").textContent = fmtUsdCompact(m.open_interest_notional_value_dollars);
  $("qOIC").textContent = `${fmtCount(m.open_interest)} contracts`;
}

/* ---------------- order book ---------------- */

function withCumulative(levels) {
  const rows = levels.slice(0, BOOK_LEVELS);
  let cum = 0;
  const withCum = rows.map(([p, q]) => ((cum += num(q)), [p, q, cum]));
  return withCum;
}

function renderBook() {
  const s = state.snap;
  if (!s) return;
  const asks = withCumulative(s.book.asks);
  const bids = withCumulative(s.book.bids);
  const max = Math.max(asks.length ? asks[asks.length - 1][2] : 0, bids.length ? bids[bids.length - 1][2] : 0, 1);
  const html = (lvls) =>
    lvls
      .map(([p, q, c]) =>
        `<div class="row"><div class="depth" style="width:${((c / max) * 100).toFixed(1)}%"></div>` +
        `<span class="p">${fmtPx(p)}</span><span>${fmtSize(q)}</span><span>${fmtSize(c)}</span></div>`)
      .join("");
  $("asks").innerHTML = html(asks);
  $("bids").innerHTML = html(bids);
  const spread = num(s.book.spread), mid = num(s.book.mid);
  $("bookMid").innerHTML =
    `<span>${fmtPx(mid)}</span><span class="muted">spread ${spread === null ? "—" : fmtPxDelta(spread)}` +
    `${mid && spread !== null ? " · " + ((spread / mid) * 1e4).toFixed(1) + " bps" : ""}</span>`;
  $("bookMeta").textContent = "size in contracts";
}

/* ---------------- funding ---------------- */

function renderFundingStatic() {
  const f = state.snap && state.snap.funding;
  if (!f) return;
  const rate = num(f.funding_rate);
  const mark = num(f.mark_price);
  $("fRate").textContent = rate === null ? "—" : pct(rate, 4);
  let dir = "—", cls = "";
  if (rate !== null) {
    if (Math.abs(rate) < 0.0001) dir = "No payment (<0.01%)";
    else if (rate > 0) { dir = "Longs pay shorts"; cls = "down"; }
    else { dir = "Shorts pay longs"; cls = "up"; }
  }
  setText("fDir", dir, cls);
  $("fPer").textContent = rate === null || mark === null ? "—" : `$${Math.abs(rate * mark).toFixed(4)} / contract`;
  $("fAnn").textContent = rate === null ? "—" : pct(rate * 3 * 365, 1);
  const next = new Date(f.next_funding_time);
  $("nextFunding").textContent = "next funding " + next.toLocaleString([], {
    weekday: "short", hour: "numeric", minute: "2-digit", timeZoneName: "short",
  });
}

function tickCountdown() {
  const f = state.snap && state.snap.funding;
  if (!f) return;
  const now = Date.now() + state.serverOffset;
  const remaining = Math.max(0, new Date(f.next_funding_time).getTime() - now);
  const h = Math.floor(remaining / 3600000);
  const mnt = Math.floor((remaining % 3600000) / 60000);
  const sec = Math.floor((remaining % 60000) / 1000);
  $("countdown").textContent = [h, mnt, sec].map((x) => String(x).padStart(2, "0")).join(":");
  $("fundBar").style.transform = `scaleX(${((FUNDING_PERIOD_MS - Math.min(remaining, FUNDING_PERIOD_MS)) / FUNDING_PERIOD_MS).toFixed(4)})`;
}

/* ---------------- account ---------------- */

async function refreshAccount() {
  try {
    const a = await getJSON("/api/account");
    if (a.reason && !a.balance_status) {
      $("aBal").textContent = "No API key configured";
      $("acctMeta").textContent = "";
      return;
    }
    $("aBal").textContent = a.available && a.balance
      ? "$" + num(a.balance.settled_funds).toFixed(2)
      : `Unavailable (HTTP ${a.balance_status})`;
    const pos = (a.positions || []).find((p) => p.market_ticker === state.cfg.ticker);
    if (!pos || num(pos.position) === 0) {
      $("aPos").textContent = "Flat";
    } else {
      const q = num(pos.position);
      const pnl = num(pos.unrealized_pnl);
      $("aPos").innerHTML = `${q > 0 ? "Long" : "Short"} ${fmtSize(Math.abs(q))} @ ${fmtPx(pos.entry_price)} ` +
        `<span class="${pnl >= 0 ? "up" : "down"}">${pnl >= 0 ? "+" : "−"}$${Math.abs(pnl).toFixed(2)}</span>`;
    }
    if (a.fees) {
      $("aMaker").textContent = a.fees.maker != null ? pct(num(a.fees.maker), 2) + " of notional" : "—";
      $("aTaker").textContent = a.fees.taker != null ? pct(num(a.fees.taker), 2) + " of notional" : "—";
    }
    $("acctMeta").textContent = "your rates · read-only";
  } catch (e) {
    $("acctMeta").textContent = "account error";
  }
}

/* ---------------- chart ---------------- */

const chart = { canvas: null, ctx: null, bars: [], hover: null, layout: null };

function aggregate(candles, ivMin) {
  const step = ivMin * 60;
  const out = [];
  let cur = null;
  for (const c of candles) {
    const end = Math.ceil(c.t / step) * step; // minute bars are (t-60, t]
    const o = num(c.o), h = num(c.h), l = num(c.l), cl = num(c.c), v = num(c.v) || 0;
    if (!cur || cur.t !== end) {
      cur = { t: end, o, h, l, c: cl, v };
      out.push(cur);
    } else {
      cur.h = Math.max(cur.h, h);
      cur.l = Math.min(cur.l, l);
      cur.c = cl;
      cur.v += v;
    }
  }
  return out;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function niceStep(range, target) {
  const raw = range / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
}

function drawChart() {
  const cv = chart.canvas;
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(W * dpr);
    cv.height = Math.round(H * dpr);
  }
  const ctx = chart.ctx;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const bars = chart.bars;
  if (!bars.length) {
    ctx.fillStyle = cssVar("--muted");
    ctx.font = "13px " + cssVar("--sans");
    ctx.fillText("Loading candles…", 12, 24);
    return;
  }

  const col = { up: cssVar("--bid"), down: cssVar("--ask"), grid: cssVar("--grid"), muted: cssVar("--muted"),
                text: cssVar("--text"), accent: cssVar("--accent"), panel: cssVar("--panel-2") };
  const mono = "11px " + cssVar("--mono");
  const padR = 78, padB = 22, padT = 8, volH = 44;
  const plotW = W - padR, plotH = H - padB - padT - volH - 6;

  let lo = Infinity, hi = -Infinity, vmax = 0;
  for (const b of bars) { lo = Math.min(lo, b.l); hi = Math.max(hi, b.h); vmax = Math.max(vmax, b.v); }
  const livePx = state.lastPrice;
  if (livePx !== null) { lo = Math.min(lo, livePx); hi = Math.max(hi, livePx); }
  const pad = (hi - lo) * 0.06 || hi * 0.001;
  lo -= pad; hi += pad;

  const y = (p) => padT + (1 - (p - lo) / (hi - lo)) * plotH;
  const slot = plotW / bars.length;
  const x = (i) => i * slot + slot / 2;
  chart.layout = { slot, plotW, padT, plotH, y, lo, hi };

  // horizontal grid + price axis (labels in the selected unit)
  ctx.font = mono;
  ctx.textBaseline = "middle";
  const uLo = toUnit(lo), uHi = toUnit(hi);
  const step = niceStep(uHi - uLo, 6);
  for (let u = Math.ceil(uLo / step) * step; u <= uHi; u += step) {
    const p = state.unit === "btc" ? u / CONTRACTS_PER_BTC : u;
    const yy = Math.round(y(p)) + 0.5;
    ctx.strokeStyle = col.grid;
    ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(plotW, yy); ctx.stroke();
    ctx.fillStyle = col.muted;
    ctx.fillText(state.unit === "btc" ? "$" + u.toLocaleString("en-US", { maximumFractionDigits: 0 }) : "$" + u.toFixed(4), plotW + 8, yy);
  }

  // time axis
  ctx.textBaseline = "alphabetic";
  const labelEvery = Math.max(1, Math.ceil(90 / slot));
  for (let i = 0; i < bars.length; i += labelEvery) {
    const d = new Date(bars[i].t * 1000);
    const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const xx = x(i);
    ctx.strokeStyle = col.grid;
    ctx.beginPath(); ctx.moveTo(Math.round(xx) + 0.5, padT); ctx.lineTo(Math.round(xx) + 0.5, H - padB); ctx.stroke();
    ctx.fillStyle = col.muted;
    ctx.textAlign = "center";
    ctx.fillText(t, xx, H - 6);
  }
  ctx.textAlign = "left";

  // volume
  const volTop = padT + plotH + 6;
  bars.forEach((b, i) => {
    const h = vmax ? (b.v / vmax) * volH : 0;
    ctx.fillStyle = b.c >= b.o ? col.up : col.down;
    ctx.globalAlpha = 0.28;
    ctx.fillRect(x(i) - Math.max(1, slot * 0.35), volTop + volH - h, Math.max(1, slot * 0.7), h);
  });
  ctx.globalAlpha = 1;

  // candles
  const bw = Math.max(1, Math.min(12, slot * 0.68));
  bars.forEach((b, i) => {
    const c = b.c >= b.o ? col.up : col.down;
    const xx = Math.round(x(i)) + 0.5;
    ctx.strokeStyle = c;
    ctx.fillStyle = c;
    ctx.beginPath(); ctx.moveTo(xx, y(b.h)); ctx.lineTo(xx, y(b.l)); ctx.stroke();
    const top = y(Math.max(b.o, b.c)), bot = y(Math.min(b.o, b.c));
    ctx.fillRect(xx - bw / 2, top, bw, Math.max(1, bot - top));
  });

  // live last-price line + tag
  if (livePx !== null) {
    const yy = Math.round(y(livePx)) + 0.5;
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = col.accent;
    ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(plotW, yy); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = col.accent;
    ctx.fillRect(plotW + 2, yy - 9, padR - 4, 18);
    ctx.fillStyle = "#fff";
    ctx.textBaseline = "middle";
    ctx.font = "600 " + mono;
    ctx.fillText(fmtPx(livePx), plotW + 7, yy);
  }

  // crosshair
  if (chart.hover !== null && bars[chart.hover]) {
    const i = chart.hover;
    const xx = Math.round(x(i)) + 0.5;
    ctx.strokeStyle = col.muted;
    ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(xx, padT); ctx.lineTo(xx, H - padB); ctx.stroke();
    ctx.setLineDash([]);
  }
}

function onChartMove(ev) {
  const L = chart.layout;
  if (!L) return;
  const r = chart.canvas.getBoundingClientRect();
  const mx = ev.clientX - r.left;
  const i = Math.floor(mx / L.slot);
  const tip = $("tip");
  if (mx > L.plotW || i < 0 || i >= chart.bars.length) {
    chart.hover = null; tip.hidden = true; drawChart(); return;
  }
  chart.hover = i;
  const b = chart.bars[i];
  const start = new Date((b.t - state.iv * 60) * 1000), end = new Date(b.t * 1000);
  const tf = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const chg = ((b.c - b.o) / b.o) * 100;
  tip.textContent =
    `${start.toLocaleDateString([], { month: "short", day: "numeric" })} ${tf(start)}–${tf(end)}\n` +
    `O ${fmtPx(b.o)}  H ${fmtPx(b.h)}\nL ${fmtPx(b.l)}  C ${fmtPx(b.c)}\n` +
    `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%   vol ${fmtCount(b.v)}`;
  tip.hidden = false;
  const tw = tip.offsetWidth;
  tip.style.left = (mx + 14 + tw > L.plotW ? mx - tw - 14 : mx + 14) + "px";
  tip.style.top = "10px";
  drawChart();
}

function rebuildBars() {
  chart.bars = aggregate(state.candles, state.iv);
  const n = state.candles.length;
  $("chartMeta").textContent = n ? `${chart.bars.length} × ${state.iv}m · last trade price` : "";
  drawChart();
}

async function refreshCandles() {
  try {
    const r = await getJSON("/api/candles");
    state.candles = r.candles;
    rebuildBars();
    renderQuotes();
  } catch (e) {
    $("chartMeta").textContent = "candles unavailable: " + e.message;
  }
}

/* ---------------- loop ---------------- */

async function refreshSnapshot() {
  try {
    const s = await getJSON("/api/snapshot");
    state.snap = s;
    state.snapAt = Date.now();
    state.serverOffset = s.server_ts_ms - Date.now();
    state.error = null;
    renderQuotes();
    renderBook();
    renderFundingStatic();
    drawChart();
    $("updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    state.error = e.message;
  }
  renderStatus();
}

function bindControls() {
  document.querySelectorAll("[data-unit]").forEach((b) =>
    b.addEventListener("click", () => {
      state.unit = b.dataset.unit;
      document.querySelectorAll("[data-unit]").forEach((x) => x.classList.toggle("on", x === b));
      renderQuotes(); renderBook(); renderFundingStatic(); drawChart(); refreshAccount();
    }));
  document.querySelectorAll("[data-iv]").forEach((b) =>
    b.addEventListener("click", () => {
      state.iv = Number(b.dataset.iv);
      document.querySelectorAll("[data-iv]").forEach((x) => x.classList.toggle("on", x === b));
      rebuildBars();
    }));
  chart.canvas.addEventListener("mousemove", onChartMove);
  chart.canvas.addEventListener("mouseleave", () => { chart.hover = null; $("tip").hidden = true; drawChart(); });
  new ResizeObserver(drawChart).observe(chart.canvas);
  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", drawChart);
}

async function main() {
  chart.canvas = $("chart");
  chart.ctx = chart.canvas.getContext("2d");
  bindControls();
  try {
    await loadConfig();
  } catch (e) {
    state.error = e.message;
    renderStatus();
  }
  await Promise.all([refreshSnapshot(), refreshCandles()]);
  refreshAccount();
  setInterval(refreshSnapshot, 1000);
  setInterval(refreshCandles, 30000);
  setInterval(refreshAccount, 10000);
  setInterval(() => { tickCountdown(); renderStatus(); }, 250);
}

main();
