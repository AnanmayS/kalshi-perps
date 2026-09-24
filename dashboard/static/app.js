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
  side: "buy",
  otype: "market",
  paper: null,
  previewSeq: 0,
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

async function postJSON(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
  return data;
}

function money(x, signed = false) {
  x = num(x);
  if (x === null) return "—";
  const s = "$" + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (!signed) return (x < 0 ? "−" : "") + s;
  return (x > 0 ? "+" : x < 0 ? "−" : "") + s;
}
function signCls(x) {
  x = num(x);
  return x > 0 ? "up" : x < 0 ? "down" : "";
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
  const stale = Date.now() - state.snapAt > 8000;
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
  document.title = `${fmtPx(last)} · ${m.ticker} · BTC Perps Desk`;
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

/* ---------------- paper trading ---------------- */

function orderBody() {
  const count = $("size").value.trim();
  const body = { side: state.side, count };
  if (state.otype === "limit") {
    const v = num($("limitPx").value);
    // The limit input is in the display unit; the API wants dollars per contract.
    body.limit_price = v === null ? "" : (state.unit === "btc" ? (v / CONTRACTS_PER_BTC).toFixed(4) : v.toFixed(4));
  }
  return body;
}

function setCheck(text, kind) {
  const el = $("pCheck");
  el.textContent = text;
  el.className = "check" + (kind ? " " + kind : "");
}

let previewTimer = null;
function schedulePreview(delay = 200) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, delay);
}

async function updatePreview() {
  const size = num($("size").value);
  $("sizeHint").textContent = size ? `≈ ${(size / CONTRACTS_PER_BTC).toFixed(4)} BTC` : "";
  const btn = $("submitBtn");
  btn.className = "submit " + state.side;
  const verb = state.side === "buy" ? "Buy" : "Sell";
  btn.textContent = `${verb} ${size || ""} · paper`;
  const clear = () => ["pVwap", "pNotional", "pFee", "pLevels", "pAfter"].forEach((id) => ($(id).textContent = "—"));
  if (!size || size <= 0) {
    clear(); setCheck("Enter a size", ""); btn.disabled = true; return;
  }
  if (state.otype === "limit" && num($("limitPx").value) === null) {
    clear(); setCheck("Enter a limit price", ""); btn.disabled = true; return;
  }
  const seq = ++state.previewSeq;
  try {
    const p = await postJSON("/api/paper/preview", orderBody());
    if (seq !== state.previewSeq) return;
    $("pVwap").textContent = p.vwap ? fmtPx(p.vwap) : "—";
    $("pNotional").textContent = money(p.notional);
    $("pFeeRate").textContent = pct(num(p.fee_rate), 2);
    $("pFee").textContent = "$" + num(p.fee_cash).toFixed(4);
    $("pLevels").textContent = p.levels.length
      ? `${p.levels.length} level${p.levels.length > 1 ? "s" : ""}` + (num(p.unfilled) > 0 ? ` · ${fmtSize(p.unfilled)} would cancel` : "")
      : "—";
    const q = num(p.position_after.qty);
    $("pAfter").textContent = q === 0 ? "Flat" : `${q > 0 ? "Long" : "Short"} ${fmtSize(Math.abs(q))} @ ${fmtPx(p.position_after.avg_entry)}`;
    if (p.allowed) {
      const partial = num(p.unfilled) > 0 ? ` (only ${fmtSize(p.filled)} of ${fmtSize(p.requested)} available)` : "";
      setCheck((p.reduces_only ? "Risk check passed · reduces position" : "Risk check passed") + partial, partial ? "" : "ok");
    } else {
      setCheck("Blocked: " + p.reason, "bad");
    }
    btn.disabled = !p.allowed;
  } catch (e) {
    if (seq !== state.previewSeq) return;
    clear(); setCheck(e.message, "bad"); btn.disabled = true;
  }
}

function renderPaper() {
  const st = state.paper;
  if (!st) return;
  const pos = st.position;
  const q = num(pos.qty);
  const side = $("ppSide");
  side.textContent = q === 0 ? "Flat" : `${q > 0 ? "Long" : "Short"} ${fmtSize(Math.abs(q))}`;
  side.className = "pos-side " + (q > 0 ? "long" : q < 0 ? "short" : "");
  const up = $("ppUpnl");
  up.textContent = money(pos.unrealized_pnl, true);
  up.className = "pos-upnl " + signCls(pos.unrealized_pnl);
  up.title = "Unrealized P&L at mark";
  $("ppEntry").textContent = q === 0 ? "—" : fmtPx(pos.avg_entry);
  $("ppMark").textContent = fmtPx(st.mark);
  $("ppNotional").textContent = q === 0 ? "—" : money(pos.notional);
  setText("ppReal", money(pos.realized_pnl, true), signCls(pos.realized_pnl));
  $("ppFees").textContent = "$" + num(pos.fees_paid).toFixed(4);
  const fp = num(pos.funding_paid);
  setText("ppFunding", fp === 0 ? "$0.00" : `${fp > 0 ? "paid" : "received"} $${Math.abs(fp).toFixed(4)}`, fp > 0 ? "down" : fp < 0 ? "up" : "");
  const eq = num(st.equity), start = num(st.starting_cash);
  $("ppEquity").innerHTML = `${money(eq)} <span class="${signCls(eq - start)}">(${money(eq - start, true)})</span>`;
  $("closeBtn").disabled = q === 0;

  const r = st.risk;
  const daily = num(r.daily_pnl), limit = num(r.daily_loss_limit);
  setText("rDaily", money(daily, true), signCls(daily));
  const used = Math.min(1, Math.max(0, -daily / limit));
  const meter = $("rMeter");
  meter.style.transform = `scaleX(${used.toFixed(4)})`;
  meter.parentElement.className = "meter" + (used >= 1 ? " bad" : used >= 0.6 ? " warn" : "");
  $("rRemain").textContent = `${money(Math.max(0, num(r.loss_remaining)))} left before kill`;
  $("rLimit").textContent = `limit −${money(limit)}/day`;
  $("rMaxNotional").textContent = `Max notional per trade ${money(r.max_notional_per_trade)} · max ${num(st.max_leverage)}× equity`;
  const ks = $("killState");
  ks.textContent = r.killed ? "Kill switch ON" : "Armed";
  ks.classList.toggle("tripped", !!r.killed);
  const reason = $("killReason");
  reason.hidden = !r.killed;
  reason.textContent = r.killed ? `Trading halted: ${r.kill_reason}. Only closing orders are allowed.` : "";
  $("killBtn").hidden = !!r.killed;
  $("resetKillBtn").hidden = !r.killed;

  const rows = st.fills.map((f) => {
    const t = new Date(f.ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
    const cancelled = num(f.cancelled) > 0 ? ` <span class="muted">(${fmtSize(f.cancelled)} cxl)</span>` : "";
    const pa = num(f.position_after);
    return `<tr><td>${t}</td><td class="${f.side === "buy" ? "up" : "down"}">${f.side === "buy" ? "Buy" : "Sell"}</td>` +
      `<td class="r">${fmtSize(f.filled)}${cancelled}</td><td class="r">${fmtPx(f.vwap)}</td><td class="r">${money(f.notional)}</td>` +
      `<td><span class="tag taker">TAKER</span> ${pct(num(f.fee_rate), 2)}</td><td class="r">$${num(f.fee).toFixed(4)}</td>` +
      `<td class="r ${signCls(f.realized_pnl)}">${num(f.realized_pnl) === 0 ? "—" : money(f.realized_pnl, true)}</td>` +
      `<td class="r">${pa === 0 ? "Flat" : (pa > 0 ? "+" : "−") + fmtSize(Math.abs(pa))}</td></tr>`;
  });
  $("fillsBody").innerHTML = rows.length ? rows.join("") : `<tr><td colspan="9" class="muted empty">No paper fills yet.</td></tr>`;

  const ev = st.events.filter((e) => e.kind !== "rejected").slice(0, 5).map((e) => {
    const t = new Date(e.ts).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    if (e.kind === "funding") {
      const paid = num(e.paid);
      return `<div>${t} · funding ${pct(num(e.rate), 4)} on ${fmtSize(Math.abs(num(e.qty)))} ${num(e.qty) > 0 ? "long" : "short"}: ${paid > 0 ? "paid" : "received"} $${Math.abs(paid).toFixed(4)}</div>`;
    }
    if (e.kind === "kill_switch") return `<div class="bad">${t} · kill switch tripped: ${e.reason}</div>`;
    return "";
  });
  $("events").innerHTML = ev.join("");
}

async function refreshPaper() {
  try {
    state.paper = await getJSON("/api/paper/state");
    renderPaper();
  } catch (e) { /* snapshot status badge already reports API trouble */ }
}

function showResult(text, bad) {
  const el = $("orderResult");
  el.textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }) + " · " + text;
  el.className = "order-result small" + (bad ? " bad" : "");
  el.hidden = false;
}

async function paperAction(path, body, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await postJSON(path, body);
    if (res.fill) {
      const f = res.fill;
      showResult(`${f.side === "buy" ? "Bought" : "Sold"} ${fmtSize(f.filled)} @ ${fmtPx(f.vwap)} · taker fee $${num(f.fee).toFixed(4)}` +
        (num(f.cancelled) > 0 ? ` · ${fmtSize(f.cancelled)} cancelled (no liquidity)` : ""));
    }
    await refreshPaper();
    setTimeout(() => schedulePreview(0), 1500);
  } catch (e) {
    showResult("Rejected: " + e.message, true);
    await refreshPaper();
  } finally {
    if (btn) btn.disabled = false;
  }
}

function bindPaper() {
  document.querySelectorAll("[data-side]").forEach((b) => b.addEventListener("click", () => {
    state.side = b.dataset.side;
    document.querySelectorAll("[data-side]").forEach((x) => x.classList.toggle("on", x === b));
    schedulePreview(0);
  }));
  document.querySelectorAll("[data-otype]").forEach((b) => b.addEventListener("click", () => {
    state.otype = b.dataset.otype;
    document.querySelectorAll("[data-otype]").forEach((x) => x.classList.toggle("on", x === b));
    $("limitRow").hidden = state.otype !== "limit";
    if (state.otype === "limit" && !$("limitPx").value && state.snap) {
      const top = state.side === "buy" ? state.snap.book.asks[0] : state.snap.book.bids[0];
      if (top) $("limitPx").value = state.unit === "btc" ? Math.round(toUnit(top[0])) : num(top[0]).toFixed(4);
    }
    schedulePreview(0);
  }));
  document.querySelectorAll("[data-q]").forEach((b) => b.addEventListener("click", () => {
    $("size").value = b.dataset.q; schedulePreview(0);
  }));
  $("size").addEventListener("input", () => schedulePreview());
  $("limitPx").addEventListener("input", () => schedulePreview());
  $("submitBtn").addEventListener("click", (e) => paperAction("/api/paper/order", orderBody(), e.currentTarget));
  $("closeBtn").addEventListener("click", (e) => paperAction("/api/paper/close", {}, e.currentTarget));
  $("killBtn").addEventListener("click", () => paperAction("/api/paper/kill", {}));
  $("resetKillBtn").addEventListener("click", () => paperAction("/api/paper/reset_kill", {}));
  $("resetAcctBtn").addEventListener("click", () => {
    if (confirm("Reset the paper account? This clears the paper position, fills, and P&L.")) paperAction("/api/paper/reset", {});
  });
}

/* ---------------- strategy runner ---------------- */

async function refreshRunner() {
  let r;
  try { r = await getJSON("/api/runner"); } catch (e) { return; }
  const badge = $("runBadge");
  const label = badge.querySelector("span");
  badge.classList.remove("ok", "bad");
  $("runEmpty").hidden = !!r.exists;
  $("runBody").hidden = !r.exists;
  if (!r.exists) { label.textContent = "Not running"; $("runMeta").textContent = ""; return; }
  if (r.running) { badge.classList.add("ok"); label.textContent = "Running"; }
  else { badge.classList.add("bad"); label.textContent = "Stopped"; }
  $("runMeta").textContent = r.running ? `heartbeat ${r.age_s}s ago · ${r.bars_seen} bars · ${r.errors} errors`
    : `last heartbeat ${new Date(r.heartbeat).toLocaleString()}`;
  const p = r.params || {};
  const desc = {
    momentum: () => `momentum · ${p.lookback}m lookback · ±${p.threshold_bps} bps · size ${p.size}`,
    carry: () => `funding carry · avg of last ${p.window} rates · ±${p.threshold_bps} bps · size ${p.size}`,
    meanrev: () => `mean reversion · ${p.lookback}m · z ${p.entry_z} · size ${p.size}`,
  }[r.strategy];
  $("runStrat").textContent = desc ? desc() : r.strategy;
  const sig = r.signal_bps;
  const thr = p.threshold_bps ?? p.min_edge_bps;
  const sigLabel = r.strategy === "carry" ? "avg funding " : "";
  setText("runSignal", sig == null ? "warming up" : `${sigLabel}${sig >= 0 ? "+" : ""}${sig.toFixed(1)} bps` + (thr != null ? ` (acts at ±${thr})` : ""),
    sig == null || thr == null ? "" : Math.abs(sig) >= thr ? (sig > 0 ? "up" : "down") : "");
  $("runBar").textContent = r.last_bar ? new Date(r.last_bar).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) + " · mid " + fmtPx(r.last_mid) : "—";
  const st = r.paper, pos = st.position, q = num(pos.qty);
  $("runPos").textContent = q === 0 ? "Flat" : `${q > 0 ? "Long" : "Short"} ${fmtSize(Math.abs(q))} @ ${fmtPx(pos.avg_entry)}`;
  setText("runUpnl", money(pos.unrealized_pnl, true), signCls(pos.unrealized_pnl));
  setText("runReal", money(pos.realized_pnl, true), signCls(pos.realized_pnl));
  const fp = num(pos.funding_paid);
  $("runCosts").textContent = `$${num(pos.fees_paid).toFixed(4)} · ${fp > 0 ? "paid" : fp < 0 ? "recv" : ""} $${Math.abs(fp).toFixed(4)}`;
  const eq = num(st.equity), start = num(st.starting_cash);
  const pctTxt = (x) => `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x * 100).toFixed(2)}%`;
  $("runEq").innerHTML = `${money(eq)} <span class="${signCls(eq - start)}">(${money(eq - start, true)})</span>`;
  const tot = (eq - start) / start;
  setText("runRet", pctTxt(tot), signCls(tot));
  const started = r.started_at ? new Date(r.started_at) : null;
  $("runRetSub").textContent = `${money(eq - start, true)} on ${money(start)}`;
  const dayStart = num(st.risk.day_start_equity), daily = num(st.risk.daily_pnl);
  const dayPct = dayStart ? daily / dayStart : 0;
  setText("runRetDay", pctTxt(dayPct), signCls(dayPct));
  $("runRetDaySub").textContent = money(daily, true);
  const lev = eq ? Math.abs(num(pos.notional)) / eq : 0;
  $("runLev").textContent = lev.toFixed(2) + "×";
  $("runLevSub").textContent = `${money(Math.abs(num(pos.notional)))} notional`;
  setText("runKill", st.risk.killed ? "ON: " + st.risk.kill_reason : `armed · today ${money(st.risk.daily_pnl, true)}`,
    st.risk.killed ? "down" : "");
  renderHistory(st);
  const log = $("runLog");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 20;
  log.textContent = (r.log || []).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function renderHistory(st) {
  $("historyCard").hidden = false;
  const when = (iso) => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const fills = st.fills || [];
  $("histTrades").innerHTML = fills.length ? fills.map((f) => {
    const pa = num(f.position_after), rp = num(f.realized_pnl);
    const cxl = num(f.cancelled) > 0 ? ` <span class="muted">(${fmtSize(f.cancelled)} cxl)</span>` : "";
    return `<tr><td>${when(f.ts)}</td><td class="${f.side === "buy" ? "up" : "down"}">${f.side === "buy" ? "Buy" : "Sell"}</td>` +
      `<td class="r">${fmtSize(f.filled)}${cxl}</td><td class="r">${fmtPx(f.vwap)}</td><td class="r">${money(f.notional)}</td>` +
      `<td><span class="tag taker">TAKER</span> ${pct(num(f.fee_rate), 2)}</td><td class="r">$${num(f.fee).toFixed(4)}</td>` +
      `<td class="r ${signCls(rp)}">${rp === 0 ? "—" : money(rp, true)}</td>` +
      `<td class="r">${pa === 0 ? "Flat" : (pa > 0 ? "Long " : "Short ") + fmtSize(Math.abs(pa))}</td></tr>`;
  }).join("") : `<tr><td colspan="9" class="muted empty">No trades yet.</td></tr>`;

  const fund = (st.events || []).filter((e) => e.kind === "funding");
  $("histFunding").innerHTML = fund.length ? fund.map((e) => {
    const paid = num(e.paid), q = num(e.qty);
    return `<tr><td>${when(e.funding_time)}</td><td class="r">${pct(num(e.rate), 4)}</td>` +
      `<td class="r">${q > 0 ? "Long " : "Short "}${fmtSize(Math.abs(q))}</td><td class="r">${fmtPx(e.mark)}</td>` +
      `<td class="r ${paid < 0 ? "up" : "down"}">${paid < 0 ? "+" : "−"}$${Math.abs(paid).toFixed(2)} ${paid < 0 ? "received" : "paid"}</td></tr>`;
  }).join("") : `<tr><td colspan="5" class="muted empty">No funding settled yet.</td></tr>`;

  const recv = fund.reduce((a, e) => a - num(e.paid), 0);
  const fees = fills.reduce((a, f) => a + num(f.fee), 0);
  $("histMeta").textContent = `${fills.length} trade${fills.length === 1 ? "" : "s"} · fees $${fees.toFixed(2)} · ` +
    `${fund.length} funding payment${fund.length === 1 ? "" : "s"} · net funding ${recv >= 0 ? "+" : "−"}$${Math.abs(recv).toFixed(2)}`;
}

/* ---------------- runner P&L charts ---------------- */

const pnl = { data: null, hover: { pnlRet: null, pnlParts: null } };

function lineChart(id, xs, series, fmtY, tipFmt) {
  const cv = $(id);
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H || !xs.length) return;
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const col = { grid: cssVar("--grid"), muted: cssVar("--muted"), line: cssVar("--line") };
  const padL = 58, padR = 10, padT = 8, padB = 22;
  const pw = W - padL - padR, ph = H - padT - padB;
  let lo = 0, hi = 0;
  for (const s of series) for (const v of s.ys) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
  const x0 = xs[0], x1 = xs[xs.length - 1] || x0 + 1;
  const X = (t) => padL + ((t - x0) / (x1 - x0 || 1)) * pw;
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * ph;
  ctx.font = "11px " + cssVar("--mono");
  ctx.textBaseline = "middle"; ctx.textAlign = "right";
  const step = niceStep(hi - lo, 4);
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    const yy = Math.round(Y(v)) + 0.5;
    ctx.strokeStyle = Math.abs(v) < step / 1e6 ? col.line : col.grid;
    ctx.lineWidth = Math.abs(v) < step / 1e6 ? 1.5 : 1;
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(W - padR, yy); ctx.stroke();
    ctx.fillStyle = col.muted; ctx.fillText(fmtY(v), padL - 6, yy);
  }
  ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
  const ticks = Math.min(5, Math.max(2, Math.floor(pw / 120)));
  for (let i = 0; i <= ticks; i++) {
    const t = x0 + ((x1 - x0) * i) / ticks;
    const d = new Date(t * 1000);
    const label = (x1 - x0) > 2 * 86400 ? d.toLocaleDateString([], { month: "short", day: "numeric" })
      : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    ctx.fillStyle = col.muted; ctx.fillText(label, Math.min(Math.max(X(t), padL + 20), W - padR - 20), H - 5);
  }
  for (const s of series) {
    ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 2; ctx.lineJoin = "round"; ctx.setLineDash(s.dash || []);
    ctx.beginPath();
    // Break the line across data gaps (Kalshi publishes no candle for some stretches).
    xs.forEach((t, i) => {
      const gap = i && t - xs[i - 1] > (pnl.data.interval_min || 1) * 60 * 15;
      (i && !gap) ? ctx.lineTo(X(t), Y(s.ys[i])) : ctx.moveTo(X(t), Y(s.ys[i]));
    });
    ctx.stroke();
  }
  ctx.setLineDash([]);
  const hi_ = pnl.hover[id];
  if (hi_ !== null && hi_ !== undefined && xs[hi_] !== undefined) {
    const xx = Math.round(X(xs[hi_])) + 0.5;
    ctx.strokeStyle = col.muted; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(xx, padT); ctx.lineTo(xx, padT + ph); ctx.stroke(); ctx.setLineDash([]);
    for (const s of series) {
      ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(xx, Y(s.ys[hi_]), 3.5, 0, Math.PI * 2); ctx.fill();
    }
    const tip = $(id + "Tip");
    tip.textContent = tipFmt(hi_);
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = (xx + 12 + tw > W ? xx - tw - 12 : xx + 12) + "px";
    tip.style.top = "6px";
  }
  cv._layout = { X, xs, padL, pw };
}

function drawPnl() {
  const d = pnl.data;
  if (!d || !d.points || !d.points.length) {
    $("pnlNote").textContent = "P&L charts appear after the runner's first trade.";
    return;
  }
  const start = d.starting_cash;
  const xs = d.points.map((p) => p[0]);
  const net = d.points.map((p) => p[1] - start);
  const fund = d.points.map((p) => p[2]);
  const priceFees = net.map((n, i) => n - fund[i]);
  const ret = net.map((n) => (n / start) * 100);
  const last = ret[ret.length - 1];
  setText("pnlRetNow", `${last >= 0 ? "+" : "−"}${Math.abs(last).toFixed(2)}% · ${money(net[net.length - 1], true)}`, signCls(last));
  const when = (i) => new Date(xs[i] * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const pctFmt = (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(Math.abs(v) < 10 ? 2 : 1)}%`;
  lineChart("pnlRet", xs, [{ ys: ret, color: cssVar("--accent"), width: 2.2 }], pctFmt,
    (i) => `${when(i)}\nreturn ${pctFmt(ret[i])}  (${money(net[i], true)})`);
  const usd = (v) => (v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  lineChart("pnlParts", xs, [
    { ys: fund, color: cssVar("--bid"), width: 2 },
    { ys: priceFees, color: cssVar("--accent"), width: 2, dash: [5, 4] },
    { ys: net, color: cssVar("--text"), width: 2.2 },
  ], usd, (i) => `${when(i)}\nfunding    ${money(fund[i], true)}\nprice+fees ${money(priceFees[i], true)}\nnet        ${money(net[i], true)}`);
  $("pnlNote").textContent = `Rebuilt from the runner's fills and funding payments, marked at each ${d.interval_min === 1 ? "minute's" : d.interval_min === 60 ? "hour's" : "day's"} bid/ask mid. Funding steps up at each payment; price + fees moves with BTC (the account is short).`;
}

async function refreshPnl() {
  try { pnl.data = await getJSON("/api/runner/history"); drawPnl(); } catch (e) { /* card still shows tables */ }
}

function bindPnl() {
  for (const id of ["pnlRet", "pnlParts"]) {
    const cv = $(id);
    cv.addEventListener("mousemove", (ev) => {
      const L = cv._layout; if (!L) return;
      const mx = ev.clientX - cv.getBoundingClientRect().left;
      let best = 0, bd = Infinity;
      L.xs.forEach((t, i) => { const dd = Math.abs(L.X(t) - mx); if (dd < bd) { bd = dd; best = i; } });
      pnl.hover[id] = best; drawPnl();
    });
    cv.addEventListener("mouseleave", () => { pnl.hover[id] = null; $(id + "Tip").hidden = true; drawPnl(); });
    new ResizeObserver(drawPnl).observe(cv);
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
      const prevUnit = state.unit;
      state.unit = b.dataset.unit;
      document.querySelectorAll("[data-unit]").forEach((x) => x.classList.toggle("on", x === b));
      const lp = num($("limitPx").value);
      if (lp !== null && prevUnit !== state.unit) {
        $("limitPx").value = state.unit === "btc" ? Math.round(lp * CONTRACTS_PER_BTC) : (lp / CONTRACTS_PER_BTC).toFixed(4);
      }
      $("limitUnit").textContent = state.unit === "btc" ? "BTC $" : "per contract";
      renderQuotes(); renderBook(); renderFundingStatic(); drawChart(); refreshAccount(); renderPaper(); schedulePreview(0);
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

// Poll at full speed while the tab is visible; in a hidden tab slow down to every 30s
// instead of stopping (some embedded views and mobile browsers report "hidden" while on screen).
function every(ms, fn) {
  let last = 0;
  setInterval(() => {
    const now = Date.now();
    if (document.hidden && now - last < Math.max(ms, 30000)) return;
    last = now;
    fn();
  }, ms);
}

async function main() {
  chart.canvas = $("chart");
  chart.ctx = chart.canvas.getContext("2d");
  bindControls();
  try {
    await loadConfig();
    $("feeSource").textContent = `Taker rate ${pct(num(state.cfg.taker_fee_rate), 2)} from ${state.cfg.fee_source}.`;
  } catch (e) {
    state.error = e.message;
    renderStatus();
  }
  const pub = !!(state.cfg && state.cfg.public);
  document.body.classList.toggle("public", pub);
  if (pub && state.cfg.repo_url) $("repoLink").href = state.cfg.repo_url;
  await Promise.all([refreshSnapshot(), refreshCandles()]);
  refreshAccount();
  refreshRunner();
  bindPnl();
  refreshPnl();
  every(60000, refreshPnl);
  if (pub) {
    // Read-only view: no manual paper account, and gentler polling.
    every(2000, refreshSnapshot);   // ~600 bytes gzipped per update
    every(15000, refreshRunner);
    every(300000, refreshCandles);
    every(60000, refreshAccount);
  } else {
    bindPaper();
    refreshPaper();
    schedulePreview(0);
    every(1000, refreshSnapshot);
    every(2000, refreshPaper);
    every(3000, refreshRunner);
    every(3000, () => schedulePreview(0));
    every(30000, refreshCandles);
    every(10000, refreshAccount);
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { refreshSnapshot(); refreshRunner(); }
  });
  setInterval(() => { tickCountdown(); renderStatus(); }, 250);
}

main();
