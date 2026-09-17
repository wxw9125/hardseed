/* ETH/USDT DEX 报价监控 · 前端逻辑 */

const PLATFORMS = ["Uniswap", "1inch", "CowSwap", "Tokenlon"];
const PLATFORM_COLORS = {
  "Uniswap": "#ff70a6",
  "1inch":   "#b794f4",
  "CowSwap": "#7ee787",
  "Tokenlon":"#58a6ff",
};
const RANK_COLORS = ["#7ee787", "#a5d6ff", "#d2a8ff", "#f85149"];

let charts = {};  // 保存所有 Chart 实例，便于 destroy 重建
let realMode = false;  // 是否启用真实 API 报价（前端 toggle，与 ?real=1 同义）

// ---------- helpers ----------
function fmtUSD(v) {
  if (v == null) return "—";
  if (Math.abs(v) >= 1e6) return (v/1e6).toFixed(3) + "M";
  if (Math.abs(v) >= 1e3) return (v/1e3).toFixed(2) + "K";
  return Number(v).toFixed(2);
}
function fmtBps(v) {
  if (v == null) return "—";
  return (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "bps";
}
function fmtNum(v, d=6) {
  if (v == null) return "—";
  return Number(v).toFixed(d);
}
function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));
}
function destroyCharts() {
  Object.values(charts).forEach(c => { try { c.destroy(); } catch(e){} });
  charts = {};
}

// ---------- 顶部状态 ----------
function renderStatus(data) {
  document.getElementById("ds").textContent  = data.data_source || "—";
  document.getElementById("snap").textContent = (data.snapshot_time || "—").replace("T"," ").replace(/\..*Z?$/," UTC");
  document.getElementById("ethp").textContent = data.eth_price ? "$" + fmtNum(data.eth_price, 2) : "—";
  document.getElementById("gas").textContent  = data.gas_gwei ? fmtNum(data.gas_gwei, 1) + " gwei" : "—";
  // 真实提供者健康状态
  const el = document.getElementById("providers");
  if (el) {
    const st = data.real_providers_status;
    if (!st || st._error) {
      el.textContent = st && st._error ? ("健康检查失败：" + st._error) : "—";
    } else {
      el.textContent = Object.entries(st).map(([k,v]) => `${k}=${v}`).join(" · ");
    }
  }
}

// ---------- 关键词解析结果展示 ----------
function renderParseInfo(parsed) {
  const el = document.getElementById("parse-info");
  if (!parsed) { el.classList.add("hidden"); el.textContent = ""; return; }
  el.classList.remove("hidden");
  const parts = [];
  if (parsed.platforms?.length) parts.push("平台=" + parsed.platforms.join("/"));
  if (parsed.directions?.length) parts.push("方向=" + parsed.directions.join("/"));
  if (parsed.amounts?.length) parts.push("金额=" + parsed.amounts.map(fmtUSD).join("/"));
  parts.push("price=" + parsed.eth_price, "gas=" + parsed.gas_gwei);
  if (parsed.unmatched?.length) {
    el.classList.add("warn");
    el.textContent = "解析: " + parts.join(" · ") + "  ⚠ 未识别: " + parsed.unmatched.join(" ");
  } else {
    el.classList.remove("warn");
    el.textContent = "解析: " + parts.join(" · ");
  }
}

// ---------- 主表 ----------
function renderMainTable(analysis) {
  const tbody = document.querySelector("#main-table tbody");
  tbody.innerHTML = "";
  const groups = analysis.group_stats || [];
  if (!groups.length) {
    tbody.innerHTML = `<tr><td colspan="13" class="empty">无数据</td></tr>`;
    return;
  }
  // 排序：方向 -> 金额升序
  groups.sort((a, b) => {
    if (a.direction !== b.direction) return a.direction < b.direction ? -1 : 1;
    return a.amount_usd - b.amount_usd;
  });
  for (const gs of groups) {
    for (const r of gs.rankings) {
      const gap = r.gap_vs_best_bps || 0;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(gs.direction)}</td>
        <td>${fmtUSD(gs.amount_usd)}</td>
        <td>${escapeHtml(r.platform)}</td>
        <td class="rank-${r.rank}">#${r.rank}</td>
        <td>${r.net_received_usd.toFixed(2)}</td>
        <td>${fmtNum(r.exec_price, 6)}</td>
        <td class="${r.rank===1 ? 'muted' : (gap>0?'neg':'pos')}">${fmtBps(gap)}</td>
        <td>${fmtNum(r.effective_spread_bps, 2)}</td>
        <td>${fmtNum(r.price_impact_bps, 2)}</td>
        <td>${r.gas_cost_usd.toFixed(2)}</td>
        <td>${'★'.repeat(r.routing_complexity)}</td>
        <td>${escapeHtml(r.mev_risk)}</td>
        <td>${escapeHtml(r.notes || "")}</td>
      `;
      tbody.appendChild(tr);
    }
  }
}

// ---------- 聚合器/CowSwap 对比表 ----------
function renderDiffTable(analysis) {
  const tbody = document.querySelector("#diff-table tbody");
  tbody.innerHTML = "";
  const rows = [];
  for (const r of (analysis.aggregator_vs_dex || [])) {
    rows.push({
      direction: r.direction, amount: r.amount_usd,
      cmp: r.aggregator + " vs Uniswap",
      bps: r.vs_uniswap_bps,
      note: r.interpretation,
    });
  }
  for (const r of (analysis.cowswap_vs_instant || [])) {
    rows.push({
      direction: r.direction, amount: r.amount_usd,
      cmp: "CowSwap vs " + r.vs,
      bps: r.diff_bps,
      note: r.note,
    });
  }
  rows.sort((a,b) => {
    if (a.direction !== b.direction) return a.direction < b.direction ? -1 : 1;
    return a.amount - b.amount;
  });
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">无对比数据</td></tr>`;
    return;
  }
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(r.direction)}</td>
      <td>${fmtUSD(r.amount)}</td>
      <td>${escapeHtml(r.cmp)}</td>
      <td class="${r.bps>0?'pos':'neg'}">${fmtBps(r.bps)}</td>
      <td>${escapeHtml(r.note)}</td>
    `;
    tbody.appendChild(tr);
  }
}

// ---------- 结论 ----------
function renderConclusions(analysis, data) {
  const ol = document.getElementById("conclusions");
  ol.innerHTML = "";
  const lines = buildConclusions(analysis);
  for (const ln of lines) {
    const li = document.createElement("li");
    li.textContent = ln;
    ol.appendChild(li);
  }
}

function buildConclusions(analysis) {
  const out = [];
  const freq = analysis.best_frequency_pct || {};
  // 每个方向、每金额最优
  const bestBy = {};
  for (const gs of (analysis.group_stats || [])) {
    bestBy[(gs.direction, gs.amount_usd)] = { dir: gs.direction, amt: gs.amount_usd, p: gs.best_platform };
  }
  for (const dir of ["ETH->USDT", "USDT->ETH"]) {
    const items = (analysis.group_stats || [])
      .filter(g => g.direction === dir)
      .map(g => `${fmtUSD(g.amount_usd)}→${g.best_platform}`);
    if (items.length) out.push(`[${dir}] 各金额区间最优平台：${items.join("；")}`);
  }
  const bestOverall = Object.entries(freq).sort((a,b)=>b[1]-a[1])[0];
  if (bestOverall) out.push(`整体最优频率最高平台：${bestOverall[0]} (${bestOverall[1]}%)，建议作为默认路由`);
  const tight = (analysis.group_stats||[]).filter(g => g.gap_best_vs_second_bps < 1);
  if (tight.length) {
    out.push(`高度竞争区间（最优-次优<1bps，路由切换敏感）：${tight.map(g=>`${g.direction}@${fmtUSD(g.amount_usd)}=${g.gap_best_vs_second_bps.toFixed(2)}bps`).join("；")}`);
  } else {
    out.push("当前无最优-次优<1bps 的高度竞争区间");
  }
  const arb = (analysis.group_stats||[]).filter(g => g.gap_best_vs_worst_bps > 30);
  if (arb.length) {
    out.push(`价差套利/路由优化机会（最优-最差>30bps）：${arb.map(g=>`${g.direction}@${fmtUSD(g.amount_usd)}:${g.best_platform}比${g.worst_platform}好${g.gap_best_vs_worst_bps.toFixed(1)}bps`).join("；")}`);
  }
  const big = (analysis.group_stats||[]).filter(g => g.amount_usd >= 1_000_000);
  if (big.length) {
    let worst = null;
    for (const g of big) for (const r of g.rankings) {
      if (!worst || r.price_impact_bps > worst.price_impact_bps) worst = r;
    }
    if (worst) out.push(`大单区间(≥1M USD)价格影响最显著：${worst.platform} (${worst.price_impact_bps.toFixed(1)}bps)，建议走 RFQ/批量拍卖路径降低滑点`);
  }
  const small = (analysis.group_stats||[]).filter(g => g.amount_usd <= 5_000);
  if (small.length) {
    let worst = null;
    for (const g of small) for (const r of g.rankings) {
      const pct = r.gas_cost_usd / Math.max(r.net_received_usd, 1) * 100;
      if (!worst || pct > worst.pct) worst = { ...r, pct };
    }
    if (worst) out.push(`小额区间(≤5K USD) gas 占比最高：${worst.platform} (gas 占净到手 ${worst.pct.toFixed(3)}%)，建议合并订单或走聚合器`);
  }
  return out;
}

// ---------- 图表：最优频率柱状图 ----------
function renderFreqChart(analysis) {
  const ctx = document.getElementById("chart-freq");
  const freq = analysis.best_frequency_pct || {};
  const labels = PLATFORMS;
  const data = labels.map(p => freq[p] || 0);
  destroyChart("freq");
  charts.freq = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "成为最优报价频率 %",
        data,
        backgroundColor: labels.map(p => PLATFORM_COLORS[p]),
        borderRadius: 6,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        y: { beginAtZero: true, ticks: { color: "#8b949e", callback: v => v + "%" }, grid: { color: "#161b22" } },
        x: { ticks: { color: "#c9d1d9" }, grid: { display: false } }
      }
    }
  });
}

// ---------- 图表：排名分布堆叠柱状图 ----------
function renderRankChart(analysis) {
  const ctx = document.getElementById("chart-rank");
  const dist = analysis.rank_distribution || {};
  const labels = PLATFORMS;
  const ranks = [1, 2, 3, 4];
  const datasets = ranks.map((rk, i) => ({
    label: `#${rk}`,
    data: labels.map(p => (dist[p] || {})[rk] || 0),
    backgroundColor: RANK_COLORS[i],
    borderRadius: 4,
  }));
  destroyChart("rank");
  charts.rank = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "bottom", labels: { color: "#8b949e" } } },
      scales: {
        x: { stacked: true, ticks: { color: "#c9d1d9" }, grid: { display: false } },
        y: { stacked: true, beginAtZero: true, ticks: { color: "#8b949e" }, grid: { color: "#161b22" } }
      }
    }
  });
}

// ---------- 图表：价差随金额变化曲线 ----------
function renderSpreadChart(analysis) {
  const ctx = document.getElementById("chart-spread");
  const groups = (analysis.group_stats || []).slice().sort((a,b) => a.amount_usd - b.amount_usd);
  // 仅取一个方向做曲线（默认 ETH->USDT，若无则取第一个）
  let dir = "ETH->USDT";
  let filtered = groups.filter(g => g.direction === dir);
  if (!filtered.length) { filtered = groups; dir = filtered[0]?.direction || ""; }
  const labels = filtered.map(g => fmtUSD(g.amount_usd));
  // 每个平台在每个金额下的 gap_vs_best_bps（若是 best 自己，画 0）
  const datasets = PLATFORMS.map(p => ({
    label: p,
    borderColor: PLATFORM_COLORS[p],
    backgroundColor: PLATFORM_COLORS[p],
    tension: .25,
    pointRadius: 3,
    data: filtered.map(g => {
      const r = g.rankings.find(x => x.platform === p);
      return r ? Number(r.gap_vs_best_bps) : null;
    })
  }));
  destroyChart("spread");
  charts.spread = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "bottom", labels: { color: "#8b949e" } },
        tooltip: { callbacks: { label: c => c.dataset.label + ": " + fmtBps(c.parsed.y) } } },
      scales: {
        x: { ticks: { color: "#c9d1d9" }, grid: { color: "#161b22" } },
        y: { ticks: { color: "#8b949e", callback: v => v + "bps" }, grid: { color: "#161b22" }, title: { display: true, text: "相对最优 bps", color: "#8b949e" } }
      }
    }
  });
}

// ---------- 图表：双向报价对比 ----------
function renderDualChart(analysis) {
  const ctx = document.getElementById("chart-dual");
  const groups = (analysis.group_stats || []).slice().sort((a,b) => a.amount_usd - b.amount_usd);
  // 选一个中间金额做对比（50K），若无则取中位
  let amt = 50_000;
  let exists = groups.find(g => g.amount_usd === amt);
  if (!exists) exists = groups[Math.floor(groups.length/2)];
  if (!exists) { destroyChart("dual"); return; }
  amt = exists.amount_usd;
  const filt = groups.filter(g => g.amount_usd === amt);
  const labels = PLATFORMS;
  const datasets = [];
  for (const g of filt) {
    datasets.push({
      label: g.direction,
      data: labels.map(p => {
        const r = g.rankings.find(x => x.platform === p);
        return r ? Number(r.net_received_usd.toFixed(2)) : null;
      }),
      backgroundColor: g.direction === "ETH->USDT" ? "rgba(255,112,166,.7)" : "rgba(88,166,255,.7)",
      borderRadius: 6,
    });
  }
  destroyChart("dual");
  charts.dual = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "bottom", labels: { color: "#8b949e" } },
        tooltip: { callbacks: { label: c => c.dataset.label + ": $" + fmtNum(c.parsed.y, 2) } },
        title: { display: true, text: `金额 = ${fmtUSD(amt)} USD`, color: "#8b949e" } },
      scales: {
        x: { ticks: { color: "#c9d1d9" }, grid: { display: false } },
        y: { ticks: { color: "#8b949e", callback: v => "$"+fmtUSD(v) }, grid: { color: "#161b22" } }
      }
    }
  });
}

// ---------- 图表：gas 占比 vs 金额 ----------
function renderGasChart(analysis) {
  const ctx = document.getElementById("chart-gas");
  const groups = (analysis.group_stats || []).slice().sort((a,b) => a.amount_usd - b.amount_usd);
  const dir = "ETH->USDT";
  let filt = groups.filter(g => g.direction === dir);
  if (!filt.length) filt = groups;
  const labels = filt.map(g => fmtUSD(g.amount_usd));
  const datasets = PLATFORMS.map(p => ({
    label: p,
    borderColor: PLATFORM_COLORS[p],
    backgroundColor: PLATFORM_COLORS[p],
    tension: .25,
    pointRadius: 3,
    data: filt.map(g => {
      const r = g.rankings.find(x => x.platform === p);
      return r ? Number((r.gas_cost_usd / Math.max(r.net_received_usd, 1) * 100).toFixed(4)) : null;
    })
  }));
  destroyChart("gas");
  charts.gas = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "bottom", labels: { color: "#8b949e" } },
        tooltip: { callbacks: { label: c => c.dataset.label + ": " + c.parsed.y + "%" } } },
      scales: {
        x: { ticks: { color: "#c9d1d9" }, grid: { color: "#161b22" } },
        y: { type: "logarithmic", ticks: { color: "#8b949e", callback: v => v + "%" }, grid: { color: "#161b22" }, title: { display: true, text: "gas / 净到手 %", color: "#8b949e" } }
      }
    }
  });
}

function destroyChart(name) {
  if (charts[name]) { try { charts[name].destroy(); } catch(e){} delete charts[name]; }
}

// ---------- 热力图 ----------
function renderHeatmap(analysis) {
  const el = document.getElementById("heatmap");
  const groups = (analysis.group_stats || []).slice().sort((a,b) => a.amount_usd - b.amount_usd);
  if (!groups.length) { el.innerHTML = `<div class="empty">无数据</div>`; return; }
  const dirs = [...new Set(groups.map(g => g.direction))];
  let html = "";
  for (const dir of dirs) {
    const filt = groups.filter(g => g.direction === dir);
    html += `<div style="margin-bottom:14px;"><div style="color:#8b949e;font-size:12px;margin-bottom:6px;">${escapeHtml(dir)}</div>`;
    html += `<table class="heatmap-table"><thead><tr><th></th>`;
    for (const g of filt) html += `<th>${fmtUSD(g.amount_usd)}</th>`;
    html += `</tr></thead><tbody>`;
    for (const p of PLATFORMS) {
      html += `<tr><td class="row-head">${escapeHtml(p)}</td>`;
      for (const g of filt) {
        const r = g.rankings.find(x => x.platform === p);
        if (r) {
          const color = RANK_COLORS[r.rank-1];
          html += `<td><div class="heatmap-cell" style="background:${color}">#${r.rank}</div></td>`;
        } else {
          html += `<td><div class="heatmap-cell" style="background:#161b22;color:#6e7681">—</div></td>`;
        }
      }
      html += `</tr>`;
    }
    html += `</tbody></table></div>`;
  }
  el.innerHTML = html;
}

// ---------- 全量渲染 ----------
function renderAll(data) {
  renderStatus(data);
  renderParseInfo(data.parsed_keywords);
  const analysis = data.analysis || {};
  renderMainTable(analysis);
  renderDiffTable(analysis);
  renderConclusions(analysis, data);
  renderFreqChart(analysis);
  renderRankChart(analysis);
  renderSpreadChart(analysis);
  renderDualChart(analysis);
  renderGasChart(analysis);
  renderHeatmap(analysis);
}

// ---------- 监控指标/告警 ----------
async function loadMetrics() {
  try {
    const resp = await fetch("/api/metrics");
    const doc = await resp.json();
    // 指标表
    const mt = document.querySelector("#metrics-table tbody");
    mt.innerHTML = "";
    for (const m of doc.metrics) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><code>${escapeHtml(m.key)}</code></td><td>${escapeHtml(m.name)}</td><td>${escapeHtml(m.definition)}</td>`;
      mt.appendChild(tr);
    }
    // 采集频率
    const fl = document.getElementById("freq-list");
    fl.innerHTML = "";
    for (const f of doc.collection_frequency) {
      const li = document.createElement("li");
      li.textContent = f;
      fl.appendChild(li);
    }
    // 告警
    const at = document.querySelector("#alert-table tbody");
    at.innerHTML = "";
    for (const a of doc.alert_rules) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${escapeHtml(a.rule)}</td><td>${escapeHtml(a.action)}</td>`;
      at.appendChild(tr);
    }
    // 图表建议
    const cs = document.getElementById("chart-sugg");
    cs.innerHTML = "";
    for (const c of doc.chart_suggestions) {
      const li = document.createElement("li");
      li.innerHTML = `<strong>${escapeHtml(c.name)}</strong>：${escapeHtml(c.desc)}`;
      cs.appendChild(li);
    }
    document.getElementById("metrics-section").classList.remove("hidden");
    document.getElementById("metrics-section").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    alert("加载监控指标失败：" + e.message);
  }
}

// ---------- 现场运行 ----------
async function runScript(kw) {
  const runBtn = document.getElementById("run");
  const originalText = runBtn.textContent;
  runBtn.disabled = true;
  runBtn.textContent = "运行中...";
  try {
    const params = new URLSearchParams();
    if (kw) params.set("kw", kw);
    if (realMode) params.set("real", "1");
    const url = "/api/quotes?" + params.toString();
    const resp = await fetch(url);
    const data = await resp.json();
    if (!resp.ok || data.error) {
      alert("运行失败：" + (data.error || resp.statusText));
      return;
    }
    // 服务器返回的真实模式状态回填前端 toggle
    if (typeof data.use_real === "boolean") {
      realMode = data.use_real;
      syncRealToggle();
    }
    renderAll(data);
  } catch (e) {
    alert("运行失败：" + e.message);
  } finally {
    runBtn.disabled = false;
    runBtn.textContent = originalText;
  }
}

// ---------- 真实模式 toggle ----------
function syncRealToggle() {
  const btn = document.getElementById("toggle-real");
  if (!btn) return;
  if (realMode) {
    btn.textContent = "真实 API: ON";
    btn.classList.add("real-on");
    btn.classList.remove("real-off");
  } else {
    btn.textContent = "真实 API: OFF";
    btn.classList.add("real-off");
    btn.classList.remove("real-on");
  }
}
async function toggleReal() {
  realMode = !realMode;
  syncRealToggle();
  // 切换后立即刷新一次
  await runScript(document.getElementById("kw").value);
}

// ---------- 健康检查（独立按钮） ----------
async function loadHealth() {
  const el = document.getElementById("providers");
  if (el) el.textContent = "健康检查中...";
  try {
    const resp = await fetch("/api/health");
    const data = await resp.json();
    if (data.error) { alert("健康检查失败：" + data.error); return; }
    if (!data.real_available) {
      alert("real_providers 模块未加载");
      return;
    }
    const lines = Object.entries(data.providers || {}).map(([k,v]) => `${k}=${v}`);
    const mp = data.eth_usd_midprice ? ` | ETH 中价=$${fmtNum(data.eth_usd_midprice,2)}` : "";
    alert("真实提供者健康检查：\n" + lines.join("\n") + mp);
    if (el) el.textContent = lines.join(" · ") + mp;
  } catch (e) {
    alert("健康检查失败：" + e.message);
  }
}

// ---------- 加载示例 ----------
async function loadExample() {
  try {
    const resp = await fetch("/api/example");
    const data = await resp.json();
    document.getElementById("kw").value = data.keyword;
    renderAll(data.result);
    renderParseInfo(data.result.parsed_keywords);
  } catch (e) {
    alert("加载示例失败：" + e.message);
  }
}

// ---------- 启动 ----------
document.addEventListener("DOMContentLoaded", () => {
  // 初始化真实模式 toggle 视觉状态
  syncRealToggle();
  document.getElementById("run").addEventListener("click", () => {
    runScript(document.getElementById("kw").value);
  });
  const toggleBtn = document.getElementById("toggle-real");
  if (toggleBtn) toggleBtn.addEventListener("click", toggleReal);
  const healthBtn = document.getElementById("health");
  if (healthBtn) healthBtn.addEventListener("click", loadHealth);
  document.getElementById("example").addEventListener("click", loadExample);
  document.getElementById("metrics").addEventListener("click", loadMetrics);
  document.getElementById("kw").addEventListener("keydown", e => {
    if (e.key === "Enter") runScript(e.target.value);
  });
  // 启动即加载示例数据
  loadExample();
});
