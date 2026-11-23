"use strict";
// ---------- Post: Ordner | Liste | Lesen | Schreiben ----------
const mail = { accounts: [], unread: 0, folderId: null, filter: "all", q: "", items: [], offset: 0, more: false,
               current: null, msg: null, timer: null, syncPoll: null, compose: null };
const ROLE_ICON = { inbox: "📥", sent: "📤", drafts: "📝", trash: "🗑️", junk: "🚫", archive: "🗄️", all: "🗂️", "": "📁" };
const ROLE_NAME = { inbox: "Posteingang", sent: "Gesendet", drafts: "Entwürfe", trash: "Papierkorb", junk: "Spam", archive: "Archiv",
                    all: "Alle Nachrichten" };

function listDate(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "gestern";
  if (now - d < 6 * 86400000) return d.toLocaleDateString("de-DE", { weekday: "short" });
  return d.toLocaleDateString("de-DE", d.getFullYear() === now.getFullYear() ? { day: "2-digit", month: "2-digit" }
                                                                            : { day: "2-digit", month: "2-digit", year: "2-digit" });
}
const fmtBytes = (b) => b >= 1048576 ? (b / 1048576).toFixed(1).replace(".", ",") + " MB" : Math.max(1, Math.round(b / 1024)) + " KB";
const folderName = (f) => ROLE_NAME[f.role] && f.role !== "" ? ROLE_NAME[f.role] : f.name;

// --- Ordner -----------------------------------------------------------------
async function refreshFolders() {
  const r = await api("folders");
  mail.accounts = r.accounts; mail.unread = r.unified_unread;
  const badge = $("mailBadge");
  badge.textContent = mail.unread > 99 ? "99+" : String(mail.unread);
  badge.classList.toggle("hidden", !mail.unread);
  document.title = (mail.unread ? `(${mail.unread}) ` : "") + (state.title || "Postfach");
  renderTree();
  const syncing = mail.accounts.some(a => a.syncing);
  $("syncBtn").innerHTML = syncing ? '<span class="spin">↻</span> Abruf …' : "↻ Abrufen";
  if (syncing && !mail.syncPoll) {
    mail.syncPoll = setTimeout(async () => {
      mail.syncPoll = null;
      try { await refreshFolders(); if (!mail.offset) await loadList(false, true); } catch (e) { /* egal */ }
    }, 2500);
  }
}

function renderTree() {
  const multi = mail.accounts.length > 1;
  let h = "";
  if (!mail.accounts.length) {
    h = `<p class="muted small" style="padding:8px">Noch kein Mailkonto eingerichtet.</p>
         <button class="mini" onclick="showTab('accounts')" style="margin:0 8px">＋ Konto hinzufügen</button>`;
  } else {
    h += `<a data-folder="" class="${mail.folderId === null && !mail.q ? "on" : ""}"><span class="ic">📥</span>
          ${multi ? "Alle Posteingänge" : "Posteingang"}${mail.unread ? `<span class="n">${mail.unread}</span>` : ""}</a>`;
    for (const a of mail.accounts) {
      const st = a.syncing ? '<span class="st spin" title="wird abgerufen">↻</span>' : a.last_error ? '<span class="st" style="color:var(--bad)" title="Abruffehler">⚠</span>' : "";
      h += `<h4><span class="dot" style="background:${esc(a.color)}"></span><span title="${esc(a.email)}">${esc(a.name)}</span>${st}</h4>`;
      if (a.last_error) h += `<div class="accerr" data-accerr="${a.id}" title="${esc(a.last_error)}">⚠ ${esc(a.last_error.slice(0, 80))}</div>`;
      if (!a.folders.length && !a.last_error) h += '<div class="muted small" style="padding:2px 8px">wird eingerichtet …</div>';
      for (const f of a.folders) {
        if (f.role === "inbox" && !multi) continue;  // steht schon oben
        const depth = f.role ? 0 : (f.name.split(f.delimiter || "/").length - 1);
        const label = f.role ? folderName(f) : f.name.split(f.delimiter || "/").pop();
        h += `<a data-folder="${f.id}" class="${mail.folderId === f.id ? "on" : ""} ${depth ? "sub" : ""}" style="${depth > 1 ? `padding-left:${8 + depth * 14}px` : ""}">
          <span class="ic">${ROLE_ICON[f.role] || "📁"}</span><span>${esc(label)}</span>${f.unread && !["sent", "trash", "junk", "all"].includes(f.role) ? `<span class="n">${f.unread}</span>` : ""}</a>`;
      }
    }
  }
  $("tree").innerHTML = h;
  $$("#tree [data-folder]").forEach(el => el.onclick = () => {
    mail.folderId = el.dataset.folder ? Number(el.dataset.folder) : null;
    mail.q = ""; $("q").value = "";
    $("mailPage").classList.add("collapsed");
    renderTree(); loadList(false).catch(showListErr);
  });
  $$("#tree [data-accerr]").forEach(el => el.onclick = () => showTab("accounts"));
}

function currentTitle() {
  if (mail.q) return `Suche: „${mail.q}“`;
  if (mail.folderId === null) return mail.accounts.length > 1 ? "Alle Posteingänge" : "Posteingang";
  for (const a of mail.accounts) {
    const f = a.folders.find(x => x.id === mail.folderId);
    if (f) return (mail.accounts.length > 1 ? a.name + " · " : "") + (f.role ? folderName(f) : f.name);
  }
  return "Ordner";
}

// --- Liste ------------------------------------------------------------------
function showListErr(e) { $("items").innerHTML = `<div class="none err">${esc(e.message)}</div>`; }

async function loadList(more, quiet) {
  const p = new URLSearchParams();
  if (mail.folderId !== null) p.set("folder_id", mail.folderId);
  if (mail.q) p.set("q", mail.q);
  if (mail.filter === "unread") p.set("unread", "1");
  if (mail.filter === "flagged") p.set("flagged", "1");
  p.set("offset", more ? mail.offset + 50 : 0);
  $("listTitle").textContent = currentTitle();
  const r = await api("messages?" + p.toString());
  mail.offset = r.offset;
  mail.items = more ? mail.items.concat(r.items) : r.items;
  mail.more = r.more;
  renderList(quiet);
}

function renderList() {
  const multi = mail.accounts.length > 1;
  if (!mail.items.length) {
    $("items").innerHTML = `<div class="none">${mail.q ? "Nichts gefunden." : mail.filter !== "all" ? "Keine Mails mit diesem Filter."
      : mail.accounts.length ? "Keine Mails in den letzten Wochen." : "Richte zuerst ein Mailkonto ein."}</div>`;
    return;
  }
  const sentView = mail.items.every(i => i.folder_role === "sent" || i.folder_role === "drafts");
  $("items").innerHTML = mail.items.map(i => {
    const who = sentView ? "An: " + (i.to_addrs.replace(/"/g, "").split(/[<,]/)[0].trim() || i.to_addrs) : (i.from_name || i.from_addr);
    return `<div class="item ${i.seen ? "" : "unread"} ${i.id === mail.current ? "on" : ""}" data-id="${i.id}">
      <span class="bar" style="background:${multi ? esc(i.account_color) : "transparent"}"></span>
      <span class="who" title="${esc(i.from_addr)}">${esc(who)}</span>
      <span class="when">${listDate(i.date)}<span class="marks">${i.has_attachments ? " 📎" : ""}${i.answered ? " ↩" : ""}${i.flagged ? ' <span class="star">★</span>' : ""}</span></span>
      <span class="subj">${esc(i.subject || "(kein Betreff)")}</span>
      <span class="snip">${esc(i.snippet || "")}</span></div>`;
  }).join("") + (mail.more ? '<button class="more" id="moreBtn">Ältere laden</button>' : "");
  $$("#items .item").forEach(el => el.onclick = () => openMessage(Number(el.dataset.id)));
  if (mail.more) $("moreBtn").onclick = () => loadList(true).catch(showListErr);
}

// --- Lesen ------------------------------------------------------------------
function textToHtml(text) {
  return esc(text).split("\n").map(line => {
    const linked = line.replace(/(https?:\/\/[^\s<>"]+[^\s<>".,;:!?)\]])/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
    return /^\s*&gt;/.test(line) ? `<span class="q">${linked}</span>` : linked;
  }).join("\n");
}

function readerEmpty() {
  $("reader").innerHTML = '<div class="rempty"><div class="big">✉️</div>Keine Nachricht ausgewählt</div>';
  $("mailPage").classList.remove("reading");
}

async function openMessage(id, images) {
  mail.current = id;
  $$("#items .item").forEach(el => el.classList.toggle("on", Number(el.dataset.id) === id));
  $("mailPage").classList.add("reading");
  $("reader").innerHTML = '<div class="rempty"><div class="big"><span class="spin">↻</span></div>wird geladen …</div>';
  let m;
  try { m = await api(`messages/${id}${images ? "?images=1" : ""}`); }
  catch (e) {
    $("reader").innerHTML = `<div class="rempty"><div class="big">⚠️</div>${esc(e.message)}<br><br><button class="back" onclick="readerEmpty()">‹ Zurück</button></div>`;
    return;
  }
  if (mail.current !== id) return;  // inzwischen eine andere geöffnet
  mail.msg = m;
  const item = mail.items.find(i => i.id === id);
  if (item && !item.seen) {
    item.seen = true;
    const el = document.querySelector(`#items .item[data-id="${id}"]`);
    if (el) el.classList.remove("unread");
    refreshFolders().catch(() => {});
  }
  const row = (k, v) => v ? `<span class="k">${k}</span><span class="v">${esc(v)}</span><span></span>` : "";
  $("reader").innerHTML = `<div class="rhead">
      <div class="acts"><button class="back" id="rBack">‹ Zurück</button>
        ${m.is_draft ? '<button class="primary" id="rEdit">✏️ Weiter bearbeiten</button>' : `<button id="rReply">↩ Antworten</button>
        <button id="rAll">↩↩ Allen</button><button id="rFwd">↪ Weiterleiten</button>`}
        <button id="rDel" title="${m.delete_permanent ? "endgültig löschen" : "in den Papierkorb (Entf)"}">🗑${m.delete_permanent ? " Endgültig" : ""}</button>
        <select id="rMove" title="in einen anderen Ordner verschieben" class="movesel"><option value="">📁 Verschieben …</option>${moveOptions(m)}</select>
        <button id="rStar" title="markieren">${m.flagged ? "★" : "☆"}</button>
        <button id="rUnread" title="als ungelesen markieren">●</button></div>
      <h2>${esc(m.subject || "(kein Betreff)")}</h2>
      <div class="from"><span class="av" style="background:${esc((mail.accounts.find(a => a.id === m.account_id) || {}).color || "")}">${esc(initials(m.from_name || m.from_addr))}</span>
        <div><b>${esc(m.from_name || m.from_addr)}</b> <span class="muted small">${m.from_name ? "&lt;" + esc(m.from_addr) + "&gt;" : ""}</span><br>
        <span class="muted small">${fmtDT(m.date)} · ${esc(m.account_name)}${m.folder && m.folder.role !== "inbox" ? " · " + esc(m.folder.role ? folderName(m.folder) : m.folder.name) : ""}</span></div></div>
      <div class="meta">${row("An", m.to_addrs)}${row("Kopie", m.cc_addrs)}${m.reply_to && m.reply_to !== m.from_addr ? row("Antwort an", m.reply_to) : ""}</div>
      ${m.attachments.length ? `<div class="atts">${m.attachments.map(a => `<button class="att" data-att="${a.id}" title="${esc(a.filename)} herunterladen">📎 <span>${esc(a.filename)}</span><small>${fmtBytes(a.size)}</small></button>`).join("")}</div>` : ""}
    </div>
    ${m.remote_blocked ? '<div class="remote">🛡️ Externe Bilder wurden blockiert (Schutz vor Tracking). <button class="mini" id="rImages">Bilder laden</button></div>' : ""}
    <div class="rbody" id="rBody"></div>`;
  const body = $("rBody");
  if (m.html) {
    const frame = document.createElement("iframe");
    // Kein allow-scripts, kein allow-same-origin: die Mail kann nichts ausführen und nicht auf Postfach zugreifen
    frame.setAttribute("sandbox", "allow-popups allow-popups-to-escape-sandbox");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.title = "Inhalt der Mail";
    frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8">
      <meta http-equiv="Content-Security-Policy" content="${esc(m.csp)}">
      <style>body{font:14.5px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:18px 22px;color:#1d2330;background:#fff;
      word-wrap:break-word;overflow-wrap:anywhere}img{max-width:100%;height:auto}img[data-blocked]{display:none}
      table{max-width:100%}pre{white-space:pre-wrap}blockquote{border-left:3px solid #ccc;margin:0 0 0 4px;padding-left:10px;color:#555}</style>
      </head><body>${m.html}</body></html>`;
    body.appendChild(frame);
  } else {
    body.innerHTML = `<pre>${textToHtml(m.text)}</pre>`;
  }
  $("rBack").onclick = readerEmpty;
  if (m.is_draft) $("rEdit").onclick = () => openDraft(id, "edit");
  else {
    $("rReply").onclick = () => openDraft(id, "reply");
    $("rAll").onclick = () => openDraft(id, "replyall");
    $("rFwd").onclick = () => openDraft(id, "forward");
  }
  $("rDel").onclick = () => deleteMessage(id);
  $("rMove").onchange = () => { const f = Number($("rMove").value); if (f) moveMessage(id, f); };
  $("rStar").onclick = () => setFlags(id, { flagged: !m.flagged });
  $("rUnread").onclick = async () => { await setFlags(id, { seen: false }); readerEmpty(); mail.current = null; renderList(); };
  if ($("rImages")) $("rImages").onclick = () => openMessage(id, true);
  $$("#reader [data-att]").forEach(b => b.onclick = () =>
    downloadFile(`api/v1/messages/${id}/attachments/${b.dataset.att}`, b.querySelector("span").textContent)
      .catch(e => alert(e.message)));
}

function moveOptions(m) {
  const a = mail.accounts.find(x => x.id === m.account_id);
  if (!a) return "";
  return a.folders.filter(f => !m.folder || f.id !== m.folder.id)
    .map(f => `<option value="${f.id}">${esc(f.role ? folderName(f) : f.name)}</option>`).join("");
}

// Nach Löschen/Verschieben: aus der Liste nehmen und die nächste Mail zeigen (wie in Outlook)
function dropFromList(id) {
  const idx = mail.items.findIndex(i => i.id === id);
  if (idx >= 0) mail.items.splice(idx, 1);
  const next = mail.items[idx] || mail.items[idx - 1];
  renderList();
  refreshFolders().catch(() => {});
  const phone = window.matchMedia && window.matchMedia("(max-width: 720px)").matches;
  if (next && !phone) openMessage(next.id);   // am Handy zurück zur Liste
  else { mail.current = null; readerEmpty(); }
}

async function deleteMessage(id) {
  const m = mail.msg && mail.msg.id === id ? mail.msg : null;
  if (m && m.delete_permanent && !confirm("Diese Mail endgültig löschen? Das lässt sich nicht rückgängig machen.")) return;
  try { await api(`messages/${id}/delete`, { method: "POST" }); dropFromList(id); }
  catch (e) { alert(e.message); }
}

async function moveMessage(id, folderId) {
  try { await api(`messages/${id}/move`, { method: "POST", body: { folder_id: folderId } }); dropFromList(id); }
  catch (e) { alert(e.message); if ($("rMove")) $("rMove").value = ""; }
}

async function setFlags(id, flags) {
  try {
    const r = await api(`messages/${id}/flags`, { method: "POST", body: flags });
    const item = mail.items.find(i => i.id === id);
    if (item) Object.assign(item, r);
    if (mail.msg && mail.msg.id === id) { Object.assign(mail.msg, r); if ($("rStar")) $("rStar").textContent = r.flagged ? "★" : "☆"; }
    renderList();
    refreshFolders().catch(() => {});
  } catch (e) { alert(e.message); }
}

// --- Schreiben --------------------------------------------------------------
function ownAccounts() { return mail.accounts.filter(a => a.active); }

function openCompose(d) {
  const accts = ownAccounts();
  if (!accts.length) { alert("Bitte zuerst unter „Konten“ ein Mailkonto einrichten."); return; }
  d = d || {};
  const defAcc = d.account_id || (mail.folderId !== null ? (mail.accounts.find(a => a.folders.some(f => f.id === mail.folderId)) || {}).id : null)
                 || accts[0].id;
  mail.compose = { reply_to_id: d.reply_to_id || null, forward_of: d.forward_of || null, in_reply_to: d.in_reply_to || "",
                   references: d.references || "", files: (d.files || []).slice(),
                   fwd: (d.forward_attachments || []).map(a => ({ ...a, on: true })),
                   sigFor: null, draftId: d.draft_message_id || "", dirty: false, saving: null, noDrafts: false };
  $("cFrom").innerHTML = accts.map(a => `<option value="${a.id}">${esc(a.name)} &lt;${esc(a.email)}&gt;</option>`).join("");
  $("cFrom").value = String(defAcc);
  $("cTo").value = d.to || ""; $("cCc").value = d.cc || ""; $("cBcc").value = d.bcc || "";
  $("cSubject").value = d.subject || "";
  $("cBody").value = d.body !== undefined ? d.body : "";
  $("cTitle").textContent = d.draft_message_id ? "Entwurf" : d.forward_of ? "Weiterleiten" : d.reply_to_id ? "Antworten" : "Neue Mail";
  $("cErr").textContent = ""; $("cDraftInfo").textContent = d.draft_message_id ? "aus den Entwürfen" : "";
  $("cDiscard").classList.toggle("hidden", !d.draft_message_id);
  clearInterval(mail.draftTimer);
  mail.draftTimer = setInterval(() => { if (mail.compose && mail.compose.dirty) saveDraft().catch(() => {}); }, 30000);
  if (d.body === undefined) applySignature();
  renderCAtts();
  $("composeModal").classList.remove("hidden");
  setTimeout(() => { const el = $("cTo").value ? $("cBody") : $("cTo"); el.focus(); if (el === $("cBody")) el.setSelectionRange(0, 0); }, 30);
}

// Signatur des gewählten Kontos (nur bei neuen Mails; bei Antworten setzt der Server sie schon ein)
let signatures = {};
async function applySignature() {
  try {
    if (!Object.keys(signatures).length) (await api("accounts")).forEach(a => { signatures[a.id] = a.signature || ""; });
  } catch (e) { return; }
  const sig = signatures[Number($("cFrom").value)] || "";
  const body = $("cBody").value;
  const old = mail.compose && mail.compose.sigFor !== null ? (signatures[mail.compose.sigFor] || "") : "";
  const oldBlock = old.trim() ? "\n\n-- \n" + old.trim() : "";
  const stripped = oldBlock && body.endsWith(oldBlock) ? body.slice(0, -oldBlock.length) : body;
  $("cBody").value = stripped + (sig.trim() ? "\n\n-- \n" + sig.trim() : "");
  if (mail.compose) mail.compose.sigFor = Number($("cFrom").value);
}

function renderCAtts() {
  const c = mail.compose;
  $("cAtts").innerHTML = c.fwd.map((a, i) => `<span class="chip ${a.on ? "" : "off"}">📎 ${esc(a.filename)} <small class="muted">${fmtBytes(a.size)}</small>
      <button data-fwd="${i}" title="${a.on ? "nicht mitschicken" : "wieder mitschicken"}">${a.on ? "✕" : "↺"}</button></span>`).join("") +
    c.files.map((f, i) => `<span class="chip">📎 ${esc(f.name)} <small class="muted">${fmtBytes(f.size)}</small>
      <button data-file="${i}" title="entfernen">✕</button></span>`).join("");
  $$("#cAtts [data-fwd]").forEach(b => b.onclick = () => { const a = c.fwd[Number(b.dataset.fwd)]; a.on = !a.on; markDirty(); renderCAtts(); });
  $$("#cAtts [data-file]").forEach(b => b.onclick = () => { c.files.splice(Number(b.dataset.file), 1); markDirty(); renderCAtts(); });
}

$("cFile").onchange = () => {
  const files = Array.from($("cFile").files);
  $("cFile").value = "";
  for (const f of files) {
    if (f.size > 25 * 1024 * 1024) { $("cErr").textContent = `„${f.name}“ ist größer als 25 MB`; continue; }
    const rd = new FileReader();
    rd.onload = () => { mail.compose.files.push({ name: f.name, mime: f.type || "application/octet-stream", size: f.size,
                                                  data_b64: String(rd.result).split(",", 2)[1] || "" }); markDirty(); renderCAtts(); };
    rd.readAsDataURL(f);
  }
};
$("cFrom").onchange = () => { if (!mail.compose.reply_to_id && !mail.compose.forward_of && !mail.compose.draftId) applySignature(); markDirty(); };

// --- Entwürfe: alle 30 Sekunden (wenn sich etwas geändert hat) im Entwürfe-Ordner des Kontos speichern ---
function markDirty() { if (mail.compose) mail.compose.dirty = true; }
["cTo", "cCc", "cBcc", "cSubject", "cBody"].forEach(id => $(id).addEventListener("input", markDirty));

function composeBody() {
  const c = mail.compose;
  return { account_id: Number($("cFrom").value), to: $("cTo").value, cc: $("cCc").value, bcc: $("cBcc").value,
           subject: $("cSubject").value, body: $("cBody").value,
           attachments: c.files.map(f => ({ name: f.name, mime: f.mime, data_b64: f.data_b64 })),
           reply_to_id: c.reply_to_id, forward_of: c.forward_of, forward_attachment_ids: c.fwd.filter(a => a.on).map(a => a.id),
           in_reply_to: c.in_reply_to, references: c.references, draft_message_id: c.draftId };
}

async function saveDraft() {
  const c = mail.compose;
  if (!c || c.noDrafts) return;
  if (c.saving) return c.saving;           // nicht doppelt speichern
  c.dirty = false;
  $("cDraftInfo").textContent = "Entwurf wird gespeichert …";
  c.saving = api("drafts", { method: "POST", body: composeBody() }).then(r => {
    c.draftId = r.draft_message_id;
    $("cDraftInfo").textContent = "Entwurf gespeichert " + new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    $("cDiscard").classList.remove("hidden");
  }).catch(e => {
    c.dirty = true;
    if (/Entwürfe-Ordner/.test(e.message)) { c.noDrafts = true; $("cDraftInfo").textContent = "kein Entwürfe-Ordner – wird nicht zwischengespeichert"; }
    else $("cDraftInfo").textContent = "Entwurf nicht gespeichert: " + e.message;
    throw e;
  }).finally(() => { c.saving = null; });
  return c.saving;
}

function closeCompose() {
  clearInterval(mail.draftTimer);
  $("composeModal").classList.add("hidden");
  mail.compose = null;
}

$("cClose").onclick = async () => {
  const c = mail.compose;
  const typed = $("cBody").value.replace(/\n*-- \n[\s\S]*$/, "").trim() || $("cSubject").value.trim() || $("cTo").value.trim();
  if (c && c.dirty && typed && !c.noDrafts) {
    // Ungespeicherte Änderungen: schnell als Entwurf sichern, dann schließen
    try { await saveDraft(); } catch (e) { if (!confirm("Der Entwurf konnte nicht gespeichert werden. Trotzdem schließen?")) return; }
  } else if (c && c.dirty && typed && !confirm("Mail verwerfen?")) return;
  const saved = c && c.draftId;
  closeCompose();
  if (saved) refreshFolders().catch(() => {});
};
$("cDiscard").onclick = async () => {
  const c = mail.compose;
  if (!confirm("Entwurf verwerfen? Er wird auch im Entwürfe-Ordner gelöscht.")) return;
  if (c && c.saving) { try { await c.saving; } catch (e) { /* egal */ } }
  if (c && c.draftId) {
    try { await api("drafts/delete", { method: "POST", body: { account_id: Number($("cFrom").value), draft_message_id: c.draftId } }); }
    catch (e) { alert(e.message); return; }
  }
  closeCompose();
  refreshFolders().catch(() => {});
  if (mail.msg && mail.msg.is_draft) { mail.current = null; readerEmpty(); loadList(false).catch(() => {}); }
};
$("cSend").onclick = async () => {
  const c = mail.compose;
  $("cErr").textContent = "";
  if (!$("cSubject").value.trim() && !confirm("Ohne Betreff senden?")) return;
  $("cSend").disabled = true; $("cSend").textContent = "Wird gesendet …";
  try {
    if (c.saving) { try { await c.saving; } catch (e) { /* egal */ } }   // laufendes Speichern abwarten (Entwurf wird danach gelöscht)
    const r = await api("send", { method: "POST", body: composeBody() });
    const wasDraft = mail.msg && mail.msg.is_draft;
    closeCompose();
    if (wasDraft) { mail.current = null; readerEmpty(); loadList(false).catch(() => {}); }
    if (r.warning) alert(r.warning);
    if (c.reply_to_id) { const it = mail.items.find(i => i.id === c.reply_to_id); if (it) { it.answered = true; renderList(); } }
  } catch (e) { $("cErr").textContent = e.message; }
  finally { $("cSend").disabled = false; $("cSend").textContent = "Senden"; }
};
// beim Verlassen der Seite: offenen Entwurf noch sichern (best effort)
window.addEventListener("beforeunload", () => { if (mail.compose && mail.compose.dirty && !mail.compose.noDrafts) saveDraft().catch(() => {}); });

async function openDraft(id, mode) {
  try { openCompose(await api(`messages/${id}/draft?mode=${mode}`)); }
  catch (e) { alert(e.message); }
}

// --- Start und Aktualisierung -----------------------------------------------
async function loadMail() {
  await refreshFolders();
  await loadList(false).catch(showListErr);
}

// ---------- Neue Mails: alle 20 Sekunden nachsehen, bei Bedarf Browser-Hinweis ----------
const notify = {
  supported: () => "Notification" in window,
  on: () => store.get("pf_notify") === "1" && "Notification" in window && Notification.permission === "granted",
};
mail.lastId = null;

function updateNotifyBtn() {
  const b = $("notifyBtn");
  b.textContent = notify.on() ? "🔔" : "🔕";
  b.title = notify.on() ? "Benachrichtigung ist an (klicken zum Ausschalten)" : "Benachrichtigung bei neuer Mail einschalten";
  if ($("meNotify")) $("meNotify").checked = notify.on();
}

async function setNotify(on) {
  if (!on) { store.set("pf_notify", "0"); updateNotifyBtn(); return true; }
  if (!notify.supported()) throw new Error("Dieser Browser kann keine Benachrichtigungen anzeigen.");
  if (!window.isSecureContext) throw new Error("Benachrichtigungen gehen nur über https:// (z. B. im DSM-Fenster oder über den Reverse Proxy).");
  const perm = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
  if (perm !== "granted") throw new Error("Der Browser hat Benachrichtigungen blockiert – in den Website-Einstellungen erlauben.");
  store.set("pf_notify", "1"); updateNotifyBtn();
  return true;
}

function showNotification(items) {
  if (!notify.on() || (!document.hidden && state.tab === "mail" && document.hasFocus())) return;
  const list = items.slice(0, 3);
  for (const i of list) {
    try {
      const n = new Notification(i.from_name || i.from_addr || "Neue Mail", {
        body: (i.subject || "(kein Betreff)") + (mail.accounts.length > 1 ? "\n" + i.account_name : ""),
        tag: "postfach-" + i.id, icon: url("static/icon.png") });
      n.onclick = () => { window.focus(); n.close(); showTab("mail").then(() => openMessage(i.id)); };
    } catch (e) { /* manche Browser erlauben das nur im Service Worker – dann eben nur der Zähler */ }
  }
  if (items.length > list.length) {
    try { new Notification(`${items.length} neue Mails`, { tag: "postfach-sammel", icon: url("static/icon.png") }); } catch (e) { /* egal */ }
  }
}

async function checkNew() {
  const r = await api("new" + (mail.lastId !== null ? "?after_id=" + mail.lastId : ""));
  const first = mail.lastId === null;
  mail.lastId = r.last_id;
  if (first || !r.items.length) return false;
  showNotification(r.items);
  return true;
}

function startMailPolling() {
  if (mail.timer) return;
  updateNotifyBtn();
  checkNew().catch(() => {});
  setInterval(async () => {
    try {
      if (await checkNew()) {
        await refreshFolders();
        if (state.tab === "mail" && !mail.offset && !mail.q) await loadList(false, true);
      }
    } catch (e) { /* offline – nächstes Mal */ }
  }, 20000);
  refreshFolders().catch(() => {});
  mail.timer = setInterval(async () => {
    if (document.hidden) return;
    try {
      const before = mail.unread;
      await refreshFolders();
      if (state.tab === "mail" && !mail.offset && (mail.unread !== before || mail.accounts.some(a => a.syncing))) await loadList(false, true);
    } catch (e) { /* offline o. ä. – nächstes Mal */ }
  }, 60000);
}

let qTimer = null;
$("q").addEventListener("input", () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { mail.q = $("q").value.trim(); renderTree(); loadList(false).catch(showListErr); }, 300);
});
$$(".mlist .filters [data-filter]").forEach(b => b.onclick = () => {
  mail.filter = b.dataset.filter;
  $$(".mlist .filters [data-filter]").forEach(x => x.classList.toggle("on", x === b));
  loadList(false).catch(showListErr);
});
$("composeBtn").onclick = () => openCompose();
$("syncBtn").onclick = async () => {
  try { await api("sync", { method: "POST" }); await refreshFolders(); } catch (e) { alert(e.message); }
};
$("treeToggle").onclick = () => $("mailPage").classList.toggle("collapsed");
$("notifyBtn").onclick = async () => {
  try { await setNotify(!notify.on()); } catch (e) { alert(e.message); }
};
$("meNotify").onchange = async () => {
  $("meNotifyErr").textContent = "";
  try { await setNotify($("meNotify").checked); } catch (e) { $("meNotifyErr").textContent = e.message; $("meNotify").checked = false; }
};
$("meNotifyTest").onclick = async () => {
  $("meNotifyErr").textContent = "";
  try {
    await setNotify(true);
    new Notification("Postfach", { body: "So sieht ein Hinweis auf neue Mail aus.", icon: url("static/icon.png") });
  } catch (e) { $("meNotifyErr").textContent = e.message; }
};
$("mailPage").classList.add("collapsed");
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("composeModal").classList.contains("hidden")) { $("cClose").click(); return; }
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "");
  if (e.key === "Delete" && !typing && state.tab === "mail" && mail.current && $("composeModal").classList.contains("hidden")) {
    deleteMessage(mail.current);
  }
});
