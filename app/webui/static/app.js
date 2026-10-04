"use strict";
/* Invoice review: two screens (list, review). Plain JavaScript, no build step.
   Every value that comes from an invoice is put into the page with textContent / .value, never as HTML. */

const view = document.getElementById("view");
let cfg = null;            // /ui/config
let cleanups = [];         // run when leaving a screen (timers, listeners, object URLs)
let flash = null;          // one-time message shown on the list

const STATE_TEXT = { reading: "Reading invoice…", ready: "Ready to review", reviewed: "Reviewed", failed: "Couldn't read this invoice." };

// ---------------------------------------------------------------- helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}

async function call(url, opts) {
  let r;
  try { r = await fetch(url, opts); }
  catch { throw { message: "Can't reach the service. Check your connection and try again." }; }
  if (!r.ok) {
    let b = {};
    try { b = await r.json(); } catch { /* not JSON */ }
    throw { status: r.status, message: b.message || "Something went wrong. Please try again.", fields: b.fields };
  }
  return r;
}
const getJSON = async (url) => (await call(url)).json();

function leave() { cleanups.forEach((f) => { try { f(); } catch { /* ignore */ } }); cleanups = []; }
function setView(...nodes) { view.replaceChildren(...nodes); }
function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }

// ---------------------------------------------------------------- list screen
async function renderList() {
  const items = [];                  // newest first
  let more = null;

  const msgBox = h("div");
  const nextMsg = h("span", { class: "muted" });
  const tbody = h("tbody");
  const moreBtn = h("button", { class: "link", type: "button", text: "Show older invoices", hidden: true, onclick: () => load(more) });
  const input = h("input", { type: "file", multiple: true, hidden: true, accept: ".pdf,.png,.jpg,.jpeg,image/png,image/jpeg,application/pdf",
                              onchange: () => { upload(input.files); input.value = ""; } });
  const drop = h("div", { class: "drop" },
    h("p", { text: "Drop invoices here" }),
    h("button", { class: "primary", type: "button", text: "Choose files", onclick: () => input.click() }),
    h("p", { class: "muted", text: `PNG, JPG or PDF, up to ${cfg.max_mb} MB each` }), input);

  const reviewNext = h("button", { class: "primary", type: "button", text: "Review next", onclick: async () => {
    nextMsg.textContent = "";
    try {
      const r = await getJSON("/ui/next");
      if (r.next) go("#/review/" + r.next); else nextMsg.textContent = "Nothing is waiting for review.";
    } catch (e) { nextMsg.textContent = e.message; }
  } });

  const table = h("table", { class: "list" },
    h("thead", null, h("tr", null, h("th", { text: "Invoice" }), h("th", { text: "Supplier" }), h("th", { text: "Date" }),
                       h("th", { class: "num", text: "Total" }), h("th", { text: "Status" }))), tbody);
  const empty = h("div", { class: "empty", text: "No invoices yet. Drop some above." });

  setView(h("div", { class: "page" }, h("h1", { text: "Invoices" }), drop, msgBox,
    h("div", { class: "toolbar" }, reviewNext, nextMsg), table, empty, h("p", null, moreBtn)));
  if (flash) { msgBox.append(h("div", { class: "msg ok", text: flash })); flash = null; }

  // dropping a file anywhere on the page must not make the browser open it
  const stop = (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) e.preventDefault(); };
  const over = (e) => { stop(e); drop.classList.add("over"); };
  const out = () => drop.classList.remove("over");
  const dropped = (e) => { stop(e); out(); if (e.dataTransfer && e.dataTransfer.files.length) upload(e.dataTransfer.files); };
  document.addEventListener("dragover", over); document.addEventListener("dragleave", out); document.addEventListener("drop", dropped);
  cleanups.push(() => { document.removeEventListener("dragover", over); document.removeEventListener("dragleave", out); document.removeEventListener("drop", dropped); });

  function row(it) {
    const openable = it.state === "ready" || it.state === "reviewed";
    const href = "#/review/" + it.id;
    const tr = h("tr", { class: openable ? "open" : "" },
      h("td", null, openable ? h("a", { href, text: it.name }) : it.name),
      h("td", { text: it.vendor || "" }), h("td", { text: it.date || "" }),
      h("td", { class: "num", text: it.total != null ? `${it.total}${it.currency ? " " + it.currency : ""}` : "" }),
      h("td", null, h("span", { class: "chip " + it.state, text: STATE_TEXT[it.state] || "" })));
    if (openable) tr.addEventListener("click", (e) => { if (e.target.tagName !== "A") location.hash = href; });
    return tr;
  }
  function draw() {
    tbody.replaceChildren(...items.map(row));
    table.hidden = empty.hidden = false;
    table.hidden = items.length === 0; empty.hidden = items.length !== 0;
    moreBtn.hidden = !more;
  }
  async function load(cursor) {
    try {
      const page = await getJSON("/ui/invoices" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""));
      if (cursor) items.push(...page.items); else merge(page.items);
      more = cursor || !items.length ? page.more : (more ?? page.more);
      draw();
    } catch (e) { msgBox.replaceChildren(h("div", { class: "msg bad", text: e.message })); }
  }
  function merge(fresh) {            // first page: update known rows in place, put new ones on top
    const known = new Map(items.map((i, n) => [i.id, n]));
    const added = [];
    for (const f of fresh) { if (known.has(f.id)) items[known.get(f.id)] = f; else added.push(f); }
    items.unshift(...added);
  }

  async function upload(files) {
    const list = [...files];
    if (!list.length) return;
    if (list.length > cfg.max_files) { msgBox.replaceChildren(h("div", { class: "msg bad", text: `Please upload at most ${cfg.max_files} invoices at a time.` })); return; }
    const fd = new FormData();
    list.forEach((f) => fd.append("files", f));
    msgBox.replaceChildren(h("div", { class: "msg", text: list.length === 1 ? "Uploading…" : `Uploading ${list.length} invoices…` }));
    try {
      const { results } = await (await call("/ui/upload", { method: "POST", body: fd })).json();
      const ok = results.filter((r) => r.ok).length, bad = results.filter((r) => !r.ok);
      const box = [];
      if (ok) box.push(h("div", { class: "msg ok", text: ok === 1 ? "1 invoice uploaded. We're reading it now." : `${ok} invoices uploaded. We're reading them now.` }));
      if (bad.length) box.push(h("div", { class: "msg bad" }, "These files weren't uploaded:", h("ul", null, bad.map((b) => h("li", { text: `${b.name}: ${b.message}` })))));
      msgBox.replaceChildren(...box);
    } catch (e) { msgBox.replaceChildren(h("div", { class: "msg bad", text: e.message })); }
    await load(null); poll();
  }

  let timer = null;
  function poll() {
    clearInterval(timer);
    if (!items.some((i) => i.state === "reading")) return;
    timer = setInterval(async () => { await load(null); if (!items.some((i) => i.state === "reading")) clearInterval(timer); }, 3000);
  }
  cleanups.push(() => clearInterval(timer));

  await load(null); poll();
}

// ---------------------------------------------------------------- review screen
async function renderReview(token) {
  let d;
  try { d = await getJSON("/ui/invoices/" + token); }
  catch (e) { setView(h("div", { class: "page" }, h("p", null, h("a", { href: "#/", text: "← All invoices" })), h("div", { class: "msg bad", text: e.message }))); return; }

  if (d.state === "reading") {
    setView(h("div", { class: "page" }, h("p", null, h("a", { href: "#/", text: "← All invoices" })), h("h1", { text: d.name }), h("p", { class: "muted", text: STATE_TEXT.reading })));
    const t = setTimeout(route, 2500); cleanups.push(() => clearTimeout(t));
    return;
  }
  if (d.state === "failed") {
    setView(h("div", { class: "page" }, h("p", null, h("a", { href: "#/", text: "← All invoices" })), h("h1", { text: d.name }), h("div", { class: "msg bad", text: STATE_TEXT.failed })));
    return;
  }

  const locked = d.reviewed;
  const inputs = {};                 // field key -> input
  const flagBoxes = {};              // field key -> element for its message

  // ---- left: the original
  const stage = h("div", { class: "stage" }, h("div", { class: "placeholder", text: "Loading the original…" }));
  const tools = h("div", { class: "tools" });
  setupOriginal(token, stage, tools);

  // ---- right: the data
  function field(spec) {
    const input = h("input", { type: spec.kind === "date" ? "date" : "text", id: "f_" + spec.key, disabled: locked, autocomplete: "off",
                               inputmode: spec.kind === "money" ? "decimal" : null, maxlength: spec.kind === "currency" ? 3 : 200 });
    input.value = d.values[spec.key] ?? "";
    const note = h("div", { class: "flag" });
    inputs[spec.key] = input; flagBoxes[spec.key] = note;
    const msg = d.flags[spec.key];
    if (msg) { input.classList.add("flagged"); note.textContent = "Please check: " + msg; }
    input.addEventListener("input", () => { input.classList.remove("error"); });
    return h("div", { class: "field" }, h("label", { for: input.id, text: spec.label }), input, note);
  }
  const spec = (group) => cfg.fields.filter((f) => f.group === group);
  const pairs = (list) => { const out = []; for (let i = 0; i < list.length; i += 2) out.push(h("div", { class: "row2" }, list.slice(i, i + 2).map(field))); return out; };

  const moreFlagged = spec("more").some((s) => d.flags[s.key]);
  const more = h("details", { open: moreFlagged }, h("summary", { text: "More details" }), spec("more").map(field));

  // line items
  const itemRows = [];
  const tbody = h("tbody");
  const itemsFlag = h("div", { class: "flag", text: d.flags.items ? "Please check: " + d.flags.items : "" });
  function itemRow(values, n) {
    const cells = {};
    const tr = h("tr", null, cfg.item_columns.map((c) => {
      const inp = h("input", { type: "text", disabled: locked, autocomplete: "off", "aria-label": `${c.label}, item ${n + 1}`, inputmode: c.kind === "text" ? null : "decimal", maxlength: 200 });
      inp.value = values[c.key] ?? ""; cells[c.key] = inp;
      inp.addEventListener("input", () => inp.classList.remove("error"));
      return h("td", { class: c.key === "description" ? "desc" : "" }, inp);
    }));
    const flag = d.flags[`items[${n}]`];
    const extra = [tr];
    if (flag) { cells.amount.classList.add("flagged"); extra.push(h("tr", { class: "rowflag" }, h("td", { colspan: cfg.item_columns.length }, h("div", { class: "flag", text: "Please check: " + flag })))); }
    itemRows.push(cells);
    tbody.append(...extra);
  }
  d.items.forEach(itemRow);
  const addBtn = h("button", { type: "button", text: "Add an item", hidden: locked, onclick: () => { itemRow({}, itemRows.length); addBtn.disabled = true; } });
  const itemsTable = h("table", { class: "items" }, h("thead", null, h("tr", null, cfg.item_columns.map((c) => h("th", { text: c.label })))), tbody);

  const note = h("textarea", { rows: 2, maxlength: 1000, id: "note", disabled: locked });
  const err = h("div", { class: "msg bad", hidden: true });
  const submitBtn = h("button", { class: "primary", type: "submit", text: "Submit", hidden: locked });
  const nextBtn = locked && d.next ? h("a", { class: "button primary", href: "#/review/" + d.next, text: "Next invoice" }) : null;

  const form = h("form", { novalidate: true, onsubmit: (e) => { e.preventDefault(); submit(); } },
    h("h2", { text: "Invoice" }), pairs(spec("invoice")),
    h("h2", { text: "Amounts" }), pairs(spec("amounts")),
    h("h2", { text: "Items" }), itemsTable, itemsFlag, h("p", null, addBtn),
    more,
    locked ? null : h("div", { class: "note" }, h("label", { for: "note", text: "Anything we should know about this invoice? (optional)" }), note),
    h("div", { class: "foot" }, cfg.notice && !locked ? h("p", { class: "notice", text: cfg.notice }) : null, err,
      h("div", { class: "actions" }, submitBtn, nextBtn)));

  const general = d.flags._general ? h("div", { class: "msg", text: d.flags._general }) : null;
  const panel = h("div", { class: "panel" },
    h("div", { class: "head" }, h("a", { href: "#/", text: "← All invoices" }), h("span", { class: "name", text: d.name }),
      locked ? h("span", { class: "chip reviewed", text: STATE_TEXT.reviewed }) : null),
    h("p", { class: "hint", text: locked ? "This invoice has been reviewed." : "Compare each value with the invoice. Fix anything that is wrong, then press Submit." }),
    general, form);

  setView(h("div", { class: "review" }, h("div", { class: "original" }, tools, stage), panel));

  const keys = (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !locked) { e.preventDefault(); submit(); } };
  document.addEventListener("keydown", keys); cleanups.push(() => document.removeEventListener("keydown", keys));

  async function submit() {
    err.hidden = true;
    const values = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
    const items = itemRows.map((cells) => Object.fromEntries(Object.entries(cells).map(([k, el]) => [k, el.value])))
      .filter((row, n) => n < d.items.length || Object.values(row).some((v) => v.trim() !== ""));
    submitBtn.disabled = true; submitBtn.textContent = "Submitting…";
    try {
      const r = await (await call(`/ui/invoices/${token}/submit`, { method: "POST", headers: { "Content-Type": "application/json" },
                                                                     body: JSON.stringify({ values, items, note: note.value }) })).json();
      if (r.next) go("#/review/" + r.next);
      else { flash = "Submitted. No more invoices are waiting for review."; go("#/"); }
    } catch (e) {
      submitBtn.disabled = false; submitBtn.textContent = "Submit";
      err.textContent = e.message; err.hidden = false;
      showFieldErrors(e.fields || {});
    }
  }
  function showFieldErrors(fields) {
    let first = null;
    for (const [k, text] of Object.entries(fields)) {
      let el = inputs[k], box = flagBoxes[k];
      const m = /^items\[(\d+)\]\.(\w+)$/.exec(k);
      if (m && itemRows[+m[1]]) el = itemRows[+m[1]][m[2]];
      if (el) { el.classList.add("error"); first = first || el; }
      if (box) { box.textContent = text; box.classList.add("error"); }
      else if (k === "items") { itemsFlag.textContent = text; itemsFlag.classList.add("error"); }
    }
    if (first) first.scrollIntoView({ block: "center" });
  }
}

// the original invoice next to the form: images are shown inline, PDFs in the browser's own viewer
async function setupOriginal(token, stage, tools) {
  try {
    const blob = await (await call(`/ui/invoices/${token}/file`)).blob();
    const url = URL.createObjectURL(blob);
    cleanups.push(() => URL.revokeObjectURL(url));
    const open = h("a", { class: "button", href: url, target: "_blank", rel: "noopener", text: "Open in a new tab" });
    if (blob.type === "application/pdf") {
      stage.replaceChildren(h("iframe", { src: url, title: "Original invoice" }));
      tools.replaceChildren(h("span", { class: "grow muted", text: "Original invoice" }), open);
    } else {
      const img = h("img", { src: url, alt: "Original invoice" });
      let zoom = 100;
      const set = (z) => { zoom = Math.max(50, Math.min(400, z)); img.style.width = zoom + "%"; };
      stage.replaceChildren(img);
      tools.replaceChildren(h("span", { class: "grow muted", text: "Original invoice" }),
        h("button", { type: "button", "aria-label": "Zoom out", text: "−", onclick: () => set(zoom - 25) }),
        h("button", { type: "button", "aria-label": "Zoom in", text: "+", onclick: () => set(zoom + 25) }),
        h("button", { type: "button", text: "Fit", onclick: () => set(100) }), open);
    }
  } catch {
    stage.replaceChildren(h("div", { class: "placeholder", text: "The original couldn't be shown. You can still correct the values." }));
  }
}

// ---------------------------------------------------------------- router
async function route() {
  leave();
  const m = /^#\/review\/([A-Za-z0-9_-]+)$/.exec(location.hash);
  window.scrollTo(0, 0);
  try {
    if (!cfg) cfg = await getJSON("/ui/config");
    if (m) await renderReview(m[1]); else await renderList();
  } catch (e) {
    setView(h("div", { class: "page" }, h("div", { class: "msg bad", text: e.message || "Something went wrong. Please try again." })));
  }
}
window.addEventListener("hashchange", route);
route();
