"use strict";
// ---------- Mailkonten: eigene (Reiter „Konten“) und alle (Verwaltung → Mailkonten) ----------
const acc = { mine: [], all: [] };
const COLORS = ["#0f766e", "#2563eb", "#7c3aed", "#c2410c", "#be123c", "#15803d", "#0891b2", "#a16207"];

function accStatus(a) {
  if (!a.active) return '<span class="st">ausgeschaltet</span>';
  if (a.syncing) return '<span class="st"><span class="spin">↻</span> wird abgerufen …</span>';
  if (a.last_error) return `<span class="st bad">⚠ ${esc(a.last_error)}</span>`;
  if (!a.password_set) return '<span class="st bad">⚠ Kein Passwort gespeichert – bitte neu eintragen</span>';
  const push = a.push === "wartet" ? " · ⚡ neue Mails kommen sofort (IDLE)"
    : a.push === "kein IDLE" ? ` · der Server meldet neue Mails nicht selbst – Abruf alle ${(state.settings && state.settings.sync_interval) || 5} Minuten` : "";
  return `<span class="st">${a.last_sync ? "zuletzt abgerufen " + fmtDT(a.last_sync) : "noch nicht abgerufen"}${push}</span>`;
}

function accRows(list, withOwner) {
  if (!list.length) return '<p class="muted">Noch keine Mailkonten.</p>';
  return list.map(a => `<div class="acc" data-acc="${a.id}">
      <span class="bar" style="background:${esc(a.color)}"></span>
      <div><b>${esc(a.name)}</b> <span class="muted small">${esc(a.email)}</span>${a.auth_type === "oauth_ms" ? ' <span class="tag">Microsoft 365</span>' : ""}${withOwner ? ` · <span class="tag">${esc(a.owner)}</span>` : ""}</div>
      <div class="row" style="gap:6px"><button class="mini" data-sync="${a.id}" ${a.active ? "" : "disabled"}>↻ Abrufen</button>
        <button class="mini" data-edit="${a.id}">Bearbeiten</button></div>
      ${accStatus(a)}</div>`).join("");
}

async function loadAccounts() {
  acc.mine = await api("accounts");
  const mayAdd = isAdmin() || state.me.users_add_accounts !== false;
  $("accNew").classList.toggle("hidden", !mayAdd);
  $("accMs").classList.toggle("hidden", !mayAdd || !(state.settings && state.settings.ms_ready));
  $("accNoAdd").classList.toggle("hidden", mayAdd);
  $("accNoAdd").textContent = mayAdd ? "" : "Mailkonten richtet bei euch der Administrator ein.";
  $("accList").innerHTML = accRows(acc.mine, false);
  bindAccRows($("accList"), acc.mine, "accForm", "accErr", loadAccounts);
  if (!acc.mine.length && mayAdd && $("accForm").classList.contains("hidden")) openAccForm("accForm", null, loadAccounts);
  if (acc.mine.some(a => a.syncing)) setTimeout(() => { if (state.tab === "accounts") loadAccounts().catch(() => {}); }, 3000);
  if (window.loadRules && !acc.rulesLoaded) { acc.rulesLoaded = true; loadRules().catch(e => { $("ruleErr").textContent = e.message; }); }
}

async function loadAllAccounts() {
  acc.all = await api("accounts?all=1");
  $("aaMs").classList.toggle("hidden", !(state.settings && state.settings.ms_ready));
  $("aaList").innerHTML = accRows(acc.all, true);
  bindAccRows($("aaList"), acc.all, "aaForm", "aaErr", loadAllAccounts);
  if (acc.all.some(a => a.syncing)) setTimeout(() => { if (state.tab === "admin") loadAllAccounts().catch(() => {}); }, 3000);
}

function bindAccRows(root, list, formId, errId, reload) {
  $$("[data-edit]", root).forEach(b => b.onclick = () => openAccForm(formId, list.find(a => a.id === Number(b.dataset.edit)), reload));
  $$("[data-sync]", root).forEach(b => b.onclick = async () => {
    $(errId).textContent = "";
    try { await api(`accounts/${b.dataset.sync}/sync`, { method: "POST" }); setTimeout(() => reload().catch(() => {}), 600); }
    catch (e) { $(errId).textContent = e.message; }
  });
}

function openAccForm(formId, a, reload) {
  const box = $(formId), isNew = !a, adminForm = formId === "aaForm";
  a = a || { name: "", email: "", from_name: state.me.display_name || "", imap_host: "", imap_port: 993, imap_security: "ssl",
             imap_user: "", smtp_host: "", smtp_port: 587, smtp_security: "starttls", smtp_user: "", signature: "",
             sync_days: 90, color: COLORS[(acc.mine.length + acc.all.length) % COLORS.length], active: true, password_set: false };
  const users = adminForm ? (state.users || []).filter(u => u.active) : [];
  box.innerHTML = `<div class="toolbar"><h2>${isNew ? "Mailkonto hinzufügen" : "Konto „" + esc(a.name) + "“ bearbeiten"}</h2>
      <button class="mini" data-x>✕</button></div>
    <div class="setgrid">
      <fieldset><legend>Konto</legend>
        ${adminForm ? `<label>Gehört zu <select data-f="owner_id">${users.map(u => `<option value="${u.id}" ${u.id === a.owner_id ? "selected" : ""}>${esc(u.display_name)} (${esc(u.username)})</option>`).join("")}</select></label>` : ""}
        <label>E-Mail-Adresse <input data-f="email" type="email" value="${esc(a.email)}" placeholder="info@praxis.de" autocomplete="off"></label>
        <label>Passwort <input data-f="imap_password" type="password" autocomplete="new-password"
          placeholder="${a.password_set ? "gespeichert – leer = unverändert" : ""}"></label>
        <div class="hint hidden" data-hint></div>
        <label>Name des Kontos (für dich) <input data-f="name" value="${esc(a.name)}" placeholder="z. B. Praxis" maxlength="60"></label>
        <label>Absendername <input data-f="from_name" value="${esc(a.from_name)}" maxlength="100"></label>
        <div class="row"><label>Farbe <input data-f="color" type="color" value="${esc(a.color)}"></label>
          <label>Zwischenspeichern (Tage) <input data-f="sync_days" type="number" min="7" max="3650" class="w80" value="${a.sync_days}"></label>
          <label class="inline"><input data-f="active" type="checkbox" ${a.active ? "checked" : ""}> abrufen</label></div>
      </fieldset>
      <fieldset><legend>Posteingang (IMAP)</legend>
        <label>Server <input data-f="imap_host" value="${esc(a.imap_host)}" placeholder="imap.beispiel.de"></label>
        <div class="row"><label>Port <input data-f="imap_port" type="number" class="w80" value="${a.imap_port}"></label>
          <label style="flex:1">Verschlüsselung <select data-f="imap_security"><option value="ssl">SSL/TLS (993)</option>
            <option value="starttls">STARTTLS (143)</option></select></label></div>
        <label>Benutzername <input data-f="imap_user" value="${esc(a.imap_user)}" placeholder="meist die E-Mail-Adresse" autocomplete="off"></label>
      </fieldset>
      <fieldset><legend>Postausgang (SMTP)</legend>
        <label>Server <input data-f="smtp_host" value="${esc(a.smtp_host)}" placeholder="smtp.beispiel.de"></label>
        <div class="row"><label>Port <input data-f="smtp_port" type="number" class="w80" value="${a.smtp_port}"></label>
          <label style="flex:1">Verschlüsselung <select data-f="smtp_security"><option value="starttls">STARTTLS (587)</option>
            <option value="ssl">SSL/TLS (465)</option><option value="none">keine (nur im LAN)</option></select></label></div>
        <label>Benutzername <input data-f="smtp_user" value="${esc(a.smtp_user)}" placeholder="leer = wie Posteingang" autocomplete="off"></label>
        <label>Eigenes Passwort <input data-f="smtp_password" type="password" autocomplete="new-password" placeholder="leer = wie Posteingang"></label>
      </fieldset>
      <fieldset><legend>Signatur</legend>
        <textarea data-f="signature" rows="5" maxlength="5000" placeholder="Viele Grüße&#10;…">${esc(a.signature)}</textarea>
        <p class="muted small">Wird beim Schreiben unter die Mail gesetzt (Trennlinie „-- “ kommt automatisch).</p>
      </fieldset>
    </div>
    <div class="testres" data-test></div>
    <div class="savebar"><button class="primary" data-save>${isNew ? "Konto hinzufügen" : "Speichern"}</button>
      <button data-testbtn>Verbindung testen</button>
      ${isNew ? "" : '<button class="danger" data-del>Konto entfernen</button>'}
      <span class="err" data-err></span></div>
    <p class="muted small">Das Passwort wird verschlüsselt auf der NAS gespeichert und nicht in Datensicherungen übernommen.
      Beim Entfernen bleiben die Mails am Server unberührt.</p>`;
  box.classList.remove("hidden");
  const f = (k) => box.querySelector(`[data-f="${k}"]`);
  f("imap_security").value = a.imap_security; f("smtp_security").value = a.smtp_security;
  if (a.auth_type === "oauth_ms") {
    // Microsoft 365: Server und Anmeldung kommen von Microsoft – statt Passwort „Neu verbinden“
    $$("fieldset", box).forEach(fs => { if (/Posteingang|Postausgang/.test(fs.querySelector("legend").textContent)) fs.classList.add("hidden"); });
    f("imap_password").closest("label").outerHTML = `<div class="hint">Meldet sich über <b>Microsoft 365</b> an.
      <button class="mini" data-msre>Neu verbinden</button><div class="mscode hidden" data-msbox style="margin-top:8px"></div></div>`;
    f("email").disabled = true;
    box.querySelector("[data-msre]").onclick = (ev) => { ev.preventDefault(); msConnect(box.querySelector("[data-msbox]"), { account_id: a.id }, reload); };
  }
  const err = box.querySelector("[data-err]");
  box.querySelector("[data-x]").onclick = () => { box.classList.add("hidden"); box.innerHTML = ""; };
  // Server vorschlagen, sobald die Adresse eingetragen ist (nur leere Felder füllen bzw. bei neuen Konten)
  f("email").addEventListener("change", async () => {
    const email = f("email").value.trim();
    if (!email.includes("@")) return;
    try {
      const s = await api("providers?email=" + encodeURIComponent(email));
      const fill = (k, v) => { if (isNew || !f(k).value) f(k).value = v; };
      ["imap_host", "imap_port", "imap_security", "imap_user", "smtp_host", "smtp_port", "smtp_security"].forEach(k => fill(k, s[k]));
      if (!f("name").value) f("name").value = s.provider || email.split("@")[1];
      const hint = box.querySelector("[data-hint]");
      hint.innerHTML = (s.provider ? `<b>${esc(s.provider)}</b> erkannt – Server sind eingetragen. ` : "") + esc(s.hint || "");
      hint.classList.toggle("hidden", !(s.provider || s.hint));
    } catch (e) { /* egal */ }
  });
  const body = () => {
    const b = {};
    ["name", "email", "from_name", "imap_host", "imap_user", "smtp_host", "smtp_user", "signature", "color"].forEach(k => { b[k] = f(k).value; });
    ["imap_port", "smtp_port", "sync_days"].forEach(k => { b[k] = Number(f(k).value); });
    b.imap_security = f("imap_security").value; b.smtp_security = f("smtp_security").value; b.active = f("active").checked;
    if (f("imap_password") && f("imap_password").value) b.imap_password = f("imap_password").value;
    if (f("smtp_password").value) b.smtp_password = f("smtp_password").value;
    if (adminForm && f("owner_id")) b.owner_id = Number(f("owner_id").value);
    if (!b.imap_user) b.imap_user = b.email;
    return b;
  };
  box.querySelector("[data-testbtn]").onclick = async (ev) => {
    const out = box.querySelector("[data-test]"); err.textContent = ""; ev.target.disabled = true;
    out.innerHTML = '<span class="muted">Verbindung wird geprüft …</span>';
    try {
      const r = await api("accounts/test", { method: "POST", body: { ...body(), account_id: isNew ? null : a.id } });
      out.innerHTML = (r.imap_ok ? `<span class="ok">✓ Posteingang: Anmeldung klappt (${r.folders} Ordner)</span>`
                                 : `<span class="err">✗ Posteingang: ${esc(r.imap_error)}</span>`) +
        (r.smtp_ok === null ? '<span class="muted">Postausgang: kein Server eingetragen</span>'
          : r.smtp_ok ? '<span class="ok">✓ Postausgang: Anmeldung klappt</span>' : `<span class="err">✗ Postausgang: ${esc(r.smtp_error)}</span>`);
    } catch (e) { out.textContent = ""; err.textContent = e.message; }
    finally { ev.target.disabled = false; }
  };
  box.querySelector("[data-save]").onclick = async (ev) => {
    err.textContent = ""; ev.target.disabled = true;
    try {
      if (isNew) await api("accounts", { method: "POST", body: body() });
      else await api(`accounts/${a.id}`, { method: "PATCH", body: body() });
      box.classList.add("hidden"); box.innerHTML = "";
      await reload();
      if (window.refreshFolders) refreshFolders().catch(() => {});
    } catch (e) { err.textContent = e.message; }
    finally { ev.target.disabled = false; }
  };
  const del = box.querySelector("[data-del]");
  if (del) del.onclick = async () => {
    if (!confirm(`Konto „${a.name}“ (${a.email}) aus Postfach entfernen?\n\nDie Mails am Server bleiben erhalten – nur die Verbindung und der Zwischenspeicher auf der NAS werden gelöscht.`)) return;
    try { await api(`accounts/${a.id}`, { method: "DELETE" }); box.classList.add("hidden"); box.innerHTML = ""; await reload(); }
    catch (e) { err.textContent = e.message; }
  };
  box.scrollIntoView({ behavior: "smooth", block: "start" });
  setTimeout(() => (isNew ? f("email") : f("name")).focus(), 50);
}

// ---------- Microsoft 365: Anmeldung mit Code ----------
async function msConnect(box, payload, reload) {
  box.classList.remove("hidden");
  box.innerHTML = '<span class="muted">Code wird bei Microsoft angefordert …</span>';
  let r;
  try { r = await api("accounts/microsoft/start", { method: "POST", body: payload }); }
  catch (e) { box.innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
  box.innerHTML = `<b>Bei Microsoft anmelden</b>
    <ol><li>Öffne <a href="${esc(r.verification_uri)}" target="_blank" rel="noopener">${esc(r.verification_uri.replace("https://", ""))}</a>
      – auf diesem oder einem anderen Gerät, z. B. am Handy.</li>
      <li>Gib diesen Code ein: <span class="code" title="markieren und kopieren">${esc(r.user_code)}</span>
        <button class="mini" data-copy>Kopieren</button></li>
      <li>Melde dich mit dem Microsoft-Konto des Postfachs an und erlaube den Zugriff.</li></ol>
    <span class="muted small" data-st>Warte auf die Anmeldung … (der Code gilt ${Math.round(r.expires_in / 60)} Minuten)</span>
    <div><button class="mini" data-cancel>Abbrechen</button></div>`;
  box.querySelector("[data-copy]").onclick = () => { try { navigator.clipboard.writeText(r.user_code); } catch (e) { /* egal */ } };
  let stop = false;
  box.querySelector("[data-cancel]").onclick = () => { stop = true; box.classList.add("hidden"); box.innerHTML = ""; };
  while (!stop) {
    await new Promise(res => setTimeout(res, 3000));
    if (stop || !document.body.contains(box)) return;
    let st;
    try { st = await api(`accounts/microsoft/${encodeURIComponent(r.flow_id)}`); }
    catch (e) { box.querySelector("[data-st]").textContent = e.message; return; }
    if (st.status === "pending") continue;
    if (st.status === "error") { box.innerHTML = `<span class="err">✗ ${esc(st.error)}</span>`; return; }
    box.innerHTML = `<span class="ok">✓ Verbunden: ${esc(st.account.email)} – die Mails werden jetzt abgerufen.</span>`;
    await reload();
    if (window.refreshFolders) refreshFolders().catch(() => {});
    return;
  }
}

$("accMs").onclick = () => msConnect($("msBox"), {}, loadAccounts);
$("aaMs").onclick = async () => {
  if (!state.users || !state.users.length) state.users = await api("users");
  const box = $("aaMsBox");
  box.classList.remove("hidden");
  box.innerHTML = `<b>Microsoft 365 für einen Benutzer verbinden</b>
    <div class="row"><label>Gehört zu <select data-owner>${state.users.filter(u => u.active).map(u =>
      `<option value="${u.id}">${esc(u.display_name)} (${esc(u.username)})</option>`).join("")}</select></label>
      <label>&nbsp;<button class="primary" data-go>Code anfordern</button></label></div>
    <p class="muted small" style="margin:0">Die Anmeldung bei Microsoft macht, wer das Passwort des Postfachs kennt – du oder der Benutzer.</p>`;
  box.querySelector("[data-go]").onclick = () => msConnect(box, { owner_id: Number(box.querySelector("[data-owner]").value) }, loadAllAccounts);
};

$("accNew").onclick = () => openAccForm("accForm", null, loadAccounts);
$("aaNew").onclick = async () => {
  if (!state.users || !state.users.length) state.users = await api("users");
  openAccForm("aaForm", null, loadAllAccounts);
};
