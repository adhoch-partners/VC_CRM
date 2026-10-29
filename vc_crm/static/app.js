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
  if (res.status === 401 && path !== "/api/auth/login") {
    showLogin();
    throw new Error("Your session has ended. Please sign in again.");
  }
  const data = await res.json().catch(() => ({}));
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
  pct: (v, digits = 1) => (v == null ? "n/a" : `${(v * 100).toFixed(digits)}%`),
  multiple: (v) => (v == null ? "n/a" : `${v.toFixed(2)}x`),
  num: (v) => (v == null || v === "" ? "" : Number(v).toLocaleString()),
};

const S = { meta: null, me: null, people: {}, look: { funds: {}, lps: {}, companies: {}, investors: {}, rounds: {} }, view: null, prefs: {} };
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
  const [funds, lps, companies, people, me, investors, rounds] = await Promise.all(
    ["funds", "lps", "companies", "users/directory", "auth/me", "investors", "rounds"]
      .map((e) => api("GET", `/api/${e}`)));
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
function renderGrid(el, { entity, rows, cols, onOpen, onChanged, search = true }) {
  const state = { sort: null, dir: 1, q: "" };
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

  el.innerHTML = `${search ? '<div style="margin-bottom:10px"><input type="search" placeholder="Search..." style="max-width:280px"></div>' : ""}
    <div class="grid-wrap"><table class="grid"><thead></thead><tbody></tbody><tfoot></tfoot></table></div>`;
  const q = $("input[type=search]", el);
  if (q) q.oninput = () => { state.q = q.value.toLowerCase(); draw(); };

  const cellHtml = (c, r) => (c.value ? c.value(r) : display(c.field, r[c.name]));
  const sortVal = (c, r) => {
    const v = c.sortValue ? c.sortValue(r) : r[c.name];
    if (c.field?.type === "ref") return refLabel(c.field.ref, v).toLowerCase();
    return typeof v === "string" ? v.toLowerCase() : v;
  };

  function draw() {
    let list = rows;
    if (state.q) {
      list = rows.filter((r) => colDefs.some((c) =>
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
  }
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
    if (ACTIVITY_OWNERS.includes(entity)) await activityLog(extra, entity, row.id, Boolean(row._can_edit));
    await history(extra, entity, row.id);
  }
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

const ACTION_LABEL = { create: "created", update: "updated", delete: "deleted", password_change: "changed password" };

function describeChange(e) {
  if (e.action !== "update" || !e.changes) return "";
  return Object.entries(e.changes).map(([k, v]) => {
    if (v && typeof v === "object" && "to" in v) return `${k}: ${v.from ?? "(blank)"} → ${v.to ?? "(blank)"}`;
    return `${k}: ${v}`;
  }).join("; ");
}

async function history(el, entity, id) {
  const items = await api("GET", `/api/audit?entity=${entity}&entity_id=${id}&limit=20`);
  const box = document.createElement("div");
  box.innerHTML = `<div class="section-title">Change history</div>${items.map((e) => `
    <div class="activity"><div class="when">${esc(e.at)} UTC · ${esc(e.user_name || "Former user")} ${esc(ACTION_LABEL[e.action] || e.action)}</div>
      ${esc(describeChange(e))}</div>`).join("") || '<div class="muted">No recorded changes.</div>'}`;
  el.append(box);
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
  const tabs = [["investments", "Investments"], ["valuations", "Valuations"], ["realizations", "Realizations"], ["rounds", "Rounds"]];
  let cur = "investments";
  const draw = async () => {
    const rows = await api("GET", `/api/${cur}?company_id=${co.id}`);
    box.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button data-t="${k}" class="${k === cur ? "on" : ""}">${l}</button>`).join("")}</div>
      <div style="margin:10px 0" ${canEditSomeFund() ? "" : "hidden"}><button class="btn small" data-add>+ Add ${cur.replace(/s$/, "")}</button></div><div data-g></div>`;
    box.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    const defaults = { company_id: co.id, fund_id: co.fund_id ?? "" };
    const dateField = { investments: "investment_date", valuations: "valuation_date", realizations: "realization_date" }[cur];
    if (dateField) defaults[dateField] = today();
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
      renderGrid(el, {
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
    actions.append(buttonIf(canCreateShared(), "+ New LP", () => editRecord("lps", null, render), "primary"));
    const [rows, commits] = await Promise.all([api("GET", "/api/lps"), api("GET", "/api/commitments")]);
    const closed = {};
    for (const c of commits) if (c.stage === "Closed") closed[c.lp_id] = (closed[c.lp_id] || 0) + (c.committed_amount || 0);
    renderGrid(el, {
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
  async render(el, actions) {
    actions.append(
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
}

VIEWS["deal-board"] = {
  title: "Deal pipeline",
  async render(el, actions) {
    const mode = pref("dealMode") ?? "board";
    actions.append(
      segmented([["board", "Board"], ["grid", "Grid"]], mode, (m) => { pref("dealMode", m); render(); }),
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
      renderGrid(el, {
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
    actions.append(buttonIf(canEditSomeFund(), "+ New round", () => editRecord("rounds", null, render), "primary"));
    const [rounds, pipe, inv] = await Promise.all(["rounds", "round_investors", "investments"].map((e) => api("GET", `/api/${e}`)));
    const totals = roundTotals(pipe, inv);
    el.onclick = (e) => {
      const x = e.target.closest("[data-pipe]");
      if (x) { pref("round", Number(x.dataset.pipe)); go("round-board"); }
    };
    renderGrid(el, {
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
      renderGrid(target, {
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
    actions.append(buttonIf(canCreateShared(), "+ New investor", () => editRecord("investors", null, render), "primary"));
    const [rows, pipe] = await Promise.all([api("GET", "/api/investors"), api("GET", "/api/round_investors")]);
    const active = {}, committed = {};
    for (const x of pipe) {
      if (x.stage === "Committed") committed[x.investor_id] = (committed[x.investor_id] || 0) + 1;
      else if (x.stage !== "Passed") active[x.investor_id] = (active[x.investor_id] || 0) + 1;
    }
    renderGrid(el, {
      entity: "investors", rows,
      cols: ["name", "investor_type", "contact_name", "email", "location", "stage_focus", "check_size_min", "check_size_max",
        { name: "active", label: "Active in rounds", num: true, editable: false, value: (r) => active[r.id] || "", sortValue: (r) => active[r.id] || 0 },
        { name: "committed", label: "Committed in", num: true, editable: false, value: (r) => committed[r.id] || "", sortValue: (r) => committed[r.id] || 0 }],
      onOpen: (r) => editRecord("investors", r, render),
      onChanged: loadLookups,
    });
  },
};

VIEWS.companies = {
  title: "All companies",
  async render(el, actions) {
    actions.append(buttonIf(canCreateShared(), "+ New company", () => editRecord("companies", null, render), "primary"));
    renderGrid(el, {
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
    const { holdings, totals: t } = await api("GET", `/api/portfolio${fund ? `?fund_id=${fund}` : ""}`);
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
    renderGrid($("[data-g]", el), {
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
      ],
      onOpen: async (r) => editRecord("companies", await api("GET", `/api/companies/${r.company_id}`), render),
    });
  },
};

VIEWS.users = {
  title: "Users",
  async render(el, actions) {
    actions.append(button("+ Invite user", () => newUser(render), "primary"));
    // Fresh fund list, so the access editor includes funds created since page load.
    const [users] = await Promise.all([api("GET", "/api/users"), loadLookups()]);
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
        { name: "last_login_at", label: "Last sign-in (UTC)", value: (r) => esc(r.last_login_at || "Never") },
        { name: "actions", label: "", value: (r) => `<span class="link" data-reset="${r.id}">Reset password</span> ·
          <span class="link" data-toggle="${r.id}">${r.active ? "Deactivate" : "Reactivate"}</span>` },
      ],
    });
    const patch = (id, body) => guard(async () => { await api("PATCH", `/api/users/${id}`, body); toast("Saved"); render(); })
      .catch(() => render());
    el.querySelectorAll("[data-role]").forEach((x) => (x.onchange = () => patch(x.dataset.role, { role: x.value })));
    el.querySelectorAll("[data-toggle]").forEach((x) => (x.onclick = () => {
      const u = users.find((r) => String(r.id) === x.dataset.toggle);
      if (u.active && !confirm(`Deactivate ${u.name}? They will be signed out.`)) return;
      patch(u.id, { active: !u.active });
    }));
    el.querySelectorAll("[data-funds]").forEach((x) => (x.onclick = () =>
      editFundAccess(users.find((r) => String(r.id) === x.dataset.funds), render)));
    el.querySelectorAll("[data-reset]").forEach((x) => (x.onclick = () => {
      const u = users.find((r) => String(r.id) === x.dataset.reset);
      const pw = prompt(`New temporary password for ${u.name} (at least 10 characters). They will be signed out everywhere.`);
      if (pw) patch(u.id, { password: pw });
    }));
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

function showLogin() {
  S.me = null;
  closeDrawer();
  $("#app").hidden = true;
  const box = $("#login");
  box.hidden = false;
  const f = $("form", box);
  $("[data-err]", box).textContent = "";
  f.password.value = "";
  (f.email.value ? f.password : f.email).focus();
  f.onsubmit = async (e) => {
    e.preventDefault();
    $("[data-err]", box).textContent = "";
    try {
      S.me = await api("POST", "/api/auth/login", { email: f.email.value, password: f.password.value });
      box.hidden = true;
      await start();
    } catch (err) {
      $("[data-err]", box).textContent = err.message;
    }
  };
}

function renderMe() {
  $("#me").innerHTML = `<div class="who">${esc(S.me.name)}</div><div class="muted">${esc(S.me.email)} · ${esc(S.me.role)}</div>
    <div style="margin-top:6px"><span class="link" data-pw>Change password</span> · <span class="link" data-out>Sign out</span></div>`;
  $("[data-pw]", $("#me")).onclick = changePassword;
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

async function render() {
  const name = S.view;
  const v = VIEWS[name];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  $("#view-title").textContent = v.title;
  const actions = $("#view-actions");
  actions.innerHTML = "";
  const el = $("#view");
  el.onclick = null; // a view may attach one; never let it leak into the next view
  try {
    await v.render(el, actions);
  } catch (e) {
    el.innerHTML = `<div class="panel bad">${esc(e.message)}</div>`;
  }
}

function go(name) {
  S.view = VIEWS[name] && (name !== "users" || isAdmin()) ? name : "dashboard";
  if (location.hash.slice(1) !== S.view) location.hash = S.view;
  render();
}

$("#nav").onclick = (e) => { const a = e.target.closest("a[data-view]"); if (a) go(a.dataset.view); };
window.addEventListener("hashchange", () => { if (location.hash.slice(1) !== S.view) go(location.hash.slice(1)); });

(async () => {
  try {
    S.me = await api("GET", "/api/auth/me");
  } catch {
    return; // showLogin() already ran on the 401
  }
  await start();
})();
