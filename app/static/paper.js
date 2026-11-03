(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inr = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const signed = (v) => (v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + "₹" + inr.format(Math.abs(v)));
  const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  const STATUS = { stopped: "Stopped", starting: "Starting…", running: "Running", waiting_open: "Waiting for 09:15", market_closed: "Market closed (weekend)", done: "Day complete" };

  async function call(url, body) {
    const r = await fetch(url, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
    let b = null; try { b = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error((b && b.error) || `Request failed (${r.status})`);
    return b;
  }

  function render(s) {
    const stat = (k, v) => `<div class="stat"><span>${k}</span><b class="num">${v}</b></div>`;
    $("stats").innerHTML = stat("Engine", esc(STATUS[s.status] || s.status)) + stat("Last NIFTY bar", esc(s.last_bar || "–")) +
      stat("Closed trades", s.closed_trades) + `<div class="stat"><span>Closed net PnL</span><b class="num ${cls(s.closed_net)}">${signed(s.closed_net)}</b></div>`;
    let html = "";
    if (s.error) html += `<div class="flash error">${esc(s.error)}</div>`;
    if (s.positions.length) {
      html += "<table><tr><th>Mode</th><th>Position</th><th class='num'>Entry</th><th class='num'>Last</th><th class='num'>Gross MTM</th></tr>" +
        s.positions.map((p) => `<tr><td>${esc(p.mode)}</td><td>${esc(p.side)} ${esc(p.symbol)} @ ${esc(p.entry_time)}</td><td class="num">${inr.format(p.entry)}</td>
          <td class="num">${p.ltp == null ? "–" : inr.format(p.ltp)}</td><td class="num ${cls(p.mtm_gross)}">${signed(p.mtm_gross)}</td></tr>`).join("") + "</table>";
    } else html += '<div class="muted">No open positions.</div>';
    if (s.run_id) html += `<p><a href="/backtest?run=${s.run_id}">Audit today's paper trades on the chart →</a></p>`;
    $("positions").innerHTML = html;
    $("log").textContent = s.log.length ? s.log.join("\n") : "No activity yet.";
    $("start").disabled = s.running; $("stop").disabled = !s.running;
    if (s.config && s.running) {
      const f = $("paper-form");
      f.timeframe.value = s.config.timeframe; f.start.value = s.config.start; f.cutoff.value = s.config.cutoff;
      for (const c of f.querySelectorAll("input[name=modes]")) c.checked = s.config.modes.includes(c.value);
    }
  }
  async function refresh() {
    try { render(await call("/api/paper")); $("msg").textContent = ""; }
    catch (e) { $("msg").textContent = e.message; }
    setTimeout(refresh, 5000);
  }
  $("paper-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const modes = [...f.querySelectorAll("input[name=modes]:checked")].map((c) => c.value);
    if (!modes.length) { $("msg").textContent = "Pick at least one mode."; return; }
    try { render(await call("/api/paper/start", { timeframe: +f.timeframe.value, start: f.start.value, cutoff: f.cutoff.value, modes })); }
    catch (err) { $("msg").textContent = err.message; }
  });
  $("stop").addEventListener("click", async () => {
    try { render(await call("/api/paper/stop", {})); } catch (err) { $("msg").textContent = err.message; }
  });
  refresh();
})();
