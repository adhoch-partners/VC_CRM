"use strict";

// ------------------------------------------------------------------ helpers

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "X-CRM": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const ownAuthErrors = ["/api/auth/login", "/api/auth/2fa", "/api/auth/forgot", "/api/auth/reset"];
  if (res.status === 401 && !ownAuthErrors.includes(path)) {
    showLogin();
    throw new Error("Your session has ended. Please sign in again.");
  }
  const data = await res.json().catch(() => ({}));
  if (res.status === 403 && String(data.detail).startsWith("Two-factor authentication must be set up")) {
    showEnroll();
    throw new Error(data.detail);
  }
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : res.statusText);
  return data;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = err ? "err" : "";
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), err ? 5000 : 2200);
}

async function guard(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); throw e; }
}

const fmt = {
  /** $750K below a million, $5.25M from a million up (including billions: $1,200M). */
  money(v) {
    if (v == null || v === "") return "";
    const n = Number(v);
    if (n === 0) return "$0";
    const a = Math.abs(n);
    const sign = n < 0 ? "-" : "";
    const num = (x, digits) => x.toLocaleString(undefined, { maximumFractionDigits: digits });
    // Switch to M once the K figure would round to 1,000K (e.g. $999,999 shows as $1M).
    return Math.round(a / 100) >= 10000 ? `${sign}$${num(a / 1e6, 2)}M` : `${sign}$${num(a / 1e3, 1)}K`;
  },
  pct: (v, digits = 1) => (v == null ? "n/a" : `${(v * 100 + 0).toFixed(digits).replace(/^-(0\.?0*)$/, "$1")}%`),
  multiple: (v) => (v == null ? "n/a" : `${v.toFixed(2)}x`),
  num: (v) => (v == null || v === "" ? "" : Number(v).toLocaleString()),
};

const S = { meta: null, me: null, people: {}, gridState: {}, activeView: {}, look: { funds: {}, lps: {}, companies: {}, investors: {}, rounds: {}, users: {}, contacts: {} }, view: null, prefs: {} };
const isAdmin = () => Boolean(S.me?.admin);
// Per-record rights come from the server as _can_edit / _can_delete on each row.
// These only decide which "create" buttons to offer.
const editableFundIds = () => Object.keys(S.look.funds).map(Number)
  .filter((id) => isAdmin() || S.me?.fund_access?.[id] === "editor");
const canEditSomeFund = () => editableFundIds().length > 0;
const canCreateShared = () => Boolean(S.me?.can_create_shared);

try { S.prefs = JSON.parse(localStorage.getItem("vccrm.prefs") || "{}"); } catch { S.prefs = {}; }
function pref(key, value) {
  if (value === undefined) return S.prefs[key];
  S.prefs[key] = value;
  try { localStorage.setItem("vccrm.prefs", JSON.stringify(S.prefs)); } catch { /* ignore */ }
}

async function loadLookups() {
  const [funds, lps, companies, people, me, investors, rounds, contacts, tasks] = await Promise.all(
    ["funds", "lps", "companies", "users/directory", "auth/me", "investors", "rounds", "contacts", "tasks"]
      .map((e) => api("GET", `/api/${e}`)));
  S.look.users = Object.fromEntries(people.map((u) => [u.id, u]));
  S.look.contacts = Object.fromEntries(contacts.map((r) => [r.id, r]));
  // Sidebar badge: my open tasks that are overdue or due today.
  const due = tasks.filter((t) => t.status === "Open" && t.assignee_id === me.id && t.due_date && t.due_date <= today()).length;
  const badge = $("#tasks-badge");
  if (badge) { badge.textContent = due; badge.hidden = !due; }
  S.me = me; // picks up fund access changes an admin made since sign-in
  S.people = Object.fromEntries(people.map((u) => [u.id, u.name]));
  S.look.funds = Object.fromEntries(funds.map((r) => [r.id, r]));
  S.look.lps = Object.fromEntries(lps.map((r) => [r.id, r]));
  S.look.companies = Object.fromEntries(companies.map((r) => [r.id, r]));
  S.look.investors = Object.fromEntries(investors.map((r) => [r.id, r]));
  // Rounds are labeled "Company · Series A" wherever they are picked or shown.
  S.look.rounds = Object.fromEntries(rounds.map((r) => [r.id, { ...r, name: roundLabel(r) }]));
}

const roundLabel = (r) => `${S.look.companies[r.company_id]?.name ?? "Company"} · ${r.round_name}`;

const fieldsOf = (entity) => S.meta.entities[entity].fields;
const fieldOf = (entity, name) => fieldsOf(entity).find((f) => f.name === name);
const refLabel = (ref, id) => (id == null ? "" : S.look[ref]?.[id]?.name ?? `#${id}`);

function display(field, v) {
  switch (field.type) {
    case "money": return fmt.money(v);
    case "percent": return v == null ? "" : `${v}%`;
    case "number": case "integer": return fmt.num(v);
    case "ref": return refLabel(field.ref, v);
    case "select": return v ? `<span class="pill">${esc(v)}</span>` : "";
    default: return esc(v);
  }
}
const isNum = (f) => ["money", "number", "integer", "percent"].includes(f.type);

// Money is typed in $000s (5250 = $5,250,000) but stored and sent in full dollars.
const toThousands = (v) => (v == null || v === "" ? "" : +(Number(v) / 1000).toFixed(5));
function fromThousands(text) {
  const t = String(text ?? "").replace(/[$,\s]/g, "");
  if (t === "") return "";
  const n = Number(t);
  return Number.isFinite(n) ? Math.round(n * 100000) / 100 : text; // let the server reject junk
}

function input(field, value) {
  const v = value ?? "";
  if (field.type === "select" || field.type === "ref") {
    let refs = field.type === "ref" ? Object.values(S.look[field.ref]) : [];
    // Only offer funds the user may add records to (plus the current value).
    if (field.ref === "funds" && !isAdmin()) {
      const ok = new Set(editableFundIds());
      refs = refs.filter((r) => ok.has(r.id) || String(r.id) === String(v));
    }
    const opts = field.type === "ref" ? refs.map((r) => [r.id, r.name]) : field.options.map((o) => [o, o]);
    return `<select name="${field.name}"><option value=""></option>${opts.map(([k, l]) =>
      `<option value="${esc(k)}" ${String(k) === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  }
  if (field.type === "longtext") return `<textarea name="${field.name}">${esc(v)}</textarea>`;
  if (field.type === "money") {
    return `<input type="number" step="any" name="${field.name}" value="${esc(toThousands(v))}" placeholder="$000s" title="Amount in $000s">`;
  }
  const type = field.type === "date" ? "date" : isNum(field) ? "number" : "text";
  return `<input type="${type}" name="${field.name}" value="${esc(v)}" ${type === "number" ? 'step="any"' : ""}>`;
}

const today = () => new Date().toISOString().slice(0, 10);
function dueClass(d) {
  if (!d) return "";
  if (d < today()) return "bad";
  return d <= new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10) ? "warn" : "";
}

function fundFilter(onChange) {
  const cur = pref("fund") ?? "";
  const sel = document.createElement("select");
  sel.innerHTML = `<option value="">All funds</option>` + Object.values(S.look.funds)
    .map((f) => `<option value="${f.id}" ${String(f.id) === String(cur) ? "selected" : ""}>${esc(f.name)}</option>`).join("");
  sel.onchange = () => { pref("fund", sel.value); onChange(sel.value); };
  return sel;
}

/** A link that downloads the record type as CSV (only the rows you may see). */
function exportButton(entity, label = "Export CSV", href = `/api/export/${entity}.csv`) {
  const a = document.createElement("a");
  a.className = "btn";
  a.textContent = label;
  a.href = href;
  a.download = "";
  return a;
}

/** Warn while typing a name that looks like an existing record (without naming hidden ones). */
const DUP_ENTITIES = ["lps", "companies", "investors", "funds"];
function watchDuplicates(input, entity, excludeId) {
  const box = document.createElement("div");
  box.className = "dup-warning";
  box.hidden = true;
  input.closest("label").after(box);
  let timer;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const name = input.value.trim();
      if (name.length < 3) { box.hidden = true; return; }
      const r = await api("GET", `/api/duplicates?entity=${entity}&name=${encodeURIComponent(name)}${excludeId ? `&exclude_id=${excludeId}` : ""}`).catch(() => null);
      if (!r || (!r.visible.length && !r.hidden_count)) { box.hidden = true; return; }
      box.innerHTML = r.visible.length
        ? `Possible duplicate: ${r.visible.map((x) => `<span class="link" data-dup="${x.id}">${esc(x.name)}</span>`).join(", ")}. Open it instead of creating a new one?`
        : "A record with a similar name exists in a fund you can't see. Ask an admin before creating a new one.";
      box.hidden = false;
    }, 350);
  });
  box.onclick = async (e) => {
    const id = e.target.closest("[data-dup]")?.dataset.dup;
    if (id) editRecord(entity, await api("GET", `/api/${entity}/${id}`));
  };
}

/** Import a CSV: preview every row's status, then import. */
function importDialog(entity, after) {
  const label = S.meta.entities[entity]?.label ?? entity;
  const d = openDrawer(`<h2>Import ${esc(label)}</h2>
    <div class="muted" style="line-height:1.5">Upload a CSV (in Excel or Numbers: File &gt; Export / Save As &gt; CSV).
      The first row must be column headers matching the field names; <a href="/api/import/${entity}/template.csv">download the template</a>.
      <b>Amounts are full dollars</b> in files (5250000, not 5250). Linked records are matched by name
      (e.g. Fund = "Fund I"), so import LPs and companies before their commitments and investments.</div>
    <div style="margin:14px 0"><input type="file" accept=".csv,text/csv" data-file></div>
    <div data-result></div>`);
  let text = null;
  const result = $("[data-result]", d);
  const run = (body) => api("POST", `/api/import/${entity}`, { csv: text, ...body });
  const show = (r) => {
    const badge = { ok: "good", duplicate: "warn", error: "bad" };
    result.innerHTML = `<div style="margin-bottom:8px"><b>${r.counts.ok}</b> ready ·
        <b class="warn">${r.counts.duplicate}</b> possible duplicates · <b class="bad">${r.counts.error}</b> with errors
        ${r.ignored_columns.length ? `<div class="muted">Ignored columns: ${r.ignored_columns.map(esc).join(", ")}</div>` : ""}</div>
      <div class="grid-wrap" style="max-height:320px"><table class="grid"><thead><tr><th>Row</th><th>Record</th><th>Status</th></tr></thead>
      <tbody>${r.rows.map((x) => `<tr><td><div class="cell">${x.row}</div></td><td><div class="cell">${esc(x.label ?? "")}</div></td>
        <td><div class="cell ${badge[x.status]}" style="white-space:normal">${esc(x.status === "ok" ? "Ready" : x.message)}</div></td></tr>`).join("")}</tbody></table></div>
      <div style="margin-top:12px; display:flex; flex-direction:column; gap:6px">
        ${r.counts.duplicate ? '<label><input type="checkbox" data-incdup> Also import the possible duplicates</label>' : ""}
        <div><button class="btn primary" data-go ${r.counts.ok + r.counts.duplicate ? "" : "disabled"}>Import
          ${r.counts.error ? `the valid rows (skip ${r.counts.error} with errors)` : "rows"}</button>
          <button class="btn" data-close>Cancel</button></div></div>`;
    $("[data-close]", result).onclick = closeDrawer;
    $("[data-go]", result).onclick = () => guard(async () => {
      const incdup = $("[data-incdup]", result)?.checked ?? false;
      const done = await run({ commit: true, skip_errors: true, skip_duplicates: !incdup });
      toast(`Imported ${done.imported} record(s)`);
      closeDrawer();
      await loadLookups();
      after?.();
    });
  };
  $("[data-file]", d).onchange = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => guard(async () => { text = reader.result; show(await run({})); });
    reader.readAsText(file);
  };
}

/** A button shown only when `allowed` (a placeholder node otherwise). */
function buttonIf(allowed, ...args) {
  return allowed ? button(...args) : document.createComment("not permitted");
}

function button(label, onClick, cls = "") {
  const b = document.createElement("button");
  b.className = `btn ${cls}`;
  b.textContent = label;
  b.onclick = onClick;
  return b;
}

function segmented(options, current, onChange) {
  const el = document.createElement("div");
  el.className = "seg";
  for (const [key, label] of options) {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = key === current ? "on" : "";
    b.onclick = () => onChange(key);
    el.append(b);
  }
  return el;
}

// ------------------------------------------------------------------ grid (Airtable-style)

/**
 * cols: [{ name, label?, field?, value?(row) -> html, editable?, num?, total?(rows) }]
 * Columns backed by an entity field are inline-editable; click a cell to edit.
 */
function renderGrid(el, { entity, rows, cols, onOpen, onChanged, search = true, persist = null }) {
  // A main list (persist = its view name) remembers search, sort and filters, which is
  // also what a saved view stores; it also shows the record type's custom fields.
  const saved = persist ? (S.gridState[persist] ??= { sort: null, dir: 1, q: "", filters: [] }) : null;
  const state = saved ?? { sort: null, dir: 1, q: "", filters: [] };
  if (persist && entity && S.meta.customizable?.includes(entity)) {
    const have = new Set(cols.map((c) => (typeof c === "string" ? c : c.name)));
    cols = [...cols, ...fieldsOf(entity).filter((f) => f.custom && f.type !== "longtext" && !have.has(f.name)).map((f) => f.name)];
  }
  const colDefs = cols.map((c) => {
    const f = typeof c === "string" ? fieldOf(entity, c) : c.field ?? (entity && fieldOf(entity, c.name));
    const base = typeof c === "string" ? { name: c } : c;
    return {
      ...base,
      field: f,
      label: base.label ?? f?.label ?? base.name,
      num: base.num ?? (f ? isNum(f) : false),
      editable: base.editable ?? Boolean(f && entity && !base.value),
    };
  });

  const filterable = entity ? fieldsOf(entity).filter((f) => ["select", "ref", "text"].includes(f.type)) : [];
  el.innerHTML = `${search ? `<div class="grid-tools"><input type="search" placeholder="Search..." style="max-width:280px" value="${esc(state.q)}">
      ${filterable.length ? '<span data-chips></span><button type="button" class="btn small" data-addf>+ Filter</button>' : ""}</div>` : ""}
    <div class="grid-wrap"><table class="grid"><thead></thead><tbody></tbody><tfoot></tfoot></table></div>`;
  const q = $("input[type=search]", el);
  if (q) q.oninput = () => { state.q = q.value.toLowerCase(); draw(); };

  const fieldNamed = (n) => filterable.find((f) => f.name === n);
  const drawChips = () => {
    const chips = $("[data-chips]", el);
    if (!chips) return;
    chips.innerHTML = state.filters.map(([n, v], i) => {
      const f = fieldNamed(n);
      const shown = f?.type === "ref" ? refLabel(f.ref, v) : v;
      return `<span class="chip">${esc(f?.label ?? n)}${f?.type === "text" ? " contains" : ":"} ${esc(shown)}
        <span class="link" data-rmf="${i}">×</span></span>`;
    }).join("");
  };
  const tools = $(".grid-tools", el);
  if (tools) tools.onclick = (e) => {
    const rm = e.target.closest("[data-rmf]");
    if (rm) { state.filters.splice(Number(rm.dataset.rmf), 1); drawChips(); draw(); return; }
    if (!e.target.closest("[data-addf]") || $(".filter-add", el)) return;
    const box = document.createElement("span");
    box.className = "filter-add";
    box.innerHTML = `<select data-ff>${filterable.map((f) => `<option value="${f.name}">${esc(f.label)}</option>`).join("")}</select>
      <span data-fv></span> <button type="button" class="btn small" data-ok>Add</button>`;
    tools.append(box);
    const fv = $("[data-fv]", box), ff = $("[data-ff]", box);
    const valueInput = () => {
      const f = fieldNamed(ff.value);
      fv.innerHTML = f.type === "text" ? '<input type="text" placeholder="contains...">' : input(f, "");
    };
    ff.onchange = valueInput;
    valueInput();
    $("[data-ok]", box).onclick = () => {
      const v = $("input,select", fv).value.trim();
      if (v) state.filters.push([ff.value, v]);
      box.remove();
      drawChips();
      draw();
    };
  };
  drawChips();

  const cellHtml = (c, r) => (c.value ? c.value(r) : display(c.field, r[c.name]));
  const sortVal = (c, r) => {
    const v = c.sortValue ? c.sortValue(r) : r[c.name];
    if (c.field?.type === "ref") return refLabel(c.field.ref, v).toLowerCase();
    return typeof v === "string" ? v.toLowerCase() : v;
  };

  function draw() {
    let list = rows;
    for (const [n, v] of state.filters) {
      const f = fieldNamed(n);
      if (!f) continue; // e.g. a filter on a custom field that was removed
      list = f.type === "text"
        ? list.filter((r) => String(r[n] ?? "").toLowerCase().includes(v.toLowerCase()))
        : list.filter((r) => String(r[n] ?? "") === String(v));
    }
    if (state.q) {
      list = list.filter((r) => colDefs.some((c) =>
        String(cellHtml(c, r) ?? "").replace(/<[^>]+>/g, "").toLowerCase().includes(state.q)));
    }
    if (state.sort) {
      const c = colDefs.find((x) => x.name === state.sort);
      list = [...list].sort((a, b) => {
        const x = sortVal(c, a), y = sortVal(c, b);
        if (x == null) return 1;
        if (y == null) return -1;
        return (x > y ? 1 : x < y ? -1 : 0) * state.dir;
      });
    }
    $("thead", el).innerHTML = `<tr>${onOpen ? "<th></th>" : ""}${colDefs.map((c) =>
      `<th data-c="${c.name}" class="${c.num ? "num" : ""}">${esc(c.label)}${state.sort === c.name ? (state.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr>`;
    $("tbody", el).innerHTML = list.length ? list.map((r) => `<tr data-id="${r.id ?? ""}">
      ${onOpen ? `<td class="open-col"><div class="cell"><span class="link" data-open>Open</span></div></td>` : ""}
      ${colDefs.map((c) => `<td data-c="${c.name}" class="${c.editable && r._can_edit ? "editable" : ""} ${c.num ? "num" : ""}">
        <div class="cell">${cellHtml(c, r) ?? ""}</div></td>`).join("")}</tr>`).join("")
      : `<tr><td colspan="${colDefs.length + 1}"><div class="empty">No records yet.</div></td></tr>`;
    const hasTotals = colDefs.some((c) => c.total);
    $("tfoot", el).innerHTML = hasTotals && list.length ? `<tr>${onOpen ? "<td></td>" : ""}${colDefs.map((c) =>
      `<td class="${c.num ? "num" : ""}"><div class="cell">${c.total ? c.total(list) : ""}</div></td>`).join("")}</tr>` : "";
    rowsById = Object.fromEntries(list.map((r) => [r.id, r]));
  }
  let rowsById = {};

  $("thead", el).onclick = (e) => {
    const th = e.target.closest("th[data-c]");
    if (!th) return;
    state.dir = state.sort === th.dataset.c ? -state.dir : 1;
    state.sort = th.dataset.c;
    draw();
  };

  $("tbody", el).onclick = (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    const row = rowsById[tr.dataset.id];
    if (e.target.closest("[data-open]")) return onOpen(row);
    const td = e.target.closest("td.editable");
    if (!td || td.querySelector("input,select,textarea")) return;
    const c = colDefs.find((x) => x.name === td.dataset.c);
    const f = c.field.type === "longtext" ? { ...c.field, type: "text" } : c.field;
    td.innerHTML = input(f, row[c.name]);
    const inp = td.querySelector("input,select");
    inp.focus();
    let done = false;
    const commit = async (save) => {
      if (done) return;
      done = true;
      const val = c.field.type === "money" ? fromThousands(inp.value) : inp.value;
      if (save && String(val) !== String(row[c.name] ?? "")) {
        try {
          const updated = await api("PATCH", `/api/${entity}/${row.id}`, { [c.name]: val });
          Object.assign(row, updated);
          onChanged?.(row);
        } catch (err) { toast(err.message, true); }
      }
      td.innerHTML = `<div class="cell">${cellHtml(c, row) ?? ""}</div>`;
    };
    inp.onblur = () => commit(true);
    inp.onkeydown = (ev) => {
      if (ev.key === "Enter") inp.blur();
      if (ev.key === "Escape") commit(false);
    };
    if (inp.tagName === "SELECT") inp.onchange = () => inp.blur();
  };

  draw();
}

// ------------------------------------------------------------------ board (HubSpot-style)

function renderBoard(el, { entity, stageField, stages, rows, card, amount, onOpen, onMove }) {
  el.innerHTML = `<div class="board">${stages.map((s) => `
    <div class="col" data-stage="${esc(s)}">
      <div class="col-head"><div class="name"><span>${esc(s)}</span><span class="muted"></span></div>
        <div class="sum"></div></div>
      <div class="col-body"></div>
    </div>`).join("")}</div>`;

  const cols = Object.fromEntries([...el.querySelectorAll(".col")].map((c) => [c.dataset.stage, c]));
  const draw = () => {
    for (const s of stages) {
      const items = rows.filter((r) => r[stageField] === s);
      const col = cols[s];
      $(".name .muted", col).textContent = items.length;
      $(".sum", col).textContent = fmt.money(items.reduce((t, r) => t + (amount(r) || 0), 0));
      $(".col-body", col).innerHTML = items.map((r) =>
        `<div class="card" draggable="${Boolean(r._can_edit)}" data-id="${r.id}">${card(r)}</div>`).join("");
    }
  };

  let dragId = null;
  el.addEventListener("dragstart", (e) => {
    const c = e.target.closest(".card");
    if (!c) return;
    dragId = Number(c.dataset.id);
    c.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
  });
  el.addEventListener("dragend", (e) => e.target.closest(".card")?.classList.remove("dragging"));
  for (const col of Object.values(cols)) {
    col.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("drop"); });
    col.addEventListener("dragleave", (e) => { if (!col.contains(e.relatedTarget)) col.classList.remove("drop"); });
    col.addEventListener("drop", async (e) => {
      e.preventDefault();
      col.classList.remove("drop");
      const row = rows.find((r) => r.id === dragId);
      const stage = col.dataset.stage;
      if (!row || !row._can_edit || row[stageField] === stage) return;
      const extra = (await onMove?.(row, stage)) ?? {};
      if (extra === false) return;
      try {
        Object.assign(row, await api("PATCH", `/api/${entity}/${row.id}`, { [stageField]: stage, ...extra }));
        draw();
      } catch (err) { toast(err.message, true); }
    });
  }
  el.addEventListener("click", (e) => {
    const c = e.target.closest(".card");
    if (c) onOpen(rows.find((r) => r.id === Number(c.dataset.id)));
  });
  draw();
}

// ------------------------------------------------------------------ drawer / forms

function openDrawer(html) {
  const d = $("#drawer");
  d.innerHTML = html;
  d.hidden = false;
  $("#drawer-backdrop").hidden = false;
  return d;
}
function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawer-backdrop").hidden = true;
}
$("#drawer-backdrop").onclick = closeDrawer;
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#drawer").hidden) closeDrawer(); });

function formHtml(fields, values = {}) {
  return `<form class="form">${fields.map((f) =>
    `<label class="${f.type === "longtext" ? "wide" : ""}">${esc(f.label)}${f.type === "money" ? " ($000s)" : ""}${f.required ? " *" : ""}${input(f, values[f.name])}</label>`).join("")}</form>`;
}
function formValues(form, fields, original = null) {
  const out = {};
  for (const f of fields) {
    const raw = form.elements[f.name].value;
    const v = f.type === "money" ? fromThousands(raw) : raw;
    if (!original || String(v) !== String(original[f.name] ?? "")) out[f.name] = v;
  }
  return out;
}

const ACTIVITY_OWNERS = ["lps", "commitments", "companies", "funds", "investors", "rounds"];

// Pipeline forms where you can type a new LP / investor instead of creating it first.
const INLINE_NEW = {
  commitments: { field: "lp_id", entity: "lps", label: "Or new LP name", what: "LP" },
  round_investors: { field: "investor_id", entity: "investors", label: "Or new investor name", what: "investor" },
};

function recordTitle(entity, r) {
  if (!r?.id) return `New ${S.meta.entities[entity].label.replace(/s$/, "").toLowerCase()}`;
  if (entity === "commitments") return `${refLabel("lps", r.lp_id)} / ${refLabel("funds", r.fund_id)}`;
  return r.name ?? `${S.meta.entities[entity].label} #${r.id}`;
}

/** Open a create/edit form for any entity. `after` re-renders the calling view. */
async function editRecord(entity, row, after, defaults = {}) {
  const fields = fieldsOf(entity);
  const isNew = !row?.id;
  const ro = !isNew && !row._can_edit;
  const values = isNew ? { ...Object.fromEntries(fields.filter((f) => f.default).map((f) => [f.name, f.default])), ...defaults } : row;
  const d = openDrawer(`
    <h2>${esc(recordTitle(entity, row))}</h2>
    <div class="muted">${esc(S.meta.entities[entity].label)}</div>
    ${formHtml(fields, values)}
    <div class="drawer-actions">
      <div>${ro ? "" : `<button class="btn primary" data-save>${isNew ? "Create" : "Save"}</button>`}
        <button class="btn" data-cancel>Close</button></div>
      ${isNew || !row._can_delete ? "" : '<button class="btn danger" data-del>Delete</button>'}
    </div>
    <div data-extra></div>`);
  const form = $("form", d);
  if (ro) form.querySelectorAll("input,select,textarea").forEach((x) => (x.disabled = true));
  // Adding to a pipeline: allow typing a brand-new LP / investor instead of
  // having to create it on its own screen first.
  const inline = isNew && canCreateShared() ? INLINE_NEW[entity] : null;
  const newRef = inline && !defaults[inline.field] ? inline : null;
  if (newRef) {
    const label = document.createElement("label");
    label.innerHTML = `${newRef.label}<input type="text" name="_new_ref" placeholder="Type to create a new ${newRef.what}">`;
    form.elements[newRef.field].closest("label").after(label);
    if (!Object.keys(S.look[newRef.entity]).length) form.elements._new_ref.focus();
    if (DUP_ENTITIES.includes(newRef.entity)) watchDuplicates(form.elements._new_ref, newRef.entity);
  }
  if (!ro && DUP_ENTITIES.includes(entity)) watchDuplicates(form.elements.name, entity, row?.id);
  $("[data-cancel]", d).onclick = closeDrawer;
  const save = $("[data-save]", d);
  if (save) save.onclick = () => guard(async () => {
    const body = formValues(form, fields, isNew ? null : row);
    let created = null;
    if (newRef) {
      const name = form.elements._new_ref.value.trim();
      const what = newRef.what;
      if (name && body[newRef.field]) throw new Error(`Choose an existing ${what} or type a new ${what} name, not both.`);
      if (!name && !body[newRef.field]) throw new Error(`Choose an ${what}, or type a new ${what} name.`);
      if (name) {
        created = await api("POST", `/api/${newRef.entity}`, { name });
        body[newRef.field] = created.id;
      }
    }
    if (isNew) {
      try {
        await api("POST", `/api/${entity}`, body);
      } catch (err) {
        // Do not leave a stray LP / investor behind if the pipeline entry was refused.
        if (created) await api("DELETE", `/api/${newRef.entity}/${created.id}`).catch(() => {});
        throw err;
      }
    } else if (Object.keys(body).length) await api("PATCH", `/api/${entity}/${row.id}`, body);
    toast("Saved");
    closeDrawer();
    await loadLookups();
    after?.();
  });
  const del = $("[data-del]", d);
  if (del) del.onclick = () => guard(async () => {
    if (!confirm(`Delete ${recordTitle(entity, row)}? This cannot be undone.`)) return;
    await api("DELETE", `/api/${entity}/${row.id}`);
    toast("Deleted");
    closeDrawer();
    await loadLookups();
    after?.();
  });
  if (!isNew) {
    const extra = $("[data-extra]", d);
    if (entity === "companies") await companyExtras(extra, row, after);
    if (entity === "lps") await lpExtras(extra, row, after);
    if (entity === "rounds") roundExtras(extra, row);
    if (entity === "investors") await investorExtras(extra, row);
    if (entity === "contacts") await contactLinksSection(extra, row);
    if (CONTACT_PARENTS.includes(entity)) await peopleSection(extra, entity, row);
    if (TASK_PARENTS.includes(entity)) await tasksSection(extra, entity, row);
    if (DOC_PARENTS.includes(entity)) await documentsSection(extra, entity, row);
    if (ACTIVITY_OWNERS.includes(entity)) await activityLog(extra, entity, row.id, Boolean(row._can_edit));
    await changeHistory(extra, entity, row.id);
  }
}

const CONTACT_PARENTS = ["lps", "investors", "companies"];
const TASK_PARENTS = ["lps", "commitments", "companies", "funds", "investors", "rounds"];
const DOC_PARENTS = [...TASK_PARENTS, "capital_events"];

/** A short name for any record, for task and document lists. */
function recordLabel(entity, id) {
  if (!entity || id == null) return "";
  if (entity === "commitments") {
    const c = S.cache?.commitments?.[id];
    return c ? `${refLabel("lps", c.lp_id)} / ${refLabel("funds", c.fund_id)}` : `Pipeline entry #${id}`;
  }
  if (entity === "capital_events") return `Capital event #${id}`;
  return refLabel(entity, id);
}

async function openRecord(entity, id, after) {
  editRecord(entity, await api("GET", `/api/${entity}/${id}`), after);
}

const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");

/** People linked to an LP / investor / company, with role and primary flag. */
async function peopleSection(el, entity, row) {
  const box = document.createElement("div");
  el.append(box);
  const draw = async () => {
    const links = await api("GET", `/api/contact_links?entity=${entity}&entity_id=${row.id}`);
    box.innerHTML = `<div class="section-title">People
        ${row._can_edit ? '<button class="btn small" data-add>+ Add person</button>' : ""}</div>
      ${links.map((l) => { const c = S.look.contacts[l.contact_id] || {};
        return `<div class="activity"><span class="link" data-open="${l.contact_id}">${esc(c.name ?? "Person")}</span>
          ${l.is_primary === "Yes" ? '<span class="pill">Primary</span>' : ""}
          <span class="muted">${esc([l.role, c.email, c.phone].filter(Boolean).join(" · "))}</span>
          ${l._can_delete ? `<span class="link" data-unlink="${l.id}" style="float:right">remove</span>` : ""}</div>`; }).join("")
        || '<div class="muted">No people linked yet.</div>'}`;
  };
  box.onclick = (e) => {
    const t = e.target.closest("[data-add],[data-open],[data-unlink]");
    if (!t) return;
    if (t.dataset.open) return openRecord("contacts", t.dataset.open);
    if (t.dataset.unlink) {
      return guard(async () => { await api("DELETE", `/api/contact_links/${t.dataset.unlink}`); draw(); });
    }
    addPersonDialog(box, entity, row, draw);
  };
  await draw();
}

function addPersonDialog(box, entity, row, after) {
  const people = Object.values(S.look.contacts).sort((a, b) => a.name.localeCompare(b.name));
  const holder = document.createElement("div");
  holder.className = "panel";
  holder.innerHTML = `<form class="form" style="margin:0">
      <label>Existing person<select name="contact_id"><option value=""></option>${people.map((c) =>
        `<option value="${c.id}">${esc(c.name)}${c.email ? ` (${esc(c.email)})` : ""}</option>`).join("")}</select></label>
      <label>Or new person's name<input type="text" name="new_name"></label>
      <label>Email (new person)<input type="text" name="email"></label>
      <label>Role / title here<input type="text" name="role" placeholder="e.g. CIO, Founder & CEO"></label>
      <label>Primary contact<select name="is_primary"><option>No</option><option>Yes</option></select></label>
      <div style="align-self:end"><button class="btn primary">Add</button> <button type="button" class="btn" data-x>Cancel</button></div>
    </form>`;
  box.append(holder);
  const f = $("form", holder);
  watchDuplicates(f.new_name, "contacts");
  $("[data-x]", holder).onclick = () => holder.remove();
  f.onsubmit = (e) => {
    e.preventDefault();
    guard(async () => {
      let id = f.contact_id.value;
      const name = f.new_name.value.trim();
      if (!id && !name) throw new Error("Choose a person or type a new name.");
      if (id && name) throw new Error("Choose an existing person or type a new name, not both.");
      if (!id) id = (await api("POST", "/api/contacts", { name, email: f.email.value })).id;
      await api("POST", "/api/contact_links", { contact_id: id, entity, entity_id: row.id,
        role: f.elements.role.value, is_primary: f.is_primary.value });
      await loadLookups();
      holder.remove();
      after();
    });
  };
}

/** On a person's record: the LPs, investors and companies they are linked to. */
async function contactLinksSection(el, contact) {
  const box = document.createElement("div");
  el.append(box);
  const kinds = { lps: "LP", investors: "Investor", companies: "Company" };
  const draw = async () => {
    const links = await api("GET", `/api/contact_links?contact_id=${contact.id}`);
    box.innerHTML = `<div class="section-title">Linked to</div>
      ${links.map((l) => `<div class="activity"><span class="muted">${kinds[l.entity]}:</span>
        <span class="link" data-open="${l.entity}:${l.entity_id}">${esc(refLabel(l.entity, l.entity_id))}</span>
        ${l.role ? ` · ${esc(l.role)}` : ""}${l.is_primary === "Yes" ? ' <span class="pill">Primary</span>' : ""}
        ${l._can_delete ? `<span class="link" data-unlink="${l.id}" style="float:right">remove</span>` : ""}</div>`).join("")
        || '<div class="muted">Not linked to any LP, investor or company yet.</div>'}
      <form class="form" style="grid-template-columns: 120px 1fr 1fr auto; align-items:end">
        <select name="entity">${Object.entries(kinds).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <select name="entity_id"></select>
        <input type="text" name="role" placeholder="Role">
        <button class="btn">Link</button></form>`;
    const f = $("form", box);
    const fill = () => {
      const recs = Object.values(S.look[f.entity.value]).sort((a, b) => a.name.localeCompare(b.name));
      f.entity_id.innerHTML = recs.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("");
    };
    f.entity.onchange = fill;
    fill();
    f.onsubmit = (e) => {
      e.preventDefault();
      guard(async () => {
        await api("POST", "/api/contact_links", { contact_id: contact.id, entity: f.entity.value,
          entity_id: f.entity_id.value, role: f.elements.role.value });
        draw();
      });
    };
  };
  box.onclick = (e) => {
    const t = e.target.closest("[data-open],[data-unlink]");
    if (!t) return;
    if (t.dataset.open) { const [en, id] = t.dataset.open.split(":"); return openRecord(en, id); }
    guard(async () => { await api("DELETE", `/api/contact_links/${t.dataset.unlink}`); draw(); });
  };
  await draw();
}

/** Links to files in Drive / Dropbox / DocSend attached to a record. */
async function documentsSection(el, entity, row) {
  const box = document.createElement("div");
  el.append(box);
  const types = fieldOf("documents", "doc_type").options;
  const draw = async () => {
    const docs = await api("GET", `/api/documents?entity=${entity}&entity_id=${row.id}`);
    box.innerHTML = `<div class="section-title">Documents</div>
      ${docs.map((x) => `<div class="activity"><a href="${esc(safeUrl(x.url))}" target="_blank" rel="noopener noreferrer">${esc(x.title)}</a>
        <span class="muted">${esc([x.doc_type, x.doc_date].filter(Boolean).join(" · "))}</span>
        ${x._can_delete ? `<span class="link" data-del-doc="${x.id}" style="float:right">remove</span>` : ""}</div>`).join("")
        || '<div class="muted">No documents linked.</div>'}
      ${row._can_edit ? `<form class="form" style="grid-template-columns: 1fr 1.4fr 140px auto; align-items:end">
        <input type="text" name="title" placeholder="Title, e.g. Q3 board deck">
        <input type="text" name="url" placeholder="Paste a Drive / Dropbox / DocSend link">
        <select name="doc_type"><option value="">Type</option>${types.map((t) => `<option>${esc(t)}</option>`).join("")}</select>
        <button class="btn">Add link</button></form>` : ""}`;
    const f = $("form", box);
    if (f) f.onsubmit = (e) => {
      e.preventDefault();
      guard(async () => {
        await api("POST", "/api/documents", { entity, entity_id: row.id, title: f.elements.title.value,
          url: f.url.value, doc_type: f.doc_type.value, doc_date: today() });
        draw();
      });
    };
  };
  box.onclick = (e) => {
    const id = e.target.closest("[data-del-doc]")?.dataset.delDoc;
    if (id) guard(async () => { await api("DELETE", `/api/documents/${id}`); draw(); });
  };
  await draw();
}

/** Open tasks about a record, with a quick add. */
async function tasksSection(el, entity, row) {
  const box = document.createElement("div");
  el.append(box);
  const people = Object.values(S.look.users).sort((a, b) => a.name.localeCompare(b.name));
  const draw = async () => {
    const tasks = (await api("GET", `/api/tasks?entity=${entity}&entity_id=${row.id}`)).filter((t) => t.status === "Open");
    box.innerHTML = `<div class="section-title">Tasks</div>
      ${tasks.map((t) => `<div class="activity"><label style="display:inline"><input type="checkbox" data-done="${t.id}" ${t._can_edit ? "" : "disabled"}>
        ${esc(t.title)}</label> <span class="muted ${dueClass(t.due_date)}">${esc([t.due_date, refLabel("users", t.assignee_id)].filter(Boolean).join(" · "))}</span></div>`).join("")
        || '<div class="muted">No open tasks.</div>'}
      <form class="form" style="grid-template-columns: 1.6fr 130px 1fr auto; align-items:end">
        <input type="text" name="title" placeholder="Add a task, e.g. Send follow-up deck">
        <input type="date" name="due_date">
        <select name="assignee_id"><option value="">Assign to...</option>${people.map((u) =>
          `<option value="${u.id}" ${u.id === S.me.id ? "selected" : ""}>${esc(u.name)}</option>`).join("")}</select>
        <button class="btn">Add</button></form>`;
    const f = $("form", box);
    f.onsubmit = (e) => {
      e.preventDefault();
      guard(async () => {
        await api("POST", "/api/tasks", { title: f.elements.title.value, due_date: f.due_date.value,
          assignee_id: f.assignee_id.value, entity, entity_id: row.id });
        await loadLookups();
        draw();
      });
    };
  };
  box.onchange = (e) => {
    const id = e.target.closest("[data-done]")?.dataset.done;
    if (id) guard(async () => { await api("PATCH", `/api/tasks/${id}`, { status: "Done" }); toast("Done"); await loadLookups(); draw(); });
  };
  await draw();
}

async function activityLog(el, entity, id, writable) {
  const box = document.createElement("div");
  el.append(box);
  const draw = async () => {
    const items = await api("GET", `/api/activities?entity=${entity}&entity_id=${id}`);
    box.innerHTML = `<div class="section-title">Activity</div>
      <form class="form" ${writable ? "" : "hidden"} style="grid-template-columns: 130px 110px 1fr auto; align-items:end; margin-top:0">
        <input type="date" name="activity_date" value="${today()}">
        <select name="kind">${S.meta.entities.activities.fields.find((f) => f.name === "kind").options
          .map((o) => `<option>${o}</option>`).join("")}</select>
        <input type="text" name="summary" placeholder="Log a call, meeting, note...">
        <button class="btn">Add</button>
      </form>
      ${items.map((a) => `<div class="activity"><div class="when">${esc(a.activity_date)} · ${esc(a.kind)}${a.created_by ? ` · ${esc(S.people[a.created_by] || "Former user")}` : ""}
        ${a._can_edit ? `<span class="link" data-del-act="${a.id}" style="float:right">remove</span>` : ""}</div>${esc(a.summary)}</div>`).join("")
        || '<div class="muted">No activity logged.</div>'}`;
    const f = $("form", box);
    f.onsubmit = (e) => {
      e.preventDefault();
      guard(async () => {
        await api("POST", "/api/activities", {
          entity, entity_id: id, activity_date: f.activity_date.value, kind: f.kind.value, summary: f.summary.value,
        });
        draw();
      });
    };
    box.querySelectorAll("[data-del-act]").forEach((x) => (x.onclick = () => guard(async () => {
      await api("DELETE", `/api/activities/${x.dataset.delAct}`);
      draw();
    })));
  };
  await draw();
}

const ACTION_LABEL = { create: "created", update: "updated", delete: "deleted", password_change: "changed their password" };
const ENTITY_NOUN = {
  commitments: "pipeline entry", investments: "investment", valuations: "valuation", realizations: "realization",
  capital_events: "capital event", rounds: "round", round_investors: "round investor", activities: "activity",
  lps: "LP", companies: "company", funds: "fund", investors: "investor", users: "user",
  contacts: "person", contact_links: "person link", documents: "document", tasks: "task",
};
const USER_FIELDS = [
  { name: "name", label: "Name", type: "text" }, { name: "email", label: "Email", type: "text" },
  { name: "role", label: "General role", type: "text" }, { name: "active", label: "Active", type: "text" },
  { name: "password", label: "Password", type: "text" },
];

function historyFields(entity) {
  if (entity === "capital_events") return S.meta.capital_event_fields;
  if (entity === "users") return USER_FIELDS;
  return S.meta.entities[entity]?.fields ?? [];
}

/** Plain-text value for the history list: labels for linked records, $K/$M for money. */
function historyValue(field, v) {
  if (v == null || v === "") return "(blank)";
  if (!field) return typeof v === "object" ? JSON.stringify(v) : String(v);
  switch (field.type) {
    case "money": return fmt.money(v);
    case "ref": return refLabel(field.ref, v);
    case "percent": return `${v}%`;
    case "number": case "integer": return fmt.num(v);
    case "longtext": return String(v).length > 120 ? `${String(v).slice(0, 120)}...` : String(v);
    default: return String(v);
  }
}

/** Which record a history entry is about, when it is a related (child) record. */
function historySubject(e) {
  const r = e.record || {};
  const co = refLabel("companies", r.company_id);
  switch (e.entity) {
    case "commitments": return `${refLabel("lps", r.lp_id)} / ${refLabel("funds", r.fund_id)}`;
    case "investments": return `${co} · ${refLabel("funds", r.fund_id)} · ${fmt.money(r.amount)} on ${r.investment_date ?? ""}`;
    case "valuations": return `${co} · mark of ${r.valuation_date ?? ""}`;
    case "realizations": return `${co} · ${r.realization_type ?? ""} ${r.realization_date ?? ""}`;
    case "capital_events": return `${r.event_type ?? ""} of ${r.event_date ?? ""} · ${fmt.money(r.total_amount)}`;
    case "rounds": return r.company_id ? roundLabel(r) : "";
    case "round_investors": return `${refLabel("investors", r.investor_id)} in ${S.look.rounds[r.round_id]?.name ?? "a round"}`;
    case "activities": return `${r.kind ?? "Note"}: ${historyValue({ type: "longtext" }, r.summary)}`;
    case "tasks": case "documents": return r.title ?? "";
    case "contact_links": return `${refLabel("contacts", r.contact_id)} at ${refLabel(r.entity, r.entity_id)}${r.role ? ` (${r.role})` : ""}`;
    default: return r.name ?? "";
  }
}

function historyLines(e) {
  const fields = Object.fromEntries(historyFields(e.entity).map((f) => [f.name, f]));
  const c = e.changes || {};
  if (e.entity === "capital_events" && "paid" in c) {
    return [`${c.lp ?? "LP"}: marked ${c.paid.to ? "paid" : "not paid"}`];
  }
  if (e.entity === "users" && c.fund_access) {
    return [`Fund access: ${fundAccessSummary(c.fund_access) || "no funds"}`];
  }
  if (e.action === "update") {
    return Object.entries(c).map(([k, v]) => {
      const f = fields[k];
      const label = f?.label ?? k;
      if (v && typeof v === "object" && "to" in v) return `${label}: ${historyValue(f, v.from)} → ${historyValue(f, v.to)}`;
      return `${label}: ${historyValue(f, v)}`;
    });
  }
  if (e.action === "create" && e.entity !== "activities") { // a note's text is already in the subject
    // Show what the record was created with (non-empty fields only).
    return Object.entries(c)
      .filter(([k, v]) => v != null && v !== "" && fields[k] && k !== "created_by")
      .map(([k, v]) => `${fields[k].label}: ${historyValue(fields[k], v)}`);
  }
  return [];
}

const localTime = (at) => new Date(`${at.replace(" ", "T")}Z`)
  .toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

/** Change history panel: this record's changes plus its related records', newest first. */
async function changeHistory(el, entity, id) {
  const PAGE = 25;
  const box = document.createElement("div");
  box.innerHTML = `<div class="section-title">Change history
      <label class="muted" style="font-weight:400;font-size:12px"><input type="checkbox" data-rel checked> Include related records</label></div>
    <div data-list></div><button class="btn small" data-more hidden>Show older changes</button>`;
  el.append(box);
  const list = $("[data-list]", box), more = $("[data-more]", box), rel = $("[data-rel]", box);
  let last = null;
  const load = async (reset) => {
    if (reset) { list.innerHTML = ""; last = null; }
    const items = await api("GET", `/api/audit?entity=${entity}&entity_id=${id}&limit=${PAGE}` +
      `&related=${rel.checked}${last ? `&before=${last}` : ""}`);
    list.insertAdjacentHTML("beforeend", items.map((e) => {
      const own = e.entity === entity && e.entity_id === id;
      const subject = own ? "" : ` ${ENTITY_NOUN[e.entity] ?? e.entity} · ${historySubject(e)}`;
      const lines = historyLines(e);
      return `<div class="activity"><div class="when">${esc(localTime(e.at))} · ${esc(e.user_name || "Former user")}
          ${esc(ACTION_LABEL[e.action] || e.action)}${esc(subject)}</div>
        ${lines.map((l) => `<div>${esc(l)}</div>`).join("")}</div>`;
    }).join(""));
    if (!list.children.length) list.innerHTML = '<div class="muted">No recorded changes.</div>';
    if (items.length) last = items[items.length - 1].id;
    more.hidden = items.length < PAGE;
  };
  rel.onchange = () => guard(() => load(true));
  more.onclick = () => guard(() => load(false));
  await load(true);
}

async function lpExtras(el, lp, after) {
  const commits = await api("GET", `/api/commitments?lp_id=${lp.id}`);
  const box = document.createElement("div");
  box.innerHTML = `<div class="section-title">Fund commitments ${canEditSomeFund() ? '<button class="btn small">+ Add to fund pipeline</button>' : ""}</div><div></div>`;
  el.append(box);
  const add = $("button", box);
  if (add) add.onclick = () => editRecord("commitments", null, after, { lp_id: lp.id });
  renderGrid(box.lastElementChild, {
    entity: "commitments", rows: commits, search: false,
    cols: ["fund_id", "stage", "target_amount", "committed_amount"],
    onOpen: (r) => editRecord("commitments", r, after),
  });
}

async function companyExtras(el, co, after) {
  const box = document.createElement("div");
  el.append(box);
  const tabs = [["company_metrics", "KPIs"], ["investments", "Investments"], ["valuations", "Valuations"],
    ["realizations", "Realizations"], ["rounds", "Rounds"]];
  const noun = { company_metrics: "KPI period", investments: "investment", valuations: "valuation",
    realizations: "realization", rounds: "round" };
  let cur = "investments";
  const draw = async () => {
    const rows = await api("GET", `/api/${cur}?company_id=${co.id}`);
    box.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button data-t="${k}" class="${k === cur ? "on" : ""}">${l}</button>`).join("")}</div>
      ${cur === "company_metrics" ? kpiSummary(rows) : ""}
      <div style="margin:10px 0" ${canEditSomeFund() ? "" : "hidden"}><button class="btn small" data-add>+ Add ${noun[cur]}</button></div><div data-g></div>`;
    box.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    const defaults = { company_id: co.id, fund_id: co.fund_id ?? "" };
    const dateField = { investments: "investment_date", valuations: "valuation_date", realizations: "realization_date" }[cur];
    if (dateField) defaults[dateField] = today();
    if (cur === "company_metrics") defaults.period_end = lastMonthEnd();
    $("[data-add]", box).onclick = () => editRecord(cur, null, () => { after?.(); }, defaults);
    renderGrid($("[data-g]", box), {
      entity: cur, rows, search: false,
      cols: fieldsOf(cur).filter((f) => !["company_id", "notes"].includes(f.name)).map((f) => f.name),
      onOpen: (r) => editRecord(cur, r, after),
      onChanged: () => after?.(),
    });
  };
  await draw();
}

function lastMonthEnd() {
  const d = new Date();
  return new Date(Date.UTC(d.getFullYear(), d.getMonth(), 0)).toISOString().slice(0, 10);
}

const runway = (k) => (k?.cash != null && k?.burn > 0 ? k.cash / k.burn : null);

/** Small trend charts of the latest KPIs for a company (rows: company_metrics, any order). */
function kpiSummary(rows) {
  if (!rows.length) return '<div class="muted" style="margin:10px 0">No KPIs yet. Add a period with revenue, ARR, burn, cash and headcount to see trends here.</div>';
  const series = [...rows].sort((a, b) => a.period_end.localeCompare(b.period_end));
  const last = series[series.length - 1];
  const tiles = [["arr", "ARR", fmt.money], ["revenue", "Revenue", fmt.money], ["burn", "Net burn / mo", fmt.money],
    ["cash", "Cash", fmt.money], ["headcount", "Headcount", fmt.num]]
    .filter(([k]) => series.some((r) => r[k] != null))
    .map(([k, label, f]) => {
      const pts = series.filter((r) => r[k] != null);
      const latest = pts[pts.length - 1][k], prev = pts.length > 1 ? pts[pts.length - 2][k] : null;
      const change = prev ? (latest - prev) / Math.abs(prev) : null;
      return `<div class="kpi"><div class="label">${label}</div><div class="value">${f(latest)}</div>
        <div class="sub">${change == null ? "&nbsp;" : `${change >= 0 ? "+" : ""}${(change * 100).toFixed(0)}% vs prior period`}</div>
        ${sparkline(pts.map((r) => r[k]))}</div>`;
    }).join("");
  const rw = runway(last);
  return `<div class="muted" style="margin-top:10px">Latest period ${esc(last.period_end)}${rw != null ? ` · runway <b>${rw.toFixed(1)} months</b> at current burn` : ""}</div>
    <div class="kpis kpis-small">${tiles}</div>`;
}

function sparkline(values, w = 140, h = 36) {
  if (values.length < 2) return "";
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - 3 - ((v - min) / span) * (h - 6)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>`;
}

function roundExtras(el, rnd) {
  const box = document.createElement("div");
  box.innerHTML = `<div class="section-title">Investor pipeline
    <button class="btn small">Open pipeline board</button></div>`;
  $("button", box).onclick = () => { pref("round", rnd.id); closeDrawer(); go("round-board"); };
  el.append(box);
}

async function investorExtras(el, inv) {
  const rows = await api("GET", `/api/round_investors?investor_id=${inv.id}`);
  const box = document.createElement("div");
  box.innerHTML = `<div class="section-title">Rounds they are in</div><div></div>`;
  el.append(box);
  renderGrid(box.lastElementChild, {
    entity: "round_investors", rows, search: false,
    cols: ["round_id", "stage", "participation", "amount"],
    onOpen: (r) => editRecord("round_investors", r),
  });
}

// ------------------------------------------------------------------ views

const VIEWS = {};

VIEWS.dashboard = {
  title: "Dashboard",
  noSavedViews: true,
  async render(el) {
    const [d, rounds, pipe, inv] = await Promise.all([api("GET", "/api/dashboard"),
      api("GET", "/api/rounds"), api("GET", "/api/round_investors"), api("GET", "/api/investments")]);
    const live = rounds.filter((r) => ["Raising", "Planning"].includes(r.status));
    const totals = roundTotals(pipe, inv);
    const p = d.portfolio;
    const raising = d.funds.filter((f) => f.status === "Raising");
    el.innerHTML = `
      <div class="kpis">
        <div class="kpi"><div class="label">Total LP commitments</div><div class="value">${fmt.money(d.funds.reduce((t, f) => t + f.committed, 0))}</div>
          <div class="sub">${d.funds.length} fund(s)</div></div>
        <div class="kpi"><div class="label">Capital called</div><div class="value">${fmt.money(d.funds.reduce((t, f) => t + f.called, 0))}</div></div>
        <div class="kpi"><div class="label">Distributed to LPs</div><div class="value">${fmt.money(d.funds.reduce((t, f) => t + f.distributed, 0))}</div></div>
        <div class="kpi"><div class="label">Invested (cost)</div><div class="value">${fmt.money(p.invested)}</div><div class="sub">${p.active} active / ${p.positions} positions</div></div>
        <div class="kpi"><div class="label">Portfolio value</div><div class="value">${fmt.money(p.total_value)}</div><div class="sub">${fmt.money(p.realized)} realized</div></div>
        <div class="kpi"><div class="label">Gross MOIC</div><div class="value">${fmt.multiple(p.gross_moic)}</div></div>
        <div class="kpi"><div class="label">Gross IRR</div><div class="value">${fmt.pct(p.gross_irr)}</div></div>
      </div>
      <div class="grid-2">
        <div class="panel"><h2>Fundraising progress</h2>${raising.length ? raising.map((f) => `
          <div style="margin-bottom:14px"><div style="display:flex;justify-content:space-between"><b>${esc(f.name)}</b>
            <span>${fmt.money(f.committed)} of ${fmt.money(f.target_size) || "no target"}</span></div>
            <div class="progress"><div style="width:${Math.min(100, (f.pct_of_target || 0) * 100)}%"></div></div>
            <div class="muted">${fmt.pct(f.pct_of_target, 0)} closed · ${fmt.money(f.soft_circled) || "$0"} soft circled · ${f.closed_lps} LPs closed</div>
          </div>`).join("") : '<div class="muted">No funds with status "Raising".</div>'}</div>
        <div class="panel"><h2>Deal pipeline</h2>${Object.entries(d.deal_pipeline).map(([s, n]) => `
          <div style="display:flex;justify-content:space-between;padding:3px 0"><span>${esc(s)}</span><b>${n}</b></div>`).join("")}</div>
      </div>
      <div class="panel"><h2>Next steps due in the next 14 days or overdue</h2><div data-tasks></div></div>
      <div class="panel"><h2>Portfolio rounds in progress</h2><div data-rounds></div></div>
      <div class="panel"><h2>Funds</h2><div data-funds></div></div>`;
    renderGrid($("[data-rounds]", el), {
      rows: live, search: false,
      cols: [
        { name: "round", label: "Round", value: (r) => esc(roundLabel(r)) },
        { name: "status", label: "Status", value: (r) => `<span class="pill">${esc(r.status)}</span>` },
        { name: "target_size", label: "Target", num: true, value: (r) => fmt.money(r.target_size) },
        { name: "committed", label: "Committed", num: true, value: (r) => fmt.money(totals(r.id).total) },
        { name: "pct", label: "% of target", num: true, value: (r) => fmt.pct(r.target_size ? totals(r.id).total / r.target_size : null, 0) },
        { name: "pro_rata_decision", label: "Our pro-rata", value: (r) => esc([fmt.money(r.pro_rata_amount), r.pro_rata_decision].filter(Boolean).join(" · ")) },
      ],
      onOpen: (r) => { pref("round", r.id); go("round-board"); },
    });
    renderGrid($("[data-tasks]", el), {
      rows: d.tasks.map((t, i) => ({ ...t, _id: t.id, id: `${t.entity}-${t.id}` })), search: false,
      cols: [
        { name: "next_step_date", label: "Due", value: (r) => `<span class="${dueClass(r.next_step_date)}">${esc(r.next_step_date)}</span>` },
        { name: "entity", label: "Type", value: (r) => (r.entity === "commitments" ? "LP" : "Deal") },
        { name: "title", label: "Record", value: (r) => esc(r.title) },
        { name: "stage", label: "Stage", value: (r) => `<span class="pill">${esc(r.stage)}</span>` },
        { name: "next_step", label: "Next step", value: (r) => esc(r.next_step) },
        { name: "owner", label: "Owner", value: (r) => esc(r.owner) },
      ],
      onOpen: async (r) => editRecord(r.entity, await api("GET", `/api/${r.entity}/${r._id}`), () => render()),
    });
    renderGrid($("[data-funds]", el), {
      rows: d.funds, search: false,
      cols: [
        { name: "name", label: "Fund", value: (r) => esc(r.name) },
        { name: "status", label: "Status", value: (r) => `<span class="pill">${esc(r.status)}</span>` },
        { name: "target_size", label: "Target", num: true, value: (r) => fmt.money(r.target_size) },
        { name: "committed", label: "Committed", num: true, value: (r) => fmt.money(r.committed) },
        { name: "called", label: "Called", num: true, value: (r) => fmt.money(r.called) },
        { name: "uncalled", label: "Uncalled", num: true, value: (r) => fmt.money(r.uncalled) },
        { name: "distributed", label: "Distributed", num: true, value: (r) => fmt.money(r.distributed) },
        { name: "dpi", label: "DPI", num: true, value: (r) => fmt.multiple(r.dpi) },
        { name: "inv", label: "Invested", num: true, value: (r) => fmt.money(r.portfolio.invested) },
        { name: "moic", label: "Gross MOIC", num: true, value: (r) => fmt.multiple(r.portfolio.gross_moic) },
        { name: "irr", label: "Gross IRR", num: true, value: (r) => fmt.pct(r.portfolio.gross_irr) },
      ],
    });
  },
};

VIEWS["lp-board"] = {
  title: "LP pipeline",
  async render(el, actions) {
    const mode = pref("lpMode") ?? "board";
    const fund = pref("fund") ?? "";
    actions.append(
      fundFilter(() => render()),
      segmented([["board", "Board"], ["grid", "Grid"]], mode, (m) => { pref("lpMode", m); render(); }),
      exportButton("commitments"),
      buttonIf(canEditSomeFund(), "Import", () => importDialog("commitments", render)),
      buttonIf(canEditSomeFund(), "+ Add LP to pipeline", () => editRecord("commitments", null, render, { fund_id: fund }), "primary"),
    );
    if (!Object.keys(S.look.funds).length) {
      el.innerHTML = `<div class="panel empty">Create a fund first (Funds &amp; capital), then add LPs to its pipeline.</div>`;
      return;
    }
    const rows = await api("GET", `/api/commitments${fund ? `?fund_id=${fund}` : ""}`);
    const amount = (r) => (r.stage === "Closed" ? r.committed_amount : r.target_amount);
    if (mode === "board") {
      renderBoard(el, {
        entity: "commitments", stageField: "stage", stages: S.meta.lp_stages, rows, amount,
        onOpen: (r) => editRecord("commitments", r, render),
        onMove: (r, stage) => {
          if (stage !== "Closed" || r.committed_amount) return {};
          const v = prompt(`Committed amount for ${refLabel("lps", r.lp_id)}, in $000s (5250 = $5.25M)?`,
            toThousands(r.target_amount));
          return v === null ? false : { committed_amount: fromThousands(v), close_date: r.close_date ?? today() };
        },
        card: (r) => `<div class="title">${esc(refLabel("lps", r.lp_id))}</div>
          <div class="meta">${esc(refLabel("funds", r.fund_id))}${r.owner ? ` · ${esc(r.owner)}` : ""}</div>
          <div class="amt">${r.stage === "Closed" ? fmt.money(r.committed_amount) + " committed" : fmt.money(r.target_amount) ? fmt.money(r.target_amount) + " target" : ""}</div>
          ${r.next_step ? `<div class="meta ${dueClass(r.next_step_date)}">${esc(r.next_step)}${r.next_step_date ? ` · ${esc(r.next_step_date)}` : ""}</div>` : ""}`,
      });
    } else {
      renderGrid(el, { persist: S.view,
        entity: "commitments", rows,
        cols: ["lp_id", "fund_id", "stage", "target_amount", "committed_amount", "close_date", "owner", "next_step", "next_step_date"]
          .map((n) => (["target_amount", "committed_amount"].includes(n)
            ? { name: n, total: (l) => fmt.money(l.reduce((t, r) => t + (r[n] || 0), 0)) } : n)),
        onOpen: (r) => editRecord("commitments", r, render),
      });
    }
  },
};

VIEWS.lps = {
  title: "LPs",
  async render(el, actions) {
    actions.append(exportButton("lps"), buttonIf(canCreateShared(), "Import", () => importDialog("lps", render)),
      buttonIf(canCreateShared(), "+ New LP", () => editRecord("lps", null, render), "primary"));
    const [rows, commits] = await Promise.all([api("GET", "/api/lps"), api("GET", "/api/commitments")]);
    const closed = {};
    for (const c of commits) if (c.stage === "Closed") closed[c.lp_id] = (closed[c.lp_id] || 0) + (c.committed_amount || 0);
    renderGrid(el, { persist: S.view,
      entity: "lps", rows,
      cols: ["name", "lp_type", "contact_name", "email", "phone", "location", "relationship_owner", "tags",
        { name: "total_committed", label: "Total committed", num: true, editable: false,
          value: (r) => fmt.money(closed[r.id]), sortValue: (r) => closed[r.id] || 0,
          total: () => fmt.money(Object.values(closed).reduce((a, b) => a + b, 0)) }],
      onOpen: (r) => editRecord("lps", r, render),
      onChanged: loadLookups,
    });
  },
};

VIEWS.funds = {
  title: "Funds & capital",
  noSavedViews: true,
  async render(el, actions) {
    actions.append(
      exportButton("capital_events", "Export capital calls"),
      buttonIf(isAdmin(), "+ New fund", () => editRecord("funds", null, render)),
      buttonIf(canEditSomeFund(), "+ Capital call / distribution", () => newCapitalEvent(render), "primary"),
    );
    const fund = pref("fund") ?? "";
    const q = fund ? `?fund_id=${fund}` : "";
    const [summary, positions, events] = await Promise.all([
      api("GET", "/api/funds/summary"), api("GET", `/api/lp-positions${q}`), api("GET", `/api/capital-events${q}`)]);
    el.innerHTML = `<div class="panel"><h2>Funds</h2><div data-f></div></div>
      <div class="panel"><h2 style="display:flex;justify-content:space-between;align-items:center">LP capital accounts <span data-ff></span></h2>
        <div class="muted" style="margin-bottom:8px">Closed commitments only. Calls and distributions are allocated pro rata to commitment size.</div><div data-p></div></div>
      <div class="panel"><h2>Capital calls &amp; distributions</h2><div data-e></div></div>`;
    $("[data-ff]", el).append(fundFilter(() => render()));
    const sumCol = (name, label, f = fmt.money) => ({ name, label, num: true, value: (r) => f(r[name]),
      total: (l) => f(l.reduce((t, r) => t + (r[name] || 0), 0)) });
    renderGrid($("[data-f]", el), {
      entity: "funds", rows: summary, search: false,
      cols: ["name", "vintage", "status", "target_size", "hard_cap",
        { name: "committed", label: "Committed", num: true, editable: false, value: (r) => fmt.money(r.committed) },
        { name: "pct_of_target", label: "% of target", num: true, editable: false, value: (r) => fmt.pct(r.pct_of_target, 0) },
        { name: "called", label: "Called", num: true, editable: false, value: (r) => fmt.money(r.called) },
        { name: "uncalled", label: "Uncalled", num: true, editable: false, value: (r) => fmt.money(r.uncalled) },
        { name: "distributed", label: "Distributed", num: true, editable: false, value: (r) => fmt.money(r.distributed) },
        { name: "dpi", label: "DPI", num: true, editable: false, value: (r) => fmt.multiple(r.dpi) },
        { name: "dry_powder", label: "Called, not invested", num: true, editable: false, value: (r) => fmt.money(r.dry_powder) },
        "first_close_date", "final_close_date"],
      onOpen: (r) => editRecord("funds", r, render),
      onChanged: loadLookups,
    });
    renderGrid($("[data-p]", el), {
      rows: positions.map((r) => ({ ...r, id: r.commitment_id })),
      cols: [
        { name: "lp", label: "LP", value: (r) => esc(r.lp) },
        { name: "fund", label: "Fund", value: (r) => esc(r.fund) },
        sumCol("committed", "Committed"), sumCol("called", "Paid-in (called)"),
        { name: "pct_called", label: "% called", num: true, value: (r) => fmt.pct(r.pct_called, 0) },
        sumCol("unfunded", "Unfunded"), sumCol("distributed", "Distributed"),
        { name: "dpi", label: "DPI", num: true, value: (r) => fmt.multiple(r.dpi) },
      ],
    });
    renderGrid($("[data-e]", el), {
      rows: events, search: false,
      cols: [
        { name: "event_date", label: "Date", value: (r) => esc(r.event_date) },
        { name: "fund", label: "Fund", value: (r) => esc(r.fund) },
        { name: "event_type", label: "Type", value: (r) => `<span class="pill">${esc(r.event_type)}</span>` },
        { name: "total_amount", label: "Amount", num: true, value: (r) => fmt.money(r.total_amount) },
        { name: "due_date", label: "Due", value: (r) => esc(r.due_date) },
        { name: "purpose", label: "Purpose", value: (r) => esc(r.purpose) },
        { name: "paid", label: "LPs paid", value: (r) => `${r.paid_count} / ${r.lp_count}` },
      ],
      onOpen: (r) => showCapitalEvent(r, render),
    });
  },
};

function newCapitalEvent(after) {
  const fields = S.meta.capital_event_fields;
  const d = openDrawer(`<h2>New capital call or distribution</h2>
    <div class="muted">The amount is split across the fund's closed commitments, pro rata to committed capital.</div>
    ${formHtml(fields, { fund_id: pref("fund") ?? "", event_type: "Capital call", event_date: today() })}
    <div class="drawer-actions"><div><button class="btn primary" data-save>Create</button>
      <button class="btn" data-cancel>Close</button></div></div>`);
  $("[data-cancel]", d).onclick = closeDrawer;
  $("[data-save]", d).onclick = () => guard(async () => {
    const ev = await api("POST", "/api/capital-events", formValues($("form", d), fields));
    toast("Created");
    after?.();
    showCapitalEvent({ ...ev, fund: refLabel("funds", ev.fund_id) }, after);
  });
}

async function showCapitalEvent(ev, after) {
  const allocs = await api("GET", `/api/capital-events/${ev.id}/allocations`);
  const isCall = ev.event_type === "Capital call";
  const d = openDrawer(`<h2>${esc(ev.event_type)} · ${esc(ev.fund ?? refLabel("funds", ev.fund_id))}</h2>
    <div class="muted">${esc(ev.event_date)}${ev.due_date ? ` · due ${esc(ev.due_date)}` : ""} · ${fmt.money(ev.total_amount)}${ev.purpose ? ` · ${esc(ev.purpose)}` : ""}</div>
    <div class="section-title">Allocation by LP</div><div data-a></div>
    <div class="drawer-actions" style="margin-top:16px"><button class="btn" data-cancel>Close</button>
      ${ev._can_edit ? '<button class="btn danger" data-del>Delete event</button>' : ""}</div>`);
  renderGrid($("[data-a]", d), {
    rows: allocs, search: false,
    cols: [
      { name: "lp", label: "LP", value: (r) => esc(r.lp) },
      { name: "committed", label: "Committed", num: true, value: (r) => fmt.money(r.committed) },
      { name: "amount", label: "Amount", num: true, value: (r) => fmt.money(r.amount),
        total: (l) => fmt.money(l.reduce((t, r) => t + r.amount, 0)) },
      { name: "paid", label: isCall ? "Received" : "Paid out",
        value: (r) => `<input type="checkbox" data-paid="${r.id}" ${r.paid ? "checked" : ""} ${ev._can_edit ? "" : "disabled"}>` },
    ],
  });
  d.querySelectorAll("[data-paid]").forEach((cb) => (cb.onchange = () => guard(async () => {
    await api("PATCH", `/api/allocations/${cb.dataset.paid}`, { paid: cb.checked });
    after?.();
  })));
  $("[data-cancel]", d).onclick = closeDrawer;
  const del = $("[data-del]", d);
  if (del) del.onclick = () => guard(async () => {
    if (!confirm("Delete this event and its LP allocations?")) return;
    await api("DELETE", `/api/capital-events/${ev.id}`);
    closeDrawer();
    after?.();
  });
  await documentsSection(d, "capital_events", ev);
  await changeHistory(d, "capital_events", ev.id);
}

VIEWS["deal-board"] = {
  title: "Deal pipeline",
  async render(el, actions) {
    const mode = pref("dealMode") ?? "board";
    actions.append(
      segmented([["board", "Board"], ["grid", "Grid"]], mode, (m) => { pref("dealMode", m); render(); }),
      exportButton("companies"),
      buttonIf(canCreateShared(), "Import", () => importDialog("companies", render)),
      buttonIf(canCreateShared(), "+ New deal", () => editRecord("companies", null, render), "primary"),
    );
    const rows = await api("GET", "/api/companies");
    if (mode === "board") {
      renderBoard(el, {
        entity: "companies", stageField: "deal_stage", stages: S.meta.deal_stages, rows,
        amount: (r) => r.proposed_check,
        onOpen: (r) => editRecord("companies", r, render),
        onMove: (r, stage) => {
          if (stage !== "Passed" || r.pass_reason) return {};
          const v = prompt(`Why are you passing on ${r.name}?`, "");
          return v === null ? false : { pass_reason: v };
        },
        card: (r) => `<div class="title">${esc(r.name)}</div>
          <div class="meta">${[r.sector, r.company_stage, r.deal_owner].filter(Boolean).map(esc).join(" · ")}</div>
          ${r.proposed_check ? `<div class="amt">${fmt.money(r.proposed_check)} check${r.pre_money ? ` @ ${fmt.money(r.pre_money)} pre` : ""}</div>` : ""}
          ${r.next_step ? `<div class="meta ${dueClass(r.next_step_date)}">${esc(r.next_step)}${r.next_step_date ? ` · ${esc(r.next_step_date)}` : ""}</div>` : ""}`,
      });
    } else {
      renderGrid(el, { persist: S.view,
        entity: "companies", rows,
        cols: ["name", "deal_stage", "sector", "company_stage", "deal_owner", "fund_id", "round_size", "proposed_check",
          "pre_money", "source", "next_step", "next_step_date", "pass_reason"],
        onOpen: (r) => editRecord("companies", r, render),
        onChanged: loadLookups,
      });
    }
  },
};

/** Committed money per round: outside investors at "Committed" plus our linked checks. */
function roundTotals(pipe, investments) {
  const outside = {}, ours = {};
  for (const x of pipe) if (x.stage === "Committed") outside[x.round_id] = (outside[x.round_id] || 0) + (x.amount || 0);
  for (const x of investments) if (x.round_id) ours[x.round_id] = (ours[x.round_id] || 0) + x.amount;
  return (id) => ({ outside: outside[id] || 0, ours: ours[id] || 0, total: (outside[id] || 0) + (ours[id] || 0) });
}

const ROUND_STATUS_ORDER = ["Raising", "Planning", "Closed", "Abandoned"];

VIEWS.rounds = {
  title: "Rounds",
  async render(el, actions) {
    actions.append(exportButton("rounds"), buttonIf(canEditSomeFund(), "Import", () => importDialog("rounds", render)),
      buttonIf(canEditSomeFund(), "+ New round", () => editRecord("rounds", null, render), "primary"));
    const [rounds, pipe, inv] = await Promise.all(["rounds", "round_investors", "investments"].map((e) => api("GET", `/api/${e}`)));
    const totals = roundTotals(pipe, inv);
    el.onclick = (e) => {
      const x = e.target.closest("[data-pipe]");
      if (x) { pref("round", Number(x.dataset.pipe)); go("round-board"); }
    };
    renderGrid(el, { persist: S.view,
      entity: "rounds", rows: rounds,
      cols: ["company_id", "round_name", "status", "target_size",
        { name: "committed", label: "Committed (incl. ours)", num: true, editable: false,
          value: (r) => fmt.money(totals(r.id).total), sortValue: (r) => totals(r.id).total },
        { name: "pct", label: "% of target", num: true, editable: false,
          value: (r) => fmt.pct(r.target_size ? totals(r.id).total / r.target_size : null, 0) },
        "final_size", "pre_money", "close_date", "lead_investor_id", "pro_rata_amount", "pro_rata_decision",
        { name: "pipeline", label: "", editable: false, value: (r) => `<span class="link" data-pipe="${r.id}">Pipeline</span>` }],
      onOpen: (r) => editRecord("rounds", r, render),
      onChanged: loadLookups,
    });
  },
};

VIEWS["round-board"] = {
  title: "Round pipeline",
  async render(el, actions) {
    const rounds = (await api("GET", "/api/rounds"))
      .sort((a, b) => ROUND_STATUS_ORDER.indexOf(a.status) - ROUND_STATUS_ORDER.indexOf(b.status));
    if (!rounds.length) {
      el.innerHTML = `<div class="panel empty">No rounds yet. Open a portfolio company and add one on its Rounds tab, or use Rounds &gt; + New round.</div>`;
      return;
    }
    const rnd = rounds.find((r) => r.id === Number(pref("round"))) ?? rounds[0];
    pref("round", rnd.id);
    const sel = document.createElement("select");
    sel.innerHTML = rounds.map((r) => `<option value="${r.id}" ${r.id === rnd.id ? "selected" : ""}>${esc(roundLabel(r))} (${esc(r.status)})</option>`).join("");
    sel.onchange = () => { pref("round", Number(sel.value)); render(); };
    const mode = pref("roundMode") ?? "board";
    actions.append(sel,
      segmented([["board", "Board"], ["grid", "Grid"]], mode, (m) => { pref("roundMode", m); render(); }),
      button("Round details", () => editRecord("rounds", rnd, render)),
      buttonIf(rnd._can_edit, "+ Add investor", () => editRecord("round_investors", null, render, { round_id: rnd.id }), "primary"));

    const [rows, inv] = await Promise.all([api("GET", `/api/round_investors?round_id=${rnd.id}`), api("GET", `/api/investments?round_id=${rnd.id}`)]);
    const t = roundTotals(rows, inv)(rnd.id);
    el.innerHTML = `<div class="kpis">
        <div class="kpi"><div class="label">Target</div><div class="value">${fmt.money(rnd.target_size) || "n/a"}</div>
          <div class="sub">${esc(rnd.round_name)} · ${esc(rnd.status)}${rnd.pre_money ? ` · ${fmt.money(rnd.pre_money)} pre` : ""}</div></div>
        <div class="kpi"><div class="label">Committed</div><div class="value">${fmt.money(t.total)}</div>
          <div class="sub">${fmt.pct(rnd.target_size ? t.total / rnd.target_size : null, 0)} of target</div></div>
        <div class="kpi"><div class="label">From outside investors</div><div class="value">${fmt.money(t.outside)}</div>
          <div class="sub">Lead: ${esc(refLabel("investors", rnd.lead_investor_id)
            || rows.filter((r) => r.participation === "Lead").map((r) => refLabel("investors", r.investor_id)).join(", ")
            || "not set")}</div></div>
        <div class="kpi"><div class="label">Our pro-rata right</div><div class="value">${fmt.money(rnd.pro_rata_amount) || "n/a"}</div>
          <div class="sub">${esc(rnd.pro_rata_decision || "No decision")} · ${fmt.money(t.ours)} invested in this round</div></div>
      </div><div data-board></div>`;
    const target = $("[data-board]", el);
    if (mode === "board") {
      renderBoard(target, {
        entity: "round_investors", stageField: "stage", stages: fieldOf("round_investors", "stage").options, rows,
        amount: (r) => r.amount,
        onOpen: (r) => editRecord("round_investors", r, render),
        onMove: (r, stage) => {
          if (stage !== "Committed" || r.amount) return {};
          const v = prompt(`Amount committed by ${refLabel("investors", r.investor_id)}, in $000s (5250 = $5.25M)?`, "");
          return v === null ? false : { amount: fromThousands(v) };
        },
        card: (r) => `<div class="title">${esc(refLabel("investors", r.investor_id))}</div>
          <div class="meta">${[r.participation, r.intro_by ? `intro: ${r.intro_by}` : "", r.owner].filter(Boolean).map(esc).join(" · ")}</div>
          ${r.amount ? `<div class="amt">${fmt.money(r.amount)}</div>` : ""}
          ${r.next_step ? `<div class="meta ${dueClass(r.next_step_date)}">${esc(r.next_step)}${r.next_step_date ? ` · ${esc(r.next_step_date)}` : ""}</div>` : ""}`,
      });
    } else {
      renderGrid(target, { persist: S.view,
        entity: "round_investors", rows,
        cols: ["investor_id", "stage", "participation",
          { name: "amount", total: (l) => fmt.money(l.reduce((s, r) => s + (r.amount || 0), 0)) },
          "intro_by", "owner", "next_step", "next_step_date"],
        onOpen: (r) => editRecord("round_investors", r, render),
      });
    }
  },
};

VIEWS.investors = {
  title: "Investors",
  async render(el, actions) {
    actions.append(exportButton("investors"), buttonIf(canCreateShared(), "Import", () => importDialog("investors", render)),
      buttonIf(canCreateShared(), "+ New investor", () => editRecord("investors", null, render), "primary"));
    const [rows, pipe] = await Promise.all([api("GET", "/api/investors"), api("GET", "/api/round_investors")]);
    const active = {}, committed = {};
    for (const x of pipe) {
      if (x.stage === "Committed") committed[x.investor_id] = (committed[x.investor_id] || 0) + 1;
      else if (x.stage !== "Passed") active[x.investor_id] = (active[x.investor_id] || 0) + 1;
    }
    renderGrid(el, { persist: S.view,
      entity: "investors", rows,
      cols: ["name", "investor_type", "contact_name", "email", "location", "stage_focus", "check_size_min", "check_size_max",
        { name: "active", label: "Active in rounds", num: true, editable: false, value: (r) => active[r.id] || "", sortValue: (r) => active[r.id] || 0 },
        { name: "committed", label: "Committed in", num: true, editable: false, value: (r) => committed[r.id] || "", sortValue: (r) => committed[r.id] || 0 }],
      onOpen: (r) => editRecord("investors", r, render),
      onChanged: loadLookups,
    });
  },
};

VIEWS.tasks = {
  title: "Tasks",
  async render(el, actions) {
    const tab = pref("taskTab") ?? "mine";
    actions.append(
      segmented([["mine", "My open tasks"], ["assigned", "I assigned"], ["open", "All open"], ["done", "Done"]], tab,
        (t) => { pref("taskTab", t); render(); }),
      button("+ New task", () => editRecord("tasks", null, render, { assignee_id: S.me.id }), "primary"));
    const [all, commitments] = await Promise.all([api("GET", "/api/tasks"), api("GET", "/api/commitments")]);
    S.cache = { commitments: Object.fromEntries(commitments.map((c) => [c.id, c])) };
    const me = S.me.id;
    const rows = all.filter((t) => ({
      mine: t.status === "Open" && t.assignee_id === me,
      assigned: t.status === "Open" && t.created_by === me && t.assignee_id !== me,
      open: t.status === "Open",
      done: t.status === "Done",
    })[tab]);
    el.onchange = (e) => {
      const id = e.target.closest("[data-done]")?.dataset.done;
      if (id) guard(async () => {
        await api("PATCH", `/api/tasks/${id}`, { status: e.target.checked ? "Done" : "Open" });
        await loadLookups();
        render();
      });
    };
    el.onclick = (e) => {
      const t = e.target.closest("[data-rec]");
      if (t) { const [en, id] = t.dataset.rec.split(":"); openRecord(en, id, render); }
    };
    renderGrid(el, { persist: S.view,
      entity: "tasks", rows,
      cols: [
        { name: "done", label: "", editable: false, value: (r) => `<input type="checkbox" data-done="${r.id}" ${r.status === "Done" ? "checked" : ""} ${r._can_edit ? "" : "disabled"}>` },
        "title",
        { name: "due_date", editable: true, value: (r) => `<span class="${r.status === "Open" ? dueClass(r.due_date) : ""}">${esc(r.due_date ?? "")}</span>` },
        "priority", "assignee_id",
        { name: "related", label: "Related to", editable: false, sortValue: (r) => recordLabel(r.entity, r.entity_id),
          value: (r) => (r.entity ? `<span class="link" data-rec="${r.entity}:${r.entity_id}">${esc(recordLabel(r.entity, r.entity_id))}</span>` : "") },
        { name: "created_by", label: "Created by", editable: false, value: (r) => esc(refLabel("users", r.created_by)) },
      ],
      onOpen: (r) => editRecord("tasks", r, render),
      onChanged: loadLookups,
    });
  },
};

VIEWS.people = {
  title: "People",
  async render(el, actions) {
    actions.append(exportButton("contacts"), buttonIf(canCreateShared(), "Import", () => importDialog("contacts", render)),
      buttonIf(canCreateShared(), "+ New person", () => editRecord("contacts", null, render), "primary"));
    const [rows, links] = await Promise.all([api("GET", "/api/contacts"), api("GET", "/api/contact_links")]);
    const linked = {};
    for (const l of links) (linked[l.contact_id] ??= []).push(refLabel(l.entity, l.entity_id) + (l.role ? ` (${l.role})` : ""));
    renderGrid(el, { persist: S.view,
      entity: "contacts", rows,
      cols: ["name", "title", "email", "phone", "location",
        { name: "linked", label: "Linked to", editable: false, value: (r) => esc((linked[r.id] || []).join("; ")),
          sortValue: (r) => (linked[r.id] || []).join() }],
      onOpen: (r) => editRecord("contacts", r, render),
      onChanged: loadLookups,
    });
  },
};

VIEWS.analytics = {
  title: "Pipeline analytics",
  noSavedViews: true,
  async render(el, actions) {
    const kind = pref("anKind") ?? "lp";
    actions.append(segmented([["lp", "LP pipeline"], ["deal", "Deal pipeline"]], kind, (k) => { pref("anKind", k); render(); }),
      fundFilter(() => render()));
    const fund = pref("fund") ?? "";
    const a = await api("GET", `/api/analytics/pipeline?kind=${kind}${fund ? `&fund_id=${fund}` : ""}`);
    const top = Math.max(1, ...a.stages.map((s) => s.reached));
    const won = kind === "lp" ? "Closed" : "Invested", lost = kind === "lp" ? "Declined" : "Passed";
    el.innerHTML = `<div class="kpis">
        <div class="kpi"><div class="label">${kind === "lp" ? "LP pipeline entries" : "Deals"}</div><div class="value">${a.total}</div></div>
        <div class="kpi"><div class="label">${won}</div><div class="value">${a.won}</div></div>
        <div class="kpi"><div class="label">${lost}</div><div class="value">${a.lost}</div></div>
        <div class="kpi"><div class="label">Win rate</div><div class="value">${fmt.pct(a.win_rate, 0)}</div><div class="sub">${won} ÷ (${won} + ${lost})</div></div>
        <div class="kpi"><div class="label">Median days to ${won.toLowerCase()}</div><div class="value">${a.median_days_to_win ?? "n/a"}</div></div>
      </div>
      <div class="panel"><h2>Funnel</h2>${a.stages.map((s) => `
        <div class="funnel-row"><div class="funnel-label">${esc(s.stage)}</div>
          <div class="funnel-bar"><div style="width:${(s.reached / top) * 100}%"></div></div>
          <div class="funnel-num">${s.reached}</div>
          <div class="funnel-conv muted">${s.conversion_to_next == null ? "" : `${fmt.pct(s.conversion_to_next, 0)} move on`}</div></div>`).join("")}
        <div class="muted" style="margin-top:8px">"Reached" counts everything that ever got to a stage or further, rebuilt from the change history.</div></div>
      <div class="panel"><h2>Time in stage</h2><div data-g></div></div>
      ${a.pass_reasons ? `<div class="panel"><h2>Why we passed</h2>${a.pass_reasons.map(([r, n]) =>
        `<div style="display:flex;justify-content:space-between;padding:3px 0"><span>${esc(r)}</span><b>${n}</b></div>`).join("") || '<div class="muted">No passed deals yet.</div>'}</div>` : ""}`;
    renderGrid($("[data-g]", el), {
      rows: a.stages.map((s, i) => ({ ...s, id: i })), search: false,
      cols: [
        { name: "stage", label: "Stage", value: (r) => esc(r.stage) },
        { name: "median_days", label: "Median days in stage", num: true, value: (r) => r.median_days ?? "" },
        { name: "completed_stays", label: "Moved on (count)", num: true, value: (r) => r.completed_stays },
        { name: "current", label: "There now", num: true, value: (r) => r.current },
        { name: "current_median_age", label: "Median days there now", num: true,
          value: (r) => (r.current_median_age == null ? "" : `<span class="${r.current_median_age > 60 ? "warn" : ""}">${r.current_median_age}</span>`) },
        { name: "lost_here", label: `${lost} from here`, num: true, value: (r) => r.lost_here || "" },
      ],
    });
  },
};

VIEWS["custom-fields"] = {
  title: "Custom fields",
  noSavedViews: true,
  async render(el, actions) {
    const defs = await api("GET", "/api/custom-fields");
    const kinds = { lps: "LPs", companies: "Companies", investors: "Investors", contacts: "People",
      commitments: "LP pipeline entries", funds: "Funds", rounds: "Rounds" };
    actions.append(button("+ New field", () => newCustomField(kinds, render), "primary"));
    el.innerHTML = `<div class="panel muted">Add your own fields to records. They appear in forms, as list columns,
      in filters, saved views, CSV import/export and the change history. Removing a field hides it; its values are kept
      and come back if you add a field with the same name again.</div>
      ${Object.entries(kinds).map(([k, label]) => `<div class="panel"><h2>${label}</h2>${defs.filter((d) => d.entity === k).map((d) => `
        <div class="activity"><b>${esc(d.label)}</b> <span class="muted">${esc(S.meta.custom_types[d.field_type])}${d.options ? `: ${esc(d.options.join(", "))}` : ""}</span>
          <span style="float:right"><span class="link" data-rename="${d.id}">Rename</span>${d.field_type === "select" ? ` · <span class="link" data-choices="${d.id}">Edit choices</span>` : ""}
          · <span class="link" data-remove="${d.id}">Remove</span></span></div>`).join("") || '<div class="muted">No custom fields.</div>'}</div>`).join("")}`;
    el.onclick = (e) => {
      const t = e.target.closest("[data-rename],[data-choices],[data-remove]");
      if (!t) return;
      const def = defs.find((x) => String(x.id) === (t.dataset.rename ?? t.dataset.choices ?? t.dataset.remove));
      const done = async (body) => { await api("PATCH", `/api/custom-fields/${def.id}`, body); S.meta = await api("GET", "/api/meta"); render(); };
      if (t.dataset.rename) {
        const label = prompt("New name:", def.label);
        if (label) guard(() => done({ label }));
      } else if (t.dataset.choices) {
        const opts = prompt("Choices, separated by commas (existing values are kept even if removed from the list):", def.options.join(", "));
        if (opts) guard(() => done({ options: opts }));
      } else if (confirm(`Remove "${def.label}"? It disappears from the app; its values are kept.`)) {
        guard(async () => { await api("DELETE", `/api/custom-fields/${def.id}`); S.meta = await api("GET", "/api/meta"); render(); });
      }
    };
  },
};

function newCustomField(kinds, after) {
  const d = openDrawer(`<h2>New custom field</h2>
    <form class="form">
      <label>Add to<select name="entity">${Object.entries(kinds).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
      <label>Field name<input type="text" name="label" placeholder="e.g. Tier, AUM, Board seat"></label>
      <label>Type<select name="field_type">${Object.entries(S.meta.custom_types).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
      <label data-opts hidden>Choices (comma separated)<input type="text" name="options" placeholder="e.g. A, B, C"></label>
    </form>
    <div class="drawer-actions"><div><button class="btn primary" data-save>Create field</button> <button class="btn" data-cancel>Cancel</button></div></div>`);
  const f = $("form", d);
  f.field_type.onchange = () => ($("[data-opts]", d).hidden = f.field_type.value !== "select");
  $("[data-cancel]", d).onclick = closeDrawer;
  $("[data-save]", d).onclick = () => guard(async () => {
    await api("POST", "/api/custom-fields", { entity: f.entity.value, label: f.elements.label.value,
      field_type: f.field_type.value, options: f.field_type.value === "select" ? f.options.value : undefined });
    S.meta = await api("GET", "/api/meta");
    closeDrawer();
    toast("Field added");
    after();
  });
}

VIEWS.companies = {
  title: "All companies",
  async render(el, actions) {
    actions.append(exportButton("companies"), buttonIf(canCreateShared(), "Import", () => importDialog("companies", render)),
      buttonIf(canEditSomeFund(), "Import investments", () => importDialog("investments", render)),
      buttonIf(canCreateShared(), "+ New company", () => editRecord("companies", null, render), "primary"));
    renderGrid(el, { persist: S.view,
      entity: "companies", rows: await api("GET", "/api/companies"),
      cols: ["name", "deal_stage", "sector", "company_stage", "location", "founders", "website", "source", "deal_owner", "fund_id"],
      onOpen: (r) => editRecord("companies", r, render),
      onChanged: loadLookups,
    });
  },
};

VIEWS.portfolio = {
  title: "Portfolio",
  async render(el, actions) {
    actions.append(fundFilter(() => render()));
    const fund = pref("fund") ?? "";
    const [{ holdings, totals: t }, kpis] = await Promise.all([api("GET", `/api/portfolio${fund ? `?fund_id=${fund}` : ""}`),
      api("GET", "/api/company_metrics")]);
    const latest = {};
    for (const k of kpis) if (!latest[k.company_id] || k.period_end > latest[k.company_id].period_end) latest[k.company_id] = k;
    el.innerHTML = `<div class="kpis">
        <div class="kpi"><div class="label">Invested</div><div class="value">${fmt.money(t.invested)}</div><div class="sub">${t.active} active / ${t.positions} positions</div></div>
        <div class="kpi"><div class="label">Realized</div><div class="value">${fmt.money(t.realized)}</div></div>
        <div class="kpi"><div class="label">Unrealized</div><div class="value">${fmt.money(t.unrealized)}</div></div>
        <div class="kpi"><div class="label">Gross MOIC</div><div class="value">${fmt.multiple(t.gross_moic)}</div></div>
        <div class="kpi"><div class="label">Gross IRR</div><div class="value">${fmt.pct(t.gross_irr)}</div></div>
      </div>
      <div class="muted" style="margin-bottom:10px">Gross of fees and carry. Positions with no valuation mark are held at cost.
        Add investments, marks and exits from a company's record.</div><div data-g></div>`;
    const m = (name, label, f = fmt.money, total = true) => ({ name, label, num: true, value: (r) => f(r[name]),
      total: total ? (l) => f(l.reduce((s, r) => s + (r[name] || 0), 0)) : undefined });
    renderGrid($("[data-g]", el), { persist: S.view,
      rows: holdings.map((h) => ({ ...h, id: `${h.company_id}-${h.fund_id}` })),
      cols: [
        { name: "company", label: "Company", value: (r) => esc(r.company) },
        { name: "fund", label: "Fund", value: (r) => esc(r.fund) },
        { name: "sector", label: "Sector", value: (r) => esc(r.sector) },
        { name: "status", label: "Status", value: (r) => `<span class="pill">${esc(r.status)}</span>` },
        { name: "first_investment", label: "First check", value: (r) => esc(r.first_investment) },
        m("invested", "Invested"), m("realized", "Realized"), m("unrealized", "Unrealized"), m("total_value", "Total value"),
        { name: "moic", label: "MOIC", num: true, value: (r) => `<span class="${r.moic >= 1 ? "good" : r.moic != null ? "bad" : ""}">${fmt.multiple(r.moic)}</span>` },
        { name: "irr", label: "IRR", num: true, value: (r) => fmt.pct(r.irr) },
        { name: "valuation_basis", label: "Valuation basis", value: (r) => esc(r.valuation_basis) },
        { name: "arr", label: "Latest ARR", num: true, value: (r) => fmt.money(latest[r.company_id]?.arr), sortValue: (r) => latest[r.company_id]?.arr },
        { name: "runway", label: "Runway (mo)", num: true, sortValue: (r) => runway(latest[r.company_id]),
          value: (r) => { const m = runway(latest[r.company_id]); return m == null ? "" : `<span class="${m < 6 ? "bad" : m < 12 ? "warn" : ""}">${m.toFixed(1)}</span>`; } },
        { name: "kpi_date", label: "KPIs as of", value: (r) => esc(latest[r.company_id]?.period_end ?? "") },
      ],
      onOpen: async (r) => editRecord("companies", await api("GET", `/api/companies/${r.company_id}`), render),
    });
  },
};

VIEWS.users = {
  title: "Users",
  noSavedViews: true,
  async render(el, actions) {
    const [users, settings] = await Promise.all([api("GET", "/api/users"), api("GET", "/api/settings"),
      loadLookups()]); // fresh fund list, so the access editor includes new funds
    const req = document.createElement("label");
    req.className = "muted";
    req.innerHTML = `<input type="checkbox" ${settings.require_2fa === "1" ? "checked" : ""}> Require two-factor for everyone`;
    $("input", req).onchange = (e) => guard(async () => {
      const on = e.target.checked;
      if (on && !confirm("Everyone without two-factor (including you, if not set up) will be asked to set it up before they can use the app. Continue?")) {
        e.target.checked = false;
        return;
      }
      await api("PUT", "/api/settings", { require_2fa: on ? "1" : "0" });
      toast(on ? "Two-factor is now required" : "Two-factor is now optional");
      if (on && !S.me.totp_enabled) showEnroll();
    });
    actions.append(req, exportButton("all", "Export everything (.zip)", "/api/export/all.zip"), button("+ Invite user", () => newUser(render), "primary"));
    el.innerHTML = `<div class="panel muted" style="line-height:1.6">
      <b>Admins</b> see and edit every fund and manage users.<br>
      <b>Everyone else</b> sees only the funds they are given: <b>Viewer</b> (read-only) or <b>Editor</b> per fund.
      New funds are visible to admins only until access is granted.<br>
      <b>General role</b> (editor / viewer) controls shared records: LP contact details and deals not yet assigned to a fund.
      Put fund-specific notes on the LP's commitment, not the LP record, since the LP record is shared across funds.<br>
      Deactivated users are signed out immediately.</div><div data-g></div>`;
    renderGrid($("[data-g]", el), {
      rows: users,
      cols: [
        { name: "name", label: "Name", value: (r) => esc(r.name) },
        { name: "email", label: "Email", value: (r) => esc(r.email) },
        { name: "role", label: "General role", value: (r) => `<select data-role="${r.id}">${S.meta.roles.map((x) =>
          `<option ${x === r.role ? "selected" : ""}>${x}</option>`).join("")}</select>` },
        { name: "funds", label: "Fund access", value: (r) => (r.role === "admin" ? '<span class="muted">All funds (admin)</span>'
          : `${esc(fundAccessSummary(r.fund_access)) || '<span class="muted">No funds</span>'} · <span class="link" data-funds="${r.id}">Edit</span>`) },
        { name: "active", label: "Status", value: (r) => (r.active ? "Active" : '<span class="muted">Deactivated</span>') },
        { name: "totp_enabled", label: "Two-factor", value: (r) => (r.totp_enabled ? "On" : '<span class="muted">Off</span>') },
        { name: "last_login_at", label: "Last sign-in (UTC)", value: (r) => esc(r.last_login_at || "Never") },
        { name: "actions", label: "", value: (r) => `<span class="link" data-reset="${r.id}">Reset password</span> ·
          ${r.totp_enabled ? `<span class="link" data-reset2fa="${r.id}">Reset two-factor</span> · ` : ""}
          <span class="link" data-toggle="${r.id}">${r.active ? "Deactivate" : "Reactivate"}</span>` },
      ],
    });
    const patch = (id, body) => guard(async () => { await api("PATCH", `/api/users/${id}`, body); toast("Saved"); render(); })
      .catch(() => render());
    const userOf = (id) => users.find((r) => String(r.id) === id);
    // Delegated, so the links keep working after the grid is sorted or searched.
    el.onchange = (e) => {
      const x = e.target.closest("[data-role]");
      if (x) patch(x.dataset.role, { role: x.value });
    };
    el.onclick = (e) => {
      const t = e.target.closest("[data-toggle],[data-funds],[data-reset],[data-reset2fa]");
      if (!t) return;
      if (t.dataset.toggle) {
        const u = userOf(t.dataset.toggle);
        if (u.active && !confirm(`Deactivate ${u.name}? They will be signed out.`)) return;
        patch(u.id, { active: !u.active });
      } else if (t.dataset.funds) {
        editFundAccess(userOf(t.dataset.funds), render);
      } else if (t.dataset.reset) {
        const u = userOf(t.dataset.reset);
        const pw = prompt(`New temporary password for ${u.name} (at least 10 characters). They will be signed out everywhere.`);
        if (pw) patch(u.id, { password: pw });
      } else if (t.dataset.reset2fa) {
        const u = userOf(t.dataset.reset2fa);
        if (confirm(`Reset two-factor for ${u.name}? Use this when they lose their phone. They are signed out and set it up again at next sign-in.`)) {
          patch(u.id, { reset_2fa: true });
        }
      }
    };
  },
};

function fundAccessSummary(access) {
  return Object.entries(access || {})
    .map(([id, level]) => `${refLabel("funds", Number(id))}: ${level}`)
    .sort().join(", ");
}

function editFundAccess(u, after) {
  const funds = Object.values(S.look.funds).sort((a, b) => a.name.localeCompare(b.name));
  const level = (id) => u.fund_access?.[String(id)] ?? "none";
  const d = openDrawer(`<h2>Fund access · ${esc(u.name)}</h2>
    <div class="muted">${esc(u.email)} · general role: ${esc(u.role)}</div>
    ${funds.length ? `<form class="form" style="grid-template-columns:1fr 160px; align-items:center">
      ${funds.map((f) => `<div>${esc(f.name)}</div>
        <select name="f${f.id}">${[["none", "No access"], ["viewer", "Viewer"], ["editor", "Editor"]].map(([k, l]) =>
          `<option value="${k}" ${level(f.id) === k ? "selected" : ""}>${l}</option>`).join("")}</select>`).join("")}
    </form>` : '<p class="muted">There are no funds yet.</p>'}
    <div class="drawer-actions"><div><button class="btn primary" data-save>Save access</button>
      <button class="btn" data-cancel>Close</button></div></div>
    <div class="muted" style="margin-top:12px">Changes apply on their next click; no need to sign out.</div>`);
  $("[data-cancel]", d).onclick = closeDrawer;
  $("[data-save]", d).onclick = () => guard(async () => {
    const body = {};
    for (const f of funds) {
      const v = $(`[name=f${f.id}]`, d).value;
      if (v !== "none") body[f.id] = v;
    }
    await api("PUT", `/api/users/${u.id}/funds`, body);
    toast("Fund access saved");
    closeDrawer();
    after();
  });
  changeHistory(d, "users", u.id);
}

function newUser(after) {
  const fields = [
    { name: "name", label: "Name", type: "text", required: true },
    { name: "email", label: "Email", type: "text", required: true },
    { name: "role", label: "Role", type: "select", options: S.meta.roles, required: true },
    { name: "password", label: "Temporary password (10+ characters)", type: "text", required: true },
  ];
  const d = openDrawer(`<h2>Invite user</h2>
    <div class="muted">Share the temporary password privately; they can change it under their name in the sidebar.
      New users have no fund access: set it from the Fund access column after creating them.</div>
    ${formHtml(fields, { role: "viewer" })}
    <div class="drawer-actions"><div><button class="btn primary" data-save>Create user</button>
      <button class="btn" data-cancel>Close</button></div></div>`);
  $("[data-cancel]", d).onclick = closeDrawer;
  $("[data-save]", d).onclick = () => guard(async () => {
    await api("POST", "/api/users", formValues($("form", d), fields));
    toast("User created");
    closeDrawer();
    after();
  });
}

function changePassword() {
  const d = openDrawer(`<h2>Change password</h2>
    <form class="form" style="grid-template-columns:1fr">
      <label>Current password<input type="password" name="current_password" autocomplete="current-password"></label>
      <label>New password (10+ characters)<input type="password" name="new_password" autocomplete="new-password"></label>
      <label>Repeat new password<input type="password" name="repeat" autocomplete="new-password"></label>
    </form>
    <div class="drawer-actions"><div><button class="btn primary" data-save>Change password</button>
      <button class="btn" data-cancel>Close</button></div></div>
    <div class="muted" style="margin-top:12px">Your other devices will be signed out.</div>`);
  const f = $("form", d);
  $("[data-cancel]", d).onclick = closeDrawer;
  $("[data-save]", d).onclick = () => guard(async () => {
    if (f.new_password.value !== f.repeat.value) throw new Error("New passwords do not match.");
    await api("POST", "/api/auth/password", { current_password: f.current_password.value, new_password: f.new_password.value });
    toast("Password changed");
    closeDrawer();
  });
}

// ------------------------------------------------------------------ session

/** Sign-in card: password, then authenticator code; forgot-password and reset-link forms. */
function loginCard(html, onsubmit) {
  S.me = null;
  closeDrawer();
  $("#app").hidden = true;
  const box = $("#login");
  box.hidden = false;
  const f = $("form", box);
  f.innerHTML = `<div class="brand">VC CRM</div>${html}<div class="bad" data-err></div>`;
  const err = $("[data-err]", f);
  f.onsubmit = async (e) => {
    e.preventDefault();
    err.textContent = "";
    try { await onsubmit(f); } catch (x) { err.textContent = x.message; }
  };
  f.onclick = (e) => {
    const link = e.target.closest("[data-go]");
    if (link) ({ forgot: showForgot, login: showLogin })[link.dataset.go]();
  };
  f.querySelector("input")?.focus();
  return f;
}

async function finishLogin(me) {
  S.me = me;
  $("#login").hidden = true;
  if (location.hash.startsWith("#reset=")) window.history.replaceState(null, "", "#dashboard");
  const full = await api("GET", "/api/auth/me");
  S.me = full;
  if (full.must_enroll_2fa) return showEnroll();
  await start();
}

function showLogin(email = "") {
  loginCard(`
    <label>Email<input type="text" name="email" autocomplete="username" required value="${esc(email)}"></label>
    <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
    <button class="btn primary">Sign in</button>
    <span class="link" data-go="forgot" style="font-size:12px">Forgot your password?</span>`, async (f) => {
    const r = await api("POST", "/api/auth/login", { email: f.email.value, password: f.password.value });
    if (r.twofa_required) return showCode(r.challenge, f.email.value);
    await finishLogin(r);
  });
}

function showCode(challenge, email) {
  loginCard(`
    <div class="muted">Enter the 6-digit code from your authenticator app, or one of your recovery codes.</div>
    <label>Code<input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" required></label>
    <button class="btn primary">Verify</button>
    <span class="link" data-go="login" style="font-size:12px">Back</span>`, async (f) => {
    try {
      await finishLogin(await api("POST", "/api/auth/2fa", { challenge, code: f.code.value }));
    } catch (x) {
      if (/expired/.test(x.message)) showLogin(email);
      throw x;
    }
  });
}

function showForgot() {
  loginCard(`
    <div class="muted">Enter your email and we'll send a link to choose a new password.</div>
    <label>Email<input type="text" name="email" autocomplete="username" required></label>
    <button class="btn primary">Send reset link</button>
    <span class="link" data-go="login" style="font-size:12px">Back to sign in</span>`, async (f) => {
    await api("POST", "/api/auth/forgot", { email: f.email.value });
    f.innerHTML = `<div class="brand">VC CRM</div><div>If that email has an account, a reset link is on its way.
      It works for 60 minutes. No email? Check spam, or ask an admin to reset your password.</div>
      <span class="link" data-go="login">Back to sign in</span>`;
  });
}

function showReset(token) {
  loginCard(`
    <div class="muted">Choose a new password (at least 10 characters). You'll be signed out on other devices.</div>
    <label>New password<input type="password" name="pw" autocomplete="new-password" required></label>
    <label>Repeat new password<input type="password" name="pw2" autocomplete="new-password" required></label>
    <button class="btn primary">Set password</button>`, async (f) => {
    if (f.pw.value !== f.pw2.value) throw new Error("Passwords do not match.");
    await api("POST", "/api/auth/reset", { token, new_password: f.pw.value });
    window.history.replaceState(null, "", "#dashboard");
    showLogin();
    toast("Password changed. Sign in with your new password.");
  });
}

/** Two-factor setup: scan the QR code, confirm a code, save recovery codes. */
async function showEnroll(fromSettings = false) {
  const setup = await api("POST", "/api/auth/2fa/setup");
  const html = `
    <h2 style="margin:0">Set up two-factor sign-in</h2>
    <div class="muted">${fromSettings ? "" : "Your admin requires this for everyone. "}Open an authenticator app
      (Google Authenticator, Microsoft Authenticator, 1Password...) and scan this code:</div>
    <div class="qr">${setup.qr_svg ?? ""}</div>
    <div class="muted" style="font-size:12px">Can't scan? Enter this key: <code>${esc(setup.secret.match(/.{1,4}/g).join(" "))}</code></div>
    <label>6-digit code from the app<input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" required></label>
    <div><button class="btn primary">Turn on two-factor</button>
      ${fromSettings ? '<button type="button" class="btn" data-cancel>Cancel</button>' : '<button type="button" class="btn" data-signout>Sign out</button>'}</div>`;
  const done = async (f) => {
    const { recovery_codes: codes } = await api("POST", "/api/auth/2fa/enable", { code: f.code.value });
    showRecoveryCodes(codes, fromSettings);
  };
  if (fromSettings) {
    const d = openDrawer(`<form class="form" style="grid-template-columns:1fr">${html}<div class="bad" data-err></div></form>`);
    const f = $("form", d);
    $("[data-cancel]", d).onclick = closeDrawer;
    f.onsubmit = (e) => { e.preventDefault(); guard(() => done(f)).catch(() => {}); };
    return;
  }
  const f = loginCard(html, done);
  $("[data-signout]", f).onclick = async () => { await api("POST", "/api/auth/logout"); showLogin(); };
}

function showRecoveryCodes(codes, inDrawer) {
  const html = `<h2 style="margin:0">Save your recovery codes</h2>
    <div class="muted">If you lose your phone, each of these codes signs you in once. Store them in your password
      manager now; they won't be shown again.</div>
    <pre class="codes">${codes.map(esc).join("\n")}</pre>
    <div><button type="button" class="btn" data-copy>Copy codes</button>
      <button type="button" class="btn primary" data-done>I've saved them</button></div>`;
  const root = inDrawer ? openDrawer(`<div class="form" style="grid-template-columns:1fr">${html}</div>`) : loginCard(html, async () => {});
  $("[data-copy]", root).onclick = () => navigator.clipboard?.writeText(codes.join("\n")).then(() => toast("Copied"));
  $("[data-done]", root).onclick = async () => {
    if (inDrawer) { closeDrawer(); S.me = await api("GET", "/api/auth/me"); return; }
    $("#login").hidden = true;
    S.me = await api("GET", "/api/auth/me");
    await start();
  };
}

/** Account security drawer: two-factor status and personal API tokens. */
async function showSecurity() {
  const tokens = await api("GET", "/api/tokens");
  const me = (S.me = await api("GET", "/api/auth/me"));
  const d = openDrawer(`<h2>Account security</h2>
    <div class="section-title">Email reminders</div>
    <label><input type="checkbox" data-remind ${me.email_reminders ? "checked" : ""}> Email me each morning about tasks that are overdue or due today</label>
    <div class="section-title">Two-factor sign-in</div>
    <div>${me.totp_enabled ? "On. You'll be asked for a code from your authenticator app when you sign in." : "Off. Turn it on to protect your account if your password leaks."}</div>
    <div style="margin:10px 0">${me.totp_enabled
      ? `<button class="btn" data-codes>New recovery codes</button>${me.require_2fa ? "" : ' <button class="btn danger" data-off>Turn off</button>'}`
      : '<button class="btn primary" data-on>Turn on two-factor</button>'}</div>
    <div class="section-title">API tokens <button class="btn small" data-new>+ New token</button></div>
    <div class="muted">Tokens let scripts, or Claude logging your Granola meetings, use the CRM as you, with your
      permissions. Treat them like passwords and revoke any you no longer use.</div>
    <div data-tokens style="margin-top:8px">${tokens.map((t) => `<div class="activity"><b>${esc(t.name)}</b>
      <span class="muted">${esc(t.prefix)}... · created ${esc(localTime(t.created_at))} ·
      ${t.last_used_at ? `last used ${esc(localTime(t.last_used_at))}` : "never used"}</span>
      <span class="link" data-revoke="${t.id}" style="float:right">revoke</span></div>`).join("") || '<div class="muted">No tokens.</div>'}</div>
    <div class="drawer-actions" style="margin-top:16px"><button class="btn" data-close>Close</button></div>`);
  $("[data-remind]", d).onchange = (e) => guard(async () => {
    await api("PATCH", "/api/auth/preferences", { email_reminders: e.target.checked });
    toast(e.target.checked ? "Reminders on" : "Reminders off");
  });
  d.onclick = (e) => {
    const t = e.target.closest("[data-on],[data-off],[data-codes],[data-new],[data-revoke],[data-close]");
    if (!t) return;
    if (t.matches("[data-close]")) closeDrawer();
    else if (t.matches("[data-on]")) guard(() => showEnroll(true));
    else if (t.matches("[data-off]")) {
      const pw = prompt("Enter your password to turn off two-factor:");
      if (pw) guard(async () => { await api("POST", "/api/auth/2fa/disable", { password: pw }); toast("Two-factor turned off"); showSecurity(); });
    } else if (t.matches("[data-codes]")) {
      const pw = prompt("Enter your password to replace your recovery codes (the old ones stop working):");
      if (pw) guard(async () => showRecoveryCodes((await api("POST", "/api/auth/2fa/recovery-codes", { password: pw })).recovery_codes, true));
    } else if (t.matches("[data-new]")) {
      const name = prompt("Name this token (e.g. 'Claude meeting notes'):");
      if (name) guard(async () => {
        const r = await api("POST", "/api/tokens", { name });
        const box = openDrawer(`<h2>Your new API token</h2><div class="muted">Copy it now; it won't be shown again.</div>
          <pre class="codes">${esc(r.token)}</pre><div><button class="btn" data-copy>Copy</button>
          <button class="btn primary" data-back>Done</button></div>`);
        $("[data-copy]", box).onclick = () => navigator.clipboard?.writeText(r.token).then(() => toast("Copied"));
        $("[data-back]", box).onclick = () => showSecurity();
      });
    } else if (t.dataset.revoke && confirm("Revoke this token? Anything using it stops working immediately.")) {
      guard(async () => { await api("DELETE", `/api/tokens/${t.dataset.revoke}`); toast("Token revoked"); showSecurity(); });
    }
  };
}

/** Sidebar search across LPs, companies, investors, funds and rounds (Cmd+K / Ctrl+K). */
function setupSearch() {
  const input = $("#search"), out = $("#search-results");
  let timer, hits = [], active = -1;
  const draw = () => {
    out.hidden = !hits.length && !input.value.trim();
    out.innerHTML = hits.length ? hits.map((h, i) => `<div class="hit ${i === active ? "on" : ""}" data-i="${i}">
        <div>${esc(h.title)}</div><div class="muted">${esc(ENTITY_NOUN[h.entity] ?? h.entity)}${h.subtitle ? ` · ${esc(h.subtitle)}` : ""}</div></div>`).join("")
      : input.value.trim().length >= 2 ? '<div class="muted" style="padding:8px">No matches</div>' : "";
  };
  const open = async (h) => {
    input.value = ""; hits = []; draw(); input.blur();
    editRecord(h.entity, await api("GET", `/api/${h.entity}/${h.id}`), () => render());
  };
  input.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      hits = q.length >= 2 ? await api("GET", `/api/search?q=${encodeURIComponent(q)}`).catch(() => []) : [];
      active = hits.length ? 0 : -1;
      draw();
    }, 200);
  };
  input.onkeydown = (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = Math.max(0, Math.min(hits.length - 1, active + (e.key === "ArrowDown" ? 1 : -1)));
      draw();
    } else if (e.key === "Enter" && hits[active]) open(hits[active]);
    else if (e.key === "Escape") { input.value = ""; hits = []; draw(); input.blur(); }
  };
  out.onmousedown = (e) => { const i = e.target.closest("[data-i]")?.dataset.i; if (i != null) open(hits[i]); };
  input.onblur = () => setTimeout(() => (out.hidden = true), 150);
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k" && S.me) { e.preventDefault(); input.focus(); }
  });
}
setupSearch();

function renderMe() {
  $("#me").innerHTML = `<div class="who">${esc(S.me.name)}</div><div class="muted">${esc(S.me.email)} · ${esc(S.me.role)}</div>
    <div style="margin-top:6px"><span class="link" data-pw>Change password</span> · <span class="link" data-sec>Security</span>
      · <span class="link" data-out>Sign out</span></div>`;
  $("[data-pw]", $("#me")).onclick = changePassword;
  $("[data-sec]", $("#me")).onclick = () => guard(showSecurity);
  $("[data-out]", $("#me")).onclick = async () => { await api("POST", "/api/auth/logout"); showLogin(); };
  document.querySelectorAll("[data-admin]").forEach((x) => (x.hidden = !isAdmin()));
}

async function start() {
  S.meta = await api("GET", "/api/meta");
  await loadLookups();
  renderMe();
  $("#app").hidden = false;
  go(location.hash.slice(1) || "dashboard");
}

// ------------------------------------------------------------------ router

// Preferences a saved view restores along with the list's search, sort and filters.
const VIEW_PREFS = ["fund", "lpMode", "dealMode", "roundMode", "taskTab"];

/** "Views" menu: apply, save (private or shared) and delete named views of a screen. */
async function savedViewsControl(view) {
  const views = await api("GET", `/api/saved-views?view=${encodeURIComponent(view)}`).catch(() => []);
  const sel = document.createElement("select");
  const current = S.activeView[view] ?? "";
  sel.innerHTML = `<option value="">All records</option>${views.map((v) =>
    `<option value="${v.id}" ${String(v.id) === String(current) ? "selected" : ""}>${esc(v.name)}${v.shared ? " (shared)" : ""}</option>`).join("")}
    <option value="save">Save current view...</option>
    ${current ? '<option value="delete">Delete this view</option>' : ""}`;
  sel.title = "Saved views";
  sel.onchange = () => guard(async () => {
    const choice = sel.value;
    if (choice === "save") {
      const name = prompt("Name this view (e.g. 'My LPs in due diligence'):", views.find((v) => String(v.id) === String(current))?.name ?? "");
      if (!name) return render();
      const shared = confirm("Share this view with everyone? (OK = shared, Cancel = only you)");
      const state = { grid: S.gridState[view] ?? {}, prefs: Object.fromEntries(VIEW_PREFS.map((k) => [k, pref(k)])) };
      const saved = await api("POST", "/api/saved-views", { view, name, shared, state });
      S.activeView[view] = saved.id;
      toast("View saved");
    } else if (choice === "delete") {
      if (confirm("Delete this saved view?")) {
        await api("DELETE", `/api/saved-views/${current}`);
        S.activeView[view] = "";
      }
    } else if (choice === "") {
      S.activeView[view] = "";
      S.gridState[view] = { sort: null, dir: 1, q: "", filters: [] };
    } else {
      const v = views.find((x) => String(x.id) === choice);
      S.activeView[view] = v.id;
      S.gridState[view] = structuredClone({ sort: null, dir: 1, q: "", filters: [], ...v.state.grid });
      for (const [k, val] of Object.entries(v.state.prefs ?? {})) if (val != null) pref(k, val);
    }
    render();
  }).catch(() => render());
  return sel;
}

async function render() {
  const name = S.view;
  const v = VIEWS[name];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  $("#view-title").textContent = v.title;
  const actions = $("#view-actions");
  actions.innerHTML = "";
  const el = $("#view");
  el.onclick = null; // a view may attach one; never let it leak into the next view
  el.onchange = null;
  try {
    await v.render(el, actions);
    if (!v.noSavedViews) actions.prepend(await savedViewsControl(name));
  } catch (e) {
    el.innerHTML = `<div class="panel bad">${esc(e.message)}</div>`;
  }
}

function go(name) {
  S.view = VIEWS[name] && (!["users", "custom-fields"].includes(name) || isAdmin()) ? name : "dashboard";
  if (location.hash.slice(1) !== S.view) location.hash = S.view;
  render();
}

$("#nav").onclick = (e) => { const a = e.target.closest("a[data-view]"); if (a) go(a.dataset.view); };
window.addEventListener("hashchange", () => {
  // A password-reset link opened in a tab where the app is already loaded.
  const reset = location.hash.match(/^#reset=(.+)$/);
  if (reset) return showReset(decodeURIComponent(reset[1]));
  if (S.me && location.hash.slice(1) !== S.view) go(location.hash.slice(1));
});

(async () => {
  const reset = location.hash.match(/^#reset=(.+)$/);
  if (reset) return showReset(decodeURIComponent(reset[1]));
  try {
    S.me = await api("GET", "/api/auth/me");
  } catch {
    return; // showLogin() already ran on the 401
  }
  if (S.me.must_enroll_2fa) return showEnroll();
  await start();
})();
