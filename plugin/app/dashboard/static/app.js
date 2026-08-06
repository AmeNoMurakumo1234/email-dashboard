"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
};
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));

async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> " + r.status);
  return r.json();
}

// ---------- persistence: remember where you were across refreshes
// (localStorage, client-only — no cookies, nothing leaves the browser) ----------
const PERSIST_KEY = "email.ui.v1";
const ui = { view: "trash", scope: "all", date: "latest", category: null, concept: null,
             groupBy: "concept", hiddenSteam: [],
             query: "", page: 0, showAllCats: false, showAccounts: false };
const PAGE_SIZE = 50;
function loadUI() {
  try { Object.assign(ui, JSON.parse(localStorage.getItem(PERSIST_KEY) || "{}")); }
  catch (e) { /* corrupt or unavailable — fall back to defaults */ }
}
function persistUI() {
  try { localStorage.setItem(PERSIST_KEY, JSON.stringify(ui)); }
  catch (e) { /* storage blocked/full — non-fatal, just won't remember */ }
}

let currentDate = "latest";   // resolved actual run_date in view
let newestDate = null;        // the latest run; when selected we store date as "latest" so new runs follow

async function init() {
  loadUI();
  const runs = await get("/api/runs");
  const sel = $("#runSelect");
  sel.innerHTML = "";
  runs.forEach((r, i) => {
    const o = el("option");
    o.value = r.run_date;
    o.textContent = r.run_date + (i === 0 ? "  (latest)" : "");
    sel.appendChild(o);
  });
  newestDate = runs.length ? runs[0].run_date : null;
  // restore the run the user was on: an explicitly-picked older run sticks; "latest" follows new runs
  currentDate = (ui.date && ui.date !== "latest" && runs.some((r) => r.run_date === ui.date))
    ? ui.date : newestDate;
  if (currentDate) sel.value = currentDate;

  sel.addEventListener("change", () => {
    currentDate = sel.value;
    ui.date = (currentDate === newestDate) ? "latest" : currentDate;
    persistUI();
    loadRun();
  });
  document.querySelectorAll('input[name="scope"]').forEach((r) =>
    r.addEventListener("change", (e) => {
      ui.scope = e.target.value; ui.page = 0; persistUI();
      if (ui.view === "senders") loadSenders(); else loadTrash();
    }));
  document.querySelectorAll("#panelTabs .ptab").forEach((b) =>
    b.addEventListener("click", () => setView(b.dataset.view)));
  $("#steamRefresh").addEventListener("click", refreshSteam);

  // ---- search: debounced so typing does not fire a query per keystroke ----
  const search = $("#trashSearch");
  const clear = $("#trashClear");
  search.value = ui.query || "";
  clear.hidden = !ui.query;
  let debounce = null;
  search.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      ui.query = search.value.trim();
      ui.page = 0;
      clear.hidden = !ui.query;
      persistUI();
      loadResults();
    }, 200);
  });
  clear.addEventListener("click", () => {
    search.value = ""; ui.query = ""; ui.page = 0; clear.hidden = true; persistUI();
    loadResults();
    search.focus();
  });

  // ---- search scope (trashed only vs everything triaged) ----
  const applyDisp = () => {
    document.querySelectorAll("#dispScope .chip").forEach((b) =>
      b.classList.toggle("on", b.dataset.disp === dispScope()));
  };
  document.querySelectorAll("#dispScope .chip").forEach((b) =>
    b.addEventListener("click", () => {
      ui.disposition = b.dataset.disp; ui.page = 0; persistUI();
      applyDisp(); loadResults();
    }));
  applyDisp();

  // ---- rail grouping (canonical concepts vs the raw labels each run wrote) ----
  document.querySelectorAll("#groupScope .chip").forEach((b) =>
    b.addEventListener("click", () => setGroupBy(b.dataset.group)));

  // ---- message viewer controls ----
  $("#mvClose").addEventListener("click", mvClose);
  $("#msgModal").addEventListener("click", (e) => {
    if (e.target.id === "msgModal") mvClose();      // click the backdrop to dismiss
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#msgModal").hidden) mvClose();
  });
  const ackClick = async (kind) => {
    if (!mvCurrent) return;
    const on = kind === "message"
      ? !(mvCurrent.message_id && ackIndex.message.has(mvCurrent.message_id))
      : !ackIndex.thread.has(threadKeyOf(mvCurrent));
    try {
      await sendAck(mvCurrent, kind, on);
      mvPaintAck();
      renderSummary(lastSurfaced);   // the summary reflects it immediately
      loadHeatmap().catch(() => {}); // and the day's dot flips amber -> green right away
    } catch (e) {
      $("#mvAckState").textContent = "could not save: " + e.message;
    }
  };
  $("#mvAckBtn").addEventListener("click", () => ackClick("message"));
  $("#mvAckThread").addEventListener("click", () => ackClick("thread"));

  $("#mvModeText").addEventListener("click", () => { mvMode("text"); mvLoad(false); });
  $("#mvModeHtml").addEventListener("click", () => { mvMode("html"); mvLoad(true); });
  const setTheme = (t) => {
    ui.mailTheme = t; persistUI();
    mvPaintThemeChips();
    if (!$("#mvFrame").hidden) mvLoad(true);   // re-render in the new view
  };
  $("#mvThemeReader").addEventListener("click", () => setTheme("reader"));
  $("#mvThemeDark").addEventListener("click", () => setTheme("dark"));
  $("#mvThemeLight").addEventListener("click", () => setTheme("light"));

  $("#catToggle").addEventListener("click", () => {
    ui.showAllCats = !ui.showAllCats; persistUI(); renderCats();
  });

  const acctToggle = $("#acctToggle");
  const applyAcct = () => {
    $("#accounts").hidden = !ui.showAccounts;
    $("#accountStrip").hidden = !!ui.showAccounts;
    acctToggle.textContent = ui.showAccounts ? "hide details" : "show details";
  };
  acctToggle.addEventListener("click", () => {
    ui.showAccounts = !ui.showAccounts; persistUI(); applyAcct();
  });
  applyAcct();
  $("#pagePrev").addEventListener("click", () => {
    if (ui.page > 0) { ui.page--; persistUI(); loadResults(); }
  });
  $("#pageNext").addEventListener("click", () => {
    ui.page++; persistUI(); loadResults();
  });

  // reflect the saved trash scope in the radio before first render
  const scopeRadio = document.querySelector(`input[name="scope"][value="${ui.scope}"]`);
  if (scopeRadio) scopeRadio.checked = true;

  $("#wfShowDone").addEventListener("click", () => { wfShowDone = !wfShowDone; loadWorkflowActions(); });
  $("#hostShowDone").addEventListener("click", () => { hostShowDone = !hostShowDone; loadNewHosts(); });
  $("#amClose").addEventListener("click", () => { $("#acctModal").hidden = true; });
  $("#acctModal").addEventListener("click", (e) => {
    if (e.target.id === "acctModal") $("#acctModal").hidden = true;
  });
  $("#prClose").addEventListener("click", () => { $("#protModal").hidden = true; });
  $("#prSave").addEventListener("click", saveProtectedNames);
  document.addEventListener("click", (e) => {
    if (e.target.id === "protModal") $("#protModal").hidden = true;  // backdrop dismiss
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#protModal").hidden) $("#protModal").hidden = true;
    else if (!$("#acctModal").hidden) $("#acctModal").hidden = true;
  });

  loadSetup().catch(() => {});     // first thing a new install needs, and silent once done
  await loadAcks();                // before the first render, so state is right immediately
  await loadRun();
  loadWorkflowActions().catch(() => {}); // never let this panel block the rest of the page
  loadNewHosts().catch(() => {});        // same: a quiet panel must never break a loud one
  loadHeatmap().catch(() => {});   // decorative-adjacent: never block the run view on it
  setView(ui.view);   // restore the tab the user left on (trash | steam)
  $("#footer").textContent = `${runs.length} run(s) recorded · data refreshed each daily routine run`;
}

async function loadRun() {
  // No runs yet means no date to ask for. Sending encodeURIComponent(null) put the literal
  // string "null" on the wire, which the server then echoed back as a run date.
  const data = await get("/api/run" +
    (currentDate ? "?date=" + encodeURIComponent(currentDate) : ""));
  $("#subtitle").textContent = data.run_date ? `showing run for ${data.run_date}` : "no runs yet";
  $("#summaryDate").textContent = data.run_date ? `(${data.run_date})` : "";

  // KPIs
  const t = data.totals || {};
  lastRun = data;                       // the tiles and their drill-downs share one source
  $("#kpis").innerHTML = "";
  [["Fetched", t.fetched, "", "fetched"], ["Trashed", t.trashed, "trash", "trashed"],
   ["Kept / surfaced", t.kept, "kept", "kept"],
   ["OTP deleted", t.otp, "otp", "otp"]].forEach(([l, n, c, key]) => {
    const k = el("div", "kpi clickable " + c);
    k.appendChild(el("div", "n", n == null ? "0" : n));
    k.appendChild(el("div", "l", l));
    k.title = "click to see what is behind this number";
    k.addEventListener("click", () => openKpi(key));
    $("#kpis").appendChild(k);
  });

  renderAccounts(data.accounts || []);
  renderSummary(data.surfaced || []);
  await loadTrash();
}

function renderAccounts(accounts) {
  renderAccountStrip(accounts);
  const wrap = $("#accounts");
  wrap.innerHTML = "";
  if (!accounts.length) {
    wrap.appendChild(el("div", "empty", "No per-account status recorded for this run."));
    return;
  }
  accounts.forEach((a) => {
    const c = el("div", "account");
    const st = statusOf(a);
    const dot = st === "ok" ? "ok" : st === "fail" ? "fail" : st === "unknown" ? "unk" : "idle";
    const addr = el("div", "addr acct-link",
      `<span class="dot ${dot}"></span>${esc(a.account)}`);
    addr.title = "click for this mailbox's detail";
    addr.addEventListener("click", () => openAccount(a.account));
    c.appendChild(addr);
    if (a.role) c.appendChild(el("div", "role", esc(a.role)));
    const meta = el("div", "meta");
    meta.appendChild(el("span", null, esc(a.status || "—") + (a.auth ? ` · ${esc(a.auth)}` : "")));
    if (a.inbox_count != null) meta.appendChild(el("span", null, `inbox ${a.inbox_count}`));
    c.appendChild(meta);
    c.appendChild(el("div", "meta counts",
      `<span>fetched <b>${a.fetched || 0}</b></span>` +
      `<span>trashed <b>${a.trashed || 0}</b></span>` +
      `<span>kept <b>${a.kept || 0}</b></span>`));
    if (a.error) c.appendChild(el("div", "reason", "⚠️ " + esc(a.error)));
    wrap.appendChild(c);
  });
}

// Collapsed account view. A failed account must be impossible to miss even in one line,
// so the summary states the failure count first and the pill carries a red border.
// THREE states, not two. `ok` and `CONNECTED` were both being written to the store and only
// one was recognised, so four runs rendered every account as not-connected. Ingest now folds
// the synonyms on write; the reader stays forgiving too, AND reports anything it does not
// recognise as an explicit "unknown" rather than guessing. Guessing green hides an outage;
// guessing red cries wolf. Unknown is the honest third answer.
const STATUS_OK = new Set(["connected", "ok", "okay", "up", "healthy"]);
const STATUS_BAD = /fail|error|timeout|refus|denied|auth|down/i;

function statusOf(a) {
  const s = (a.status || "").trim();
  if (!s) return "idle";
  if (STATUS_OK.has(s.toLowerCase())) return "ok";
  if (STATUS_BAD.test(s)) return "fail";
  return "unknown";
}

function renderAccountStrip(accounts) {
  const strip = $("#accountStrip");
  const summary = $("#acctSummary");
  strip.innerHTML = "";
  if (!accounts.length) {
    summary.textContent = "- nothing recorded for this run";
    summary.style.color = "";
    return;
  }
  const n = (k) => accounts.filter((a) => statusOf(a) === k).length;
  const ok = n("ok"), bad = n("fail"), unk = n("unknown");
  const bits = [`${ok}/${accounts.length} connected`];
  if (bad) bits.unshift(`${bad} FAILED`);
  if (unk) bits.push(`${unk} unrecognised status`);
  summary.textContent = "- " + bits.join(", ");
  summary.style.color = bad ? "var(--red)" : (unk ? "var(--amber)" : "");

  accounts.forEach((a) => {
    const st = statusOf(a);
    const pill = el("div", "acct-pill" + (st === "fail" ? " fail" : ""));
    pill.title = `${a.account} - ${a.status || "no status recorded"}` +
      ` · fetched ${a.fetched || 0}, trashed ${a.trashed || 0}, kept ${a.kept || 0}` +
      (a.error ? ` · ${a.error}` : "") + "\nclick for this mailbox's detail";
    pill.style.cursor = "pointer";
    pill.addEventListener("click", () => openAccount(a.account));
    const short = String(a.account || "").split("@")[0];
    const dot = st === "ok" ? "ok" : st === "fail" ? "fail" : st === "unknown" ? "unk" : "idle";
    pill.innerHTML =
      `<span class="dot ${dot}"></span>` +
      `<span class="pn">${esc(short)}</span>` +
      `<span class="pc">${a.fetched || 0}/${a.trashed || 0}</span>` +
      (st === "unknown" ? `<span class="pc">?${esc(a.status)}</span>` : "");
    strip.appendChild(pill);
  });
}

const ORDER = ["action-needed", "family", "financial", "security", "info"];
const HEAD = {
  "action-needed": "🔴 Needs your attention", family: "👪 Family / people",
  financial: "💵 Financial", security: "🔐 Security", info: "ℹ️ FYI", other: "📌 Other",
};

let lastSurfaced = [];

function renderSummary(msgs) {
  lastSurfaced = msgs || [];
  const wrap = $("#summary");
  wrap.innerHTML = "";
  if (!msgs.length) {
    wrap.appendChild(el("div", "empty", "Nothing surfaced for this run."));
    return;
  }
  // Acknowledged items COLLAPSE, they do not disappear. Default closed so they stop
  // competing for attention; one click reopens them, because "I have seen this" must never
  // mean "you can never look at it again" - and an undo has to be reachable.
  const acked = msgs.filter(isAcked);
  msgs = msgs.filter((m) => !isAcked(m));
  const groups = {};
  msgs.forEach((m) => {
    const key = ORDER.includes(m.importance) ? m.importance : "other";
    (groups[key] = groups[key] || []).push(m);
  });
  [...ORDER, "other"].forEach((key) => {
    const items = groups[key];
    if (!items || !items.length) return;
    const g = el("div", "group");
    g.appendChild(el("h3", null, `${HEAD[key]} <span class="badge">${items.length}</span>`));
    items.forEach((m) => {
      const d = el("div", "msg " + (ORDER.includes(m.importance) ? m.importance : "info"));
      // The summary holds the mail that EARNED attention, so it is the most likely place
      // to want to open something. Clicking here opens the same sandboxed viewer.
      d.classList.add("msgrow");
      if (m.message_id) d.classList.add("linked");
      d.title = m.message_id
        ? "Open this message (sandboxed, images blocked)"
        : "Not linked - nothing was recorded that can identify this message";
      d.appendChild(el("div", "subj", esc(m.subject || "(no subject)")));
      d.appendChild(el("div", "from", esc(m.sender || "")));
      if (m.reason) d.appendChild(el("div", "reason", esc(m.reason)));
      d.appendChild(el("div", "acct", esc(m.account)));
      d.addEventListener("click", () => mvOpen(m));
      g.appendChild(d);
    });
    wrap.appendChild(g);
  });

  if (acked.length) {
    const det = el("details", "acked-fold");
    if (ui.showAcked) det.open = true;
    const sum = el("summary", null,
      `${acked.length} acknowledged - still on file, click to look again`);
    det.appendChild(sum);
    acked.forEach((m) => {
      const d = el("div", "msg info msgrow" + (m.message_id ? " linked" : "") + " done");
      d.title = "Open this message";
      d.appendChild(el("div", "subj", esc(m.subject || "(no subject)")));
      d.appendChild(el("div", "from", esc(m.sender || "")));
      if (m.reason) d.appendChild(el("div", "reason", esc(m.reason)));
      d.appendChild(el("div", "acct", esc(m.account)));
      d.addEventListener("click", () => mvOpen(m));
      det.appendChild(d);
    });
    // remember whether the fold was left open, like every other view state here
    det.addEventListener("toggle", () => { ui.showAcked = det.open; persistUI(); });
    wrap.appendChild(det);
  }
}

// How many categories the rail shows before collapsing. Raw labels drift - many live
// labels end up standing for about 12 real concepts - so the long tail is noise that
// should not push the useful rows out of reach. Group by Concepts to collapse it.
const CAT_HEAD = 12;

let trashStats = null;   // last stats payload, so the rail can re-render without a refetch
let lastLabel = null;    // human label of the selected category, for the results title

function isRunScope() {
  const scope = (document.querySelector('input[name="scope"]:checked') || {}).value || ui.scope;
  return scope === "run";
}

async function loadTrash() {
  const isRun = isRunScope();
  $("#trashScopeLabel").textContent = isRun ? `(${currentDate})` : "(all time)";
  const sp = new URLSearchParams({ disposition: dispScope() });
  if (isRun) sp.set("date", currentDate);
  trashStats = await get("/api/trash/stats?" + sp.toString());
  renderCats();

  // Restore the drill-down the user had open, if that row still exists in scope.
  // With nothing selected and no search this falls through to "all trashed mail, paged" rather
  // than an empty pane with a prompt: an empty pane is what the old view effectively gave
  // (the results rendered below the fold), and landing on real rows is strictly more useful
  // than being told to go and click something. It also keeps ONE behaviour - first load,
  // clearing a search, and deselecting a row all end in the same honest place.
  const saved = railRows().find((r) => r.key === selectedKey());
  if (saved) lastLabel = saved.label;
  else { ui.category = null; ui.concept = null; lastLabel = null; }
  await loadResults();
}

// ONE shape for the rail whichever grouping is active, so every downstream reader (render,
// selection, restore, the results title) works off the same {key, label, n} and cannot drift
// between the two modes.
function railRows() {
  if (!trashStats) return [];
  if (ui.groupBy === "concept") {
    return (trashStats.by_concept || []).map((c) => ({
      key: c.key || c.concept, label: c.concept, n: c.n,
    }));
  }
  return (trashStats.by_category || []).map((c) => ({
    key: c.category || "other", label: c.label, n: c.n,
  }));
}

function selectedKey() {
  return ui.groupBy === "concept" ? ui.concept : ui.category;
}

function renderCats() {
  const cats = $("#trashCats");
  const toggle = $("#catToggle");
  cats.innerHTML = "";
  if (!trashStats || !trashStats.total) {
    cats.appendChild(el("div", "empty", "No trashed mail in scope."));
    toggle.hidden = true;
    markActiveCat();   // the grouping chips still have to show which mode is live
    return;
  }
  const all = railRows();
  const shown = ui.showAllCats ? all : all.slice(0, CAT_HEAD);
  const max = Math.max(...all.map((c) => c.n));

  shown.forEach((c) => {
    const row = el("div", "cat-row");
    row.dataset.cat = c.key;
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    row.title = `${c.label} - ${c.n} message(s)`;
    const block = el("div", "cat-block");
    block.style.flex = "1";
    block.appendChild(el("div", "cn",
      `<span class="cn">${esc(c.label)}</span> <span class="cv">${c.n}</span>`));
    const bar = el("div", "bar");
    bar.style.width = Math.max(6, Math.round((c.n / max) * 100)) + "%";
    block.appendChild(bar);
    row.appendChild(block);
    const pick = () => selectCategory(c.key, c.label);
    row.addEventListener("click", pick);
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
    });
    cats.appendChild(row);
  });

  // Never hide rows without saying so. A silently truncated list reads as a complete one.
  const hiddenN = all.length - shown.length;
  if (all.length > CAT_HEAD) {
    toggle.hidden = false;
    toggle.textContent = ui.showAllCats
      ? `show top ${CAT_HEAD}`
      : `+${hiddenN} more`;
  } else {
    toggle.hidden = true;
  }
  markActiveCat();
}

function markActiveCat() {
  const sel = selectedKey();
  document.querySelectorAll(".cat-row").forEach((r) =>
    r.classList.toggle("active", sel != null && r.dataset.cat === sel));
  document.querySelectorAll("#groupScope .chip").forEach((b) =>
    b.classList.toggle("on", b.dataset.group === ui.groupBy));
  const t = $("#railTitle");
  if (t) t.textContent = ui.groupBy === "concept" ? "Concepts" : "Raw labels";
}

function selectCategory(key, label) {
  const k = key || "other";
  const field = ui.groupBy === "concept" ? "concept" : "category";
  // clicking the selected row again clears it - back to "everything"
  if (ui[field] === k) { ui[field] = null; lastLabel = null; }
  else { ui[field] = k; lastLabel = label; }
  ui.page = 0;
  persistUI();
  markActiveCat();
  loadResults();
}

// Switching grouping CLEARS the other mode's selection rather than carrying it silently.
// A concept filter left armed while the rail shows raw labels would filter the results by
// something the screen no longer displays - the same "unstated scope" defect as a search
// that was secretly locked to one pile.
function setGroupBy(mode) {
  if (ui.groupBy === mode) return;
  ui.groupBy = mode;
  ui.category = null;
  ui.concept = null;
  lastLabel = null;
  ui.page = 0;
  ui.showAllCats = false;
  persistUI();
  renderCats();
  loadResults();
}

// Which pile the search looks in. Defaults to 'trashed' so the Trash tab keeps its meaning.
function dispScope() {
  // The Kept tab is the INVERSE of the trash tab and reuses the same two-pane machinery,
  // so the disposition comes from the active view rather than from the search-scope chips
  // (which are hidden there - on a panel that is by definition about kept mail, a
  // "trashed only" toggle would be a contradiction sitting in the corner).
  if (ui.view === "kept") return "kept";
  return ui.disposition === "all" ? "all" : "trashed";
}

async function loadResults() {
  const isRun = isRunScope();
  const params = new URLSearchParams();
  if (ui.groupBy === "concept") {
    if (ui.concept) params.set("concept", ui.concept);
  } else if (ui.category) {
    params.set("category", ui.category === "other" ? "" : ui.category);
  }
  if (isRun) params.set("date", currentDate);
  if (ui.query) params.set("q", ui.query);
  params.set("disposition", dispScope());
  params.set("limit", PAGE_SIZE);
  params.set("offset", ui.page * PAGE_SIZE);

  const res = await get("/api/trash/list?" + params.toString());
  const { total, offset, limit, items } = res;

  // A page that has run off the end of a shrinking result set shows nothing and looks broken.
  if (!items.length && total > 0 && offset >= total) {
    ui.page = 0; persistUI(); return loadResults();
  }

  // Always say WHICH slice of WHAT. A bare count with no denominator is how a filtered
  // view starts lying about its own scope.
  const scope = dispScope();
  const all = scope === "all";
  const PILE = { all: "all triaged mail", kept: "kept + surfaced mail",
                 trashed: "trashed only" };
  const BASE = { all: "All triaged mail", kept: "All kept + surfaced mail",
                 trashed: "All trashed mail" };
  const scopeBits = [];
  if (selectedKey()) scopeBits.push(lastLabel || selectedKey());
  if (ui.query) scopeBits.push(`matching "${ui.query}"`);
  // Name the pile every time. "no matches" is only honest when it says what was searched.
  const what = scopeBits.length
    ? scopeBits.join(" ") + ` (${PILE[scope] || scope})`
    : (BASE[scope] || scope);
  const from = total ? offset + 1 : 0;
  const to = Math.min(offset + limit, total);
  $("#drillTitle").textContent = total
    ? `${what} - showing ${from}-${to} of ${total}`
    : `${what} - no matches`;

  const d = $("#drill");
  d.innerHTML = "";
  if (!total) {
    // An empty state must name the pile it searched. "Nothing matches" over a silently
    // narrowed set is the failure this whole scope control exists to remove.
    const pile = PILE[scope] || scope;
    d.appendChild(el("div", "empty",
      ui.query
        ? `No ${pile} matches "${esc(ui.query)}" in this scope.` +
          (scope === "trashed"
            ? " Try \"Everything I triaged\" - kept and surfaced mail is not searched here."
            : (scope === "kept"
               ? " This tab only searches mail I did NOT bin; the Trash stats tab has the rest."
               : ""))
        : `No ${pile} in this category.`));
    $("#trashPager").hidden = true;
    return;
  }

  // In all-mail scope every row needs its OWN verdict: a surfaced bill and a trashed promo
  // sitting in one list with a "Why trashed" header would misread every kept row as binned.
  const table = el("table");
  table.innerHTML =
    "<colgroup><col class='c-date'><col class='c-acct'>" +
    (all ? "<col class='c-disp'>" : "") +
    "<col class='c-from'><col class='c-subj'><col class='c-why'></colgroup>" +
    "<thead><tr><th>Date</th><th>Account</th>" +
    (all ? "<th>What I did</th>" : "") +
    "<th>From</th><th>Subject</th>" +
    (all ? "<th>Why</th>" : "<th>Why trashed</th>") + "</tr></thead>";
  const tb = el("tbody");
  items.forEach((m) => {
    const tr = el("tr");
    const disp = String(m.disposition || "");
    const linked = !!m.message_id;
    tr.className = "msgrow" + (linked ? " linked" : "");
    tr.title = linked ? "Open this message (sandboxed, images blocked)"
                      : "Not linked - triaged before message linking existed";
    tr.innerHTML = `<td class="date">${esc(m.run_date)}</td>` +
      `<td class="acctcell">${esc(m.account)}</td>` +
      (all ? `<td><span class="disp ${esc(disp)}">${esc(disp || "?")}</span></td>` : "") +
      `<td>${hl(m.sender)}</td><td>${hl(m.subject)}</td><td>${hl(m.reason)}</td>`;
    tr.addEventListener("click", () => mvOpen(m));
    tb.appendChild(tr);
  });
  table.appendChild(tb);
  d.appendChild(table);
  $("#drillScroll").scrollTop = 0;

  const pages = Math.ceil(total / limit);
  const pager = $("#trashPager");
  pager.hidden = pages <= 1;
  $("#pageLabel").textContent = `page ${ui.page + 1} of ${pages}`;
  $("#pagePrev").disabled = ui.page <= 0;
  $("#pageNext").disabled = ui.page >= pages - 1;
}

// Highlight the search term in results so a hit is findable inside a long "why trashed".
function hl(s) {
  const safe = esc(s);
  if (!ui.query) return safe;
  const needle = esc(ui.query).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return safe.replace(new RegExp(needle, "gi"), (m) => `<mark>${m}</mark>`);
}

// ---------- Top senders: who actually fills the bin ----------

async function loadSenders() {
  const isRun = isRunScope();
  const q = isRun ? "?date=" + encodeURIComponent(currentDate) : "";
  const data = await get("/api/trash/senders" + q);
  const wrap = $("#sendersList");
  wrap.innerHTML = "";
  if (!data.senders.length) {
    wrap.appendChild(el("div", "empty", "No trashed mail in scope."));
    return;
  }
  const max = data.senders[0].n;
  const note = el("div", "muted");
  note.style.marginBottom = "10px";
  const folded = data.raw_rows - data.distinct_senders;
  note.textContent =
    `top ${data.showing} of ${data.distinct_senders} senders ` +
    `${isRun ? "in this run" : "all time"} - click one to see every message it sent` +
    (folded > 0 ? ` · ${folded} spelling variant(s) folded together` : "");
  wrap.appendChild(note);

  data.senders.forEach((s) => {
    const row = el("div", "sender-row");
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    row.appendChild(el("div", "sname", esc(s.sender)));
    row.appendChild(el("div", "sn", String(s.n)));
    const span = s.first_seen === s.last_seen
      ? s.first_seen : `${s.first_seen} to ${s.last_seen}`;
    // Never hide a merge. If two stored spellings were folded, say so and show the split.
    const variants = s.variant_count > 1
      ? ` · ${s.variant_count} spellings: ` +
        s.variants.map((v) => `${v.raw || "(blank)"} (${v.n})`).join(", ")
      : "";
    row.appendChild(el("div", "smeta", esc(span + variants)));
    const bar = el("div", "sbar");
    bar.style.width = Math.max(4, Math.round((s.n / max) * 100)) + "%";
    row.appendChild(bar);
    // Opens the sender's whole story. The old behaviour - jumping straight to a search -
    // is kept as a button INSIDE that panel, so nothing is lost, it is just no longer the
    // only thing a click can do.
    const pick = () => openSender(s.key);
    row.addEventListener("click", pick);
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
    });
    wrap.appendChild(row);
  });
}

// ---------- Steam sales panel ----------

function setView(view) {
  ui.view = view;
  persistUI();
  document.querySelectorAll("#panelTabs .ptab").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view));
  // The Kept tab REUSES the trash two-pane view rather than duplicating it - same rail,
  // same search, same pager, opposite pile. Duplicating the markup would mean every future
  // fix has to be made twice, which is how the two halves drift apart.
  const twoPane = (view === "trash" || view === "kept");
  $("#trashView").hidden = !twoPane;
  $("#sendersView").hidden = view !== "senders";
  $("#steamView").hidden = view !== "steam";
  $("#quietView").hidden = view !== "quiet";
  $("#repeatsView").hidden = view !== "repeats";
  $("#dispScope").hidden = view === "kept";      // fixed scope here; the chips would lie
  // railTitle belongs to the grouping control (Concepts / Raw labels) - which pile we are
  // looking at is carried by the active tab and by the results title, not by hijacking a
  // label that means something else.
  $("#trashSearch").placeholder = view === "kept"
    ? "Search kept + surfaced mail - sender, subject, why, account"
    : "Search - sender, subject, why, account";
  // The scope radios apply to both trash and senders (both are "in this run vs all time").
  // Neither the Steam panel nor the quiet panel is run-scoped - "gone quiet" is a question
  // about the WHOLE history by definition, so showing a run filter beside it would invite
  // exactly the scope confusion this panel exists to cure.
  $("#trashScope").hidden = (view === "steam" || view === "quiet" || view === "repeats");
  $("#steamScope").hidden = view !== "steam";
  if (view === "steam") loadSteam();
  else if (view === "senders") loadSenders();
  else if (view === "quiet") loadQuiet();
  else if (view === "repeats") loadRepeats();
  // The rail is disposition-scoped, so switching between trash and kept must RE-FETCH the
  // stats, not just re-filter the results - otherwise the kept tab shows trash categories.
  else loadTrash();
}

// ---------- Message viewer ----------
// Nothing in here ever inserts message markup into THIS document. The sanitised HTML only
// ever reaches a sandboxed iframe via srcdoc; the plain-text view goes into a <pre> as
// textContent, never innerHTML. Those are the two rules that keep a hostile message from
// touching the dashboard itself.

let mvCurrent = null;

function mvOpen(row) {
  mvCurrent = row;
  $("#msgModal").hidden = false;
  $("#mvSubject").textContent = row.subject || "(no subject)";
  $("#mvMeta").textContent = `${row.sender || "unknown sender"} - ${row.account} - ${row.run_date}`;
  $("#mvText").textContent = "Loading...";
  $("#mvFrame").hidden = true;
  $("#mvText").hidden = false;
  $("#mvSafety").innerHTML = "";
  $("#mvFoot").textContent = "";
  mvMode("text");
  mvPaintAck();

  if (!row.message_id) { mvUnlinked(); return; }
  mvLoad(false);
}

// The honest explanation for an unopenable row. Deliberately distinguishes "I never
// recorded a way to find this" from "the mail server says it is gone" - they are different
// facts and only one of them means something is missing.
function mvUnlinked() {
  $("#mvFrame").hidden = true;
  $("#mvText").hidden = false;
  $("#mvSafety").innerHTML = '<span class="bad">not linked</span> - nothing was recorded ' +
    "that can identify this message";
  $("#mvFoot").textContent = "";
  $("#mvModeHtml").disabled = true;
  const trashed = mvCurrent && mvCurrent.disposition === "trashed";
  $("#mvText").textContent =
    "This row is not linked to a message, so there is nothing to open.\n\n" +
    "It is NOT that the message is missing from your mailbox - it is that this row never " +
    "recorded anything that could identify it. Message linking started once.\n\n" +
    (trashed
      ? "This one was trashed, and providers purge trash after about 30 days, so for older\n" +
        "trashed mail the message itself is genuinely gone and no amount of linking would\n" +
        "bring it back."
      : "This one was kept or surfaced, so the message is most likely still sitting in the\n" +
        "mailbox - it just cannot be located from this row. Running\n" +
        "  python dashboard/backfill_message_ids.py --apply\n" +
        "links the historical rows whose subject matches exactly one message.") +
    "\n\nIt is shown as unopenable rather than guessed at: matching on sender and subject " +
    "alone could open the WRONG email, which is not an acceptable failure for a viewer " +
    "whose whole job is inspecting untrusted mail.";
}

async function mvLoad(wantHtml) {
  const row = mvCurrent;
  // GUARD HERE TOO, not only in mvOpen. The mode buttons call straight into this, so an
  // unlinked row used to send message_id=null to the server, which then honestly answered
  // "not found in this mailbox" - a much more alarming claim than the truth, which is that
  // nothing was ever recorded to find it by. Two different failures wearing one message is
  // how a reader concludes their kept mail has gone missing.
  if (!row || !row.message_id) { mvUnlinked(); return; }
  const p = new URLSearchParams({ account: row.account, message_id: row.message_id });
  if (wantHtml) { p.set("html", "1"); p.set("theme", mailTheme()); }
  let res;
  try {
    res = await get("/api/message?" + p.toString());
  } catch (e) {
    $("#mvText").textContent = "Could not reach the mail server: " + e;
    return;
  }
  if (!res.ok) {
    $("#mvText").textContent =
      (res.error || "could not retrieve") +
      "\n\nTrashed mail is recoverable for about 30 days; older items may be gone.";
    return;
  }

  const r = res.report || {};
  const auth = (res.headers && res.headers.authentication_results) || "";
  const verdict = /dmarc=pass/i.test(auth)
    ? '<span class="ok">DMARC pass</span>'
    : (auth ? '<span class="bad">DMARC not confirmed</span>' : "");
  const bits = [];
  if (r.images_blocked) bits.push(`${r.images_blocked} images blocked`);
  if (r.remote_refs_stripped) bits.push(`${r.remote_refs_stripped} remote refs stripped`);
  if (r.links_defanged) bits.push(`${r.links_defanged} links defanged`);
  if (r.scripts_removed) bits.push(`<b>${r.scripts_removed} scripts removed</b>`);
  if (r.frames_removed) bits.push(`<b>${r.frames_removed} frames/objects removed</b>`);
  const hosts = (r.external_hosts || []).slice(0, 8);
  $("#mvSafety").innerHTML =
    (verdict ? verdict + " &middot; " : "") +
    (bits.length ? bits.join(" &middot; ") : "nothing to strip") +
    (hosts.length
      ? `<div class="mv-hosts">would have contacted: ${hosts.map(esc).join(", ")}` +
        `${(r.external_hosts || []).length > hosts.length ? " ..." : ""}</div>`
      : "");

  $("#mvFoot").textContent =
    `${res.bytes} bytes` +
    (res.attachments && res.attachments.length
      ? ` - ${res.attachments.length} attachment(s) listed but NOT downloaded: ` +
        res.attachments.map((a) => `${a.name} (${a.type})`).join(", ")
      : " - no attachments") +
    ". Opening this did not mark it read.";

  if (wantHtml && res.html) {
    // REVEAL BEFORE LOADING. Assigning srcdoc to an iframe that is still display:none
    // means the document never commits, and un-hiding it afterwards does not re-trigger
    // the load - so the panel stayed permanently blank while the markup sat right there
    // in the attribute. Proved by rendering the identical document in a visible iframe at
    // three different sandbox settings: all three painted it correctly, so neither the
    // sandbox nor the CSP was ever the problem, only the order of these two lines.
    $("#mvText").hidden = true;
    $("#mvFrame").hidden = false;
    // ASSIGN AFTER LAYOUT COMMITS, not merely after un-hiding. Setting hidden=false and
    // srcdoc in the same task still assigns while the frame is display:none as far as
    // style recalc is concerned, so the document never commits and the panel stays blank
    // with the full markup sitting right there in the attribute.
    //
    // The flush is a forced synchronous reflow, NOT requestAnimationFrame. rAF does not
    // fire while the page is hidden or backgrounded, so an rAF version leaves the viewer
    // permanently blank for anyone whose tab is not focused - it failed exactly that way
    // here on the first attempt. Reading offsetHeight is deterministic and visibility
    // independent.
    void $("#mvFrame").offsetHeight;
    $("#mvFrame").srcdoc = res.html;      // sanitised server-side, CSP inside, sandboxed
  } else {
    $("#mvText").textContent =
      res.text ||
      (res.has_html
        ? "This message has no plain-text part - it is HTML only.\n\n" +
          "Use \"Show HTML\" above to view it sanitised, with every image and remote " +
          "reference blocked."
        : "(this message has no readable body)");
    $("#mvText").hidden = false;
    $("#mvFrame").hidden = true;
  }
  $("#mvModeHtml").disabled = !res.has_html;
}

// Reader is the DEFAULT formatted view: the sender's layout is what breaks, so the view
// that discards it is the one that should be reached for first.
function mailTheme() {
  return ["reader", "dark", "light"].includes(ui.mailTheme) ? ui.mailTheme : "reader";
}

function mvPaintThemeChips() {
  const t = mailTheme();
  $("#mvThemeReader").classList.toggle("on", t === "reader");
  $("#mvThemeDark").classList.toggle("on", t === "dark");
  $("#mvThemeLight").classList.toggle("on", t === "light");
}

function mvMode(mode) {
  $("#mvModeText").classList.toggle("on", mode === "text");
  $("#mvModeHtml").classList.toggle("on", mode === "html");
  // The view picker only means anything for the rendered HTML, so it appears with it.
  $("#mvThemes").hidden = mode !== "html";
  mvPaintThemeChips();
}

function mvClose() {
  $("#msgModal").hidden = true;
  $("#mvFrame").srcdoc = "";              // drop the message from the DOM entirely
  $("#mvFrame").hidden = true;
  mvCurrent = null;
}

// ---------- Sender detail, and the rule button ----------

async function openSender(key) {
  const modal = $("#acctModal");
  modal.hidden = false;
  $("#amTitle").textContent = key;
  $("#amMeta").textContent = "loading...";
  $("#amBody").innerHTML = "";
  let s;
  try {
    s = await get("/api/sender?key=" + encodeURIComponent(key));
  } catch (e) {
    $("#amMeta").textContent = "could not load: " + e.message; return;
  }
  if (!s.found) { $("#amMeta").textContent = "no messages recorded"; return; }

  const binPct = s.total ? Math.round((s.binned / s.total) * 100) : 0;
  $("#amMeta").textContent =
    `${s.total} messages over ${s.runs} runs - ${s.first_seen} to ${s.last_seen}` +
    (s.quiet ? "  -  QUIET: silent longer than it has ever been" : "");

  const kpi = (n, l, cls) =>
    `<div class="kpi ${cls || ""}"><div class="n">${n}</div><div class="l">${l}</div></div>`;

  // THE VERDICT, not just numbers: rule 8 exists to turn a pattern like this into a rule.
  const r = s.rule || {};
  let ruleBox;
  if (s.already_ruled) {
    ruleBox = `<div class="rule-box on"><b>Locked to auto-trash.</b> Every run bins this ` +
      `sender without review. <button class="btn" id="ruleLift">Lift the rule</button></div>`;
  } else if (r.eligible) {
    ruleBox = `<div class="rule-box go"><b>Filter candidate.</b> Binned ` +
      `<b>${r.binned} of ${r.total}</b> across ${r.runs} runs, never once kept. ` +
      `Rule 8 says a sender like this gets locked to auto-trash once you confirm it.` +
      `<button class="btn primary" id="ruleAdd">Always trash this sender</button></div>`;
  } else {
    ruleBox = `<div class="rule-box no"><b>Not a filter candidate.</b> ` +
      `${esc(r.why || "")}</div>`;
  }

  const cmax = Math.max(1, ...(s.by_concept || []).map((c) => c.n));
  $("#amBody").innerHTML =
    `<div class="kpis am-kpis">${kpi(s.total, "messages")}` +
    `${kpi(s.binned, `binned (${binPct}%)`, "trash")}` +
    `${kpi(s.kept, "kept / surfaced", "kept")}` +
    `${kpi(s.runs, "runs seen in")}</div>` +
    ruleBox +
    `<div class="am-sec"><h4>When they write</h4><div class="am-spark">` +
    `${spark((s.activity || []).map((a) => ({ run_date: a.run_date, n: a.n, trashed: 0 })))}` +
    `</div>` +
    (s.worst_gap != null
      ? `<div class="muted" style="font-size:11.5px;margin-top:4px">silent ` +
        `${s.silence} runs; longest gap before now was ${s.worst_gap}</div>` : "") +
    "</div>" +
    `<div class="am-sec"><h4>Which mailbox</h4>` +
    (s.by_account || []).map((a) =>
      `<div class="am-row"><span>${esc(a.account)}</span><b>${a.n}</b></div>`).join("") +
    "</div>" +
    `<div class="am-sec"><h4>What they send</h4>` +
    (s.by_concept || []).map((c) =>
      `<div class="am-bar"><span class="am-lab">${esc(c.concept)}</span>` +
      `<span class="am-track"><i style="width:${Math.round((c.n / cmax) * 100)}%"></i></span>` +
      `<span class="am-n">${c.n}</span></div>`).join("") + "</div>" +
    `<div class="am-sec"><h4>Where their links go ` +
    `<span class="muted">${s.profile_established ? "established profile" :
      "too little history to judge new hosts"}</span></h4>` +
    ((s.hosts || []).length
      ? (s.hosts).slice(0, 10).map((h) =>
        `<div class="am-row"><span>${esc(h.host)}</span><b>${h.messages}</b></div>`).join("")
      : '<div class="muted">no link hosts recorded</div>') + "</div>" +
    (s.variants.length > 1
      ? `<div class="am-sec"><h4>Spellings folded together</h4>` +
        s.variants.map((v) => `<div class="am-row"><span>${esc(v)}</span></div>`).join("") +
        "</div>" : "") +
    `<div class="am-sec"><h4>Recent</h4>` +
    (s.recent || []).map((m) =>
      `<div class="kpi-msg msgrow${m.message_id ? " linked" : ""}">` +
      `<div class="kpi-subj">${esc(m.subject || "")}</div>` +
      `<div class="kpi-from">${esc(m.run_date)} - ${esc(m.disposition)}</div>` +
      (m.reason ? `<div class="kpi-why">${esc(m.reason)}</div>` : "") + "</div>").join("") +
    "</div>";

  $("#amBody").querySelectorAll(".kpi-msg").forEach((el2, i) => {
    el2.addEventListener("click", () => { modal.hidden = true; mvOpen(s.recent[i]); });
  });

  // The old one-click behaviour, preserved as an explicit action.
  const seeAll = el("button", "btn", "See all their mail &rarr;");
  seeAll.addEventListener("click", () => {
    // Search on the grouped KEY, not the longest raw variant: the key is a substring of
    // every spelling, so it finds them all. The full "Name <addr>" form would silently
    // miss rows stored as the bare name.
    ui.query = key; ui.category = null; ui.concept = null; ui.page = 0;
    ui.disposition = "all";
    persistUI();
    $("#trashSearch").value = key;
    $("#trashClear").hidden = false;
    document.querySelectorAll("#dispScope .chip").forEach((b) =>
      b.classList.toggle("on", b.dataset.disp === "all"));
    modal.hidden = true;
    setView("trash");
  });
  $("#amBody").appendChild(seeAll);

  const setRule = async (on) => {
    const btn = $(on ? "#ruleAdd" : "#ruleLift");
    btn.disabled = true;
    btn.textContent = on ? "writing the rule..." : "lifting...";
    try {
      const res = await fetch("/api/sender-rule", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
        body: JSON.stringify({ key, on, label: key }),
      }).then((x) => x.json());
      if (!res.ok) throw new Error(res.error || "refused");
      openSender(key);                     // re-render from the server's new truth
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "could not: " + e.message;
    }
  };
  if ($("#ruleAdd")) $("#ruleAdd").addEventListener("click", () => setRule(true));
  if ($("#ruleLift")) $("#ruleLift").addEventListener("click", () => setRule(false));
}

// ---------- KPI drill-downs ----------
// Built from the run payload ALREADY fetched, not a second endpoint. A separate query
// could disagree with the number on the tile, and a tile that disagrees with its own
// drill-down is worse than no drill-down.

let lastRun = null;      // the /api/run payload behind the current tiles
let allTime = null;      // totals from the calendar, for context

const KPI_DEFS = {
  fetched: {
    title: "Everything I looked at",
    blurb: "Every message triaged in this run, whatever became of it.",
    pick: (d) => [...(d.surfaced || []), ...(d.trashed || [])],
    groupBy: "account",
  },
  trashed: {
    title: "What I binned, and why",
    blurb: "Moved to the provider's Trash - never permanently deleted, and every one " +
           "journalled with its reason. Recoverable for about 30 days.",
    pick: (d) => d.trashed || [],
    groupBy: "concept",
  },
  kept: {
    title: "What I kept or surfaced",
    blurb: "Mail that earned a place in the record - filed, or put in front of you.",
    pick: (d) => d.surfaced || [],
    groupBy: "importance",
  },
  otp: {
    title: "One-time codes deleted",
    blurb: "Rule 1: a login code is dead the moment it is read, so these go on sight - " +
           "journalled like everything else.",
    pick: (d) => [...(d.surfaced || []), ...(d.trashed || [])]
      .filter((m) => /otp|verification|one.?time|passcode/i.test(
        `${m.category || ""} ${m.subject || ""}`)),
    groupBy: "account",
  },
};

function openKpi(which) {
  const def = KPI_DEFS[which];
  if (!def || !lastRun) return;
  const items = def.pick(lastRun);
  const modal = $("#acctModal");
  modal.hidden = false;
  $("#amTitle").textContent = def.title;
  $("#amMeta").textContent =
    `${items.length} message${items.length === 1 ? "" : "s"} - run of ` +
    `${lastRun.run_date || "?"}`;

  const groups = {};
  items.forEach((m) => {
    let k = m[def.groupBy] || (def.groupBy === "importance" ? "unranked" : "unknown");
    (groups[k] = groups[k] || []).push(m);
  });
  const order = Object.keys(groups).sort((a, b) => groups[b].length - groups[a].length);

  // All-time context, so a daily number is never read as the whole story.
  let context = "";
  if (allTime) {
    const map = { fetched: allTime.messages, trashed: allTime.trashed,
                  kept: allTime.kept };
    if (map[which] != null) {
      context = `<div class="am-context">${items.length} today &middot; ` +
        `<b>${map[which]}</b> across all ${allTime.runs} runs</div>`;
    }
  }

  const body = !items.length
    ? `<div class="empty">${which === "otp"
        ? "No one-time codes arrived in this run. That is the normal case - they only " +
          "appear when you have just logged in somewhere."
        : "Nothing in this bucket for this run."}</div>`
    : order.map((k) => {
      const rows_ = groups[k].map((m) => {
        const linked = m.message_id ? " linked" : "";
        return `<div class="kpi-msg msgrow${linked}" data-mid="${esc(m.message_id || "")}">` +
          `<div class="kpi-subj">${esc(m.subject || "(no subject)")}</div>` +
          `<div class="kpi-from">${esc(m.sender || "")}</div>` +
          (m.reason ? `<div class="kpi-why">${esc(m.reason)}</div>` : "") + "</div>";
      }).join("");
      return `<div class="am-sec"><h4>${esc(k)} <span class="muted">` +
        `${groups[k].length}</span></h4>${rows_}</div>`;
    }).join("");

  $("#amBody").innerHTML =
    `<div class="am-blurb">${esc(def.blurb)}</div>` + context + body;

  // Rows open the message viewer, same as everywhere else.
  $("#amBody").querySelectorAll(".kpi-msg").forEach((el2, i) => {
    const flat = order.flatMap((k) => groups[k]);
    el2.addEventListener("click", () => {
      $("#acctModal").hidden = true;
      mvOpen(flat[i]);
    });
  });
}

// ---------- Account detail: each box has a different JOB ----------

function spark(days, w = 240, h = 26) {
  if (!days.length) return "";
  const max = Math.max(...days.map((d) => d.n)) || 1;
  const step = w / Math.max(days.length, 1);
  const bars = days.map((d, i) => {
    const bh = Math.max(1, Math.round((d.n / max) * (h - 2)));
    return `<rect x="${(i * step).toFixed(1)}" y="${h - bh}" ` +
      `width="${Math.max(1, step - 1).toFixed(1)}" height="${bh}" fill="#4b7bd6">` +
      `<title>${esc(d.run_date)}: ${d.n} triaged, ${d.trashed} binned</title></rect>`;
  }).join("");
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" ` +
         `preserveAspectRatio="none" role="img" aria-label="messages per run">${bars}</svg>`;
}

async function openAccount(addr) {
  const modal = $("#acctModal");
  modal.hidden = false;
  $("#amTitle").textContent = addr;
  $("#amMeta").textContent = "loading...";
  $("#amBody").innerHTML = "";
  let a;
  try {
    a = await get("/api/account?account=" + encodeURIComponent(addr));
  } catch (e) {
    $("#amMeta").textContent = "could not load: " + e.message;
    return;
  }
  const t = a.totals || {};
  const pct = t.triaged ? Math.round((t.trashed / t.triaged) * 100) : 0;
  $("#amMeta").textContent =
    `${a.role || "no role recorded"} - ${a.status || "unknown"}` +
    `${a.auth ? " / " + a.auth : ""}` +
    `${a.inbox_count != null ? ` - inbox ${a.inbox_count}` : ""}` +
    `${a.as_of ? ` (as of ${a.as_of})` : ""}`;

  const kpi = (n, l, cls) =>
    `<div class="kpi ${cls || ""}"><div class="n">${n}</div><div class="l">${l}</div></div>`;
  const concepts_ = (a.by_concept || []).slice(0, 8);
  const cmax = Math.max(1, ...concepts_.map((c) => c.n));

  // Health across EVERY run, not just the latest - a box that fails intermittently is
  // invisible in one snapshot, which is exactly how a 7/8 doctor pass went unexplained.
  const health = (a.health || []).map((h) =>
    `<span class="chip ${h.status === "CONNECTED" ? "" : "bad"}">${esc(h.status)} ` +
    `x${h.n}</span>`).join(" ");

  $("#amBody").innerHTML =
    `<div class="kpis am-kpis">${kpi(t.triaged || 0, "triaged all time")}` +
    `${kpi(t.trashed || 0, `binned (${pct}%)`, "trash")}` +
    `${kpi(t.kept || 0, "kept / surfaced", "kept")}` +
    `${kpi(t.runs || 0, "runs seen in")}</div>` +
    `<div class="am-sec"><h4>Activity per run <span class="muted">` +
    `${esc(t.first_run || "")} to ${esc(t.last_run || "")}</span></h4>` +
    `<div class="am-spark">${spark(a.activity || [])}</div></div>` +
    `<div class="am-sec"><h4>What arrives here</h4>` +
    concepts_.map((c) =>
      `<div class="am-bar"><span class="am-lab">${esc(c.concept)}</span>` +
      `<span class="am-track"><i style="width:${Math.round((c.n / cmax) * 100)}%"></i></span>` +
      `<span class="am-n">${c.n}</span></div>`).join("") + "</div>" +
    `<div class="am-sec"><h4>Who writes here</h4>` +
    (a.top_senders || []).map((s) =>
      `<div class="am-row"><span>${esc(s.sender)}</span><b>${s.n}</b></div>`).join("") +
    "</div>" +
    `<div class="am-sec"><h4>Health across all runs</h4><div>${health || "-"}</div>` +
    (a.error ? `<div class="am-err">last error: ${esc(a.error)}</div>` : "") + "</div>" +
    `<div class="am-sec"><h4>Things that wanted you (${(a.attention || []).length})</h4>` +
    ((a.attention || []).length
      ? (a.attention).map((m) =>
        `<div class="am-row am-att${m.acked ? " done" : ""}">` +
        `<span><b>${esc(m.run_date)}</b> ${esc(m.subject || "")}</span>` +
        `<span class="muted">${esc(m.importance || "")}${m.acked ? " - handled" : ""}</span>` +
        `</div>`).join("")
      : '<div class="muted">nothing flagged for this box</div>') + "</div>";
}

// ---------- first run: what still needs doing, and how to do it ----------

async function loadSetup() {
  let data;
  try { data = await get("/api/setup"); } catch (e) { return; }
  const panel = $("#setupPanel");
  // Hidden the moment every step is done. A setup panel that lingers becomes furniture,
  // and furniture is not read - so its presence has to keep meaning something.
  panel.hidden = !!data.complete;
  if (data.complete) return;

  const steps = data.steps || [];
  const left = steps.filter((s) => !s.done).length;
  $("#setupCount").textContent =
    `(${steps.length - left} of ${steps.length} done)`;

  const wrap = $("#setupList");
  wrap.innerHTML = "";
  steps.forEach((s) => {
    const row = el("div", "setup-row" + (s.done ? " done" : ""));
    row.innerHTML =
      `<span class="setup-tick">${s.done ? "✓" : "○"}</span>` +
      `<div class="setup-body">` +
      `<div class="setup-title">${esc(s.title)}</div>` +
      `<div class="setup-detail">${esc(s.detail)}</div>` +
      (s.done ? "" : `<div class="setup-action">${esc(s.action)}</div>`) +
      `</div>`;
    wrap.appendChild(row);
  });

  // The guard is the one step worth acting on from here: it is safety-critical, it is the
  // file most likely to be left as shipped placeholders, and asking someone to hand-edit
  // JSON is where this tool loses the people it would help most.
  const guard = steps.find((s) => s.key === "protected" && !s.done);
  if (guard) {
    const btn = el("button", "btn setup-fix");
    btn.textContent = "Fill in the protected list";
    btn.addEventListener("click", openProtectedEditor);
    wrap.appendChild(btn);
  }
}

async function openProtectedEditor() {
  const modal = $("#protModal");
  const box = $("#prNames");
  const status = $("#prStatus");
  status.textContent = "";
  // Seed from what the SERVER currently resolves, not from the file: placeholders that the
  // loader ignores must not appear here as if they were protecting someone.
  try {
    const s = await get("/api/setup");
    const step = (s.steps || []).find((x) => x.key === "protected");
    box.value = (step && step.names ? step.names : []).join("\n");
    $("#prMeta").textContent = step ? step.detail : "";
  } catch (e) {
    box.value = "";
  }
  modal.hidden = false;
  box.focus();
}

async function saveProtectedNames() {
  const btn = $("#prSave");
  const status = $("#prStatus");
  const names = $("#prNames").value.split("\n").map((s) => s.trim()).filter(Boolean);
  btn.disabled = true;
  status.textContent = "saving...";
  try {
    const res = await fetch("/api/protected-names", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
      body: JSON.stringify({ names }),
    }).then((x) => x.json());
    if (!res.ok) throw new Error(res.error || "refused");
    // Report what the server RE-DERIVED, never what we sent. The loader's opinion is the
    // one that counts, and this panel exists because the two once disagreed silently.
    status.textContent = res.configured
      ? `saved — ${res.written} name(s), guard is now armed`
      : `saved, but still unconfigured: ${res.why}`;
    // Re-seed the box and the header from what the server RESOLVED, so what is on screen
    // after a save is the guard's actual state - not the text I typed, and not a stale
    // "no protected names yet" sitting above a list that now has three.
    $("#prNames").value = (res.names || []).join("\n");
    $("#prMeta").textContent = res.configured
      ? `${res.names.length} name(s) protected`
      : res.why;
    await loadSetup();
  } catch (e) {
    status.textContent = "refused: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

// ---------- workflow actions: the links that must not be missed ----------

// Keyed on the workflow KIND you chose in config, not on any one organisation's jargon.
// Anything unlisted falls back to a neutral glyph, so an unknown kind still renders.
const WORKFLOW_ICON = { screening: "📋", video: "🎥", appointment: "🎥",
                        notice: "📨", form: "📝" };

// The state comes from the SERVER - one implementation of "is this an action yet",
// the same discipline as the ack keys. Re-deriving it here is how two spellings of one
// rule drift apart.
function wfLabel(it) {
  if (!it.when) return { cls: it.state || "now", label: "" };
  const n = it.days_until;
  const suffix = n == null ? "" :
    n < 0 ? " - already passed" :
    n === 0 ? " - TODAY" :
    n === 1 ? " - tomorrow" : ` - in ${n} days`;
  return { cls: it.state || "now", label: it.when + suffix };
}

let wfShowDone = false;

async function loadWorkflowActions() {
  let data;
  try {
    data = await get("/api/workflow-actions");
  } catch (e) { return; }
  // The domain is whatever config says it is. Naming one here would be a second copy of
  // a setting, and wrong for everyone who configured a different one.
  const domain = data.domain || "the configured domain";
  const all = data.items || [];
  const live = all.filter((i) => !i.acked && ["now", "today", "soon"].includes(i.state));
  const upcoming = all.filter((i) => !i.acked && i.state === "upcoming");
  const shown = wfShowDone ? all : live;
  const panel = $("#wfPanel");
  // Hidden entirely when nothing is outstanding, so its presence always means something.
  // A dated visit still weeks away does NOT keep it open - it is carried on one quiet line
  // underneath instead, so it is never lost but never shouts either.
  panel.hidden = !shown.length && !upcoming.length;
  if (panel.hidden) return;

  $("#wfCount").textContent = wfShowDone
    ? `(${all.length} total, ${live.length} outstanding)`
    : (live.length ? `(${live.length})` : "(nothing right now)");
  $("#wfShowDone").textContent = wfShowDone ? "hide handled" : "show handled";

  const wrap = $("#wfList");
  wrap.innerHTML = "";
  shown.forEach((it) => {
    const w = wfLabel(it);
    const row = el("div", `wf-row ${w.cls}` + (it.acked ? " done" : ""));
    const p = it.primary;
    // A live, clickable link ONLY when the sender is verified AND the destination is
    // inside the configured domain. Anything else is shown as text with the reason -
    // people expecting a clinic or benefits link are a standing phishing target, so
    // "it looked right" is not enough.
    const action = (p && it.safe_to_click)
      ? `<a class="wf-go" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">` +
        `${esc(p.label || "Open")} &rarr;</a>` +
        `<div class="wf-url">${esc(p.url)}</div>`
      : (p ? `<div class="wf-blocked">Link NOT made clickable - ` +
             `${it.auth_ok ? `destination is not a ${esc(domain)} host`
                           : "sender could not be verified"}` +
             `<div class="wf-url">${esc(p.url)}</div></div>`
           : '<div class="wf-blocked">No action link in this message.</div>');
    row.innerHTML =
      `<div class="wf-top"><span class="wf-ico">${WORKFLOW_ICON[it.kind] || "⚕"}</span>` +
      `<span class="wf-kind">${esc(it.kind_label)}</span>` +
      (w.label ? `<span class="wf-when ${w.cls}">${esc(w.label)}</span>` : "") +
      (it.auth_ok ? '<span class="wf-auth">sender verified</span>'
                  : '<span class="wf-auth bad">sender NOT verified</span>') +
      (it.acked ? '<span class="wf-auth done">handled</span>' : "") + "</div>" +
      `<div class="wf-subj">${esc(it.subject)}</div>` + action;
    const btn = el("button", "linkbtn wf-ack");
    btn.textContent = it.acked ? "handled - undo" : "mark handled";
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await sendAck(it, "message", !it.acked);
        it.acked = !it.acked;
        loadWorkflowActions();
        loadHeatmap().catch(() => {});
      } catch (err) { btn.textContent = "could not save"; }
    });
    row.appendChild(btn);
    wrap.appendChild(row);
  });

  // Dated but still far off: one quiet line, not a card. It reappears as a full item on
  // its own once it comes inside the horizon (14 days, a typical reminder cadence).
  if (upcoming.length && !wfShowDone) {
    const line = el("div", "wf-upcoming");
    line.innerHTML = upcoming.map((u) =>
      `<b>${esc(u.kind_label)}</b> ${esc(u.when || "")}` +
      (u.days_until != null ? ` - in ${u.days_until} days` : "")).join(" &middot; ") +
      ` &middot; <span class="muted">shown here until ${data.horizon} days out</span>`;
    wrap.appendChild(line);
  }
}

// ---------- A known sender pointing somewhere new ----------
// The panel is deliberately quiet: it shows only pairings nobody has ruled on, and a ruling
// retires the pairing permanently rather than for a day. A security panel that is on screen
// every morning is one that stops being read, which would cost more than it buys.

let hostShowDone = false;

async function loadNewHosts() {
  let data;
  try {
    data = await get("/api/new-hosts?show=all");
  } catch (e) { return; }
  const open = data.open || [];
  const reviewed = data.reviewed || [];
  const shown = hostShowDone ? open.concat(reviewed) : open;
  const panel = $("#hostPanel");
  panel.hidden = !shown.length;
  if (panel.hidden) return;

  $("#hostCount").textContent = hostShowDone
    ? `(${open.length} unreviewed of ${data.ever_flagged} ever flagged)`
    : `(${open.length}, from ${data.profiled_senders} senders with enough history to judge)`;
  $("#hostShowDone").textContent = hostShowDone ? "hide reviewed" : "show reviewed";

  const wrap = $("#hostList");
  wrap.innerHTML = "";
  shown.forEach((it) => {
    const row = el("div", "wf-row" + (it.verdict ? " done" : "") +
                          (it.weighty && !it.verdict ? " weighty" : ""));
    // The host is NEVER a link. The entire premise of this panel is that we do not know
    // where this host goes, so offering a click would hand someone the exact thing the
    // check exists to catch.
    row.innerHTML =
      `<div class="wf-top"><span class="wf-ico">🔗</span>` +
      `<span class="wf-kind">${esc(it.sender || it.sender_key)}</span>` +
      `<span class="wf-auth">${it.profile_messages} messages of history</span>` +
      (it.weighty ? '<span class="wf-auth bad">subject looks costly to get wrong</span>' : "") +
      (it.times_seen > 1 ? `<span class="wf-auth">seen ${it.times_seen}x</span>` : "") +
      (it.verdict ? `<span class="wf-auth done">${esc(it.verdict)}</span>` : "") +
      "</div>" +
      `<div class="wf-subj">${esc(it.subject || "(no subject)")}</div>` +
      `<div class="wf-url">new host: <b>${esc(it.host)}</b>` +
      ` &middot; first seen ${esc(it.first_flagged || "?")}</div>` +
      (it.verdict_note ? `<div class="wf-url">note: ${esc(it.verdict_note)}</div>` : "");

    const mk = (label, verdict) => {
      const b = el("button", "linkbtn wf-ack");
      b.textContent = label;
      b.addEventListener("click", async (e) => {
        e.stopPropagation();
        b.disabled = true;
        try {
          const r = await fetch("/api/host-review", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
            body: JSON.stringify({ sender_key: it.sender_key, host: it.host, verdict }),
          }).then((x) => x.json());
          if (!r.ok) throw new Error(r.error || "refused");
          loadNewHosts();
        } catch (err) { b.disabled = false; b.textContent = "could not save"; }
      });
      return b;
    };
    if (it.verdict) row.appendChild(mk("undo - put it back", null));
    else {
      row.appendChild(mk("normal for them", "cleared"));
      row.appendChild(mk("looks wrong", "suspicious"));
    }
    wrap.appendChild(row);
  });
}

// ---------- Acknowledgement: "I have seen this" ----------
// The write path carries a custom header so the server can tell a request from THIS page
// apart from one any other website could fire at 127.0.0.1. See do_POST for why localhost
// is not the same thing as private.

let ackIndex = { message: new Set(), thread: new Set() };

// The keys are computed ONCE, on the server, and arrive with every row as
// ack_key_message / ack_key_thread. Re-deriving them here in JavaScript is how the same
// concept ends up with two spellings that drift apart - and it would fail silently, as
// acknowledgements that simply never render.
const msgKeyOf = (row) => row.ack_key_message || row.message_id || "";
const threadKeyOf = (row) => row.ack_key_thread || "";

function isAcked(row) {
  if (row.acked != null) return !!row.acked;      // server already decided
  return (msgKeyOf(row) && ackIndex.message.has(msgKeyOf(row))) ||
         (threadKeyOf(row) && ackIndex.thread.has(threadKeyOf(row)));
}

async function loadAcks() {
  try {
    const d = await get("/api/acks");
    ackIndex = { message: new Set(), thread: new Set() };
    (d.items || []).forEach((a) => ackIndex[a.kind] && ackIndex[a.kind].add(a.key));
  } catch (e) { /* never block the page on this */ }
}

async function sendAck(row, kind, on) {
  const r = await fetch("/api/ack", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
    body: JSON.stringify({
      kind, on,
      message_id: row.message_id || null,
      account: row.account, sender: row.sender, subject: row.subject,
    }),
  });
  const res = await r.json();
  if (!res.ok) throw new Error(res.error || "acknowledge failed");
  const set = ackIndex[kind];
  if (res.acked) set.add(res.key); else set.delete(res.key);
  // The row carries a server-computed `acked` from when it was fetched; refresh it so the
  // change shows without a reload instead of being masked by that stale value.
  row.acked = (msgKeyOf(row) && ackIndex.message.has(msgKeyOf(row))) ||
              (threadKeyOf(row) && ackIndex.thread.has(threadKeyOf(row)));
  return res;
}

function mvPaintAck() {
  const row = mvCurrent;
  const box = $("#mvAck");
  if (!row) { box.hidden = true; return; }
  box.hidden = false;
  const onMsg = !!(msgKeyOf(row) && ackIndex.message.has(msgKeyOf(row)));
  const onThread = !!(threadKeyOf(row) && ackIndex.thread.has(threadKeyOf(row)));
  $("#mvAckBtn").textContent = onMsg ? "Acknowledged - undo" : "Acknowledge";
  $("#mvAckBtn").classList.toggle("on", onMsg);
  // NEVER disabled for want of a Message-ID. Acknowledging is about YOUR attention, not
  // about whether I can fetch the mail - an item I cannot open still has to be dismissable,
  // and it was precisely the unopenable ones piling up unacknowledged.
  $("#mvAckBtn").disabled = false;
  $("#mvAckThread").textContent = onThread
    ? "and everything like it - undo" : "and everything like it";
  $("#mvAckThread").classList.toggle("on", onThread);
  $("#mvAckState").textContent = onThread
    ? "every future notice of this is silenced"
    : (onMsg ? "this one will stop being surfaced" : "");
}

// ---------- The record: every run as one grid ----------

const CONCEPT_TINT = {
  money: "#4ade80", security: "#f87171", family: "#c084fc", medical: "#38bdf8",
  social: "#64748b", promo: "#64748b", newsletters: "#fbbf24", steam: "#22d3ee",
  logistics: "#94a3b8", questions: "#fb923c", calendar: "#a3e635", other: "#94a3b8",
};

async function loadHeatmap() {
  const data = await get("/api/calendar");
  const days = data.days || [];
  if (!days.length) return;
  const byDate = new Map(days.map((d) => [d.run_date, d]));
  const max = Math.max(...days.map((d) => d.n));

  // Span the whole record, INCLUDING days with no run - a gap in the grid is itself
  // information (it says nobody looked), and skipping those days would quietly hide it.
  const first = new Date(days[0].run_date + "T00:00:00");
  const last = new Date(days[days.length - 1].run_date + "T00:00:00");
  const start = new Date(first);
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7));   // back to Monday

  const cells = [];
  const weeks = [];
  let cur = [];
  for (let d = new Date(start); d <= last; d.setDate(d.getDate() + 1)) {
    const iso = d.toISOString().slice(0, 10);
    cur.push(iso);
    if (cur.length === 7) { weeks.push(cur); cur = []; }
  }
  if (cur.length) weeks.push(cur);

  const CELL = 17, GAP = 4, TOP = 16;
  const w = weeks.length * (CELL + GAP);
  const h = TOP + 7 * (CELL + GAP);
  const parts = [];
  let lastMonth = "";
  weeks.forEach((week, wi) => {
    const m = week[0].slice(0, 7);
    if (m !== lastMonth) {
      lastMonth = m;
      const label = new Date(week[0] + "T00:00:00")
        .toLocaleString(undefined, { month: "short" });
      parts.push(`<text x="${wi * (CELL + GAP)}" y="9" class="hm-mon">${label}</text>`);
    }
    week.forEach((iso, di) => {
      const x = wi * (CELL + GAP), y = TOP + di * (CELL + GAP);
      const day = byDate.get(iso);
      if (!day) {
        // No run that day. Drawn as a small INSET dot with no border: the first version
        // gave it a stroke, which at 13px was visually indistinguishable from the amber
        // "needed you" ring - two opposite meanings rendered almost identically, which
        // only showed up once the page was looked at rather than queried.
        const i = 5;
        parts.push(`<rect class="hm hm-none" x="${x + i}" y="${y + i}" ` +
                   `width="${CELL - i * 2}" height="${CELL - i * 2}" rx="1">` +
                   `<title>${iso}: no run</title></rect>`);
        return;
      }
      const tint = CONCEPT_TINT[day.concept] || "#94a3b8";
      const op = (0.22 + 0.78 * (day.n / max)).toFixed(2);
      parts.push(
        `<rect class="hm hm-day" data-date="${iso}" x="${x}" y="${y}" width="${CELL}" ` +
        `height="${CELL}" rx="3" fill="${tint}" fill-opacity="${op}">` +
        `<title>${iso}\n${day.n} triaged - ${day.kept} kept, ${day.trashed} binned` +
        `\nmostly ${day.concept}` +
        `${day.action ? `\n${day.action} needed your attention` +
          `${day.action_open ? ` - ${day.action_open} still open`
                             : " - all acknowledged"}` : ""}</title></rect>`);
      // The attention marker is a DOT in the corner, not a border. A border competes with
      // every other edge in the grid; a dot cannot be confused with anything else.
      // AMBER = something on that day is still waiting. GREEN = there WAS something and
      // all of it is acknowledged. The distinction is the whole point: a day that has
      // already worked through should stop asking for attention, and only the days that
      // still want you should stay lit.
      if (day.action) {
        const open = day.action_open == null ? day.action : day.action_open;
        parts.push(`<circle class="hm-act ${open ? "open" : "done"}" ` +
                   `cx="${x + CELL - 3.5}" cy="${y + 3.5}" r="2.6" pointer-events="none"/>`);
      }
    });
  });

  $("#heatmap").innerHTML =
    `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" ` +
    `role="img" aria-label="Every run day, shaded by volume and tinted by topic">` +
    parts.join("") + "</svg>";

  const t = data.totals || {};
  allTime = t;                          // context for the KPI drill-downs
  $("#heatSummary").textContent =
    `${t.runs} runs - I read ${t.messages} so you read ${t.kept}`;
  const used = [...new Set(days.map((d) => d.concept))].slice(0, 6);
  const open = days.filter((d) => (d.action_open == null ? d.action : d.action_open)).length;
  const done = days.filter((d) => d.action &&
    !(d.action_open == null ? d.action : d.action_open)).length;
  $("#heatLegend").innerHTML =
    used.map((k) => `<span class="lg"><i style="background:${CONCEPT_TINT[k] ||
      "#94a3b8"}"></i>${esc(k)}</span>`).join("") +
    `<span class="lg"><i class="dot open"></i>still wants you (${open})</span>` +
    `<span class="lg"><i class="dot done"></i>all acknowledged (${done})</span>` +
    '<span class="lg"><i class="none"></i>no run</span>';

  // clicking a day selects that run - the grid replaces the date dropdown as the way in
  $("#heatmap").querySelectorAll("rect.hm-day").forEach((el2) => {
    el2.style.cursor = "pointer";
    el2.addEventListener("click", () => {
      const d = el2.getAttribute("data-date");
      currentDate = d;
      ui.date = (d === newestDate) ? "latest" : d;
      persistUI();
      const sel = $("#runSelect");
      if (sel) sel.value = d;
      loadRun();
    });
  });
}

// ---------- Repeats: the same thing, arriving again ----------

async function loadRepeats() {
  const data = await get("/api/repeats");
  const items = data.items || [];
  const badge = $("#repeatBadge");
  const loud = items.filter((i) => i.weight >= 3 || i.accelerating);
  badge.hidden = !loud.length;
  badge.textContent = loud.length;

  $("#repeatReach").innerHTML =
    `Grouped <b>${data.groups_examined}</b> sender+subject shapes; showing the ` +
    `<b>${items.length}</b> that arrived <b>${data.min_notices}+</b> times. ` +
    `Counted by <i>distinct message</i> wherever the rows are linked - a message that sits ` +
    `in the inbox is re-listed by every run, and counting those would invent urgency. ` +
    `Rows marked <span class="warn">approximate</span> predate message linking, so they ` +
    `count listings instead and make no claim about acceleration.`;

  const wrap = $("#repeatList");
  wrap.innerHTML = "";
  if (!items.length) {
    wrap.appendChild(el("div", "empty", "Nothing has arrived repeatedly."));
    return;
  }
  items.forEach((it) => {
    const row = el("div", "quiet-row" + (it.weight >= 3 ? " money" : ""));
    const badges =
      (it.accelerating ? '<span class="qratio hot">arriving faster</span>' : "") +
      (it.basis === "listings" ? '<span class="qcat warn">approximate</span>' : "");
    row.innerHTML =
      `<div class="qhead"><span class="qname">${esc(it.subject || "(no subject)")}</span>` +
      `<span class="qcat">${esc(it.concept_key)}</span>${badges}</div>` +
      `<div class="qbody"><b>${it.notices}</b> ${it.basis === "messages"
        ? "separate arrivals" : "run appearances"} from ${esc(it.sender || "")}, ` +
      `${esc(it.first_seen)} to ${esc(it.last_seen)}` +
      `${it.still_open ? " - still in the mailbox" : " - binned"}.</div>`;
    row.addEventListener("click", () => {
      ui.query = it.subject || ""; ui.category = null; ui.concept = null;
      ui.page = 0; ui.disposition = "all";
      persistUI();
      $("#trashSearch").value = ui.query;
      $("#trashClear").hidden = false;
      document.querySelectorAll("#dispScope .chip").forEach((b) =>
        b.classList.toggle("on", b.dataset.disp === "all"));
      setView("trash");
    });
    wrap.appendChild(row);
  });
}

// ---------- Gone quiet: the only panel that alarms by seeing NOTHING ----------

async function loadQuiet() {
  const data = await get("/api/quiet");
  const r = data.reach || {};
  // State the observation window BEFORE the findings. An absence claim is worth exactly
  // its stated reach, and this panel's entire output is absence claims.
  $("#quietReach").innerHTML =
    `Watching <b>${r.established || 0}</b> senders with an established rhythm ` +
    `(of ${r.senders_total || 0} seen), across <b>${r.runs || 0}</b> runs ` +
    `${esc(r.first_run || "")} to ${esc(r.last_run || "")}. ` +
    `A sender qualifies after ${r.min_obs} appearances spanning ${r.min_span} runs, and is ` +
    `flagged only when it has been silent <i>longer than its own worst gap ever</i>. ` +
    `<span class="warn">Monthly billers cannot qualify yet</span> - the run history is too ` +
    `short to see a monthly rhythm, so treat this as a watch on frequent senders only.`;

  const wrap = $("#quietList");
  wrap.innerHTML = "";
  const items = data.items || [];

  const badge = $("#quietBadge");
  badge.hidden = !items.length;
  badge.textContent = items.length;

  if (!items.length) {
    wrap.appendChild(el("div", "empty",
      "No established sender is quieter than its own worst gap. That is a measured " +
      "all-clear across the window above, not an absence of data."));
    return;
  }

  items.forEach((it) => {
    const row = el("div", "quiet-row" + (it.weight >= 2 ? " money" : ""));
    const ratio = it.ratio >= 2 ? "hot" : "";
    row.innerHTML =
      `<div class="qhead">` +
        `<span class="qname">${esc(it.sender)}</span>` +
        `<span class="qcat">${esc(it.category)}</span>` +
        `<span class="qratio ${ratio}">${it.ratio}x its worst</span>` +
      `</div>` +
      `<div class="qbody">Silent <b>${it.silent_runs}</b> runs. ` +
      `Never went more than <b>${it.worst_gap}</b> before, across ${it.observations} ` +
      `appearances since ${esc(it.first_seen)}. Last seen <b>${esc(it.last_seen)}</b>.</div>` +
      (it.variants && it.variants.length > 1
        ? `<div class="qvar muted">folded ${it.variants.length} spellings: ` +
          `${it.variants.map(esc).join(" | ")}</div>`
        : "");
    // Clicking a quiet sender should show its actual history, across every disposition -
    // otherwise the panel raises a question it cannot answer.
    row.addEventListener("click", () => {
      ui.query = it.sender;
      ui.category = null;
      ui.concept = null;
      ui.page = 0;
      ui.disposition = "all";
      persistUI();
      $("#trashSearch").value = it.sender;
      $("#trashClear").hidden = false;
      document.querySelectorAll("#dispScope .chip").forEach((b) =>
        b.classList.toggle("on", b.dataset.disp === "all"));
      setView("trash");
    });
    wrap.appendChild(row);
  });
}

let steamData = null;  // last-loaded sales payload, so we can re-render after a hide

async function loadSteam() {
  steamData = await get("/api/steam/sales");
  renderSteam(steamData);
}

async function refreshSteam() {
  const btn = $("#steamRefresh");
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "↻ Checking Steam…";
  try {
    // refreshing prices also un-hides everything, so hidden games come back
    ui.hiddenSteam = [];
    persistUI();
    const data = await get("/api/steam/refresh");
    steamData = data.result || data;
    renderSteam(steamData);
  } catch (e) {
    $("#steamMeta").textContent = "refresh failed: " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

function hideSteam(appId) {
  if (!ui.hiddenSteam.includes(appId)) ui.hiddenSteam.push(appId);
  persistUI();
  if (steamData) renderSteam(steamData);
}

// Show the sale as a real calendar range instead of a rolling "N days" counter.
// START = first_seen (approximated by the wishlist-email date). END = sale_ends,
// scraped from the store page's "Offer ends ..." countdown by steam_refresh.py.
// If the end is unknown (store page age-gated / no countdown), fall back to "→ now".
function fmtDay(d) {
  return new Date(d + "T00:00:00").toLocaleDateString(undefined,
    { weekday: "short", month: "short", day: "numeric" });
}
function fmtSaleSpan(sale) {
  if (!sale || !sale.first_seen) return "";
  const start = fmtDay(sale.first_seen);
  return sale.sale_ends ? `${start} → ${fmtDay(sale.sale_ends)}` : `On sale ${start} → now`;
}

// A sale is expired once its scraped end date is strictly before today (kept
// visible through the end date itself, since Steam sales run into that day).
// This drops a game off the board on its exact expiry day in real time —
// without waiting for the next price poll to notice the discount is gone.
function steamExpired(s) {
  if (!s.sale_ends) return false;
  return s.sale_ends < new Date().toISOString().slice(0, 10);
}

function renderSteam(data) {
  const active = (data.sales || []).filter((s) => s.active && !steamExpired(s));
  const hidden = new Set(ui.hiddenSteam || []);
  const sales = active.filter((s) => !hidden.has(s.app_id));
  const hiddenCount = active.length - sales.length;
  const meta = $("#steamMeta");
  const checked = data.last_checked
    ? `prices checked ${new Date(data.last_checked).toLocaleString()}`
    : "prices not yet checked — hit Refresh";
  const hiddenNote = hiddenCount ? ` · ${hiddenCount} hidden (Refresh to restore)` : "";
  meta.textContent = `${sales.length} active${hiddenNote} · ${checked}`;
  const wrap = $("#steamList");
  wrap.innerHTML = "";
  if (!sales.length) {
    wrap.appendChild(el("div", "empty", hiddenCount
      ? "All active sales are hidden — hit “Refresh prices” to bring them back."
      : "No active Steam sales tracked. Wishlist-sale emails add games here; " +
        "ended sales drop off automatically."));
    return;
  }
  const grid = el("div", "steam-grid");
  sales.forEach((s) => {
    const title = esc(s.title || ("App " + s.app_id));
    const tile = el("a", "steam-tile");
    if (s.url) { tile.href = s.url; tile.target = "_blank"; tile.rel = "noopener"; }

    // Thumbnail (Steam header capsule); falls back to a titled gradient on error.
    const thumb = el("div", "steam-thumb");
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = title;
    img.src = `https://cdn.cloudflare.steamstatic.com/steam/apps/${s.app_id}/header.jpg`;
    img.onerror = () => { thumb.classList.add("noimg"); thumb.dataset.title = s.title || ("App " + s.app_id); img.remove(); };
    thumb.appendChild(img);
    if (s.discount_pct) thumb.appendChild(el("span", "steam-disc", `-${s.discount_pct}%`));
    // red X (top-left) to hide this game; persists until "Refresh prices" restores all
    const hide = el("button", "steam-hide", "✕");
    hide.title = "Hide this game (Refresh prices brings it back)";
    hide.setAttribute("aria-label", "Hide " + title);
    hide.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); hideSteam(s.app_id); });
    thumb.appendChild(hide);
    tile.appendChild(thumb);

    const body = el("div", "steam-body");
    body.appendChild(el("div", "steam-title", title));

    const price = el("div", "steam-price");
    if (s.price_final_fmt) {
      price.innerHTML =
        (s.price_initial_fmt && s.price_initial !== s.price_final
          ? `<span class="was">${esc(s.price_initial_fmt)}</span> ` : "") +
        `<span class="now">${esc(s.price_final_fmt)}</span>`;
    } else {
      price.innerHTML = `<span class="muted">price not checked — Refresh</span>`;
    }
    body.appendChild(price);
    body.appendChild(el("div", "steam-span muted", esc(fmtSaleSpan(s))));
    tile.appendChild(body);

    grid.appendChild(tile);
  });
  wrap.appendChild(grid);
}

init().catch((e) => { $("#subtitle").textContent = "error: " + e.message; });
