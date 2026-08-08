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
  $("#openShowDone").addEventListener("click", () => { openShowDone = !openShowDone; loadOpenItems(); });
  $("#hostShowDone").addEventListener("click", () => { hostShowDone = !hostShowDone; loadNewHosts(); });
  $("#amClose").addEventListener("click", () => { $("#acctModal").hidden = true; });
  $("#acctModal").addEventListener("click", (e) => {
    if (e.target.id === "acctModal") $("#acctModal").hidden = true;
  });
  $("#prClose").addEventListener("click", () => { $("#protModal").hidden = true; });
  $("#qClose").addEventListener("click", () => { $("#qModal").hidden = true; });
  $("#qOpen").addEventListener("click", openQuestions);
  $("#prSave").addEventListener("click", saveProtectedNames);
  document.addEventListener("click", (e) => {
    if (e.target.id === "protModal") $("#protModal").hidden = true;  // backdrop dismiss
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#protModal").hidden) $("#protModal").hidden = true;
    else if (!$("#acctModal").hidden) $("#acctModal").hidden = true;
  });

  await loadFeatures();            // before the first setView, or a hidden tab can be restored
  loadSetup().catch(() => {});     // first thing a new install needs, and silent once done
  await loadAcks();                // before the first render, so state is right immediately
  await loadRun();
  loadWorkflowActions().catch(() => {}); // never let this panel block the rest of the page
  loadOpenItems().catch(() => {});
  wireModalCloses();
  loadScoreboard().catch(() => {});
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
  // THE SELECTED DATE IS THE LOUDEST THING IN THE HEADER. It was small grey text reading
  // "showing run for 2026-08-06", which meant that after switching days you had to hunt the
  // page to find out which day you were on - the one fact every other number depends on.
  if (data.run_date) {
    const isLatest = !newestDate || data.run_date === newestDate;
    $("#subtitle").innerHTML =
      `<span class="showing-label">showing</span>` +
      `<span class="showing-date">${esc(data.run_date)}</span>` +
      (isLatest ? `<span class="showing-tag">latest</span>`
                : `<span class="showing-tag older">older run</span>`);
  } else {
    $("#subtitle").innerHTML = `<span class="showing-label">no runs yet</span>`;
  }
  markSelectedDay();      // keep the grid's ring on the same day as the header
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

  renderAccounts(data.accounts || [], data.accounts_as_of);
  renderSummary(data.surfaced || [], data.carried_hidden || 0);
  await loadTrash();
}

function renderAccounts(accounts, asOf) {
  renderAccountStrip(accounts, asOf);
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

function renderAccountStrip(accounts, asOf) {
  const strip = $("#accountStrip");
  const summary = $("#acctSummary");
  strip.innerHTML = "";
  if (!accounts.length) {
    // No run at all on this day is a different statement from "the accounts are unknown".
    summary.textContent = "- no run on this day";
    summary.style.color = "";
    return;
  }
  const n = (k) => accounts.filter((a) => statusOf(a) === k).length;
  const ok = n("ok"), bad = n("fail"), unk = n("unknown");
  // "as of" when the status came from a different day. A historical run - anything staged
  // by arrival from an intake - has no account status of its own, because nothing connected
  // to a mailbox that day. Whether eight mailboxes are reachable is a fact about NOW, so an
  // old answer WITH ITS DATE beats a blank panel, and a bare old answer without the date
  // would be the worst of the three.
  const bits = [`${ok}/${accounts.length} connected`];
  if (asOf && asOf !== currentDate) bits.push(`as of ${asOf}`);
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

function renderSummary(msgs, carriedHidden) {
  lastSurfaced = msgs || [];
  const wrap = $("#summary");
  wrap.innerHTML = "";
  // ALREADY SEEN IS NOT NEWS - said out loud rather than quietly shown. A message still in
  // the inbox is re-listed by every sweep, so the same item was raised on four consecutive
  // days with nothing changed, and two in five of the security "alerts" read here were a
  // repeat of one already read. That is how a channel gets ignored before it matters.
  if (carriedHidden) {
    const note = el("div", "carried-note");
    note.innerHTML = `<b>${carriedHidden}</b> item${carriedHidden === 1 ? "" : "s"} ` +
      `already surfaced on an earlier run ${carriedHidden === 1 ? "is" : "are"} not ` +
      `repeated here. <a href="#" id="showCarried">show them</a>`;
    wrap.appendChild(note);
    $("#showCarried").addEventListener("click", async (e) => {
      e.preventDefault();
      // currentDate, not ui.date - ui.date carries the sentinel "latest", and sending that
      // as a date would ask the API for a run that does not exist.
      const d = await get("/api/run?carried=1" +
                          (currentDate ? "&date=" + encodeURIComponent(currentDate) : ""));
      renderSummary(d.surfaced || [], 0);
    });
  }
  if (!msgs.length) {
    wrap.appendChild(el("div", "empty", carriedHidden
      ? "Nothing NEW surfaced for this run - everything above was already raised earlier."
      : "Nothing surfaced for this run."));
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
  $("#signinsView").hidden = view !== "signins";
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
  $("#trashScope").hidden = (view === "steam" || view === "quiet" || view === "repeats"
                             || view === "signins");
  $("#steamScope").hidden = view !== "steam";
  if (view === "steam") loadSteam();
  else if (view === "senders") loadSenders();
  else if (view === "quiet") loadQuiet();
  else if (view === "repeats") loadRepeats();
  else if (view === "signins") loadSignins();
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
  // WHEN THE MAIL ARRIVED, not when we got around to reading it. This showed `run_date`,
  // which is the day the sweep ran - so after a historical intake every message in it read
  // as having arrived today, and a refund notice from last September was presented as
  // this morning's mail. Same defect the calendar was fixed for; it survived here because
  // this line was never the one anybody looked at while fixing that.
  //
  // The sweep date is still shown when it differs, because "I read this eleven months
  // late" is a real fact about the tool and hiding it would be its own small lie.
  const arrived = row.msg_day || (row.msg_date || "").slice(0, 10) || row.run_date;
  const swept = row.run_date && row.run_date !== arrived
    ? ` · swept ${row.run_date}` : "";
  $("#mvMeta").textContent =
    `${row.sender || "unknown sender"} - ${row.account} - ${arrived}${swept}`;
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
    // THE RETENTION NOTE IS AN INFERENCE ABOUT THE MAIL, so it is only earned when a search
    // actually happened. It used to print on every failure, including the one where the
    // backend never connected - telling someone their message was probably deleted, about
    // mail sitting in their inbox. An absence is only reportable by an instrument that ran.
    let msg = res.error || "could not retrieve";
    // NO LOCAL FETCHER IS A THIRD STATE, and it used to render as the first one - "not
    // found in this mailbox" above a detail that correctly said nothing had gone looking.
    // The headline contradicted its own explanation, about mail sitting untouched in
    // someone's inbox.
    if (res.reason === "no_local_fetcher") {
      $("#mvText").textContent =
        (res.detail || "") + (res.hint ? "\n\n" + res.hint : "");
      const link = res.web_link;
      const box = $("#mvSafety");
      if (link) {
        // LABELLED, never a silent fallback. This leaves the sandbox entirely: it opens in
        // the provider's own UI, with images, tracking pixels and all. Saying so is the
        // difference between an informed choice and the tool quietly undoing its own
        // headline privacy feature.
        box.innerHTML =
          '<a class="mv-external" target="_blank" rel="noopener noreferrer" href="' +
          esc(link) + '">Open in your mail client &#8599;</a>' +
          '<div class="mv-hosts">Leaves this viewer. Images and tracking will load, ' +
          'because the message is rendered by your mail provider, not here.</div>';
      } else {
        box.innerHTML =
          '<div class="mv-hosts">Nothing to open with. Supply <code>body_text</code> at ' +
          'ingest to read messages here, or <code>web_link</code> to open them in your ' +
          'mail client.</div>';
      }
      return;
    }
    if (res.searched) {
      msg += "\n\nTrashed mail is recoverable for about 30 days; older items may be gone.";
    } else {
      if (res.hint) msg += "\n\n" + res.hint;
      // detail was captured server-side from the very beginning and never displayed, so the
      // one thing that said what actually broke was the one thing nobody could see.
      if (res.detail) msg += "\n\n--- what the mail tool reported ---\n" + res.detail.trim();
    }
    $("#mvText").textContent = msg;
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

  // A NOTIFICATION ADDRESS IS NOT ONE THING, and the whole-sender verdict above says so
  // correctly and uselessly. One tracker address carries status noise, bot chatter, and the
  // handful of messages where a person named you - and "not pure noise" is true of the
  // address while being false of most of its mail. Sender-scoped rules could therefore never
  // fire on exactly the senders worth ruling on: the volume that earns a rule is the volume
  // that guarantees the sender is mixed.
  //
  // So the breakdown is the feature, not a detail of it. A button that never lights up and
  // never says why is indistinguishable from one that is broken.
  const slices = (s.rule_slices || []).filter((x) => x.n > 0);
  if (slices.length > 1) {
    const canRule = slices.filter((x) => x.eligible && !x.already_ruled).length;
    ruleBox += `<div class="rule-slices"><h4>By label` +
      `<span class="muted">${canRule ? `${canRule} of ${slices.length} could be ruled on` :
        "none of these can be ruled on yet"}</span></h4>` +
      slices.map((sl, i) =>
        `<div class="rule-slice${sl.eligible && !sl.already_ruled ? " go" : ""}">` +
        `<span class="rs-lab">${esc(sl.category)}</span>` +
        `<span class="rs-n">${sl.n}</span>` +
        (sl.already_ruled
          ? `<button class="btn rs-btn" data-off="${i}">lift</button>`
          : sl.eligible
            ? `<button class="btn primary rs-btn" data-on="${i}">always trash this label` +
              `</button>`
            : `<span class="rs-why">${esc(sl.why || "")}</span>`) +
        "</div>").join("") +
      `<div class="muted rs-note">A label rule bins only mail the triage files under that ` +
      `label. It is only as good as the label, which is assigned to future mail by the ` +
      `same triage - so it is offered, never applied on its own.</div></div>`;
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

  const setRule = async (on, btn, category) => {
    btn.disabled = true;
    btn.textContent = on ? "writing the rule..." : "lifting...";
    try {
      const res = await fetch("/api/sender-rule", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
        // The category rides along; the server re-derives the entitlement for that exact
        // slice and refuses whatever this page happens to claim about it.
        body: JSON.stringify({ key, on, label: key, category: category || null }),
      }).then((x) => x.json());
      if (!res.ok) throw new Error(res.error || "refused");
      openSender(key);                     // re-render from the server's new truth
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "could not: " + e.message;
    }
  };
  if ($("#ruleAdd")) {
    $("#ruleAdd").addEventListener("click", (e) => setRule(true, e.target));
  }
  if ($("#ruleLift")) {
    $("#ruleLift").addEventListener("click", (e) => setRule(false, e.target));
  }
  $("#amBody").querySelectorAll(".rs-btn").forEach((b) => {
    const on = b.dataset.on !== undefined;
    const sl = slices[Number(on ? b.dataset.on : b.dataset.off)];
    b.addEventListener("click", () => setRule(on, b, sl.category));
  });
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

// ---------- optional panels ----------

let features = { steam: false };

async function loadFeatures() {
  try {
    const data = await get("/api/features");
    features = Object.assign(features, data.panels || {});
  } catch (e) {
    // Unreachable config means every OPTIONAL panel stays off. Nothing here is load-bearing,
    // so the harmless answer is the right one.
  }
  const tab = document.querySelector('.ptab[data-view="steam"]');
  if (tab) tab.hidden = !features.steam;
  // A view persisted from when the panel WAS on would otherwise restore a tab that is now
  // hidden, leaving an empty panel and no visible tab explaining it.
  if (!features.steam && ui.view === "steam") {
    ui.view = "trash";
    persistUI();
  }
}

// ---------- first run: what still needs doing, and how to do it ----------


// ---------- header chips: attention costs nothing until it has something to say ----------
//
// These four panels used to sit inline above the working area, where they were the worst of
// both worlds: they ate the vertical space the mail needed AND were too short to use. A
// four-row window onto an outstanding list is not a list. As a chip each costs nothing when
// it is empty, and opens onto the whole screen when it is not.
const ATTN_MODALS = ["setupModal", "wfModal", "openModal", "hostModal"];

function chip(btnId, modalId, label, count) {
  const b = $("#" + btnId);
  if (!b) return;
  b.hidden = !count;
  b.textContent = count > 1 ? label + " (" + count + ")" : label;
  b.onclick = () => { $("#" + modalId).hidden = false; };
}

function wireModalCloses() {
  document.querySelectorAll("[data-close]").forEach((b) => {
    b.onclick = () => { $("#" + b.dataset.close).hidden = true; };
  });
  ATTN_MODALS.forEach((id) => {
    const m = $("#" + id);
    if (m) m.addEventListener("click", (e) => { if (e.target === m) m.hidden = true; });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    ATTN_MODALS.forEach((id) => {
      const m = $("#" + id);
      if (m && !m.hidden) m.hidden = true;
    });
  });
}

async function loadSetup() {
  let data;
  try { data = await get("/api/setup"); } catch (e) { return; }
  const panel = $("#setupPanel");
  const steps = data.steps || [];
  const outstanding = steps.filter((s) => !s.done);
  // Hidden the moment every step is done. A setup panel that lingers becomes furniture,
  // and furniture is not read - so its presence has to keep meaning something.
  //
  // `data.complete` is NOT the test, because it deliberately ignores advisory steps. If it
  // were, "tell the tool how you work" would be reported by the server and rendered by
  // nothing - the shape of bug this project keeps finding, where the honest answer exists
  // and no one is shown it. When only advisory steps remain the panel stays, but quietly:
  // one line and a button, not a block of unfinished business.
  const onlyAdvisory = outstanding.length > 0 && outstanding.every((s) => s.advisory);
  panel.hidden = false;                 // it lives in a modal; the chip decides visibility
  panel.classList.toggle("setup-advisory", onlyAdvisory);
  chip("setupBtn", "setupModal", "Finish setting up", outstanding.length);

  // The header entry point is driven from the same payload and is INDEPENDENT of whether
  // the setup panel is showing. On this install the rules step reported itself done - no
  // placeholders left, no high-weight question - while thirteen real questions waited
  // behind a panel that had already hidden itself. Correct, and unreachable.
  const rulesStep = steps.find((s) => s.key === "rules");
  const waiting = (rulesStep && rulesStep.questions_waiting) || 0;
  const opener = $("#qOpen");
  opener.hidden = !waiting;
  opener.textContent = waiting === 1 ? "1 question" : `${waiting} questions`;
  opener.onclick = openQuestions;
  opener.title = "Questions generated from your own mail. Answering them is how this " +
                 "tool learns your rules instead of assuming them.";

  if (outstanding.length === 0) return;

  const left = outstanding.length;
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

  const rules = steps.find((s) => s.key === "rules" && !s.done);
  if (rules && rules.questions_waiting) {
    const btn = el("button", "btn setup-fix");
    btn.textContent =
      rules.questions_waiting === 1
        ? "Answer 1 question about your mail"
        : `Answer ${rules.questions_waiting} questions about your mail`;
    btn.addEventListener("click", openQuestions);
    wrap.appendChild(btn);
  }
}

// ---------- elicitation: the tool asking, instead of assuming ----------

async function openQuestions() {
  const modal = $("#qModal");
  const list = $("#qList");
  $("#qStatus").textContent = "";
  list.innerHTML = '<div class="muted">loading…</div>';
  modal.hidden = false;
  let data;
  try {
    data = await get("/api/questions?limit=6");
  } catch (e) {
    list.innerHTML = '<div class="muted">could not load questions</div>';
    return;
  }
  const qs = data.questions || [];
  // Say what is being withheld. "6 questions" above a list of 6 that is really 20 is the
  // understatement this whole project keeps tripping over: correct, and read as complete.
  $("#qMeta").textContent =
    (data.total > qs.length
      ? `showing ${qs.length} of ${data.total} — the rest keep for later`
      : `${qs.length} question${qs.length === 1 ? "" : "s"}`) +
    (data.answered ? ` · ${data.answered} already answered` : "");
  list.innerHTML = "";
  if (!qs.length) {
    list.innerHTML =
      '<div class="muted">Nothing to ask right now. New questions appear as your ' +
      "mailbox changes.</div>";
    return;
  }
  qs.forEach((q) => list.appendChild(questionCard(q)));
}

function questionCard(q) {
  const card = el("div", "q-card");
  const head = el("div", "q-question");
  head.textContent = q.question;
  card.appendChild(head);

  if (q.why_it_matters) {
    const why = el("div", "q-why");
    why.textContent = q.why_it_matters;
    card.appendChild(why);
  }

  // THE EVIDENCE IS THE POINT. Without it this is a checklist, and a checklist asking
  // "how should we treat bots?" is unanswerable. With it the answer is recall.
  const ev = el("div", "q-evidence");
  ev.appendChild(evidenceOf(q.evidence));
  card.appendChild(ev);

  const opts = el("div", "q-options");
  (q.options || []).forEach((o) => {
    const b = el("button", "btn q-opt");
    b.textContent = o;
    b.addEventListener("click", () => answerQuestion(q, o, card));
    opts.appendChild(b);
  });
  card.appendChild(opts);

  // Free text always, even when there are options. Every option list here is a guess at
  // the shape of an answer, and the answers worth most are the ones that did not fit.
  const row = el("div", "q-free");
  const box = el("input", "q-input");
  box.type = "text";
  box.placeholder = (q.options || []).length
    ? "…or say it in your own words"
    : "your answer";
  const send = el("button", "btn");
  send.textContent = "Save";
  const submit = () => box.value.trim() && answerQuestion(q, box.value.trim(), card);
  send.addEventListener("click", submit);
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit();
  });
  row.appendChild(box);
  row.appendChild(send);
  card.appendChild(row);

  const skip = el("button", "btn q-skip");
  skip.textContent = "Not now";
  // Skip does NOT record an answer - an unanswered question has to stay askable, and a
  // "skip" stored as an answer is how a question disappears without ever being decided.
  skip.addEventListener("click", () => card.remove());
  card.appendChild(skip);
  return card;
}

function evidenceOf(ev) {
  const wrap = el("div", "q-ev-body");
  if (!ev || typeof ev !== "object") return wrap;
  Object.keys(ev).forEach((k) => {
    const v = ev[k];
    if (v === null || v === undefined || (Array.isArray(v) && !v.length)) return;
    const line = el("div", "q-ev-line");
    const label = el("span", "q-ev-k");
    label.textContent = k.replace(/_/g, " ") + ": ";
    line.appendChild(label);
    const val = el("span", "q-ev-v");
    val.textContent = Array.isArray(v)
      ? v.map((x) => (typeof x === "object" ? JSON.stringify(x) : x)).join(" · ")
      : String(v);
    line.appendChild(val);
    wrap.appendChild(line);
  });
  return wrap;
}

async function answerQuestion(q, answer, card) {
  const status = $("#qStatus");
  status.textContent = "saving…";
  try {
    const res = await fetch("/api/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
      body: JSON.stringify({
        id: q.id,
        kind: q.kind,
        question: q.question,
        evidence: q.evidence,
        answer,
        written_to: q.writes,
      }),
    }).then((x) => x.json());
    if (!res.ok) throw new Error(res.error || "refused");
    card.classList.add("q-answered");
    card.innerHTML =
      '<div class="q-question">' + esc(q.question) + "</div>" +
      '<div class="q-answer">✓ ' + esc(answer) + "</div>";
    status.textContent = `recorded — it will be applied to ${q.writes}`;
    await loadSetup();
  } catch (e) {
    status.textContent = "could not save: " + e.message;
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

// ---------- still open: the one list that gets worse by being ignored ----------

let openShowDone = false;

// ---------- the scoreboard: the only number here that measures the outcome ----------
// Everything else on this page counts what the tool DID. This counts how often it failed:
// somebody gave up on the inbox and went to another channel to find its owner.

async function loadScoreboard() {
  const body = $("#scoreBody");
  let d;
  try {
    d = await get("/api/scoreboard");
  } catch (e) {
    // NEVER LEAVE THE BOX EMPTY. Returning silently here left a titled panel with nothing
    // in it, which reads as a broken feature rather than as a failed request - and the one
    // thing this whole tool argues is that silence is not an answer.
    // SAY WHICH FAILURE IT IS. "could not load" is true and useless: the overwhelmingly
    // likely cause is a dashboard that picked up new static files while still running the
    // old server process, so the endpoint genuinely does not exist yet - and the fix is a
    // restart, not a bug report.
    body.className = "score-body unmeasured";
    body.textContent = "needs a dashboard restart";
    why.hidden = false;
    why.onclick = () => alert(
      "This panel asks the server for /api/scoreboard and got nothing back.\n\n"
      + "Almost always that means the page is newer than the running server: the browser "
      + "loaded the updated files, but the process answering them was started before this "
      + "feature existed.\n\nStop the dashboard and start it again, then reload.\n\n"
      + "(" + (e && e.message ? e.message : String(e)) + ")");
    return;
  }
  const why = $("#scoreWhy");

  // NOT MEASURED IS NOT ZERO, and it must not LOOK like zero either. A big confident "0"
  // from an instrument that has never fired once is congratulation for having no
  // instrument, which is worse than showing nothing.
  if (!d.measured) {
    body.className = "score-body unmeasured";
    body.textContent = "not measured yet";
    why.hidden = false;
    why.onclick = () => alert(
      "A “reach” is somebody giving up on your inbox and messaging you on "
      + "another channel instead.\n\n" + d.why_not);
    return;
  }

  body.className = "score-body";
  const t = d.trend || {};
  const last = (d.months || []).filter((m) => !m.partial).slice(-1)[0];
  const rate = last ? last.rate : null;
  const arrow = { better: "↓", worse: "↑", flat: "→" }[t.direction] || "";

  // TWO LINES. The tile is one column of a band that sits between the reader and their
  // mail, so every line it takes is a line of email somebody does not see. The months it
  // compared, the caveat about volume, and the note about an empty guard all live behind
  // "why?" - present, one click away, and not costing height on every screen forever.
  // ONE LINE. In the header there is no room for three, and there does not need to be:
  // the number, which way it is moving, and how many came from people who matter. The
  // months compared and the volume caveat are behind "what is this?".
  body.innerHTML =
    `<span class="score-rate">${rate === null ? "—" : rate}</span> ` +
    `<span class="muted">per 100</span> ` +
    `<span class="score-dir ${esc(t.direction || "unknown")}">${arrow} ` +
    `${esc(t.direction || "")}</span>` +
    (d.protected_known && last && last.from_people_who_matter
      ? ` <span class="muted">· ${last.from_people_who_matter} from people ` +
        `who matter</span>` : "");

  // The caveat is one click away rather than inline, but it is never absent: a direction
  // without the volume behind it is how "quiet month" gets read as "tool working".
  why.hidden = false;
  why.onclick = () =>
    alert([t.detail, t.caveat, d.who_matters_unknown].filter(Boolean).join("\n\n"));
}

async function loadOpenItems() {
  let data;
  try {
    data = await get("/api/open-items?state=" + (openShowDone ? "all" : "open"));
  } catch (e) {
    return;
  }
  const panel = $("#openPanel");
  const items = data.items || [];
  chip("openBtn", "openModal", "Still open", data.open || 0);
  // Hidden only when there is genuinely nothing outstanding AND nothing to review. A
  // standing list that is always on screen becomes furniture; one that hides when empty
  // means something every time it appears.
  panel.hidden = false;
  if (items.length === 0) return;

  // The count says OPEN and OLDEST, because those are the two facts that decide whether
  // this panel is worth reading today. "4 items" says neither.
  // MEDIAN BESIDE OLDEST. Length says nothing about whether this list is working: one
  // that churns is fine however long it is, and one whose median age climbs every week is
  // being ignored however short. A length target would push toward hiding things.
  // Said out loud. "1 open" quietly becoming "0 open" with no explanation is the same
  // silence this project keeps arguing against, just in the pleasant direction.
  const acked = data.hidden_because_acknowledged || 0;
  $("#openCount").textContent =
    `(${data.open} open` +
    (data.oldest_days ? `, oldest ${data.oldest_days}d` : "") +
    (data.median_days ? `, median ${data.median_days}d` : "") +
    (acked ? ` · ${acked} acknowledged, not counted` : "") +
    (data.resolved_off_channel
      ? ` · ${data.resolved_off_channel} closed elsewhere`
      : "") +
    ")";

  // WHO IS WAITING, not just how many things. The owner acts by person: four asks from one
  // colleague is one conversation, four from four people is four.
  const whoBox = $("#openWho");
  const who = data.waiting_on_you_from || [];
  whoBox.hidden = who.length < 2;
  whoBox.innerHTML = who.length < 2 ? "" :
    "waiting on you: " +
    who.map((w) => `${esc(w.who)}${w.items > 1 ? ` ×${w.items}` : ""}`).join(" · ");

  const wrap = $("#openList");
  wrap.innerHTML = "";
  items.forEach((it) => wrap.appendChild(openRow(it)));
}

function openRow(it) {
  const row = el("div", "open-row" + (it.state === "resolved" ? " done" : "") +
                         (it.stale ? " stale" : ""));
  const age =
    it.days_open === null || it.days_open === undefined
      ? "age unknown"     // never "0 days" - that would sort the oldest item to the bottom
      : it.days_open === 0
      ? "today"
      : `${it.days_open} day${it.days_open === 1 ? "" : "s"}`;

  // WHICH MAILBOX, and a way IN. This row named a subject and a sender and nothing else,
  // so closing an item meant first working out which of eight accounts it had come from and
  // then finding it by hand. You cannot decide that something is done if you cannot see
  // what it is; the panel was asking for a judgement while withholding the evidence.
  const main = el("div", "open-main");
  main.innerHTML =
    `<div class="open-subject">${esc(it.subject || "(no subject)")}</div>` +
    `<div class="open-meta">${esc(it.sender || "")}` +
    (it.account ? `<span class="open-acct">${esc(it.account)}</span>` : "") +
    `<span class="open-age">${esc(age)}</span>` +
    (it.runs_seen > 1 ? `<span class="muted">seen in ${it.runs_seen} runs</span>` : "") +
    (it.importance ? `<span class="badge">${esc(it.importance)}</span>` : "") +
    // Said out loud, because both can be true and the combination looks like a fault:
    // acknowledging is "I have seen this" and it deliberately does not close an item.
    // Without this the row is a demand about something the owner knows they dismissed.
    (it.acknowledged
      ? `<span class="badge acked" title="You acknowledged this. Acknowledging means you ` +
        `have seen it, not that it is done - so it stays here until you close it.">` +
        `acknowledged</span>`
      : "");
  if (it.state !== "resolved") {
    // A thread-keyed item has no Message-ID, so the viewer cannot fetch it - mvOpen says so
    // in its own words rather than this row guessing. Either way the click is offered,
    // because "here is why I cannot open it" is still an answer and silence is not.
    main.classList.add("open-clickable");
    main.title = "Open this message";
    main.addEventListener("click", () =>
      mvOpen({
        subject: it.subject, sender: it.sender, account: it.account,
        msg_day: it.first_seen,          // when the mail arrived
        run_date: it.last_seen || it.first_seen,
        message_id: it.kind === "message" ? it.key : null,
        open_item: true,
      }));
  }
  row.appendChild(main);

  const acts = el("div", "open-actions");
  if (it.state === "resolved") {
    const note = el("div", "open-resolved");
    const said = {
      "off-channel": "resolved elsewhere",
      "email": "resolved here",
      "declined": "not doing this",
      "expired": "expired",
      "moot": "not doing this",
    }[it.resolved_where] || `resolved ${it.resolved_where}`;
    note.textContent = said + (it.resolved_note ? ` — ${it.resolved_note}` : "");
    acts.appendChild(note);
    const reopen = el("button", "btn");
    reopen.textContent = "Reopen";
    reopen.addEventListener("click", () => resolveItem(it.key, { open: true }));
    acts.appendChild(reopen);
  } else {
    // THREE OUTCOMES, not one. "Done" alone forces a person to record a lie for anything
    // that was settled on a call, and a list you have to lie to is a list you stop using.
    // FOUR EXITS, and three of them are not "done". A standing list whose only way out is
    // completion becomes a graveyard - and a graveyard is what teaches its reader to skim
    // past the one live item. "Not doing this" is a decision, and deciding is finishing.
    [
      ["Done here", "email"],
      ["Done elsewhere", "off-channel"],
      ["Not doing this", "declined"],
      ["Expired", "expired"],
    ].forEach(([label, where]) => {
      const b = el("button", "btn");
      b.textContent = label;
      b.addEventListener("click", () => {
        const note = where === "off-channel"
          ? prompt("Where was it settled? (optional — e.g. 'on a call')") || ""
          : "";
        resolveItem(it.key, { where, note });
      });
      acts.appendChild(b);
    });
  }
  row.appendChild(acts);
  return row;
}

async function resolveItem(key, payload) {
  try {
    const res = await fetch("/api/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
      body: JSON.stringify(Object.assign({ key }, payload)),
    }).then((x) => x.json());
    if (!res.ok) throw new Error(res.error || "refused");
    await loadOpenItems();
  } catch (e) {
    alert("Could not update that item: " + e.message);
  }
}

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
  // The chip carries the count; the panel itself lives in a modal and is always "shown"
  // once opened. `live` rather than `shown` because a dated visit weeks out is carried on
  // a quiet line, and a chip that shouts about it every day is one nobody reads.
  chip("wfBtn", "wfModal", "Needs you to do something", live.length);
  panel.hidden = false;
  if (!shown.length && !upcoming.length) return;

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
  chip("hostBtn", "hostModal", "New link host", open.length);
  panel.hidden = false;
  if (!shown.length) return;

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
  // NOT TRUE OF AN ITEM THAT IS STILL OPEN. The old copy promised the message would stop
  // being surfaced, while the Still-open panel went on surfacing it - which is how an
  // acknowledged row sitting in a to-do list reads as the tool having lost track, rather
  // than as the two different things they are.
  const alsoOpen = mvCurrent && mvCurrent.open_item;
  $("#mvAckState").textContent = onThread
    ? (alsoOpen ? "silenced in the run reports - still on your open list until you close it"
                : "every future notice of this is silenced")
    : (onMsg
        ? (alsoOpen ? "seen - but still on your open list until you close it"
                    : "this one will stop being surfaced")
        : "");
}

// ---------- The record: every run as one grid ----------

const CONCEPT_TINT = {
  money: "#4ade80", security: "#f87171", family: "#c084fc", medical: "#38bdf8",
  social: "#64748b", promo: "#64748b", newsletters: "#fbbf24", steam: "#22d3ee",
  logistics: "#94a3b8", questions: "#fb923c", calendar: "#a3e635", other: "#94a3b8",
};

// WHICH DAY AM I LOOKING AT. The grid is the main way in, and until now nothing on it said
// which cell was currently selected - so every time the day changed, the answer had to be
// hunted for elsewhere on the page. The ring is moved rather than the grid redrawn, so this
// is cheap enough to call on every date change.
function markSelectedDay() {
  const ring = document.getElementById("hmSel");
  if (!ring) return;
  const cell = currentDate
    ? document.querySelector(`#heatmap rect.hm-day[data-date="${currentDate}"]`)
    : null;
  if (!cell) { ring.setAttribute("visibility", "hidden"); return; }
  // Centred on the cell: the ring is 6px larger, so it sits 3px outside on every edge.
  ring.setAttribute("x", String(parseFloat(cell.getAttribute("x")) - 3));
  ring.setAttribute("y", String(parseFloat(cell.getAttribute("y")) - 3));
  ring.setAttribute("visibility", "visible");
}

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

  // The record sets the height of the whole top band, so its cell size is a layout
  // decision rather than a taste one: 17+4 made a seven-row grid 147px tall before the
  // heading, and the band is the thing standing between the reader and their mail.
  // 13+3 keeps a day comfortably clickable and gives back ~40 vertical pixels.
  const CELL = 13, GAP = 3, TOP = 13;
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
    parts.join("") +
    // The selection ring lives OUTSIDE the cells, drawn last so it is never clipped by a
    // neighbour and never has to fight a fill for contrast. Moved rather than redrawn, so
    // switching days does not rebuild the grid.
    `<rect id="hmSel" class="hm-sel" width="${CELL + 6}" height="${CELL + 6}" rx="5" ` +
    `pointer-events="none" visibility="hidden"/>` +
    "</svg>";
  markSelectedDay();

  const t = data.totals || {};
  allTime = t;                          // context for the KPI drill-downs
  // TERSE, because this line was setting the width of the whole panel. "56 runs - I read
  // 1510 so you read 552" is 250px of text wrapped around a 144px chart, so the record
  // drew itself in a box half of which was empty - which reads as a failure to load. The
  // sentence still exists, as the tooltip, for anyone who wants it spelled out.
  // "runs" is only the right word when the grid is keyed on SWEEPS. By default it is keyed
  // on when mail ARRIVED, so each square is a day the mailbox received something - and
  // calling 57 arrival days "57 runs" overstates how often the tool has actually run.
  const unit = data.by === "swept" ? "runs" : "days";
  $("#heatSummary").textContent =
    `${t.runs} ${unit} · ${t.messages} → ${t.kept}`;
  $("#heatSummary").title = data.by === "swept"
    ? `${t.runs} sweeps - I read ${t.messages} so you read ${t.kept}`
    : `${t.runs} days on which mail arrived - I read ${t.messages} so you read ${t.kept}. `
      + `Each square is the day mail ARRIVED, not the day it was swept.`;
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
    `<b>${items.length}</b> that arrived <b>${data.min_notices}+</b> times — ` +
    // Live vs dormant, stated. A year of history accumulates finished series, and burying
    // a live dunning notice under fifty completed ones is how a live one goes unread.
    `<b>${data.live}</b> still running, <b>${data.dormant}</b> dormant (listed last). ` +
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
    const row = el("div", "quiet-row" + (it.weight >= 3 && !it.dormant ? " money" : "") +
                              (it.dormant ? " dormant" : ""));
    const badges =
      (it.accelerating ? '<span class="qratio hot">arriving faster</span>' : "") +
      (it.dormant ? '<span class="qcat">dormant</span>' : "") +
      (it.basis === "listings" ? '<span class="qcat warn">approximate</span>' : "");
    row.innerHTML =
      `<div class="qhead"><span class="qname">${esc(it.subject || "(no subject)")}</span>` +
      `<span class="qcat">${esc(it.concept_key)}</span>${badges}</div>` +
      `<div class="qbody"><b>${it.notices}</b> ${it.basis === "messages"
        ? "separate arrivals" : "run appearances"} from ${esc(it.sender || "")}, ` +
      `${esc(it.first_seen)} to ${esc(it.last_seen)}` +
      // The cadence, in DAYS, said out loud. Gaps used to be counted in runs, which made
      // "arriving faster" partly a statement about how often the tool ran; days are a fact
      // about the sender. Showing the unit is what stops the old reading surviving the fix.
      (it.median_gap
        ? `, about every <b>${it.median_gap}</b> day${it.median_gap === 1 ? "" : "s"}`
        : "") +
      (it.days_since_last != null
        ? `, last ${it.days_since_last} day${it.days_since_last === 1 ? "" : "s"} ago`
        : "") +
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

// ---------- Sign-ins: escalate on ANOMALY, never on occurrence (rule 26) ----------
//
// An alert that fires on every login is not an alert, it is a log. Logs are things you
// consult; alerts are things you trust. Merging them destroys the channel silently, because
// nothing is ever WRONG - each notice is true, the reader simply learns that opening them
// never pays, and by the time one matters that habit is built.
//
// So this panel has exactly two registers. Anomalies get a card and a reason. Everything
// routine gets ONE LINE. And the coverage note is not decoration: device novelty is only as
// good as what the provider put in the subject, and "no unknown devices" must never be
// readable as "every device was recognised".

async function loadSignins() {
  const d = await get("/api/signins");
  const s = d.summary || {}, cov = d.coverage || {}, w = d.window || {};
  const anomalies = d.anomalies || [];

  const badge = $("#signinBadge");
  badge.hidden = !anomalies.length;
  badge.textContent = anomalies.length;

  const services = {};
  (d.routine || []).forEach((r) => { services[r.service] = (services[r.service] || 0) + 1; });
  const svcList = Object.keys(services).sort((a, b) => services[b] - services[a]);

  $("#signinReach").innerHTML =
    `Account-security mail from the last <b>${w.days}</b> days ` +
    `(${esc(w.from || "")} to ${esc(w.to || "")}): <b>${w.judged}</b> messages judged, ` +
    `<b>${w.baseline}</b> older ones used only to learn what is normal. ` +
    // NOT MEASURED IS NOT ZERO.
    `Device signatures come from the subject line, and most providers do not include one - ` +
    `parsed for <b>${cov.device_parsed}</b> of ${cov.messages}. An unparsed notice is ` +
    `<i>unknown</i>, never "known", so it falls back to whether the service itself is new. ` +
    (d.bursts && d.bursts.length
      ? `<span class="warn">${d.bursts.length} burst(s) detected.</span>`
      : `No bursts: no window with sign-ins across ${3} or more services, which is the ` +
        `shape of somebody working through a credential list.`);

  const wrap = $("#signinList");
  wrap.innerHTML = "";

  // THE LEDGER. One line, always present, even at zero - because "6 sign-ins, all routine"
  // and "we did not look" have to be distinguishable.
  const ledger = el("div", "signin-ledger");
  ledger.innerHTML =
    `<b>${s.routine || 0}</b> routine sign-in${s.routine === 1 ? "" : "s"}` +
    (svcList.length
      ? ` across ${svcList.length} service${svcList.length === 1 ? "" : "s"}: ` +
        svcList.map((k) => `${esc(k)}&nbsp;<span class="muted">${services[k]}</span>`)
          .join(" &middot; ")
      : "") +
    `. <span class="muted">${s.consent || 0} consent receipt(s), ` +
    `${s.policy || 0} terms/policy notice(s) - neither is an account event.</span>`;
  wrap.appendChild(ledger);

  if (!anomalies.length) {
    wrap.appendChild(el("div", "empty",
      "Nothing anomalous in this window. That is a measured all-clear over the reach " +
      "stated above, not an absence of data."));
    return;
  }

  anomalies.forEach((a) => {
    const row = el("div", "quiet-row money");
    row.innerHTML =
      `<div class="qhead">` +
        `<span class="qname">${esc(a.subject || "(no subject)")}</span>` +
        `<span class="qcat">${esc(a.service || "")}</span>` +
        (a.collapsed > 1
          ? `<span class="qratio">${a.collapsed} notices, one event</span>` : "") +
      `</div>` +
      `<div class="qbody">${esc(String(a.msg_day || "").slice(0, 10))} &middot; ` +
      `${esc(a.sender || "")}` +
      (a.device ? ` &middot; <b>${esc(a.device)}</b>` : "") + `</div>` +
      `<ul class="signin-why">` +
      (a.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("") + `</ul>` +
      ((a.also || []).length
        ? `<div class="qvar muted">also in this event: ` +
          a.also.map(esc).join(" | ") + `</div>`
        : "");
    row.addEventListener("click", () => {
      ui.query = a.subject || ""; ui.category = null; ui.concept = null;
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

let quietShowAll = false;

async function loadQuiet() {
  const data = await get("/api/quiet" + (quietShowAll ? "?include=all" : ""));
  const r = data.reach || {};
  // State the observation window BEFORE the findings. An absence claim is worth exactly
  // its stated reach, and this panel's entire output is absence claims.
  $("#quietReach").innerHTML =
    `Watching <b>${r.established || 0}</b> senders with an established rhythm ` +
    `(of ${r.senders_total || 0} seen) over <b>${r.window_days || 0} days</b>, ` +
    `${esc(r.first_run || "")} to ${esc(r.last_run || "")}. ` +
    // Every sender is measured against the runs that covered ITS mailbox, not against all
    // of them. A historical intake creates runs that only ever touched one account, and
    // counting those as observations of every other account reports senders as silent when
    // nobody looked at them - which is an absence asserted by an instrument that never ran.
    `Each sender is measured only against the days its <i>own mailbox</i> was looked at, ` +
    `so a backfilled day covering one account is not counted as silence from the others. ` +
    `A sender qualifies after ${r.min_obs} appearances spanning ${r.min_span_days} days, ` +
    `and is flagged only when it has been silent <i>longer than its own worst gap ever</i> ` +
    `— by at least ${r.min_ratio}x, and at least ${r.min_silence_days} days either way. ` +
    // DERIVED from the window, not asserted. The old caption stated flatly that monthly
    // billers could not qualify, and went on saying it after a year of arrival-dated
    // history made them qualify - while a monthly bank statement sat at the top of the
    // list it was captioning. A hard-coded caveat is a claim that goes stale in silence.
    (r.monthly_observable
      ? `The window is long enough to see a <b>monthly</b> rhythm.`
      : `<span class="warn">Monthly billers cannot qualify yet</span> - the window is too ` +
        `short to see a monthly rhythm, so treat this as a watch on frequent senders only.`) +
    (data.hidden_social
      ? ` <span class="muted">${data.hidden_social} social-notification sender` +
        `${data.hidden_social === 1 ? " is" : "s are"} hidden — a friend posting less is ` +
        `not a finding. <a href="#" id="quietAll">show them</a></span>`
      : "");

  if ($("#quietAll")) {
    $("#quietAll").addEventListener("click", (e) => {
      e.preventDefault();
      quietShowAll = true;
      loadQuiet();
    });
  }

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
      // DAYS, not runs. "Silent 105 of 173 runs" is a true sentence about the store and
      // tells you nothing about your bank - and once a backfill exists, a run in 2025 and
      // a run in 2026 no longer represent the same amount of the world.
      `<div class="qbody">Silent <b>${it.silent_days} days</b>. ` +
      `Never went more than <b>${it.worst_gap} days</b> before, across ${it.observations} ` +
      `appearances from ${esc(it.first_seen)} to <b>${esc(it.last_seen)}</b>. ` +
      `<span class="muted">Its mailbox was last looked at ${esc(it.last_looked)}.</span></div>` +
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
