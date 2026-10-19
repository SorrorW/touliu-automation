/* 投流操作台前端逻辑（抖音：监控扫描 / 建计划agent / 重点关注 / 授权管理；视频号：投流下单；系统：会话任务 / 系统日志） */
"use strict";

// ---------- 基础工具 ----------

function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function apiGet(path) {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);
  return resp.json();
}

async function apiPost(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data;
}

async function apiDelete(path) {
  const resp = await fetch(path, { method: "DELETE" });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data;
}

// ---------- 任务栏（支持多任务同时展示） ----------

const _activeJobs = new Map();  // job_id → { name, status: "queued"|"running", submittedAt(ms) }

function _fmtHHMM(ms) {
  const d = new Date(ms || Date.now());
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function _renderTaskBar() {
  const bar = document.getElementById("task-bar");
  const entries = [..._activeJobs.entries()];  // insertion order = submission order
  if (!entries.length) {
    bar.className = "task-bar";
    bar.innerHTML = "";
    return;
  }
  // 按状态排序：running 在前，queued 在后
  entries.sort((a, b) => {
    const order = { running: 0, queued: 1 };
    return (order[a[1].status] || 2) - (order[b[1].status] || 2);
  });
  bar.className = "task-bar show";
  bar.innerHTML = entries.map(([, job]) => {
    const statusText = job.status === "running" ? "执行中" : "排队中";
    return `<div class="task-item">
      <span class="spin" aria-hidden="true"></span>
      <span class="task-label">「${esc(job.name)}」${statusText} · ${_fmtHHMM(job.submittedAt)}</span>
    </div>`;
  }).join("");
}

function showTaskBarResult(text, state) {
  // 显示完成/失败/终止条（6s 后自动消失），不影响活跃任务展示
  const bar = document.getElementById("task-bar");
  bar.className = "task-bar show" + (state === "ok" ? " ok" : " err");
  bar.innerHTML = "<span>" + esc(text) + "</span>";
  setTimeout(() => {
    // 只有当没有活跃任务时才清除
    if (_activeJobs.size === 0) bar.classList.remove("show");
    else _renderTaskBar();
  }, 6000);
}

function showToast(text, type) {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  const icons = { info: "ℹ", ok: "✓", err: "✗" };
  el.className = "toast " + (type || "info");
  el.innerHTML = '<span class="toast-icon">' + (icons[type] || "") + "</span>" + esc(text);
  container.appendChild(el);
  setTimeout(() => {
    el.style.animation = "toast-out .3s ease forwards";
    el.addEventListener("animationend", () => el.remove());
  }, 4000);
}

function fmtNum(v) { return v === null || v === undefined ? "-" : v; }

function setBusy(btn, busy) {
  if (!btn) return;
  btn.disabled = busy;
  btn.setAttribute("aria-busy", busy ? "true" : "false");
}

async function submitAndPoll(endpoint, payload, btn, onDone, progressContainer) {
  setBusy(btn, true);
  let job_id;
  try {
    ({ job_id } = await apiPost(endpoint, payload));
    // 注册到活跃任务列表
    _activeJobs.set(job_id, { name: "…", status: "queued", submittedAt: Date.now() });
    _renderTaskBar();
    let lastStatus = "queued";
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      const job = await apiGet(`/api/jobs/${job_id}`);
      // 真实进度（后端数得出来的任务）：推进进度条
      if (progressContainer && job.progress && job.progress.total > 0) {
        updateProgress(progressContainer, job.progress.done, job.progress.total);
      }
      // 更新任务名（首次拿到）
      if (_activeJobs.has(job_id) && job.name) {
        _activeJobs.get(job_id).name = job.name;
      }
      // 状态转换 toast
      if (lastStatus === "queued" && job.status === "running") {
        showToast(`「${job.name}」正在执行…`, "info");
        _activeJobs.get(job_id).status = "running";
        _renderTaskBar();
      }
      lastStatus = job.status;
      if (job.status === "queued") { _renderTaskBar(); continue; }
      if (job.status === "running") continue;
      // 完成/失败/取消 — 移出活跃列表
      _activeJobs.delete(job_id);
      if (job.status === "cancelled") {
        showTaskBarResult(`「${job.name}」已终止`, "err");
        throw new Error("任务已被终止");
      }
      if (job.status === "error") {
        showTaskBarResult(`「${job.name}」失败: ${job.error}`, "err");
        throw new Error(job.error);
      }
      if (progressContainer) updateProgress(progressContainer, 1, 1);
      showTaskBarResult(`「${job.name}」完成`, "ok");
      showToast(`「${job.name}」完成 ✓`, "ok");
      onDone(job.result);
      return;
    }
  } catch (err) {
    if (job_id !== undefined) { _activeJobs.delete(job_id); _renderTaskBar(); }
    showToast(err.message || String(err), "err");
    onDone(null, err.message || String(err));
  } finally {
    setBusy(btn, false);
  }
}

function renderError(container, message) {
  document.getElementById(container).innerHTML =
    `<div class="card"><p class="danger-text">操作失败: ${esc(message)}</p></div>`;
}

function renderEmpty(container, title, tip) {
  document.getElementById(container).innerHTML =
    `<div class="card empty-state"><div class="title">${esc(title)}</div><div class="tip">${esc(tip)}</div></div>`;
}

function renderProgress(container, note) {
  // 加载态：进度条（默认不定态流动动画；有真实进度时由 updateProgress 设为确定值）
  document.getElementById(container).innerHTML =
    `<div class="card">${note ? `<p class="muted" style="margin-bottom:10px">${esc(note)}</p>` : ""}
     <div class="progress-track">
       <div class="progress-fill indeterminate" id="pb-${container}"></div>
     </div>
     <p class="progress-label muted" id="pb-label-${container}"></p></div>`;
}

function updateProgress(container, done, total) {
  const bar = document.getElementById(`pb-${container}`);
  if (!bar) return;
  const label = document.getElementById(`pb-label-${container}`);
  if (total > 0) {
    const pct = Math.min(100, Math.round((done / total) * 100));
    bar.classList.remove("indeterminate");
    bar.style.width = pct + "%";
    // 扫描页只显示百分比（分母是预估值，计数会误导）；其他页保留「第几步/共几步」
    if (label) label.textContent =
      container === "scan-result" ? `已完成 ${pct}%` : `${done} / ${total}（${pct}%）`;
  }
}

function renderJson(container, obj) {
  document.getElementById(container).innerHTML =
    `<div class="card"><pre class="result">${esc(JSON.stringify(obj, null, 2))}</pre></div>`;
}

function statusCell(status) {
  const s = String(status || "");
  const cls = s.includes("投放中") ? "running" : s.includes("暂停") ? "paused" : "";
  return `<span class="status-cell"><span class="status-dot ${cls}"></span>${esc(s)}</span>`;
}

function chip(label, ok) {
  return `<span class="chip ${ok ? "ok" : "bad"}">${esc(label)}</span>`;
}

// ---------- 导航 ----------

function activateNav(el) {
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  el.classList.add("active");
  document.getElementById(`page-${el.dataset.page}`).classList.add("active");
  if (el.dataset.page === "system") {
    refreshJobs();
    refreshScheduledTasks();
  }
  if (el.dataset.page === "logs") refreshLogs();
  if (el.dataset.page === "auto") refreshAuto();
  if (el.dataset.page === "whitelist") loadManualWhitelist();
  if (el.dataset.page === "watch") loadWatchlist();
  if (el.dataset.page === "board") loadPlanBoard();
  // 切换到建计划页时，如果还在显示加载进度条则清理；并加载待建计划面板
  if (el.dataset.page === "plan") {
    const result = document.getElementById("agent-result");
    if (result && result.querySelector(".progress-track")) {
      result.innerHTML = "";
    }
    loadTodos();
  }
}

document.querySelectorAll(".nav-item").forEach((el) => {
  el.addEventListener("click", () => activateNav(el));
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activateNav(el); }
  });
});

// 板块分组折叠/展开（抖音 / 视频号）
document.querySelectorAll(".nav-group-title").forEach((el) => {
  const toggle = () => el.parentElement.classList.toggle("collapsed");
  el.addEventListener("click", toggle);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
});

// ---------- 自动化（企微表格轮询） ----------

let _autoFollowTimer = null;

async function refreshAuto() {
  const statusEl = document.getElementById("auto-status");
  try {
    const d = await apiGet("/api/auto/status");
    const sw = document.getElementById("auto-enabled");
    sw.checked = !!d.enabled;
    const runner = d.runner || {};
    const label = document.getElementById("auto-enabled-label");
    const runnerText = runner.mode === "running" ? "本轮执行中"
      : runner.mode === "waiting" ? "等待下一轮"
      : runner.mode === "external" ? "桌面托管轮询运行中"
      : runner.mode === "error" ? "轮询异常"
      : runner.mode === "not_started" ? (runner.message || "桌面轮询器未启动")
      : "已关闭";
    label.textContent = d.enabled ? `抖音任务表：${runnerText}（桌面每 15 分钟轮询）` : "抖音任务表：已关闭（桌面轮询器待命）";
    label.className = "switch-label" + (d.enabled ? "" : " danger-text");

    const vsw = document.getElementById("auto-video-enabled");
    vsw.checked = !!d.video_enabled;
    const vlabel = document.getElementById("auto-video-enabled-label");
    vlabel.textContent = d.video_enabled ? "视频号任务表：运行中" : "视频号任务表：已关闭";
    vlabel.className = "switch-label" + (d.video_enabled ? "" : " danger-text");

    const st = d.status || {};
    const parts = [st.state === "running" ? "● 正在执行" : "○ 空闲"];
    if (runner.message) parts.push("桌面轮询器：" + runner.message);
    if (runner.next_run_at) parts.push("下次 " + runner.next_run_at);
    if (st.sheet) parts.push(st.sheet);
    if (st.creator || st.record_id) parts.push("达人 " + (st.creator || st.record_id));
    if (st.step) parts.push("步骤：" + st.step);
    if (st.note) parts.push(st.note);
    statusEl.textContent = parts.join("　·　");
    document.getElementById("auto-updated").textContent =
      st.updated_at ? "状态更新于 " + st.updated_at
        : (runner.last_finished_at ? "上轮结束于 " + runner.last_finished_at : "");

    const recs = (d.records || []).filter((r) => !r.finished);
    document.getElementById("auto-records-tbody").innerHTML = recs.length
      ? recs.map((r) => {
          const progress = r.finished ? "已完成"
            : (r.waiting || []).length ? "等待授权"
            : (r.failed || []).length ? "模块失败"
            : "进行中";
          return `<tr><td>${esc(r.creator || r.record_id)}</td><td>${esc(progress)}</td>` +
            `<td>${esc(r.breakpoint || "—")}</td><td class="muted">${esc(r.failure_reason || r.error || "—")}</td>` +
            `<td>${esc(r.next_action || "")}</td><td class="num">${r.attempts}</td>` +
            `<td><button class="secondary small auto-retry-btn" data-rid="${esc(r.record_id)}">立即重试</button></td></tr>`;
        }).join("")
      : '<tr><td colspan="7" class="muted">没有进行中的记录</td></tr>';

    document.getElementById("auto-waiting-tbody").innerHTML = (d.waiting || []).length
      ? d.waiting.map((w) => `<tr><td>${esc(w.creator || w.record_id)}</td><td>${esc(w.step || "—")}</td>` +
          `<td class="num">${w.waited_hours}</td><td>${esc(w.next_check)}</td>` +
          `<td class="muted">${esc(w.note)}</td></tr>`).join("")
      : '<tr><td colspan="5" class="muted">没有等待授权的记录</td></tr>';

    document.getElementById("auto-log-file").textContent = d.log_file ? `（${d.log_file}）` : "";
    const lv = document.getElementById("auto-log-view");
    lv.textContent = (d.log_tail || []).join("\n") || "今天还没有日志";
    lv.scrollTop = lv.scrollHeight;
  } catch (e) {
    statusEl.textContent = `读取失败：${e.message}`;
  }
}

function setAutoFollow(on) {
  if (_autoFollowTimer) { clearInterval(_autoFollowTimer); _autoFollowTimer = null; }
  if (on) {
    _autoFollowTimer = setInterval(() => {
      const active = document.querySelector(".nav-item.active");
      if (active && active.dataset.page === "auto") refreshAuto();
    }, 10000);
  }
}

document.getElementById("auto-refresh").addEventListener("click", refreshAuto);
document.getElementById("auto-records-tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest(".auto-retry-btn");
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  try {
    await apiPost("/api/auto/retry", { record_id: btn.dataset.rid });
    showToast("已触发重试：该记录会立即从断点续跑", "ok");
    setTimeout(refreshAuto, 6000);
  } catch (err) {
    showToast("重试触发失败：" + err.message, "err");
    btn.disabled = false;
  }
});
document.getElementById("auto-follow").addEventListener("change", (e) => setAutoFollow(e.target.checked));
document.getElementById("auto-enabled").addEventListener("change", async (e) => {
  const want = e.target.checked;
  try {
    await apiPost("/api/auto/enabled", { enabled: want });
    showToast(want ? "自动化已开启，桌面轮询器将立即执行一轮" : "自动化已关闭，进行中的本轮完成后不再运行", "ok");
    refreshAuto();
  } catch (err) {
    e.target.checked = !want;  // 失败回弹
    showToast("开关切换失败：" + err.message, "err");
  }
});
document.getElementById("auto-video-enabled").addEventListener("change", async (e) => {
  const want = e.target.checked;
  try {
    await apiPost("/api/auto/video_enabled", { enabled: want });
    showToast(want ? "视频号轮询已开启" : "视频号轮询已关闭（抖音任务不受影响）", "ok");
    refreshAuto();
  } catch (err) {
    e.target.checked = !want;  // 失败回弹
    showToast("开关切换失败：" + err.message, "err");
  }
});
setAutoFollow(true);

// ---------- 人工白名单 ----------

let _manualWhitelist = [];

function splitWhitelistValues(value) {
  return String(value || "").split(/[,，]/).map((item) => item.trim()).filter(Boolean);
}

function renderManualWhitelist(data) {
  _manualWhitelist = Array.isArray(data.creators) ? data.creators : [];
  const host = document.getElementById("wl-result");
  if (!_manualWhitelist.length) {
    renderEmpty("wl-result", "暂无人工白名单", "填写上方信息后加入名单");
    return;
  }
  host.innerHTML = `<div class="card"><h3>当前名单（${_manualWhitelist.length} 位）</h3>
    <div class="table-wrap"><table><thead><tr><th>达人</th><th>部门</th><th>别名</th><th>计划 ID</th><th>操作</th></tr></thead>
    <tbody>${_manualWhitelist.map((item, index) => `<tr>
      <td>${esc(item.name)}</td><td>${esc(item.department)}</td>
      <td>${esc((item.aliases || []).join("、") || "—")}</td>
      <td class="muted">${esc((item.plan_ids || []).join("、") || "—")}</td>
      <td><button class="small danger" data-wl-remove="${index}">移除</button></td>
    </tr>`).join("")}</tbody></table></div></div>`;
  host.querySelectorAll("[data-wl-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const index = Number(btn.dataset.wlRemove);
      const item = _manualWhitelist[index];
      if (!item || !confirm(`确认将「${item.name}」移出人工白名单？`)) return;
      await saveManualWhitelist(_manualWhitelist.filter((_, i) => i !== index));
    });
  });
}

async function loadManualWhitelist() {
  try {
    renderManualWhitelist(await apiGet("/api/manual-whitelist"));
  } catch (err) {
    renderError("wl-result", err.message);
  }
}

async function saveManualWhitelist(creators) {
  try {
    const response = await fetch("/api/manual-whitelist", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ creators }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || response.statusText);
    renderManualWhitelist(data);
    showToast("人工白名单已保存", "ok");
    return true;
  } catch (err) {
    showToast("保存失败: " + err.message, "err");
    return false;
  }
}

document.getElementById("wl-add").addEventListener("click", async () => {
  const name = document.getElementById("wl-name").value.trim();
  const department = document.getElementById("wl-department").value;
  const aliases = splitWhitelistValues(document.getElementById("wl-aliases").value);
  const plan_ids = splitWhitelistValues(document.getElementById("wl-plan-ids").value);
  if (!name) return showToast("请填写达人名", "err");
  if (plan_ids.some((item) => !/^\d+$/.test(item))) return showToast("计划 ID 只能包含数字", "err");
  if (_manualWhitelist.some((item) => item.name === name && item.department === department)) {
    return showToast("该达人已在此部门白名单中", "err");
  }
  const btn = document.getElementById("wl-add");
  setBusy(btn, true);
  const saved = await saveManualWhitelist([..._manualWhitelist, { name, aliases, department, plan_ids, reason: "人工投放" }]);
  setBusy(btn, false);
  if (!saved) return;
  document.getElementById("wl-name").value = "";
  document.getElementById("wl-aliases").value = "";
  document.getElementById("wl-plan-ids").value = "";
});

// ---------- 初始化：状态/下拉框/agent配置 ----------

let chaseDefaults = { budget: 100, hours: 24 };

async function refreshStatus() {
  try {
    const s = await apiGet("/api/status");
    const dot = document.getElementById("cdp-dot");
    dot.className = "cdp-dot " + (s.cdp_ok ? "ok" : "bad");
    document.getElementById("cdp-status").textContent = s.cdp_ok ? "已连接" : "未连接";
    document.getElementById("sys-cdp").textContent =
      s.cdp_ok ? `CDP 已连接 (${s.browser})` : "CDP 未连接 — 请先启动 Edge，或点下方按钮拉起";
    document.getElementById("sys-url").textContent = s.qianchuan_url ? `当前页面: ${s.qianchuan_url}` : "";
    const chSess = document.getElementById("ch-session-status");
    if (chSess) {
      chSess.textContent = s.channels_url
        ? `当前页面: ${s.channels_url}`
        : (s.cdp_ok ? "浏览器已连接，未打开视频号页面" : "浏览器未连接 — 点下方按钮拉起");
    }
    const topDot = document.getElementById("top-health-dot");
    const topText = document.getElementById("top-health-text");
    if (topDot) topDot.className = "health-dot " + (s.cdp_ok ? "ok" : "bad");
    if (topText) topText.textContent = s.cdp_ok ? "平台连接正常" : "平台未连接";
    updateOpsCenter();
    return s;
  } catch {
    return null;
  }
}

async function initConfig() {
  const s = await refreshStatus();
  if (!s) return;
  const depOptions = s.departments.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join("");
  document.querySelectorAll("select.dep-select").forEach((sel) => { sel.innerHTML = depOptions; });
  document.getElementById("scan-department").innerHTML =
    `<option value="all">全部部门</option>` + depOptions;
  document.getElementById("scan-thresholds").textContent =
    `全表扫描各部门计划并标记达标（ROI ≥ ${s.thresholds.min_roi} 且 成交 ≥ ${s.thresholds.min_orders}）；只收集数据，不会创建追投`;
  chaseDefaults = s.chase_defaults;
  // 商品下拉：已知商品 + 自定义ID
  document.getElementById("pf-product").innerHTML =
    s.products.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("") +
    `<option value="__custom">自定义商品ID…</option>`;
  document.getElementById("pf-product").addEventListener("change", (e) => {
    document.getElementById("pf-product-custom-wrap").style.display =
      e.target.value === "__custom" ? "" : "none";
  });
  loadLastScan();
  loadAutoscan();
  document.getElementById("sys-dep-buttons").innerHTML = s.departments
    .map((d) => `<button class="secondary" data-dep="${esc(d)}">切换到 ${esc(d)}</button>`)
    .join("");
  document.querySelectorAll("#sys-dep-buttons button").forEach((btn) => {
    btn.addEventListener("click", () => {
      submitAndPoll("/api/session", { department: btn.dataset.dep }, btn, (result, err) => {
        if (!err) refreshStatus();
      });
    });
  });
  await loadAgentConfig();
}

// ---------- 监控扫描（筛选 + 全行一键追投 + 结果持久化） ----------

let lastScanReport = null;  // {dep: {data, scanned_at}}
let scanFilter = "all";   // all | qualified | active | inactive
let scanDepFilter = null; // 点击总览卡片选中部门（只看该部门达标），null=全部部门
let scanRunning = false;  // 扫描进行中：筛选/排序只记状态不重绘，避免顶掉进度条
let sortState = { key: null, dir: "desc" };  // key: spent|roi|orders|null（null=原始顺序）
const boostedPlanIds = new Set();

const SORT_COLUMNS = [
  ["spent", "消耗(元)"],
  ["roi", "综合ROI"],
  ["orders", "成交订单"],
];

function fmtTs(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function updateOpsCenter() {
  const dateEl = document.getElementById("topbar-date");
  if (dateEl) {
    const now = new Date(); const pad = (n) => String(n).padStart(2, "0");
    dateEl.textContent = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} · ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  }
  const entries = Object.values(lastScanReport || {});
  const rows = entries.flatMap((entry) => (entry && entry.data && !entry.data.error ? (entry.data.plans || []) : []));
  const total = rows.length;
  const qualified = rows.filter((p) => !!p.qualified).length;
  const running = rows.filter((p) => {
    const v = String(p.status || p.platform_status || "").toLowerCase();
    return /投放|运行|active|deliver|ongoing|启用/.test(v) && !/暂停|关闭|停止|pause|off|close|熔断/.test(v);
  }).length;
  const mismatches = rows.filter((p) => {
    const platform = p.platform_status ?? p.status_platform; const db = p.db_status ?? p.database_status;
    return platform != null && db != null && String(platform) !== String(db);
  }).length;
  const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
  set("ops-total", total || "—"); set("ops-qualified", total ? qualified : "—"); set("ops-running", total ? running : "—"); set("ops-alerts", total ? mismatches : "—");
  const latest = entries.map((e) => e && e.scanned_at).filter(Boolean).sort((a, b) => b - a)[0];
  set("ops-scan-time", latest ? `最近扫描 ${fmtTs(latest)}` : "等待扫描");
  set("ops-alerts-meta", total ? (mismatches ? `${mismatches} 条平台 / 数据库不一致` : "平台 / 数据库状态一致") : "平台 / 数据库状态差异");
  const sync = document.getElementById("top-sync");
  if (sync) { sync.classList.toggle("ok", total > 0 && mismatches === 0); sync.classList.toggle("warn", mismatches > 0); }
  const nowMinutes = new Date().getHours() * 60 + new Date().getMinutes();
  const slots = [[480, "08:00"], [720, "12:00"], [1020, "17:00"], [1350, "22:30"], [1380, "23:00"]];
  document.querySelectorAll(".workflow-step[data-slot]").forEach((step) => {
    const slot = slots.find((x) => x[1] === step.dataset.slot);
    step.classList.toggle("done", !!slot && nowMinutes > slot[0]); step.classList.toggle("current", !!slot && nowMinutes >= slot[0] && nowMinutes < slot[0] + 120);
  });
}

function setWaTs(ts) {
  const el = document.getElementById("wa-ts");
  el.textContent = ts ? `上次评估: ${fmtTs(ts)}` : "";
}

let lastScanSig = "";
async function loadLastScan() {
  try {
    const last = await apiGet("/api/scan/last");
    // 数据没变就不重渲染，避免看页面时滚动位置被刷掉
    const sig = JSON.stringify(
      Object.entries(last.departments || {}).map(([k, v]) => [k, v && v.scanned_at]));
    if (sig === lastScanSig) return;
    lastScanSig = sig;
    lastScanReport = last.departments;
    updateOpsCenter();
    renderScan();
  } catch {
    /* 没有历史扫描记录，保持空状态 */
  }
}

function setScanFilterUI(filter) {
  scanFilter = filter;
  document.querySelectorAll("#scan-filter .seg").forEach((b) => {
    b.classList.toggle("active", b.dataset.filter === filter);
  });
}

document.getElementById("scan-btn").addEventListener("click", () => {
  const department = document.getElementById("scan-department").value;
  renderProgress("scan-result", "扫描中…全部门约需 2-3 分钟，请勿操作千川页面");
  scanRunning = true;
  submitAndPoll("/api/scan", { department }, document.getElementById("scan-btn"), (result, err) => {
    scanRunning = false;
    if (err) return renderError("scan-result", err);
    // 按部门合并：只更新扫过的部门，其他部门数据和时间戳保留
    lastScanReport = lastScanReport || {};
    const now = Date.now() / 1000;
    for (const [dep, data] of Object.entries(result)) {
      if (data.error) continue;
      lastScanReport[dep] = { data, scanned_at: now };
    }
    updateOpsCenter();
    renderScan();
  }, "scan-result");
});

document.querySelectorAll("#scan-filter .seg").forEach((btn) => {
  btn.addEventListener("click", () => {
    scanDepFilter = null;  // 手动切筛选时取消部门卡片选中
    setScanFilterUI(btn.dataset.filter);
    if (lastScanReport && !scanRunning) renderScan();
  });
});

// ---- 自动扫描（每 N 小时后台提交全部门扫描，设置存服务端）----
function fmtAutoscanTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function renderAutoscanStatus(st) {
  const el = document.getElementById("autoscan-status");
  if (!st.enabled) {
    el.textContent = "已关闭";
    return;
  }
  const parts = [];
  if (st.last_run) parts.push(`上次: ${fmtAutoscanTime(st.last_run)}`);
  if (st.next_run) parts.push(`下次: ${fmtAutoscanTime(st.next_run)}`);
  el.textContent = parts.join(" · ") || "已开启";
}

async function loadAutoscan() {
  try {
    const st = await apiGet("/api/scan/autoscan");
    document.getElementById("autoscan-enabled").checked = st.enabled;
    document.getElementById("autoscan-interval").value = String(st.interval_hours);
    renderAutoscanStatus(st);
  } catch { /* 设置读取失败保持默认显示 */ }
}

async function saveAutoscan() {
  const enabled = document.getElementById("autoscan-enabled").checked;
  const interval = parseInt(document.getElementById("autoscan-interval").value, 10);
  try {
    const st = await apiPost("/api/scan/autoscan", { enabled, interval_hours: interval });
    renderAutoscanStatus(st);
  } catch (e) {
    renderError("scan-result", e.message || "自动扫描设置保存失败");
  }
}

document.getElementById("autoscan-enabled").addEventListener("change", saveAutoscan);
document.getElementById("autoscan-interval").addEventListener("change", saveAutoscan);

// 自动扫描跑完后页面数据也要跟上：每分钟拉一次设置与最近扫描结果
// （loadLastScan 内部对未变化的数据跳过重渲染）
setInterval(() => {
  loadAutoscan();
  if (!scanRunning) loadLastScan();
}, 60000);

// 总览卡片 = 按钮：只看该部门的达标数据，再点取消
document.addEventListener("click", (e) => {
  const card = e.target.closest(".summary-card[data-dep]");
  if (!card) return;
  const dep = card.dataset.dep;
  if (scanDepFilter === dep) {
    scanDepFilter = null;
    setScanFilterUI("all");
  } else {
    scanDepFilter = dep;
    setScanFilterUI("qualified");
  }
  if (lastScanReport && !scanRunning) renderScan();
});

document.addEventListener("keydown", (e) => {
  const card = e.target.closest && e.target.closest(".summary-card[data-dep]");
  if (!card) return;
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); card.click(); }
});

function planHasData(p) {
  return (p.spent > 0) || (p.orders > 0) || (p.roi !== null && p.roi !== undefined);
}

function planVisible(p) {
  if (scanDepFilter) return !!p.qualified;  // 部门卡片选中 = 只看该部门达标
  if (scanFilter === "qualified") return !!p.qualified;
  if (scanFilter === "active") return planHasData(p);
  if (scanFilter === "inactive") return !planHasData(p);
  return true;
}

function sortPlans(plans) {
  if (!sortState.key) return plans;  // 原始顺序
  const key = sortState.key;
  const withValue = [];
  const empty = [];
  for (const p of plans) {
    const v = p[key];
    if (v === null || v === undefined) empty.push(p);
    else withValue.push(p);
  }
  withValue.sort((a, b) => sortState.dir === "desc" ? b[key] - a[key] : a[key] - b[key]);
  return withValue.concat(empty);  // 空值永远垫底
}

function sortHeaderHtml() {
  return SORT_COLUMNS.map(([key, label]) => {
    const active = sortState.key === key;
    const arrow = active ? (sortState.dir === "desc" ? " ↓" : " ↑") : "";
    const ariaSort = active ? (sortState.dir === "desc" ? "descending" : "ascending") : "none";
    return `<th class="num sortable${active ? " sorted" : ""}" data-sort="${key}" aria-sort="${ariaSort}" tabindex="0">${label}<span class="sort-arrow">${arrow}</span></th>`;
  }).join("");
}

document.addEventListener("click", (e) => {
  const th = e.target.closest("th.sortable");
  if (!th) return;
  const key = th.dataset.sort;
  if (sortState.key !== key) {
    sortState = { key, dir: "desc" };        // 换列 → 直接降序
  } else if (sortState.dir === "desc") {
    sortState = { key, dir: "asc" };         // 再点 → 升序
  } else {
    sortState = { key: null, dir: "desc" };  // 再点 → 恢复原始顺序
  }
  if (lastScanReport && !scanRunning) renderScan();
});

document.addEventListener("keydown", (e) => {
  const th = e.target.closest && e.target.closest("th.sortable");
  if (!th) return;
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); th.click(); }
});

function renderScan() {
  const deps = Object.entries(lastScanReport);
  let html = '<div class="summary-cards">';
  for (const [dep, entry] of deps) {
    const data = entry.data;
    if (data.error) {
      html += `<div class="summary-card error"><div class="dep">${esc(dep)}</div>
        <div class="num">失败</div><div class="muted">${esc(data.error)}</div></div>`;
    } else {
      html += `<div class="summary-card${scanDepFilter === dep ? " active" : ""}" data-dep="${esc(dep)}" role="button" tabindex="0" title="点击只看该部门达标计划">
        <div class="dep">${esc(dep)}</div>
        <div class="num">${data.total_plans}<small> 个计划</small></div>
        <div class="hit">达标 ${data.qualified_count} 个</div></div>`;
    }
  }
  html += "</div>";

  let anyRow = false;
  for (const [dep, entry] of deps) {
    const data = entry.data;
    if (data.error) continue;
    if (scanDepFilter && dep !== scanDepFilter) continue;
    const plans = sortPlans(data.plans.filter(planVisible));
    if (!plans.length) continue;
    anyRow = true;
    const ts = entry.scanned_at ? ` <span class="muted" style="font-weight:400;font-size:12.5px">· ${fmtTs(entry.scanned_at)}</span>` : "";
    html += `<div class="card"><h3>${esc(dep)}（显示 ${plans.length} / ${data.total_plans} 个计划，达标 ${data.qualified_count}）${ts}</h3>
      <div class="table-wrap"><table>
      <thead><tr><th>计划名称</th><th>达人</th><th>状态</th>${sortHeaderHtml()}<th>达标</th><th>计划ID</th><th>操作</th></tr></thead>
      <tbody>${plans.map((p) => `
        <tr class="${p.qualified ? "qualified" : ""}">
          <td>${esc(p.plan_name)}</td><td>${esc(p.creator_name || "-")}</td>
          <td>${statusCell(p.status)}</td>
          <td class="num">${fmtNum(p.spent)}</td><td class="num">${fmtNum(p.roi)}</td><td class="num">${fmtNum(p.orders)}</td>
          <td>${p.qualified ? '<span class="qual-badge">达标</span>' : ""}</td>
          <td class="muted">${esc(p.plan_id || "")}</td>
          <td>${p.plan_id
            ? `<button class="small" data-boost="${esc(p.plan_id)}" data-dep="${esc(dep)}" data-name="${esc(p.plan_name)}">一键追投</button><span class="muted" data-boost-result="${esc(p.plan_id)}"></span>`
            : ""}</td>
        </tr>`).join("")}
      </tbody></table></div></div>`;
  }

  if (!anyRow) {
    // 总览卡片常驻：空状态只替换表格区，卡片留着可以随时切回/切换部门
    const tips = {
      qualified: "当前没有达标计划。早上数据少很正常，建议下午或晚上再扫",
      active: "当前没有产生数据的计划",
      inactive: "没有无数据的计划——所有计划今天都有消耗或成交",
    };
    const tipText = scanDepFilter
      ? `${scanDepFilter} 当前没有达标计划，点击上方卡片可取消筛选`
      : (tips[scanFilter] || "当前筛选条件下没有计划");
    html += `<div class="card empty-state"><div class="title">没有可显示的计划</div><div class="tip">${esc(tipText)}</div></div>`;
  }
  document.getElementById("scan-result").innerHTML = html;

  document.querySelectorAll("button[data-boost]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const planId = btn.dataset.boost;
      if (boostedPlanIds.has(planId)) return;
      if (!confirm(`确认为「${btn.dataset.name}」创建追投？预算 ${chaseDefaults.budget} 元 / ${chaseDefaults.hours} 小时`)) return;
      const slot = document.querySelector(`[data-boost-result="${planId}"]`);
      submitAndPoll("/api/chase/quick", { plan_id: planId, department: btn.dataset.dep }, btn, (result, err) => {
        if (err) {
          if (slot) slot.textContent = " 创建失败: " + err;
          return;
        }
        boostedPlanIds.add(planId);
        btn.style.display = "none";
        if (slot) slot.textContent = ` 已创建 ✓ task ${result.task_id || ""}`;
      });
    });
  });
}

// ---------- 建计划（agent：配置 → 填空表单 → 执行） ----------

async function loadAgentConfig() {
  try {
    const cfg = await apiGet("/api/agent/config");
    document.getElementById("cfg-provider").value = cfg.provider;
    document.getElementById("cfg-model").value = cfg.model || "";
    document.getElementById("cfg-key").value = "";
    document.getElementById("cfg-key").placeholder = cfg.has_key ? `已保存（${cfg.api_key_masked}），留空不修改…` : "sk-…";
    document.getElementById("cfg-status").textContent = cfg.has_key
      ? `当前: ${cfg.provider_label} / ${cfg.model}（Key ${cfg.api_key_masked}）`
      : "未配置 API Key 时将使用规则解析（不接 LLM）";
  } catch { /* 忽略 */ }
}

document.getElementById("cfg-save").addEventListener("click", async () => {
  const btn = document.getElementById("cfg-save");
  setBusy(btn, true);
  try {
    await apiPost("/api/agent/config", {
      provider: document.getElementById("cfg-provider").value,
      api_key: document.getElementById("cfg-key").value.trim() || null,
      model: document.getElementById("cfg-model").value.trim() || null,
    });
    showTaskBarResult("API 配置已保存", "ok");
    await loadAgentConfig();
  } catch (err) {
    showTaskBarResult("保存失败: " + err.message, "err");
  } finally {
    setBusy(btn, false);
  }
});

document.getElementById("cfg-test").addEventListener("click", async () => {
  const btn = document.getElementById("cfg-test");
  setBusy(btn, true);
  try {
    const r = await apiPost("/api/agent/test", {});
    showTaskBarResult(`连接成功 ✓ 模型 ${r.model} 可用`, "ok");
  } catch (err) {
    showTaskBarResult("连接失败: " + err.message, "err");
  } finally {
    setBusy(btn, false);
  }
});

// 粘贴文本自动填写（折叠区）
document.getElementById("paste-toggle").addEventListener("click", () => {
  const zone = document.getElementById("paste-zone");
  const open = zone.style.display !== "none";
  zone.style.display = open ? "none" : "";
  document.getElementById("paste-toggle").setAttribute("aria-expanded", String(!open));
});

document.getElementById("parse-btn").addEventListener("click", async () => {
  const text = document.getElementById("agent-text").value.trim();
  const note = document.getElementById("parse-note");
  if (!text) { note.textContent = "请先粘贴任务文本"; return; }
  const btn = document.getElementById("parse-btn");
  setBusy(btn, true);
  try {
    const r = await apiPost("/api/agent/parse", { text });
    fillForm(r.fields);
    note.textContent = `已填入表单（${r.parse_note}）`;
  } catch (err) {
    note.textContent = "解析失败: " + err.message;
  } finally {
    setBusy(btn, false);
  }
});

const PRODUCT_SELECT_ID = "pf-product";

function fillForm(fields) {
  document.getElementById("pf-department").value = fields.department || "";
  document.getElementById("pf-seller").value = fields.seller || "";
  document.getElementById("pf-creator").value = fields.creator_id || "";
  document.getElementById("pf-code").value = fields.code || "";
  // 商品：已知选项直接选，否则走自定义ID
  const sel = document.getElementById(PRODUCT_SELECT_ID);
  const known = Array.from(sel.options).some((o) => o.value === fields.product);
  sel.value = known ? fields.product : "__custom";
  document.getElementById("pf-product-custom-wrap").style.display = known ? "none" : "";
  document.getElementById("pf-product-custom").value = known ? "" : (fields.product || "");
  // 达人名：解析兜底成抖音号时不填（执行端会再兜底）
  document.getElementById("pf-creator-name").value =
    fields.creator_name && fields.creator_name !== fields.creator_id ? fields.creator_name : "";
  document.getElementById("pf-roi").value = fields.roi ?? 4.5;
  document.getElementById("pf-budget").value = fields.budget ?? 300;
  document.getElementById("pf-publish").checked = !!fields.publish;
}

function collectForm() {
  const sel = document.getElementById(PRODUCT_SELECT_ID);
  const product = sel.value === "__custom"
    ? document.getElementById("pf-product-custom").value.trim()
    : sel.value;
  return {
    department: document.getElementById("pf-department").value,
    seller: document.getElementById("pf-seller").value.trim(),
    creator_id: document.getElementById("pf-creator").value.trim(),
    code: document.getElementById("pf-code").value.trim() || null,
    product,
    creator_name: document.getElementById("pf-creator-name").value.trim() || null,
    roi: parseFloat(document.getElementById("pf-roi").value) || 4.5,
    budget: parseInt(document.getElementById("pf-budget").value, 10) || 300,
    publish: document.getElementById("pf-publish").checked,
  };
}

document.getElementById("exec-btn").addEventListener("click", () => {
  const fields = collectForm();
  const missing = [];
  if (!fields.seller) missing.push("业务员");
  if (!fields.creator_id) missing.push("抖音号 ID");
  if (!fields.product) missing.push("商品");
  if (missing.length) return renderError("agent-result", "请先填写必填项: " + missing.join("、"));
  if (fields.publish && !confirm("「直接发布」已打开，计划将【立即上线】消耗预算，确认执行？")) return;
  renderProgress("agent-result", "执行中：授权 → 建计划 → 建追投…");
  submitAndPoll("/api/agent/plan", { fields }, document.getElementById("exec-btn"), (result, err) => {
    if (err) return renderError("agent-result", err);
    renderAgentSteps(result);
    loadTodos();
  }, "agent-result");
});

function renderAgentSteps(result) {
  const icon = { ok: "✓", failed: "✗", skip: "—" };
  document.getElementById("agent-result").innerHTML = `<div class="card">
    <h3>${result.ok ? "执行完成" : "执行中止"}</h3>
    <ol class="steps">${result.steps.map((s) => `
      <li class="step ${esc(s.status)}">
        <span class="step-mark" aria-hidden="true">${icon[s.status] || "—"}</span>
        <div class="step-body">
          <div class="step-name">${esc(s.step)}</div>
          ${s.detail ? `<div class="step-detail">${esc(s.detail)}</div>` : ""}
        </div>
      </li>`).join("")}
    </ol>
    ${result.ok ? '<p class="hint">计划未发布时，可稍后在千川后台人工发布，或文本中写「发布：是」。</p>' : ""}
  </div>`;
}

// ---------- 待建计划（右侧面板：未上线达人 + 授权状态 + 一键重建） ----------

const TODO_STATUS = {
  pending_auth: "待授权",
  pending_plan: "待建计划",
  pending_chase: "待追投",
  pending_publish: "待发布",
};

async function loadTodos() {
  const list = document.getElementById("todo-list");
  try {
    const { items } = await apiGet("/api/agent/todos");
    if (!items.length) {
      list.innerHTML = `<p class="muted" style="padding:8px 0">暂无待建计划</p>`;
      return;
    }
    list.innerHTML = items.map((t) => `
      <div class="todo-item">
        <div class="todo-head">
          <span>${esc(t.creator_id)}${t.creator_name ? `-${esc(t.creator_name)}` : ""}</span>
          <span class="muted">${esc(t.department)}</span>
        </div>
        <div class="todo-meta chip-row">
          ${chip(TODO_STATUS[t.status] || t.status, false)}
          ${t.auth_status
            ? chip(t.authorized ? "授权已生效" : t.auth_status, !!t.authorized)
            : `<span class="muted">点「重新查询授权」获取状态</span>`}
        </div>
        ${t.message ? `<div class="todo-meta muted">${esc(t.message)}</div>` : ""}
        ${t.auth_checked_at ? `<div class="todo-meta muted">授权查询于 ${fmtTs(t.auth_checked_at)}</div>` : ""}
        <div class="todo-actions">
          <button class="secondary small" data-todo-recheck="${t.id}">重新查询授权</button>
          <button class="secondary small" data-todo-reauth="${t.id}">重新申请授权</button>
          <button class="small" data-todo-retry="${t.id}" data-name="${esc(t.creator_name || t.creator_id)}">发布计划+追投</button>
          <button class="secondary small" data-todo-del="${t.id}">删除</button>
        </div>
      </div>`).join("");
  } catch (err) {
    list.innerHTML = `<p class="danger-text">加载失败: ${esc(err.message)}</p>`;
  }
}

document.getElementById("todo-refresh").addEventListener("click", loadTodos);

async function loadPlanCatalog() {
  const list = document.getElementById("catalog-list");
  try {
    const { items } = await apiGet("/api/plan-catalog");
    if (!items.length) {
      list.innerHTML = `<p class="muted" style="padding:8px 0">尚无扫描计划</p>`;
      return;
    }
    list.innerHTML = items.map((p) => `
      <div class="todo-item">
        <div class="todo-head"><span>${esc(p.plan_name || p.plan_id)}</span><span class="muted">${esc(p.department)}</span></div>
        <div class="todo-meta chip-row">${chip(p.category_name, p.category === "key_plan")}${chip(p.plan_status || "状态未知", false)}</div>
        <div class="todo-meta muted">达人 ${esc(p.creator_name || "-")} · 消耗 ${esc(p.spent ?? "-")} · ROI ${esc(p.roi ?? "-")} · 订单 ${esc(p.orders ?? "-")}</div>
        <div class="todo-meta muted">${esc(p.category_reason || "")}</div>
      </div>`).join("");
  } catch (err) {
    list.innerHTML = `<p class="danger-text">加载失败: ${esc(err.message)}</p>`;
  }
}

document.getElementById("catalog-refresh").addEventListener("click", loadPlanCatalog);

// ---------- 计划每日看板（8/12/17/23 扫描档位标签） ----------

const BOARD_CATS = {
  new_plan: { label: "新计划", bg: "#e6f4ff", fg: "#0958d9" },
  potential_plan: { label: "潜力", bg: "#fff7e6", fg: "#d46b08" },
  key_plan: { label: "重点", bg: "#f6ffed", fg: "#389e0d" },
  old_pool: { label: "日常", bg: "#f5f5f5", fg: "#595959" },
  manual_review: { label: "白名单", bg: "#f9f0ff", fg: "#722ed1" },
  invalid_plan: { label: "无效", bg: "#fff1f0", fg: "#cf1322" },
};

function boardChip(cat) {
  const c = BOARD_CATS[cat];
  if (!c) return '<span class="muted">·</span>';
  return `<span class="chip" style="background:${c.bg};color:${c.fg}">${c.label}</span>`;
}

function boardLatest(r) {
  return r.label_23 || r.label_17 || r.label_12 || r.label_08 || "";
}

async function loadPlanBoard() {
  const result = document.getElementById("board-result");
  const summary = document.getElementById("board-summary");
  const dateInput = document.getElementById("board-date");
  if (!dateInput.value) dateInput.value = new Date().toISOString().slice(0, 10);
  try {
    const { items } = await apiGet("/api/plan-board?date=" + encodeURIComponent(dateInput.value));
    if (!items.length) {
      summary.textContent = "";
      result.innerHTML = `<p class="muted" style="padding:8px 0">该日期暂无扫描标签</p>`;
      return;
    }
    const counts = {};
    items.forEach((r) => { const c = boardLatest(r) || "none"; counts[c] = (counts[c] || 0) + 1; });
    summary.innerHTML = "当前分类：" + Object.entries(BOARD_CATS).map(([k, v]) =>
      `${v.label} ${counts[k] || 0}`).join(" · ") + ` · 共 ${items.length} 条`;
    result.innerHTML = `<div class="card"><div class="table-wrap"><table>
      <thead><tr><th>计划名称</th><th>部门</th><th class="num">目标ROI</th><th class="num">预算</th>
      <th>8点</th><th>12点</th><th>17点</th><th>23点</th>
      <th class="num">最新ROI</th><th class="num">订单</th><th class="num">消耗</th><th>状态</th></tr></thead>
      <tbody>${items.map((r) => `<tr>
        <td>${esc(r.plan_name || r.plan_id)}</td>
        <td>${esc(r.department || "")}</td>
        <td class="num">${r.roi_target ?? "-"}</td>
        <td class="num">${r.daily_budget ?? "-"}</td>
        <td>${boardChip(r.label_08)}</td><td>${boardChip(r.label_12)}</td>
        <td>${boardChip(r.label_17)}</td><td>${boardChip(r.label_23)}</td>
        <td class="num">${r.last_roi != null ? r.last_roi.toFixed(2) : "-"}</td>
        <td class="num">${r.last_orders ?? "-"}</td>
        <td class="num">${r.last_spent ?? "-"}</td>
        <td>${esc(r.last_status || "")}</td>
      </tr>`).join("")}</tbody></table></div></div>`;
  } catch (err) {
    result.innerHTML = `<p class="danger-text">加载失败: ${esc(err.message)}</p>`;
  }
}

document.getElementById("board-refresh").addEventListener("click", loadPlanBoard);
document.getElementById("board-date").addEventListener("change", loadPlanBoard);

document.getElementById("todo-list").addEventListener("click", async (e) => {
  const recheck = e.target.closest("[data-todo-recheck]");
  const reauth = e.target.closest("[data-todo-reauth]");
  const retry = e.target.closest("[data-todo-retry]");
  const del = e.target.closest("[data-todo-del]");
  if (recheck) {
    const id = recheck.getAttribute("data-todo-recheck");
    submitAndPoll(`/api/agent/todos/${id}/recheck`, {}, recheck, () => loadTodos());
  } else if (reauth) {
    const id = reauth.getAttribute("data-todo-reauth");
    if (!confirm("确认重新提交授权申请？将使用建计划时填的合作码")) return;
    submitAndPoll(`/api/agent/todos/${id}/reauth`, {}, reauth, () => loadTodos());
  } else if (retry) {
    const id = retry.getAttribute("data-todo-retry");
    const name = retry.getAttribute("data-name") || `#${id}`;
    if (!confirm(`确认为「${name}」执行 发布计划+追投？计划将上线消耗预算。`)) return;
    submitAndPoll(`/api/agent/todos/${id}/retry`, {}, retry, () => loadTodos());
  } else if (del) {
    const id = del.getAttribute("data-todo-del");
    if (!confirm(`删除待建计划条目 #${id}？（不影响已建计划）`)) return;
    try {
      await apiDelete(`/api/agent/todos/${id}`);
      loadTodos();
    } catch (err) {
      showToast("删除失败: " + err.message, "err");
    }
  }
});

// ---------- 重点关注达人（big_creator_monitor 状态机规则） ----------

const MACHINE_STATUS = {
  normal: "正常",
  chase_1: "第 1 次追投中",
  chase_2: "第 2 次追投中",
  rest: "休息期",
};

async function loadWatchlist() {
  try {
    const big = await apiGet("/api/bigwatch");
    document.getElementById("wa-rules").textContent =
      `规则：近七天单量 < ${big.rules.poor_order_threshold} 为低效 → 追投 ${big.rules.chase_budget} 元/${big.rules.chase_hours} 小时，最多 2 次仍低效休息 7 天`;
    if (!big.items.length) {
      setWaTs(null);
      return renderEmpty("wa-result", "还没有关注的达人", "上方输入达人名称和部门，点「添加关注」");
    }
    // 有上次评估结果就直接展示（数据常驻），没有则显示待评估
    let evalResults = null;
    try {
      const last = await apiGet("/api/bigwatch/last");
      evalResults = last.report;
      setWaTs(last.evaluated_at);
    } catch {
      setWaTs(null);
    }
    renderBigWatch(big.items, evalResults);
  } catch (err) {
    renderError("wa-result", err.message);
  }
}

function renderBigWatch(items, evalResults) {
  const byName = {};
  (evalResults || []).forEach((r) => { byName[r.name] = r; });
  document.getElementById("wa-result").innerHTML = `<div class="card">
    <h3>关注列表（${items.length} 个）</h3>
    <div class="table-wrap"><table>
    <thead><tr><th>达人</th><th>部门</th><th class="num">近七天单量</th><th class="num">消耗(元)</th><th class="num">综合ROI</th><th>监控状态</th><th>是否符合追投</th><th>操作</th></tr></thead>
    <tbody>${items.map((item) => {
      const r = byName[item.name];
      let eligibleCell = '<span class="muted">点「刷新评估」判断</span>';
      let actionCell = "";
      if (r) {
        if (r.error) {
          eligibleCell = `<span class="danger-text">${esc(r.error)}</span>`;
        } else if (r.eligible) {
          eligibleCell = chip("符合（单量 " + fmtNum(r.orders) + "，低效）", true);
          actionCell = `<button class="small warn" data-chase="${esc(item.name)}" data-dep="${esc(item.department)}">一键追投</button><span class="muted" data-chase-result="${esc(item.name)}"></span>`;
        } else {
          const reason = r.reason || (r.machine_status && r.machine_status.startsWith("chase_") ? "追投任务仍在执行" : "单量达标");
          eligibleCell = chip("不符合", false) + `<span class="muted"> ${esc(reason)}</span>`;
        }
      }
      const machine = r ? (MACHINE_STATUS[r.machine_status] || esc(r.machine_status)) : "";
      return `<tr>
        <td>${esc(item.name)}</td>
        <td>${esc(item.department)}</td>
        <td class="num">${r && !r.error ? fmtNum(r.orders) : ""}</td>
        <td class="num">${r && !r.error && r.spent != null ? fmtNum(r.spent) : ""}</td>
        <td class="num">${r && !r.error && r.roi != null ? fmtNum(r.roi) : ""}</td>
        <td>${machine}</td>
        <td>${eligibleCell}</td>
        <td>${actionCell}<button class="small danger" data-wa-remove="${esc(item.name)}">移除</button></td>
      </tr>`;
    }).join("")}</tbody></table></div></div>`;

  document.querySelectorAll("button[data-chase]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.chase;
      if (!confirm(`确认为「${name}」创建追投（100 元/24 小时）？状态机会自动拦截休息期/追投中/已达上限的情况`)) return;
      const slot = document.querySelector(`[data-chase-result="${name}"]`);
      submitAndPoll("/api/bigwatch/chase", { name, department: btn.dataset.dep }, btn, (result, err) => {
        if (err) { if (slot) slot.textContent = " 失败: " + err; return; }
        if (result.chase_created) {
          if (slot) slot.textContent = ` 已创建 ✓ task ${result.task_id}`;
          btn.style.display = "none";
        } else {
          if (slot) slot.textContent = ` 未追投: ${result.reason || ""}`;
        }
      });
    });
  });

  document.querySelectorAll("button[data-wa-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm(`确认将「${btn.dataset.waRemove}」移出关注列表？`)) return;
      try {
        await apiDelete(`/api/watchlist/${encodeURIComponent(btn.dataset.waRemove)}`);
        loadWatchlist();
      } catch (err) {
        showTaskBarResult("移除失败: " + err.message, "err");
      }
    });
  });
}

document.getElementById("wa-add").addEventListener("click", async () => {
  const name = document.getElementById("wa-name").value.trim();
  if (!name) return;
  const btn = document.getElementById("wa-add");
  setBusy(btn, true);
  try {
    await apiPost("/api/watchlist", {
      name,
      department: document.getElementById("wa-department").value,
    });
    document.getElementById("wa-name").value = "";
    loadWatchlist();
  } catch (err) {
    showTaskBarResult("添加失败: " + err.message, "err");
  } finally {
    setBusy(btn, false);
  }
});

document.getElementById("wa-scan").addEventListener("click", () => {
  renderProgress("wa-result", "逐达人评估中（不触发追投）…");
  submitAndPoll("/api/bigwatch/scan", {}, document.getElementById("wa-scan"), async (result, err) => {
    if (err) return renderError("wa-result", err);
    setWaTs(Date.now() / 1000);
    const big = await apiGet("/api/bigwatch");
    renderBigWatch(big.items, result);
  }, "wa-result");
});

// ---------- 授权管理 ----------

document.getElementById("au-check-btn").addEventListener("click", () => {
  const payload = {
    creator: document.getElementById("au-check-creator").value.trim(),
    department: document.getElementById("au-check-department").value,
  };
  if (!payload.creator) return renderEmpty("au-check-result", "请输入抖音号 ID", "");
  renderProgress("au-check-result", "检查中…");
  submitAndPoll("/api/auth/check", payload, document.getElementById("au-check-btn"), (result, err) => {
    if (err) return renderError("au-check-result", err);
    document.getElementById("au-check-result").innerHTML = `<div class="card">
      <h3>${esc(result.creator)}（${esc(result.department)}）</h3>
      <div class="chip-row">
        ${chip("抖音号授权: " + result.douyin_auth, result.douyin_auth === "已生效")}
        ${chip("投放授权: " + result.toufang_auth, result.toufang_auth === "已生效")}
        ${chip(result.authorized ? "整体: 已完成" : "整体: 未完成", result.authorized)}
      </div>
      ${!result.authorized ? '<p class="hint">未生效时可在下方提交授权申请（需要合作码）。</p>' : ""}
    </div>`;
  });
});

document.getElementById("au-btn").addEventListener("click", () => {
  const payload = {
    creator: document.getElementById("au-creator").value.trim(),
    code: document.getElementById("au-code").value.trim() || null,
    department: document.getElementById("au-department").value,
  };
  if (!payload.creator) return renderEmpty("au-result", "请输入抖音号 ID", "");
  if (!confirm(`确认为抖音号 ${payload.creator} 提交授权申请？`)) return;
  submitAndPoll("/api/auth/authorize", payload, document.getElementById("au-btn"), (result, err) => {
    if (err) return renderError("au-result", err);
    renderJson("au-result", result);
  });
});

// ---------- 视频号投流（填表不提交，人工核对后自行提交） ----------

// select + 「自定义…」输入框联动：选中 __custom 时显示文本框，取值时优先自定义内容
function bindSelectWithCustom(selectId, customId) {
  const sel = document.getElementById(selectId);
  const custom = document.getElementById(customId);
  sel.addEventListener("change", () => {
    custom.style.display = sel.value === "__custom" ? "" : "none";
  });
}

function selectValue(selectId, customId) {
  const sel = document.getElementById(selectId);
  if (sel.value !== "__custom") return sel.value;
  return document.getElementById(customId).value.trim();
}

bindSelectWithCustom("ch-priority", "ch-priority-custom");
bindSelectWithCustom("ch-bid", "ch-bid-custom");
bindSelectWithCustom("ch-hours", "ch-hours-custom");
bindSelectWithCustom("ch-template", "ch-template-custom");

document.getElementById("ch-session-btn").addEventListener("click", () => {
  renderProgress("ch-result", "正在拉起 Edge 并打开视频号推广页…如未登录请在浏览器中扫码");
  submitAndPoll("/api/channels/session", {}, document.getElementById("ch-session-btn"), (result, err) => {
    if (err) return renderError("ch-result", err);
    document.getElementById("ch-result").innerHTML = `<div class="card">
      <h3>会话就绪 ✓</h3>
      <p class="muted" style="word-break:break-all">当前页面: ${esc(result.url || "")}</p>
    </div>`;
    refreshStatus();
  });
});

document.getElementById("ch-submit-btn").addEventListener("click", () => {
  const noTemplate = document.getElementById("ch-no-template").checked;
  const priority = selectValue("ch-priority", "ch-priority-custom");
  const bid = selectValue("ch-bid", "ch-bid-custom");
  const hoursRaw = selectValue("ch-hours", "ch-hours-custom");
  const template = noTemplate ? null : selectValue("ch-template", "ch-template-custom");
  const missing = [];
  if (!priority) missing.push("优先提升目标");
  if (!bid) missing.push("出价方式");
  if (!noTemplate && !template) missing.push("人群模版");
  if (missing.length) return renderError("ch-result", "请先填写: " + missing.join("、"));
  const hours = parseInt(hoursRaw, 10);
  if (!hours || hours < 1) return renderError("ch-result", "加热时长无效: " + (hoursRaw || "空"));
  const payload = {
    keyword: document.getElementById("ch-keyword").value.trim() || null,
    start_date: document.getElementById("ch-date").value || null,
    roi: parseFloat(document.getElementById("ch-roi").value) || 7.3,
    budget: parseInt(document.getElementById("ch-budget").value, 10) || 300,
    no_budget: document.getElementById("ch-no-budget").checked,
    hours,
    priority_target: priority,
    bid_method: bid,
    template,
    no_template: noTemplate,
  };
  if (!confirm("确认开始填表？脚本将操作浏览器完成 选视频 → 填表 → 勾协议（不点提交）")) return;
  renderProgress("ch-result", "填表进行中，请勿操作浏览器…");
  submitAndPoll("/api/channels/submit", payload, document.getElementById("ch-submit-btn"), (result, err) => {
    if (err) return renderError("ch-result", err);
    const f = (result && result.filled_fields) || {};
    document.getElementById("ch-result").innerHTML = `<div class="card">
      <h3>填表完成 ✓（未提交）</h3>
      <div class="chip-row">
        ${chip("目标: " + f.priority_target, true)}
        ${chip("出价: " + f.bid_method, true)}
        ${chip("ROI: " + f.roi, true)}
        ${chip("预算: " + (f.budget === null || f.budget === undefined ? "页面自动带出" : f.budget + " 元"), true)}
        ${chip("时长: " + f.hours + " 小时", true)}
        ${chip("模版: " + (f.template || "未使用"), true)}
        ${chip("协议已勾选", true)}
      </div>
      <p class="hint" style="margin-top:10px">请在 Edge 中人工核对订单内容，确认无误后自行点击「提交」。</p>
    </div>`;
  });
});

// ---------- 会话/任务页：任务列表 ----------

// 「清空」是截止线语义：遮住清空那一刻已有的内容，之后的新任务/新日志照常显示；
// 点「加载最近信息」撤掉截止线、恢复显示全部。
let jobsClearedMaxId = null;   // 清空时最大 job id；null = 未清空
let logsClearedTotal = null;   // 清空时日志总行数；null = 未清空

async function refreshScheduledTasks() {
  const tbody = document.getElementById("scheduled-tasks-tbody");
  if (!tbody) return;
  try {
    const { tasks } = await apiGet("/api/system/scheduled-tasks");
    const STATE = { Ready: "就绪", Running: "执行中", Disabled: "已禁用", Missing: "未找到", Unavailable: "无法读取" };
    const fmtTime = (value) => {
      if (!value) return "-";
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
    };
    const result = (task) => {
      if (task.error) return `<span class="danger-text">${esc(task.error)}</span>`;
      if (task.last_result === null || task.last_result === undefined) return "-";
      return task.last_result === 0 ? "成功 (0)" : `异常 (${esc(task.last_result)})`;
    };
    tbody.innerHTML = tasks.map((task) => `<tr>
      <td>${esc(task.label || task.name)}</td>
      <td>${statusCell(STATE[task.state] || task.state || "未知")}</td>
      <td class="num">${fmtTime(task.last_run_at)}</td>
      <td class="num">${fmtTime(task.next_run_at)}</td>
      <td>${result(task)}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted" style="text-align:center;padding:18px">未找到系统定时任务</td></tr>`;
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="danger-text" style="padding:18px">读取定时任务失败：${esc(err.message)}</td></tr>`;
  }
}

async function refreshJobs() {
  try {
    let { jobs } = await apiGet("/api/jobs");
    const tbody = document.getElementById("jobs-tbody");
    if (jobsClearedMaxId !== null) {
      jobs = jobs.filter((j) => j.id > jobsClearedMaxId);
      if (!jobs.length) return;  // 没有新任务，保留清空占位
    }
    if (!jobs.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px">还没有任务记录</td></tr>`;
      return;
    }
    const JOB_STATUS = { done: "完成", error: "失败", running: "执行中", queued: "排队中", cancelled: "已终止" };
    function fmtJobTime(ts) {
      const d = new Date(ts * 1000);
      const pad = (n) => String(n).padStart(2, "0");
      return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    function jobElapsed(j) {
      // 耗时从开始执行（拿到浏览器锁）算起；排队中的时间不计
      const base = j.started_at || (j.finished_at ? j.submitted_at : null);
      if (!base) return "-";  // 排队中 / 未执行就被终止
      if (j.finished_at) return (j.finished_at - base).toFixed(1);
      return `<span data-elapsed="${base}">${(Date.now() / 1000 - base).toFixed(1)}</span>`;
    }
    tbody.innerHTML = jobs.map((j) => `<tr>
      <td class="num">${fmtJobTime(j.submitted_at)}</td><td>${esc(j.name)}</td>
      <td>${statusCell(JOB_STATUS[j.status] || j.status)}</td>
      <td class="num">${jobElapsed(j)}</td>
      <td class="danger-text">${esc(j.error || "")}</td>
      <td>${j.status === "running" || j.status === "queued"
        ? `<button class="secondary small" data-cancel-job="${j.id}">终止</button>` : ""}</td>
    </tr>`).join("");
    // 仅滚动表格容器到顶部（不带动整个页面）
    const wrap = tbody.closest(".table-wrap");
    if (wrap) wrap.scrollTop = 0;
  } catch { /* 忽略 */ }
}

document.getElementById("jobs-tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-cancel-job]");
  if (!btn) return;
  const id = btn.getAttribute("data-cancel-job");
  if (!confirm(`确认终止任务 #${id}？\n正在操作浏览器的任务被终止后，千川页面可能停在中间状态。`)) return;
  setBusy(btn, true);
  try {
    await apiPost(`/api/jobs/${id}/cancel`, {});
  } catch (err) {
    showTaskBarResult(`终止任务失败: ${err.message}`, "err");
  }
  refreshJobs();
});

document.getElementById("jobs-refresh").addEventListener("click", refreshJobs);
document.getElementById("scheduled-tasks-refresh").addEventListener("click", refreshScheduledTasks);

document.getElementById("jobs-clear").addEventListener("click", async () => {
  try {
    const { jobs } = await apiGet("/api/jobs");
    jobsClearedMaxId = jobs.length ? Math.max(...jobs.map((j) => j.id)) : 0;
  } catch {
    jobsClearedMaxId = 0;  // 拉取失败：不遮挡后续新任务
  }
  document.getElementById("jobs-tbody").innerHTML =
    `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px">已清空（新任务会自动显示；点「加载最近信息」查看全部历史）</td></tr>`;
});

document.getElementById("jobs-load").addEventListener("click", () => {
  jobsClearedMaxId = null;
  refreshJobs();
});

// ---------- 系统日志页：运行日志 ----------

async function refreshLogs() {
  const view = document.getElementById("log-view");
  try {
    const lines = document.getElementById("log-lines").value;
    const data = await apiGet(`/api/logs?lines=${lines}`);
    if (logsClearedTotal !== null) {
      // 已清空：只显示清空之后新增的日志行
      const newCount = (data.total || 0) - logsClearedTotal;
      if (newCount <= 0) return;  // 没有新增，保留清空占位
      view.textContent = data.lines.slice(-newCount).join("\n");
    } else {
      view.textContent = data.lines.length ? data.lines.join("\n") : "日志文件还没有内容";
    }
    view.scrollTop = view.scrollHeight;
  } catch (err) {
    view.textContent = "日志读取失败: " + err.message;
  }
}

document.getElementById("log-refresh").addEventListener("click", refreshLogs);
document.getElementById("log-lines").addEventListener("change", refreshLogs);
document.getElementById("log-download").addEventListener("click", () => {
  window.open("/api/logs/download", "_blank");
});

document.getElementById("log-clear").addEventListener("click", async () => {
  try {
    const data = await apiGet("/api/logs?lines=1");
    logsClearedTotal = data.total || 0;
  } catch {
    logsClearedTotal = null;  // 拉取失败：退化为不过滤
  }
  document.getElementById("log-view").textContent =
    "已清空（日志文件未改动；新日志会继续显示，点「加载最近信息」查看全部）";
});

document.getElementById("log-load").addEventListener("click", () => {
  logsClearedTotal = null;
  refreshLogs();
});

// 停在会话/任务页或系统日志页时每 10 秒自动刷新对应内容
setInterval(() => {
  const active = document.querySelector(".nav-item.active");
  if (!active) return;
  if (active.dataset.page === "system") {
    refreshJobs();
    refreshScheduledTasks();
  }
  if (active.dataset.page === "logs") refreshLogs();
}, 10000);

// 执行中任务的耗时秒数实时跳动（只更新数字，不整表重绘）
setInterval(() => {
  document.querySelectorAll("[data-elapsed]").forEach((el) => {
    el.textContent = (Date.now() / 1000 - Number(el.getAttribute("data-elapsed"))).toFixed(1);
  });
}, 1000);

// ---------- 启动 ----------

initConfig();
setInterval(refreshStatus, 15000);

// 全局任务状态追踪：维护 _activeJobs + 检测排队→运行转换（跨页面生效）
const _jobHistory = new Map();
setInterval(async () => {
  try {
    const { jobs } = await apiGet("/api/jobs");
    for (const j of jobs.slice(0, 10)) {
      const prev = _jobHistory.get(j.id);
      if (prev && prev === "queued" && j.status === "running") {
        showToast(`「${j.name}」开始执行`, "info");
      }
      _jobHistory.set(j.id, j.status);
      // 同步到活跃任务列表
      if (j.status === "running" || j.status === "queued") {
        if (!_activeJobs.has(j.id)) {
          _activeJobs.set(j.id, {
            name: j.name,
            status: j.status,
            submittedAt: j.submitted_at ? j.submitted_at * 1000 : Date.now(),
          });
        } else {
          _activeJobs.get(j.id).status = j.status;
          _activeJobs.get(j.id).name = j.name;
        }
      } else {
        _activeJobs.delete(j.id);
      }
    }
    _renderTaskBar();
  } catch {}
}, 5000);
