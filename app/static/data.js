(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const n = new Intl.NumberFormat("en-IN");
  let submitter = null, polling = null;

  async function getJSON(url, opts) {
    const r = await fetch(url, opts);
    let b = null; try { b = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error((b && b.error) || `Request failed (${r.status})`);
    return b;
  }

  $("bt-form").addEventListener("click", (e) => { if (e.target.matches("button[type=submit]")) submitter = e.target; });
  $("bt-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = Object.fromEntries(f.entries());
    body.slippage_pct = (parseFloat(body.slippage_pct_ui) || 0) / 100; delete body.slippage_pct_ui;
    const kind = submitter ? submitter.dataset.download : "1";
    body.download = kind === "1";
    try {
      await getJSON(kind === "free" ? "/api/free-data" : "/api/backtest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      poll();
    } catch (err) { showJob({ status: "error", message: err.message }); }
  });

  function showJob(s) {
    $("job").classList.remove("hidden");
    const busy = s.status === "running";
    for (const b of document.querySelectorAll("#bt-form button")) b.disabled = busy;
    const pct = s.total ? Math.round((s.done / s.total) * 100) : busy ? 3 : 100;
    $("job-bar").style.width = pct + "%";
    $("job-bar").style.background = s.status === "error" ? "var(--neg)" : "";
    const stage = { spot: "NIFTY candles", expiries: "Expiry calendar", contracts: "Contract lists", options: "Option candles", simulate: "Simulating", download: "Downloading free data", validate: "Checking data", store: "Saving data" }[s.stage] || "Starting";
    $("job-title").textContent = s.status === "running" ? stage : s.status === "done" ? "Done" : s.status === "error" ? "Failed" : "Idle";
    $("job-count").textContent = s.total && busy ? `${n.format(s.done)} / ${n.format(s.total)}` : "";
    let msg = esc(s.message || "");
    if (s.status === "done" && s.result && s.result.run_id) msg = `Run #${s.result.run_id} is ready. <a href="/backtest?run=${s.result.run_id}">Open the dashboard →</a>`;
    if (s.status === "done" && s.result && s.result.data_quality) {
      const q = s.result.data_quality;
      const warn = q.weekdays_without_data + q.incomplete_spot_days + q.strike_not_in_dataset + q.no_candles_that_day + q.sparse_contract_days + q.days_without_expiry;
      msg += `<div style="margin-top:8px">Imported ${q.trading_days} trading days (${esc(q.first_day)} → ${esc(q.last_day)}), ${n.format(q.option_contract_days_imported)} of ${n.format(q.option_contract_days_needed)} option contract-days.` +
        (warn ? ` <b>Data gaps:</b> ${q.weekdays_without_data} weekdays without index data, ${q.incomplete_spot_days} incomplete index days, ${q.strike_not_in_dataset} strikes not in dataset, ${q.no_candles_that_day} contract-days with no candles, ${q.sparse_contract_days} sparse, ${q.days_without_expiry} days without expiry.` : " No gaps found.") +
        ` <a href="/api/free-data/report" target="_blank">Full report</a></div>`;
    }
    if (s.status === "error" && /GROWW_API_KEY|TOTP|login|auth/i.test(s.message || "")) msg += ' · <a href="/">Connect Groww</a>';
    $("job-msg").innerHTML = msg;
  }

  async function poll() {
    clearTimeout(polling);
    try {
      const s = await getJSON("/api/job");
      if (s.status !== "idle") showJob(s);
      if (s.status === "running") polling = setTimeout(poll, 1500);
      else { loadStats(); loadRuns(); }
    } catch (e) { polling = setTimeout(poll, 4000); }
  }

  async function loadStats() {
    try {
      const s = await getJSON("/api/data/summary");
      const stat = (k, v) => `<div class="stat"><span>${k}</span><b class="num">${v}</b></div>`;
      $("data-stats").innerHTML = s.spot_rows
        ? stat("NIFTY 1m candles", n.format(s.spot_rows)) + stat("From", esc((s.spot_first || "").slice(0, 10))) +
          stat("To", esc((s.spot_last || "").slice(0, 10))) + stat("Option contracts", n.format(s.option_symbols)) +
          stat("Option 1m candles", n.format(s.option_rows))
        : '<div class="muted">Nothing downloaded yet.</div>';
    } catch (e) { $("data-stats").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
  }

  async function loadRuns() {
    try {
      const runs = await getJSON("/api/runs");
      if (!runs.length) { $("runs").innerHTML = '<tr><td class="muted">No runs yet.</td></tr>'; return; }
      $("runs").innerHTML = "<tr><th>#</th><th>Kind</th><th>Dates</th><th>Status</th><th>Settings</th><th></th></tr>" + runs.map((r) => {
        const p = r.params || {};
        const st = r.status === "done" ? '<span class="pill yes">done</span>' : r.status === "failed" ? '<span class="pill off">failed</span>' : '<span class="pill no">' + esc(r.status) + "</span>";
        const settings = `slip ${((p.slippage_pct || 0) * 100).toFixed(2)}% + ${p.slippage_points} pts · ${p.lots}×${p.lot_size}` + (r.kind === "paper" ? ` · ${p.timeframe}m ${p.start}-${p.cutoff}` : "");
        return `<tr><td>${r.id}</td><td>${esc(r.kind)}</td><td class="num">${esc(r.date_from)} → ${esc(r.date_to)}</td><td>${st}<div class="muted">${esc(r.message || "")}</div></td>
          <td class="muted">${esc(settings)}</td><td>${r.status === "done" ? `<a href="/backtest?run=${r.id}">View</a> · <a href="/export/run/${r.id}.csv">CSV</a>` : ""}</td></tr>`;
      }).join("");
    } catch (e) { $("runs").innerHTML = `<tr><td class="muted">${esc(e.message)}</td></tr>`; }
  }

  poll(); loadStats(); loadRuns();
})();
