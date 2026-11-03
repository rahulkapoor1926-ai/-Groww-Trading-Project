/* Backtest dashboard: filters, KPIs, trade list, chart and cost card. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  const inr0 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
  const money = (v, dp = true) => (v == null ? "–" : (v < 0 ? "−₹" : "₹") + (dp ? inr : inr0).format(Math.abs(v)));
  const signed = (v) => (v == null ? "–" : (v > 0 ? "+" : "") + money(v));
  const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const MODE_SHORT = { ITM5_BUY: "ITM5 buy", ATM_BUY: "ATM buy", ATM_SELL: "ATM sell" };
  const REASON = { opposite_cross: "Opposite crossover", square_off: "15:00 square-off" };

  const state = { run: null, tf: "15", mode: "ITM5_BUY", start: "09:30", cutoff: "14:30", outcome: "all", cross: "all",
                  trades: [], sel: -1, chartSrc: "spot", data: null };
  const tradeCache = new Map();
  let summaryCtl = null;

  // ---------- URL state ----------
  function readUrl() {
    const p = new URLSearchParams(location.search);
    for (const k of ["run", "tf", "mode", "start", "cutoff", "outcome", "cross"]) if (p.get(k)) state[k] = p.get(k);
    try { const s = localStorage.getItem("chartSrc"); if (s) state.chartSrc = s; } catch (e) {}
  }
  function writeUrl() {
    const p = new URLSearchParams();
    for (const k of ["run", "tf", "mode", "start", "cutoff", "outcome", "cross"]) if (state[k] != null) p.set(k, state[k]);
    history.replaceState(null, "", "?" + p.toString());
  }

  // ---------- filters ----------
  function syncControls() {
    for (const b of $("f-tf").querySelectorAll("button")) b.classList.toggle("on", b.dataset.v === String(state.tf));
    $("f-mode").value = state.mode; $("f-start").value = state.start; $("f-cutoff").value = state.cutoff;
    $("f-outcome").value = state.outcome; $("f-cross").value = state.cross;
    if (state.run) $("f-run").value = state.run;
    for (const b of $("chart-src").querySelectorAll("button")) b.classList.toggle("on", b.dataset.v === state.chartSrc);
  }
  function lockForPaper(run) {
    const paper = run && run.kind === "paper";
    for (const id of ["f-start", "f-cutoff"]) $(id).disabled = paper;
    for (const b of $("f-tf").querySelectorAll("button")) b.disabled = paper;
    if (paper) {
      state.tf = String(run.params.timeframe); state.start = run.params.start; state.cutoff = run.params.cutoff;
      for (const [id, v] of [["f-start", state.start], ["f-cutoff", state.cutoff]]) {
        if (![...$(id).options].some((o) => o.value === v)) $(id).add(new Option(v, v));
      }
    }
  }
  function bindFilters() {
    $("f-tf").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b || b.disabled) return; state.tf = b.dataset.v; changed(); });
    for (const [id, key] of [["f-mode", "mode"], ["f-start", "start"], ["f-cutoff", "cutoff"], ["f-outcome", "outcome"], ["f-cross", "cross"]]) {
      $(id).addEventListener("change", (e) => { state[key] = e.target.value; changed(); });
    }
    $("f-run").addEventListener("change", (e) => { state.run = e.target.value; changed(); });
    $("chart-src").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      state.chartSrc = b.dataset.v; try { localStorage.setItem("chartSrc", state.chartSrc); } catch (x) {}
      syncControls(); const t = state.trades[state.sel]; if (t) showTrade(t.id);
    });
  }
  function changed() { syncControls(); writeUrl(); loadSummary(); }

  // ---------- page states ----------
  function pageState(kind, title, body) {
    const el = $("page-state");
    if (!kind) { el.classList.add("hidden"); $("results").classList.remove("hidden"); return; }
    el.className = "card" + (kind === "error" ? " error" : "");
    el.innerHTML = `<div class="state">${kind === "loading" ? '<div class="spinner"></div>' : ""}<h3>${esc(title)}</h3><div>${body || ""}</div></div>`;
    $("results").classList.toggle("hidden", kind !== "loading-inline");
  }

  async function getJSON(url, opts) {
    const r = await fetch(url, opts);
    let body = null;
    try { body = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new Error((body && body.error) || `Request failed (${r.status})`);
    return body;
  }

  async function loadRuns() {
    const runs = await getJSON("/api/runs");
    const sel = $("f-run");
    sel.innerHTML = "";
    const done = runs.filter((r) => r.status === "done" || (r.kind === "paper" && r.status === "running"));
    for (const r of done) {
      const label = r.kind === "paper"
        ? `Paper ${r.date_from} (${r.params.timeframe}m)`
        : `#${r.id} · ${r.date_from} → ${r.date_to}`;
      sel.add(new Option(label, r.id));
    }
    state.runs = done;
    if (!done.length) return false;
    if (!state.run || !done.some((r) => String(r.id) === String(state.run))) {
      const bt = done.find((r) => r.kind === "backtest") || done[0];
      state.run = String(bt.id);
    }
    return true;
  }

  // ---------- summary ----------
  async function loadSummary() {
    if (summaryCtl) summaryCtl.abort();
    summaryCtl = new AbortController();
    const run = (state.runs || []).find((r) => String(r.id) === String(state.run));
    lockForPaper(run); syncControls();
    document.body.classList.add("loading");
    setSkeleton(true);
    const p = new URLSearchParams({ run: state.run, tf: state.tf, mode: state.mode, start: state.start,
                                    cutoff: state.cutoff, outcome: state.outcome, cross: state.cross });
    try {
      const d = await getJSON("/api/summary?" + p, { signal: summaryCtl.signal });
      if (d.empty) { pageState("empty", "No backtest yet", 'Open the <a href="/data">Data</a> page, pick dates and tap <b>Download &amp; run</b>.'); return; }
      pageState(null);
      state.data = d;
      renderSummary(d);
    } catch (e) {
      if (e.name === "AbortError") return;
      pageState("error", "Couldn't load results", esc(e.message) + ' <br><button class="btn ghost sm" id="retry" style="margin-top:12px">Retry</button>');
      const r = $("retry"); if (r) r.onclick = loadSummary;
    } finally { setSkeleton(false); }
  }
  function setSkeleton(on) {
    for (const el of document.querySelectorAll(".kpi b, .mode-card .big")) el.classList.toggle("skeleton", on);
  }

  function renderSummary(d) {
    const m = d.metrics;
    $("k-trades").textContent = m.trades;
    $("k-wl").textContent = `${m.wins} won · ${m.losses} lost`;
    $("k-winrate").textContent = m.win_rate == null ? "–" : m.win_rate.toFixed(1) + "%";
    setMoney("k-net", m.net); $("k-netsub").textContent = m.trades ? `${signed(m.expectancy)} per trade` : "";
    setMoney("k-gross", m.gross); $("k-friction").textContent = `friction ${money(m.friction)}`;
    $("k-pf").textContent = m.profit_factor == null ? (m.wins ? "∞" : "–") : m.profit_factor.toFixed(2);
    $("k-payoff").textContent = m.payoff == null ? "–" : m.payoff.toFixed(2);
    setMoney("k-exp", m.expectancy);
    $("k-dd").textContent = money(-m.max_dd); $("k-dd").className = "num" + (m.max_dd ? " neg" : "");
    $("k-ddp").textContent = m.max_dd_pct.toFixed(2) + "%";
    $("k-cap").textContent = `of peak, start ${money(m.capital, false)}`;
    $("k-avg").innerHTML = `<span class="pos">${money(m.avg_win, false)}</span> <span class="muted">/</span> <span class="neg">${money(m.avg_loss, false)}</span>`;

    // mode comparison
    $("modes").innerHTML = Object.entries(d.modes).map(([k, v]) => `
      <button type="button" class="mode-card ${k === state.mode ? "on" : ""}" data-mode="${k}">
        <div class="t"><span>${esc(d.mode_labels[k])}</span><span class="big num ${cls(v.net)}">${signed(v.net)}</span></div>
        <div class="row num"><span>${v.trades} trades</span><span>win ${v.win_rate == null ? "–" : v.win_rate.toFixed(1) + "%"}</span>
        <span>PF ${v.profit_factor == null ? (v.wins ? "∞" : "–") : v.profit_factor.toFixed(2)}</span><span>DD ${money(-v.max_dd, false)}</span></div>
      </button>`).join("");
    for (const b of $("modes").querySelectorAll(".mode-card")) b.onclick = () => { state.mode = b.dataset.mode; changed(); };

    // trade list
    state.trades = d.trades;
    $("list-title").textContent = `Trades (${d.trades.length})`;
    const list = $("trade-list");
    if (!d.trades.length) {
      list.innerHTML = `<div class="state"><h3>No trades match</h3>Try another time window, outcome or cross filter.</div>`;
    } else {
      list.innerHTML = d.trades.map((t, i) => `
        <div class="trow" role="option" data-i="${i}" id="tr-${i}">
          <div class="a">${t.day} <span class="muted">${t.entry}→${t.exit}</span></div>
          <div class="pnl num ${cls(t.net)}">${signed(t.net)}</div>
          <div class="b"><span class="badge ${t.direction > 0 ? "long" : "short"}">${t.direction > 0 ? "▲ Bull" : "▼ Bear"}</span>
            ${t.side === "SELL" ? "Sell" : "Buy"} ${t.strike} ${t.opt_type} · #${t.cross_no}
            ${t.whipsaw ? '<span class="badge whip">whipsaw</span>' : ""}</div>
          <div class="b" style="text-align:right">${t.exit_reason === "square_off" ? "15:00" : "cross"}</div>
        </div>`).join("");
    }
    renderEquity(d);
    renderDays(d.days);
    // keep the same trade selected if still present
    const prevId = state.selId;
    let idx = d.trades.findIndex((t) => t.id === prevId);
    if (idx < 0) idx = d.trades.length ? 0 : -1;
    select(idx, false);
  }
  function setMoney(id, v) { const el = $(id); el.textContent = signed(v); el.className = "num " + cls(v); }

  // ---------- selection ----------
  function select(i, scroll = true) {
    const list = $("trade-list");
    const prev = list.querySelector(".trow.sel"); if (prev) { prev.classList.remove("sel"); prev.removeAttribute("aria-selected"); }
    state.sel = i;
    if (i < 0) { state.selId = null; clearTrade(); return; }
    const row = $("tr-" + i);
    if (row) { row.classList.add("sel"); row.setAttribute("aria-selected", "true"); if (scroll) row.scrollIntoView({ block: "nearest" }); }
    const t = state.trades[i];
    state.selId = t.id;
    $("k-sel").textContent = signed(t.net); $("k-sel").className = "num " + cls(t.net);
    $("k-selsub").textContent = `${t.day} ${t.entry} · ${t.strike} ${t.opt_type}`;
    showTrade(t.id);
    for (const j of [i - 1, i + 1]) if (state.trades[j]) fetchTrade(state.trades[j].id).catch(() => {});
  }
  function bindList() {
    $("trade-list").addEventListener("click", (e) => {
      const r = e.target.closest(".trow"); if (!r) return;
      select(+r.dataset.i);
      if (window.innerWidth <= 860) $("meta").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    document.addEventListener("keydown", (e) => {
      if (e.target.matches("input, select, textarea")) return;
      if (e.key === "ArrowDown" || e.key === "j") { if (state.sel < state.trades.length - 1) { select(state.sel + 1); e.preventDefault(); } }
      else if (e.key === "ArrowUp" || e.key === "k") { if (state.sel > 0) { select(state.sel - 1); e.preventDefault(); } }
    });
  }

  // ---------- trade detail ----------
  function fetchTrade(id) {
    if (!tradeCache.has(id)) {
      const p = getJSON("/api/trade/" + id).catch((e) => { tradeCache.delete(id); throw e; });
      tradeCache.set(id, p);
      if (tradeCache.size > 400) tradeCache.delete(tradeCache.keys().next().value);
    }
    return tradeCache.get(id);
  }
  let showToken = 0;
  async function showTrade(id) {
    const token = ++showToken;
    const ov = $("chart-overlay");
    ov.innerHTML = '<div><div class="spinner"></div>Loading chart…</div>'; ov.classList.remove("hidden");
    try {
      const d = await fetchTrade(id);
      if (token !== showToken) return;
      ov.classList.add("hidden");
      renderMeta(d.trade); renderCost(d.trade); renderChart(d);
    } catch (e) {
      if (token !== showToken) return;
      ov.innerHTML = `<div class="state"><h3>Chart failed to load</h3>${esc(e.message)}</div>`;
    }
  }
  function clearTrade() {
    $("meta").innerHTML = '<span class="muted">No trade selected.</span>';
    $("cost").innerHTML = '<tr><td class="muted">No trade selected.</td></tr>';
    $("k-sel").textContent = "–"; $("k-sel").className = "num"; $("k-selsub").textContent = "none selected";
    if (chart) { candle.setData([]); e9.setData([]); e21.setData([]); }
  }
  const hm = (iso) => (iso ? iso.slice(11, 16) : "–");
  function renderMeta(t) {
    const dir = t.direction > 0 ? '<span class="badge long">▲ Bullish</span>' : '<span class="badge short">▼ Bearish</span>';
    const how = (h) => (h === "fallback" ? " <span class='badge' title='No candle at that minute; used the last traded price within 5 min'>LTP</span>"
                       : h === "stale" ? " <span class='badge whip' title='Price gap; used last traded price'>stale</span>" : "");
    const items = [
      ["Symbol", `${esc(t.symbol)}`],
      ["Direction", `${dir} ${t.side === "SELL" ? "Sell" : "Buy"} ${t.opt_type}`],
      ["NIFTY entry → exit", `${t.spot_entry ? inr.format(t.spot_entry) : "–"} → ${t.spot_exit ? inr.format(t.spot_exit) : "–"}`],
      ["Option entry → exit", `₹${inr.format(t.entry_raw)}${how(t.entry_how)} → ₹${inr.format(t.exit_raw)}${how(t.exit_how)}`],
      ["Fills (after slippage)", `₹${inr.format(t.entry_fill)} → ₹${inr.format(t.exit_fill)}`],
      ["Time", `${t.day} ${hm(t.entry_time)} → ${hm(t.exit_time)}`],
      ["Expiry / DTE", `${t.expiry} · ${t.dte}d`],
      ["Exit reason", REASON[t.exit_reason] || t.exit_reason],
      ["Cross #", `${t.cross_no}${t.reversal_entry ? " (reversal)" : ""}`],
      ["Net PnL", `<span class="${cls(t.net)}">${signed(t.net)}</span>`],
    ];
    $("meta").innerHTML = items.map(([k, v]) => `<div class="m"><span>${k}</span><b>${v}</b></div>`).join("")
      + (t.whipsaw ? '<span class="badge whip" title="Closed by the opposite crossover within 2 signal bars">⚠ whipsaw</span>' : "")
      + `<div class="nav-btns"><button class="btn ghost sm" id="prev" aria-label="Previous trade">↑ Prev</button><button class="btn ghost sm" id="next" aria-label="Next trade">Next ↓</button></div>`;
    $("prev").onclick = () => state.sel > 0 && select(state.sel - 1);
    $("next").onclick = () => state.sel < state.trades.length - 1 && select(state.sel + 1);
    $("prev").disabled = state.sel <= 0; $("next").disabled = state.sel >= state.trades.length - 1;
  }
  function renderCost(t) {
    const rows = [
      ["Gross PnL", t.gross, ""],
      ["Slippage", -t.slippage, "sub"],
      ["Brokerage (₹20 × 2 orders)", -t.brokerage, "sub"],
      ["STT (sell side)", -t.stt, "sub"],
      ["Exchange transaction charge", -t.exchange, "sub"],
      ["SEBI fee", -t.sebi, "sub"],
      ["Stamp duty (buy side)", -t.stamp, "sub"],
      ["GST (18%)", -t.gst, "sub"],
      ["Total friction", -t.friction, ""],
    ];
    $("cost").innerHTML = rows.map(([k, v, c]) => `<tr class="${c}"><td>${k}</td><td class="num ${c ? "" : cls(v)}">${signed(v)}</td></tr>`).join("")
      + `<tr class="total"><td>Net PnL</td><td class="num ${cls(t.net)}">${signed(t.net)}</td></tr>`;
  }

  // ---------- charts ----------
  let chart = null, candle, e9, e21, eqChart = null, eqLine;
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function chartOpts() {
    return {
      layout: { background: { type: "solid", color: "transparent" }, textColor: css("--muted"), fontFamily: "inherit" },
      grid: { vertLines: { color: css("--line") + "66" }, horzLines: { color: css("--line") + "66" } },
      rightPriceScale: { borderColor: css("--line") }, timeScale: { borderColor: css("--line"), timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 }, autoSize: true,
    };
  }
  function ensureChart() {
    if (chart || !window.LightweightCharts) return;
    chart = LightweightCharts.createChart($("chart"), chartOpts());
    candle = chart.addCandlestickSeries({ upColor: css("--pos"), downColor: css("--neg"), borderVisible: false,
                                          wickUpColor: css("--pos"), wickDownColor: css("--neg") });
    e9 = chart.addLineSeries({ color: css("--ema9"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 4 });
    e21 = chart.addLineSeries({ color: css("--ema21"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 4 });
  }
  function renderChart(d) {
    ensureChart(); if (!chart) return;
    const t = d.trade, c = d.chart, spot = state.chartSrc === "spot";
    const buyish = (t.side === "BUY");
    const entryColor = css("--info"), exitColor = css("--warn");
    let data, markers;
    if (spot) {
      data = c.spot; e9.setData(c.ema9); e21.setData(c.ema21);
      markers = [
        { time: c.entry_bar, position: t.direction > 0 ? "belowBar" : "aboveBar", color: entryColor, shape: t.direction > 0 ? "arrowUp" : "arrowDown", text: "Entry " + hm(t.entry_time) },
        { time: c.exit_bar, position: t.direction > 0 ? "aboveBar" : "belowBar", color: exitColor, shape: "circle", text: "Exit " + hm(t.exit_time) },
      ];
    } else {
      data = c.option; e9.setData([]); e21.setData([]);
      markers = [
        { time: c.entry_minute, position: buyish ? "belowBar" : "aboveBar", color: entryColor, shape: buyish ? "arrowUp" : "arrowDown", text: (buyish ? "Buy " : "Sell ") + t.entry_fill },
        { time: c.exit_minute, position: buyish ? "aboveBar" : "belowBar", color: exitColor, shape: buyish ? "arrowDown" : "arrowUp", text: (buyish ? "Sell " : "Buy ") + t.exit_fill },
      ];
    }
    $("chart-legend").classList.toggle("hidden", !spot);
    candle.setData(data);
    const times = new Set(data.map((x) => x.time));
    candle.setMarkers(markers.filter((m) => m.time != null && times.has(m.time)).sort((a, b) => a.time - b.time));
    chart.timeScale().fitContent();
    if (!data.length) {
      $("chart-overlay").innerHTML = '<div class="state"><h3>No candles stored for this view</h3></div>';
      $("chart-overlay").classList.remove("hidden");
    }
  }
  function renderEquity(d) {
    if (!window.LightweightCharts) return;
    if (!eqChart) {
      eqChart = LightweightCharts.createChart($("equity"), { ...chartOpts(), timeScale: { visible: false }, handleScroll: false, handleScale: false,
        localization: { priceFormatter: (v) => money(v, false) } });
      eqLine = eqChart.addAreaSeries({ lineColor: css("--info"), topColor: css("--info") + "40", bottomColor: css("--info") + "05", lineWidth: 2, priceLineVisible: false });
    }
    // one point per trade, spaced 1 minute apart so the axis is trade order
    const base = 1700000000;
    const pts = [{ time: base, value: d.metrics.capital }].concat(d.metrics.equity.map((v, i) => ({ time: base + 60 * (i + 1), value: v })));
    eqLine.setData(d.metrics.equity.length ? pts : []);
    eqChart.timeScale().fitContent();
  }
  window.addEventListener("themechange", () => {
    for (const ch of [chart, eqChart]) if (ch) ch.applyOptions(chartOpts());
    if (candle) candle.applyOptions({ upColor: css("--pos"), downColor: css("--neg"), wickUpColor: css("--pos"), wickDownColor: css("--neg") });
    if (e9) { e9.applyOptions({ color: css("--ema9") }); e21.applyOptions({ color: css("--ema21") }); }
    if (eqLine) eqLine.applyOptions({ lineColor: css("--info"), topColor: css("--info") + "40", bottomColor: css("--info") + "05" });
    const t = state.trades[state.sel]; if (t) showTrade(t.id);
  });

  // ---------- day log ----------
  const DAY_STATUS = { traded: "Traded", no_signal: "No cross in window", skipped: "Entry skipped", incomplete_data: "Incomplete data" };
  function renderDays(days) {
    const counts = {};
    for (const x of days) counts[x.status] = (counts[x.status] || 0) + 1;
    $("days-sub").textContent = `${days.length} days · ${counts.traded || 0} traded · ${counts.no_signal || 0} no-trade` +
      (counts.skipped ? ` · ${counts.skipped} skipped` : "") + (counts.incomplete_data ? ` · ${counts.incomplete_data} incomplete` : "");
    $("days").innerHTML = "<tr><th>Day</th><th>Status</th><th class='num'>Trades</th><th class='num'>Net</th><th class='num'>1m bars</th><th>Notes</th></tr>" +
      days.map((x) => `<tr><td>${x.day}</td><td>${DAY_STATUS[x.status]}${x.data === "incomplete" && x.status !== "incomplete_data" ? ' <span class="badge whip">gaps</span>' : ""}</td>
        <td class="num">${x.trades}</td><td class="num ${cls(x.net)}">${x.trades ? signed(x.net) : "–"}</td><td class="num">${x.minutes}</td>
        <td class="muted">${x.events.map(esc).join("<br>")}</td></tr>`).join("");
  }

  // ---------- boot ----------
  async function boot() {
    readUrl(); bindFilters(); bindList(); syncControls();
    pageState("loading", "Loading results…");
    try {
      const has = await loadRuns();
      if (!has) { pageState("empty", "No backtest yet", 'Open the <a href="/data">Data</a> page, pick a date range and tap <b>Download &amp; run</b>. Results show up here.'); return; }
      syncControls(); writeUrl();
      await loadSummary();
    } catch (e) {
      pageState("error", "Couldn't load results", esc(e.message) + ' <br><button class="btn ghost sm" onclick="location.reload()" style="margin-top:12px">Retry</button>');
    }
  }
  boot();
})();
