/* Relocheck web UI — vanilla JS, no build step. */
"use strict";

const state = {
  ctx: null,
  runId: null,
  run: null,
  detailKey: null, // {table, segment}
  detail: null,
  filter: "attention",
  poll: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}
const post = (path, payload) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

let toastTimer = null;
function toast(msg, ms = 3200) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
}

const KIND_LABEL = {
  null: "NULL", bool: "bool", int: "int", decimal: "Decimal",
  float: "float", text: "text", bytes: "bytes", date: "date",
  time: "time", datetime: "datetime", json: "json",
};

const CODE_TEXT = {
  null_mismatch: ["NULL / 非空", "hard", "一侧是 SQL NULL，另一侧有值；不会与空串 '' 或文本 'NULL' 划等号"],
  type_mismatch: ["类型不同", "hard", "值的语义类型不同，未做字符串折中"],
  value_mismatch: ["值不同", "hard", "按类型精确比较后不相等"],
  numeric_kind_mismatch: ["数值类型不同", "hard", "浮点与精确数值无法在默认策略下判等"],
  precision_mismatch: ["精度/标度不一致(严格)", "hard", "严格精度策略：Decimal 标度不同即判差异"],
  equal_value_precision_drift: ["数值相等 · 标度漂移", "soft", "Decimal 量级相等，但两侧声明/解码标度不同"],
  within_tolerance: ["容差内相等", "soft", "声明的绝对误差容差内"],
  both_null: ["两侧 NULL", "soft", "NULL 与 NULL 类型化判等"],
  equal: ["相同", "soft", ""],
};

const REASON_UI = {
  write_during_scan: "扫描期间该键段在至少一侧发生写入，读到的行与水位无法对应到同一状态。",
  verify_changed: "确认读取时键段指纹已变化：首次比较的内容在复核前就被改写。",
  event_lag: "两侧共享复制事件位点不一致（复制延迟 / 错版本），本次比较混合了两个逻辑状态。",
  alignment_unproven: "缺少共享事件位点或静默时间窗证据，无法证明两侧读到同一状态。",
  hot_window: "最新写入时间落在扫描时间窗内，无法排除两侧分别读到写入前后状态。",
  snapshot_not_aligned: "声明的快照位点尚未在两侧同时应用。",
};

const VERDICT_ICON = { match: "✓", diff: "✕", deferred: "⏳" };
const VERDICT_TEXT = { match: "确定相同", diff: "确定差异", deferred: "不可判定 · 延期复核" };

/* ------------------------------------------------------------------ */
/* Value / evidence rendering                                          */
/* ------------------------------------------------------------------ */

function renderValue(w) {
  if (!w) return '<span class="val null">∅ missing</span>';
  if (w.kind === "null" || w.v === null || w.v === undefined)
    return '<span class="val null">NULL</span><span class="kind-tag">null</span>';
  if (w.kind === "text" && w.v === "")
    return '<span class="val empty">′′（空字符串）</span><span class="kind-tag">text</span>';
  const cls = "val" + (w.kind === "float" ? " kind-float" : "");
  const title = w.kind === "float" ? ' title="完整 round-trip 浮点表示，未做四舍五入；0.1+0.2 与 0.3 会被如实区分"' : "";
  return `<span class="${cls}"${title}>${esc(w.v)}</span>` +
    `<span class="kind-tag">${KIND_LABEL[w.kind] || w.kind}</span>` +
    (w.sql_type ? `<span class="type-tag">${esc(w.sql_type)}</span>` : "");
}

function codePill(code) {
  const [label, kind, title] = CODE_TEXT[code] || [code, "hard", ""];
  return `<span class="code-pill ${kind}" title="${esc(title)}">${esc(label)}</span>`;
}

function renderDiffTable(row) {
  const hard = row.column_diffs.filter((d) => !d.equal);
  const soft = row.column_diffs.filter((d) => d.equal && d.code !== "equal");
  if (!hard.length && !soft.length) return "";
  const all = [...hard, ...soft];
  return `<div class="table-scroll"><table class="diff-table">
    <tr><th>列</th><th>A（旧库）</th><th></th><th>B（新库）</th><th>判定</th></tr>
    ${all.map((d) => `
      <tr>
        <td class="col-name">${esc(d.column)}</td>
        <td>${renderValue(d.a)}</td>
        <td class="arrow-cell">≠</td>
        <td>${renderValue(d.b)}</td>
        <td>${codePill(d.code)}</td>
      </tr>`).join("")}
  </table></div>`;
}

function renderSoloRows(row) {
  if (row.disposition === "only_a")
    return `<div class="solo-row only_a"><div class="solo-title">仅 A（旧库）存在的行</div>${renderKv(row.values_a)}</div>`;
  if (row.disposition === "only_b")
    return `<div class="solo-row only_b"><div class="solo-title">仅 B（新库）存在的行</div>${renderKv(row.values_b)}</div>`;
  return "";
}

function renderKv(values) {
  if (!values) return "";
  return Object.entries(values)
    .map(([k, w]) => `<div class="kv-line">${esc(k)} = ${renderValue(w)}</div>`)
    .join("");
}

function keyVisible(ke, filter) {
  const attention = ke.status !== "match" || ke.duplicate_group;
  if (filter === "attention") return attention;
  if (filter === "all") return true;
  if (filter === "diff") return ke.status !== "match";
  if (filter === "dup") return ke.duplicate_group;
  return true;
}

function renderEvidence(detail) {
  const evs = detail.evidence || [];
  const visible = evs.filter((ke) => keyVisible(ke, state.filter));
  if (!visible.length)
    return '<div class="evidence-empty">当前筛选下没有键 —— 该键段的业务键与内容全部一致。</div>';
  return visible.map((ke) => {
    const cls = ke.status === "match"
      ? (ke.duplicate_group ? "key-card dupmatch" : "key-card")
      : "key-card nonmatch";
    const statusBadge = ke.status === "match"
      ? '<span class="badge match">键一致</span>'
      : ke.status === "missing_b"
        ? '<span class="badge diff">B 缺行</span>'
        : ke.status === "extra_b"
          ? '<span class="badge diff">B 多行</span>'
          : '<span class="badge diff">内容/行数差异</span>';
    return `<div class="${cls}">
      <div class="key-head">
        <span class="key-val">${esc(ke.key_display)}</span>
        ${statusBadge}
        <span class="key-sub">行数 A/B = ${ke.multiplicity_a}/${ke.multiplicity_b}
          · 完全抵消相同行 ${ke.identical_rows}</span>
        ${ke.duplicate_group ? '<span class="badge ghost">同键多行（按多重集核对）</span>' : ""}
      </div>
      <div class="key-body">
        ${ke.row_evidence.map((r) =>
          r.disposition === "paired" ? renderDiffTable(r) : renderSoloRows(r)
        ).join("")}
        ${ke.status === "match" && !ke.row_evidence.length
          ? '<div class="key-sub">所有比较列逐值相同。</div>' : ""}
      </div>
    </div>`;
  }).join("");
}

/* ------------------------------------------------------------------ */
/* Detail panel                                                        */
/* ------------------------------------------------------------------ */

function wmCard(title, wm) {
  if (!wm) return "";
  return `<div class="wm"><b>${esc(title)}</b>
    event#${esc(wm.event_seq ?? "—")} · global#${esc(wm.global_seq ?? "—")}
    · ver=${esc(wm.version ?? "—")}</div>`;
}

function renderTimeline(tl) {
  if (!tl || !tl.length) return "";
  return `<ul class="timeline">${tl.map((e) => {
    let cls = "";
    if (e.code === "write_observed") cls = "write";
    else if (e.code === "lag_observed") cls = "lag";
    else if (e.code === "verdict")
      cls = (e.detail || {}).verdict === "deferred" ? "verdict-wait" : "verdict-ok";
    const events = (e.detail && e.detail.events) || [];
    return `<li class="tl-item ${cls}">
      <span class="tl-time">${esc((e.at || "").slice(11, 23))}</span>
      ${e.side ? `<span class="side-chip ${e.side}">${e.side}</span>` : ""}
      <span class="tl-code">${esc(e.code)}</span>
      <span class="tl-msg">${esc(e.message)}</span>
      ${events.length ? `<table class="event-table">
        <tr><th>seq</th><th>segment</th><th>key</th><th>op</th><th>at</th></tr>
        ${events.map((x) => `<tr><td>${esc(x.seq)}</td><td>${esc(x.segment)}</td>
          <td>${esc(x.order_no)}</td><td>${esc(x.op)}</td><td>${esc((x.at || "").slice(11, 23))}</td></tr>`).join("")}
      </table>` : ""}
    </li>`;
  }).join("")}</ul>`;
}

function renderDetail() {
  const d = state.detail;
  if (!d) return;
  $("detailPanel").classList.remove("hidden");
  $("detailTitle").textContent = `② 行级证据 · ${d.table} · 键段 #${d.index}`;
  const v = d.verdict;
  $("detailVerdictLine").innerHTML = `
    <span class="badge ${v}">${VERDICT_ICON[v]} ${VERDICT_TEXT[v]}</span>
    ${d.basis ? `<span class="basis">一致性依据：${esc(d.basis)}</span>` : ""}
    <span class="basis">扫描于 ${esc((d.scanned_at || "").replace("T", " ").slice(0, 19))}</span>
    <span class="basis">指纹 A <code>${esc(d.fingerprint_a || "—")}</code> / B <code>${esc(d.fingerprint_b || "—")}</code></span>`;

  const why = v === "deferred" ? `
    <div class="why-box">
      <div class="why-title">⚠ 为什么这一次对比不可判定？</div>
      <p>${esc(d.reason || REASON_UI[d.reason_code] || "")} <code>${esc(d.reason_code || "")}</code></p>
      <p>扫描协议：读前双水位 → 读行 + 读后水位 → 双侧复核指纹与括号水位。
         只要位点移动、复核指纹变化或两侧共享事件位点不一致，就延期。</p>
      <p class="guard">🛡 即使下面的临时逐行比较恰好显示“${d.provisional_content === "match" ? "内容相同" : "有差异"}”，
         它也<b>不是确定结论</b>：该键段不会被计入“确定相同”，顶部总结论也不会放行。请在写入平息 / B 库追平后点“延期复核此段”。</p>
    </div>` : "";

  const wm = d.watermarks || {};
  const wmRow = v === "deferred" || d.basis === "aligned_event_token" ? `
    <div class="wm-row">
      ${wmCard("A 读后", wm.post_a)}${wmCard("A 复核", wm.verify_a)}
      ${wmCard("B 读后", wm.post_b)}${wmCard("B 复核", wm.verify_b)}
    </div>` : "";

  const history = (d.history || []);
  const hist = history.length ? `<div class="history">
      ${history.map((h) => `<span class="badge ghost">${esc((h.at || "").slice(11, 19))} · ${VERDICT_TEXT[h.verdict] || h.verdict}${h.reason_code ? " · " + esc(h.reason_code) : ""}</span>`).join("")}
    </div>` : "";

  $("detailBody").innerHTML = `
    ${why}
    ${hist}
    ${wmRow}
    ${renderTimeline(d.timeline)}
    <div class="evidence-tools">
      ${[["attention", "问题 + 同键多行"], ["all", "全部键"], ["diff", "仅差异键"], ["dup", "仅同键多行"]]
        .map(([k, label]) => `<button class="chip-filter ${state.filter === k ? "active" : ""}" data-filter="${k}">${label}</button>`).join("")}
    </div>
    <div id="evidenceList">${renderEvidence(d)}</div>`;

  document.querySelectorAll(".chip-filter").forEach((b) =>
    b.addEventListener("click", () => {
      state.filter = b.dataset.filter;
      $("evidenceList").innerHTML = renderEvidence(state.detail);
      document.querySelectorAll(".chip-filter").forEach((x) =>
        x.classList.toggle("active", x.dataset.filter === state.filter));
    }));
  $("detailPanel").scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ------------------------------------------------------------------ */
/* Run / boards                                                        */
/* ------------------------------------------------------------------ */

function overallBanner(totals, status, error) {
  const el = $("overallBanner");
  el.classList.remove("hidden", "verified", "differences", "incomplete", "running");
  if (error) {
    el.classList.add("differences");
    el.innerHTML = `<div class="banner-title">运行出错</div><div class="banner-sub">${esc(error)}</div>`;
    return;
  }
  if (status === "running" || totals.overall === "running") {
    el.classList.add("running");
    el.innerHTML = `<div class="banner-title"><span class="seg-spinner"></span> 正在分段扫描并执行一致性确认 …</div>
      <div class="banner-sub">实时模式下，扫描期内被持续写入 / 复制延迟波及的键段会被标记为“不可判定”，而不是被强行判同或判异。</div>`;
    return;
  }
  const map = {
    verified_equal: ["verified", "✅ 所有键段均给出确定结论：一致",
      `共 ${totals.match} 个键段通过稳定性 / 位点对齐确认，未发现差异。`],
    differences_found: ["differences", "❌ 已确定存在差异",
      `${totals.diff} 个键段有确定差异（键差异 ${totals.keys_diff}，缺 B ${totals.missing_b}，多 B ${totals.extra_b}，同键多行组 ${totals.duplicate_groups}）。`],
    incomplete_deferred: ["incomplete", "⏳ 尚不能判定整体一致：有键段需要延期复核",
      `${totals.deferred} 个键段不可判定（其中 ${totals.provisional_equal_among_deferred} 个临时内容恰好相同，但系统拒绝将其计为一致）；${totals.match} 段确定相同。请待写入平息后逐段复核。`],
    incomplete_with_diffs: ["incomplete", "⚠ 已发现确定差异，同时部分键段仍不可判定",
      `${totals.diff} 段确定有差异，${totals.deferred} 段需延期复核，${totals.match} 段确定相同。延期段不会被误判相同。`],
    empty: ["running", "没有可显示的键段", ""],
  };
  const [cls, title, sub] = map[totals.overall] || ["running", totals.overall, ""];
  el.classList.add(cls);
  el.innerHTML = `<div class="banner-title">${title}</div><div class="banner-sub">${sub}</div>`;
}

function renderTiles(t) {
  const tiles = [
    ["good", t.match, "确定相同段"],
    ["critical", t.diff, "确定差异段"],
    ["warning", t.deferred, "延期复核段"],
    ["", t.keys_match, "一致业务键"],
    ["critical", t.keys_diff, "差异业务键"],
    ["info", t.duplicate_groups, "同键多行组"],
  ];
  $("tiles").innerHTML = tiles.map(([cls, num, lbl]) =>
    `<div class="tile ${cls}"><div class="num">${num ?? "–"}</div><div class="lbl">${lbl}</div></div>`).join("");

  const total = t.segments_total || 1;
  const pct = (n) => `${((n || 0) / total) * 100}%`;
  $("compBar").innerHTML =
    `<div class="seg match" style="width:${pct(t.match)}"></div>
     <div class="seg diff" style="width:${pct(t.diff)}"></div>
     <div class="seg deferred" style="width:${pct(t.deferred)}"></div>`;
  $("compLegend").innerHTML = `
    <span class="legend-item"><span class="swatch match"></span>确定相同 ${t.match}</span>
    <span class="legend-item"><span class="swatch diff"></span>确定差异 ${t.diff}</span>
    <span class="legend-item"><span class="swatch deferred"></span>延期复核 ${t.deferred}</span>
    <span class="legend-item">共 ${t.segments_total} 段 / 已扫描 ${t.segments_ready}</span>`;
}

function segCard(table, s) {
  if (!s)
    return `<button class="seg-card pending" disabled><div class="seg-top">
      <span class="seg-idx">#?</span><span class="seg-spinner"></span></div>
      <div class="seg-meta">扫描中…</div></button>`;
  const selected = state.detailKey && state.detailKey.table === table && state.detailKey.segment === s.index;
  const icon = VERDICT_ICON[s.verdict];
  const tags = [];
  if (s.verdict === "deferred") tags.push('<span class="seg-tag deferred">延期复核</span>');
  if (s.verdict === "deferred")
    tags.push(`<span class="seg-tag provisional">临时${s.provisional_content === "match" ? "相同·不作数" : "有差异·不作数"}</span>`);
  if (s.precision_warnings > 0) tags.push(`<span class="seg-tag precision">精度提示×${s.precision_warnings}</span>`);
  return `<button class="seg-card ${s.verdict} ${selected ? "selected" : ""}"
      data-table="${esc(table)}" data-seg="${s.index}">
    <div class="seg-top"><span class="seg-idx">#${s.index}</span><span class="seg-icon">${icon}</span></div>
    <div class="seg-meta">行 ${s.rows_a}/${s.rows_b} · 键 ${s.keys_match}同/${s.keys_diff}异</div>
    <div class="seg-meta">缺B ${s.missing_b} · 多B ${s.extra_b} · 多行 ${s.duplicate_groups}</div>
    <div>${tags.join(" ")}</div>
  </button>`;
}

function renderBoards(run) {
  const html = Object.entries(run.tables).map(([table, segs]) => {
    const tcfg = (state.ctx.tables || []).find((x) => x.name === table) || {};
    return `<div class="table-block">
      <div class="table-title">▦ ${esc(table)}
        <span class="bk">业务键 (${(tcfg.business_key || []).map(esc).join(", ")}) · hash 分 ${segs.length} 段</span>
      </div>
      <div class="seg-grid">${segs.map((s) => segCard(table, s)).join("")}</div>
    </div>`;
  }).join("");
  $("segmentBoards").innerHTML = html;
  document.querySelectorAll(".seg-card[data-seg]").forEach((el) =>
    el.addEventListener("click", () => openSegment(el.dataset.table, Number(el.dataset.seg))));
}

function renderDrift(run) {
  const d = run.schema_drift || [];
  if (!d.length) { $("driftPanel").classList.add("hidden"); return; }
  $("driftPanel").classList.remove("hidden");
  $("driftList").innerHTML = d.map((x) =>
    `<div class="drift-item">${esc(x.table)}.${esc(x.column)}：${esc(x.a_type || "—")}
      <span class="arrow">→</span> ${esc(x.b_type || "—")}
      <span class="badge ghost">${esc(x.kind)}</span></div>`).join("");
}

function renderRunSelector(runs) {
  const sel = $("historyRuns");
  if (!runs.length) { sel.innerHTML = ""; return; }
  sel.innerHTML = runs.map((r) =>
    `<option value="${r.id}" ${r.id === state.runId ? "selected" : ""}>${r.id} · ${r.totals.overall} · ${(r.created_at || "").slice(11, 19)}</option>`).join("");
}

async function refreshRunList() {
  const { runs } = await api("/api/runs");
  renderRunSelector(runs);
}

async function loadRun(runId, { poll = true } = {}) {
  state.runId = runId;
  const run = await api(`/api/runs/${runId}`);
  state.run = run;
  overallBanner(run.totals, run.status, run.error);
  renderTiles(run.totals);
  renderBoards(run);
  renderDrift(run);
  renderRunSelector((await api("/api/runs")).runs);
  if (state.detailKey) refreshDetailSilently();
  if (poll && run.status !== "done") {
    clearTimeout(state.poll);
    state.poll = setTimeout(() => loadRun(runId), 600);
  } else if (run.status === "done") {
    refreshRunList();
  }
}

async function openSegment(table, seg) {
  state.detailKey = { table, segment: seg };
  state.filter = "attention";
  document.querySelectorAll(".seg-card").forEach((c) =>
    c.classList.toggle("selected", c.dataset.table === table && Number(c.dataset.seg) === seg));
  const q = `?run_id=${encodeURIComponent(state.runId)}&table=${encodeURIComponent(table)}&segment=${seg}`;
  state.detail = await api("/api/segment" + q);
  renderDetail();
}

async function refreshDetailSilently() {
  if (!state.detailKey) return;
  const { table, segment } = state.detailKey;
  try {
    state.detail = await api(`/api/segment?run_id=${state.runId}&table=${encodeURIComponent(table)}&segment=${segment}`);
    if (!$("detailPanel").classList.contains("hidden")) renderDetail();
  } catch (_) { /* segment may still be pending */ }
}

/* ------------------------------------------------------------------ */
/* Context / writer                                                    */
/* ------------------------------------------------------------------ */

function renderContext(ctx) {
  state.ctx = ctx;
  $("connChips").innerHTML = `
    <div class="conn a"><div class="conn-name">A · ${esc(ctx.source_a.name)}</div><div class="conn-dsn" title="${esc(ctx.source_a.dsn)}">${esc(ctx.source_a.dsn)}</div></div>
    <div class="conn b"><div class="conn-name">B · ${esc(ctx.source_b.name)}</div><div class="conn-dsn" title="${esc(ctx.source_b.dsn)}">${esc(ctx.source_b.dsn)}</div></div>`;
  if (!ctx.writer) $("writerControls").style.display = "none";
  renderWriter(ctx.writer);
}

function renderWriter(w) {
  if (!w) return;
  const box = $("writerState");
  box.classList.toggle("live", !!w.running);
  box.classList.toggle("idle", !w.running);
  $("writerStateText").textContent = w.running
    ? `持续写入中 · A/B 位点 ${w.global_seq_a}/${w.global_seq_b} · 待同步 ${w.pending_b}`
    : `写入已暂停 · A/B 位点 ${w.global_seq_a}/${w.global_seq_b}`;
  $("btnWriterToggle").textContent = w.running ? "暂停持续写入" : "开始持续写入";
}

async function pollWriter() {
  try {
    const ctx = await api("/api/context");
    renderWriter(ctx.writer);
  } catch (_) { /* ignore */ }
}

/* ------------------------------------------------------------------ */
/* Actions                                                             */
/* ------------------------------------------------------------------ */

async function startLiveRun() {
  const strict = $("optStrict").checked;
  const { run_id } = await post("/api/runs", { mode: "live", strict_precision: strict });
  state.detailKey = null;
  $("detailPanel").classList.add("hidden");
  await loadRun(run_id);
}

async function startSnapshotRun() {
  const btn = $("btnRunSnapshot");
  btn.disabled = true;
  try {
    toast("正在停止写入并排空 A→B 复制延迟…");
    const snap = await post("/api/writer", { action: "prepare_snapshot" });
    if (snap.error || snap.token === null || snap.token === undefined) {
      toast("快照未能对齐：" + (snap.error || "位点不一致"));
      return;
    }
    toast(`两侧已对齐事件位点 ${snap.token}，以快照一致性读取`);
    const strict = $("optStrict").checked;
    const { run_id } = await post("/api/runs", {
      mode: "snapshot", snapshot_token: snap.token, strict_precision: strict,
    });
    state.detailKey = null;
    $("detailPanel").classList.add("hidden");
    await loadRun(run_id);
  } finally {
    btn.disabled = false;
    pollWriter();
  }
}

async function globalBurstAndRun() {
  try {
    const b = await post("/api/writer", { action: "burst", lag: 2.8 });
    toast(`已在 A 侧写入 seq ${b.burst.seq}（键段 #${b.burst.segment}：${b.burst.order_no}），B 将在 2.8s 后应用 —— 立即扫描可观察不可判定键段`);
    await startLiveRun();
  } catch (e) { toast(e.message); }
}

async function segmentBurst() {
  if (!state.detailKey) return;
  const { segment } = state.detailKey;
  try {
    const b = await post("/api/writer", { action: "burst", segment, lag: 3.0 });
    toast(`已向 #${segment} 段 A 侧写入 seq ${b.burst.seq}，B 侧 3s 后追平；请稍后点“延期复核此段”`);
    pollWriter();
  } catch (e) { toast(e.message); }
}

async function recheckSegment() {
  if (!state.detailKey) return;
  const btn = $("btnRecheck");
  btn.disabled = true;
  try {
    const d = await post("/api/recheck", {
      run_id: state.runId,
      table: state.detailKey.table,
      segment: state.detailKey.segment,
    });
    state.detail = d;
    renderDetail();
    const run = await api(`/api/runs/${state.runId}`);
    state.run = run;
    renderTiles(run.totals);
    renderBoards(run);
    overallBanner(run.totals, run.status);
    toast(`复核完成：${VERDICT_TEXT[d.verdict]}${d.reason_code ? "（" + d.reason_code + "）" : ""}`);
  } catch (e) {
    toast("复核失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function resetDemo() {
  if (!confirm("重建演示库？当前校对记录会清空，植入差异恢复初始状态。")) return;
  await post("/api/demo/reset", {});
  state.detailKey = null;
  $("detailPanel").classList.add("hidden");
  toast("演示数据已重置");
  const ctx = await api("/api/context");
  renderContext(ctx);
  await startLiveRun();
}

/* ------------------------------------------------------------------ */
/* Boot                                                                */
/* ------------------------------------------------------------------ */

async function boot() {
  try {
    const ctx = await api("/api/context");
    renderContext(ctx);
  } catch (e) {
    toast("无法加载上下文：" + e.message);
    return;
  }

  $("btnRunLive").addEventListener("click", startLiveRun);
  $("btnRunSnapshot").addEventListener("click", startSnapshotRun);
  $("btnBurst").addEventListener("click", globalBurstAndRun);
  $("btnReset").addEventListener("click", resetDemo);
  $("btnRecheck").addEventListener("click", recheckSegment);
  $("btnSegmentBurst").addEventListener("click", segmentBurst);
  $("btnCloseDetail").addEventListener("click", () => $("detailPanel").classList.add("hidden"));
  $("btnWriterToggle").addEventListener("click", async () => {
    const running = $("writerState").classList.contains("live");
    await post("/api/writer", { action: running ? "stop" : "start" });
    pollWriter();
  });
  $("historyRuns").addEventListener("change", (e) => loadRun(e.target.value));

  setInterval(pollWriter, 1500);
  pollWriter();

  // Instant first-run experience.
  await startLiveRun();
}

document.addEventListener("DOMContentLoaded", boot);
