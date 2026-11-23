"use strict";
// ---------- Adressbuch ----------
const ct = { items: [], total: 0, offset: 0 };

async function loadContacts(more) {
  const p = new URLSearchParams({ filter: $("ctFilter").value, offset: more ? ct.items.length : 0, limit: 200 });
  if ($("ctQ").value.trim()) p.set("q", $("ctQ").value.trim());
  const r = await api("contacts?" + p.toString());
  ct.items = more ? ct.items.concat(r.items) : r.items;
  ct.total = r.total;
  renderContacts();
}

function renderContacts() {
  $("ctCount").textContent = ct.total ? `(${ct.total})` : "";
  $("ctTable").innerHTML = ct.items.length ? `<thead><tr><th>Name</th><th>E-Mail</th><th>Firma</th><th>Telefon</th>
      <th class="num" title="angeschrieben / bekommen">Kontakt</th><th></th></tr></thead><tbody>` +
    ct.items.map(c => `<tr data-ct="${c.id}"><td><b>${esc(c.name || "–")}</b>${c.manual ? "" : ' <span class="tag" title="automatisch aus Mails">auto</span>'}</td>
      <td>${esc(c.email)}</td><td class="wrap">${esc(c.company)}</td><td>${esc(c.phone)}</td>
      <td class="num muted small" title="${c.sent_count}× angeschrieben, ${c.received_count}× bekommen">↗${c.sent_count} ↙${c.received_count}</td>
      <td class="num"><button class="mini" data-ctmail="${c.id}" title="Mail schreiben">✉️</button>
        <button class="mini" data-ctedit="${c.id}">Bearbeiten</button></td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="muted">${$("ctQ").value.trim() ? "Nichts gefunden." : "Noch keine Kontakte – sie sammeln sich beim Mailen von selbst."}</td></tr></tbody>`;
  $("ctMore").classList.toggle("hidden", ct.items.length >= ct.total);
  $$("#ctTable [data-ctedit]").forEach(b => b.onclick = () => openContact(ct.items.find(c => c.id === Number(b.dataset.ctedit))));
  $$("#ctTable [data-ctmail]").forEach(b => b.onclick = async () => {
    const c = ct.items.find(x => x.id === Number(b.dataset.ctmail));
    await showTab("mail");
    openCompose({ to: c.name ? `"${c.name.replace(/"/g, "'")}" <${c.email}>` : c.email });
  });
}

function openContact(c) {
  const box = $("ctForm"), isNew = !c;
  c = c || { email: "", name: "", company: "", phone: "", notes: "" };
  box.innerHTML = `<div class="toolbar" style="margin:0"><h2 style="margin:0">${isNew ? "Neuer Kontakt" : "Kontakt bearbeiten"}</h2>
      <button class="mini" data-x>✕</button></div>
    <div class="ugrid">
      <label>Name <input data-f="name" maxlength="120" value="${esc(c.name)}"></label>
      <label>E-Mail <input data-f="email" type="email" maxlength="200" value="${esc(c.email)}"></label>
      <label>Firma <input data-f="company" maxlength="120" value="${esc(c.company)}"></label>
      <label>Telefon <input data-f="phone" maxlength="60" value="${esc(c.phone)}"></label>
    </div>
    <label>Notiz <textarea data-f="notes" rows="2" maxlength="2000">${esc(c.notes)}</textarea></label>
    <div class="row"><button class="primary" data-save>${isNew ? "Anlegen" : "Speichern"}</button>
      ${isNew ? "" : '<button class="danger" data-del>Löschen</button>'}<span class="err" data-err></span></div>`;
  box.classList.remove("hidden");
  const f = (k) => box.querySelector(`[data-f="${k}"]`);
  const err = box.querySelector("[data-err]");
  box.querySelector("[data-x]").onclick = () => { box.classList.add("hidden"); box.innerHTML = ""; };
  box.querySelector("[data-save]").onclick = async () => {
    err.textContent = "";
    const body = {};
    ["name", "email", "company", "phone", "notes"].forEach(k => { body[k] = f(k).value.trim(); });
    try {
      if (isNew) await api("contacts", { method: "POST", body });
      else await api(`contacts/${c.id}`, { method: "PATCH", body });
      box.classList.add("hidden"); box.innerHTML = "";
      await loadContacts(false);
    } catch (e) { err.textContent = e.message; }
  };
  const del = box.querySelector("[data-del]");
  if (del) del.onclick = async () => {
    if (!confirm(`Kontakt ${c.name || c.email} löschen? Er wird dann auch nicht mehr automatisch aufgenommen.`)) return;
    try { await api(`contacts/${c.id}`, { method: "DELETE" }); box.classList.add("hidden"); box.innerHTML = ""; await loadContacts(false); }
    catch (e) { err.textContent = e.message; }
  };
  setTimeout(() => f(isNew ? "name" : "name").focus(), 30);
}

let ctTimer = null;
$("ctQ").addEventListener("input", () => { clearTimeout(ctTimer); ctTimer = setTimeout(() => loadContacts(false).catch(e => { $("ctErr").textContent = e.message; }), 250); });
$("ctFilter").onchange = () => loadContacts(false).catch(e => { $("ctErr").textContent = e.message; });
$("ctNew").onclick = () => openContact(null);
$("ctMore").onclick = () => loadContacts(true).catch(e => { $("ctErr").textContent = e.message; });

// ---------- Vorschläge beim Tippen von Empfängern (An, Kopie, Blindkopie) ----------
function attachSuggest(input) {
  const box = document.createElement("div");
  box.className = "suggest hidden";
  input.parentElement.appendChild(box);
  let items = [], idx = -1, timer = null, seq = 0;
  const token = () => {
    const v = input.value, pos = input.selectionStart || v.length;
    const start = v.lastIndexOf(",", pos - 1) + 1;
    return { start, end: pos, text: v.slice(start, pos).trim() };
  };
  const close = () => { box.classList.add("hidden"); items = []; idx = -1; };
  const choose = (c) => {
    const t = token(), v = input.value;
    const addr = c.name ? `"${c.name.replace(/"/g, "'")}" <${c.email}>` : c.email;
    const before = v.slice(0, t.start).replace(/\s*$/, "");
    input.value = (before ? before + " " : "") + addr + ", " + v.slice(t.end).replace(/^\s*,?\s*/, "");
    const caret = (before ? before.length + 1 : 0) + addr.length + 2;
    input.setSelectionRange(caret, caret);
    input.dispatchEvent(new Event("input"));
    close();
  };
  const render = () => {
    box.innerHTML = items.map((c, i) => `<div data-i="${i}" class="${i === idx ? "on" : ""}">${esc(c.name || c.email)}${c.name ? `<small>${esc(c.email)}</small>` : ""}${c.company ? `<small>· ${esc(c.company)}</small>` : ""}</div>`).join("");
    box.style.left = input.offsetLeft + "px"; box.style.top = (input.offsetTop + input.offsetHeight + 2) + "px";
    box.style.width = input.offsetWidth + "px";
    box.classList.toggle("hidden", !items.length);
    $$("[data-i]", box).forEach(d => d.onmousedown = (e) => { e.preventDefault(); choose(items[Number(d.dataset.i)]); });
  };
  input.addEventListener("input", (e) => {
    if (e.isTrusted === false) return;
    clearTimeout(timer);
    const t = token();
    if (t.text.length < 2 || t.text.includes("<")) { close(); return; }
    timer = setTimeout(async () => {
      const my = ++seq;
      try {
        const r = await api("contacts/suggest?q=" + encodeURIComponent(t.text));
        if (my !== seq) return;
        items = r; idx = r.length ? 0 : -1; render();
      } catch (err) { close(); }
    }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (box.classList.contains("hidden")) return;
    if (e.key === "ArrowDown") { idx = (idx + 1) % items.length; render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { idx = (idx - 1 + items.length) % items.length; render(); e.preventDefault(); }
    else if ((e.key === "Enter" || e.key === "Tab") && idx >= 0) { choose(items[idx]); e.preventDefault(); }
    else if (e.key === "Escape") { close(); e.stopPropagation(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}
["cTo", "cCc", "cBcc"].forEach(id => attachSuggest($(id)));
