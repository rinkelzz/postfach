"use strict";
// ---------- Regeln (Reiter „Konten“) ----------
const rl = { rules: [], fields: {}, ops: {}, accounts: [], folders: {} };

async function loadRules() {
  const [r, f] = await Promise.all([api("rules"), api("folders")]);
  rl.rules = r.rules; rl.fields = r.fields; rl.ops = r.ops;
  rl.accounts = f.accounts;
  rl.folders = {};
  f.accounts.forEach(a => { rl.folders[a.id] = a.folders; });
  renderRules();
}

function renderRules() {
  const accName = (id) => { const a = rl.accounts.find(x => x.id === id); return a ? a.name : "?"; };
  $("ruleList").innerHTML = rl.rules.length ? rl.rules.map((r, i) => `<div class="rule ${r.active ? "" : "off"}" data-rule="${r.id}">
      <div class="ord"><button class="mini" data-up="${i}" ${i ? "" : "disabled"} title="nach oben">↑</button>
        <button class="mini" data-down="${i}" ${i < rl.rules.length - 1 ? "" : "disabled"} title="nach unten">↓</button></div>
      <div><b>${esc(r.name)}</b> <span class="tag">${r.account_id ? esc(accName(r.account_id)) : "alle Konten"}</span>
        ${r.active ? "" : '<span class="tag off">aus</span>'}${r.stop ? ' <span class="tag" title="danach keine weiteren Regeln">⏹ Stopp</span>' : ""}
        <div class="sum">${esc(r.summary)}</div></div>
      <div class="row" style="gap:6px"><button class="mini" data-apply="${r.id}">Jetzt anwenden</button>
        <button class="mini" data-redit="${r.id}">Bearbeiten</button></div>
      <div class="meta ${r.last_error ? "bad" : ""}">${r.hits ? `${r.hits}× angewendet, zuletzt ${fmtDT(r.last_hit)}` : "noch nicht angewendet"}${r.last_error ? " · ⚠ " + esc(r.last_error) : ""}</div>
    </div>`).join("") : '<p class="muted">Noch keine Regeln.</p>';
  $$("#ruleList [data-redit]").forEach(b => b.onclick = () => openRuleForm(rl.rules.find(r => r.id === Number(b.dataset.redit))));
  $$("#ruleList [data-up]").forEach(b => b.onclick = () => moveRule(Number(b.dataset.up), -1));
  $$("#ruleList [data-down]").forEach(b => b.onclick = () => moveRule(Number(b.dataset.down), 1));
  $$("#ruleList [data-apply]").forEach(b => b.onclick = () => applyRule(Number(b.dataset.apply)));
}

async function moveRule(i, dir) {
  const ids = rl.rules.map(r => r.id);
  [ids[i], ids[i + dir]] = [ids[i + dir], ids[i]];
  try { const r = await api("rules/order", { method: "POST", body: { ids } }); rl.rules = r.rules; renderRules(); }
  catch (e) { $("ruleErr").textContent = e.message; }
}

async function applyRule(id) {
  $("ruleErr").textContent = "";
  try {
    const d = await api(`rules/${id}/apply`, { method: "POST", body: { dry_run: true } });
    if (!d.matches) { okMsg($("ruleErr"), "Im Posteingang passt gerade keine Mail auf diese Regel."); return; }
    const list = d.sample.map(s => `• ${s.from}: ${s.subject}`).join("\n");
    if (!confirm(`${d.matches} Mail${d.matches === 1 ? "" : "s"} im Posteingang passen, z. B.:\n\n${list}${d.matches > d.sample.length ? "\n…" : ""}\n\n` +
                 "Regel jetzt darauf anwenden? (Weiterleiten passiert dabei nicht.)")) return;
    const r = await api(`rules/${id}/apply`, { method: "POST", body: { dry_run: false } });
    okMsg($("ruleErr"), `Erledigt: ${r.moved} verschoben, ${r.trashed} in den Papierkorb${r.errors ? `, ${r.errors} Fehler` : ""}.`);
    await loadRules();
    if (window.refreshFolders) refreshFolders().catch(() => {});
  } catch (e) { $("ruleErr").textContent = e.message; }
}

function folderSelect(accountId, current) {
  const fl = rl.folders[accountId] || [];
  return fl.filter(f => f.role !== "inbox").map(f => `<option value="${esc(f.path)}" ${f.path === current ? "selected" : ""}>${
    esc(f.role && typeof folderName === "function" ? folderName(f) : f.name)}</option>`).join("");
}

function openRuleForm(r, prefill) {
  const box = $("ruleForm"), isNew = !r;
  r = r || { name: "", account_id: prefill && prefill.account_id || null, match_all: true, stop: false, active: true,
             conditions: prefill && prefill.conditions || [{ field: "from", op: "contains", value: "" }],
             actions: [{ type: "move", folder: "" }] };
  const st = { conds: r.conditions.map(c => ({ ...c })), acts: r.actions.map(a => ({ ...a })) };
  const opts = (obj, cur) => Object.entries(obj).map(([k, v]) => `<option value="${k}" ${k === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
  const draw = () => {
    const acc = Number($("rAcc").value) || null;
    $("rConds").innerHTML = st.conds.map((c, i) => `<div class="crow">
        <select data-cf="${i}">${opts(rl.fields, c.field)}</select>
        ${c.field === "attachment" ? '<span class="muted small">(Mail hat mindestens einen Anhang)</span>'
          : `<select data-co="${i}">${opts(rl.ops, c.op || "contains")}</select>
             <input data-v="${i}" value="${esc(c.value || "")}" maxlength="200" placeholder="${c.field === "from" ? "z. B. @newsletter.de" : ""}">`}
        <button class="mini" data-cx="${i}" title="Bedingung entfernen" ${st.conds.length > 1 ? "" : "disabled"}>✕</button></div>`).join("");
    $("rActs").innerHTML = st.acts.map((a, i) => `<div class="crow">
        <select data-at="${i}"><option value="move" ${a.type === "move" ? "selected" : ""}>Verschieben nach</option>
          <option value="forward" ${a.type === "forward" ? "selected" : ""}>Weiterleiten an</option>
          <option value="trash" ${a.type === "trash" ? "selected" : ""}>In den Papierkorb</option></select>
        ${a.type === "move" ? (acc ? `<select data-af="${i}"><option value="">– Ordner wählen –</option>${folderSelect(acc, a.folder)}</select>`
                                    : '<span class="err small">Für „Verschieben“ oben ein bestimmtes Konto wählen</span>')
          : a.type === "forward" ? `<input data-ato="${i}" type="email" value="${esc(a.to || "")}" placeholder="buchhaltung@beispiel.de">` : ""}
        <button class="mini" data-ax="${i}" title="Aktion entfernen" ${st.acts.length > 1 ? "" : "disabled"}>✕</button></div>`).join("");
    $$("#rConds [data-cf]").forEach(el => el.onchange = () => { st.conds[el.dataset.cf].field = el.value; draw(); });
    $$("#rConds [data-co]").forEach(el => el.onchange = () => { st.conds[el.dataset.co].op = el.value; });
    $$("#rConds [data-v]").forEach(el => el.oninput = () => { st.conds[el.dataset.v].value = el.value; });
    $$("#rConds [data-cx]").forEach(el => el.onclick = () => { st.conds.splice(Number(el.dataset.cx), 1); draw(); });
    $$("#rActs [data-at]").forEach(el => el.onchange = () => { st.acts[el.dataset.at] = { type: el.value }; draw(); });
    $$("#rActs [data-af]").forEach(el => el.onchange = () => { st.acts[el.dataset.af].folder = el.value; });
    $$("#rActs [data-ato]").forEach(el => el.oninput = () => { st.acts[el.dataset.ato].to = el.value; });
    $$("#rActs [data-ax]").forEach(el => el.onclick = () => { st.acts.splice(Number(el.dataset.ax), 1); draw(); });
  };
  box.innerHTML = `<div class="toolbar" style="margin:0"><h2 style="margin:0">${isNew ? "Neue Regel" : "Regel bearbeiten"}</h2>
      <button class="mini" id="rClose">✕</button></div>
    <div class="row"><label style="flex:1;min-width:200px">Name <input id="rName" maxlength="80" value="${esc(r.name)}" placeholder="z. B. Newsletter"></label>
      <label>Gilt für <select id="rAcc"><option value="">alle meine Konten</option>${rl.accounts.map(a =>
        `<option value="${a.id}" ${a.id === r.account_id ? "selected" : ""}>${esc(a.name)} (${esc(a.email)})</option>`).join("")}</select></label></div>
    <h3>Wenn <select id="rMatch" style="width:auto"><option value="1" ${r.match_all ? "selected" : ""}>alle</option>
      <option value="0" ${r.match_all ? "" : "selected"}>eine</option></select> dieser Bedingungen zutreffen:</h3>
    <div id="rConds"></div><div><button class="mini" id="rAddCond">＋ Bedingung</button></div>
    <h3>dann:</h3>
    <div id="rActs"></div><div><button class="mini" id="rAddAct">＋ Aktion</button></div>
    <div class="row"><label class="inline"><input type="checkbox" id="rStop" ${r.stop ? "checked" : ""}> danach keine weiteren Regeln anwenden</label>
      <label class="inline"><input type="checkbox" id="rActive" ${r.active ? "checked" : ""}> aktiv</label></div>
    <div class="row"><button class="primary" id="rSave">${isNew ? "Regel anlegen" : "Speichern"}</button>
      ${isNew ? "" : '<button class="danger" id="rDelete">Löschen</button>'}<span class="err" id="rErr"></span></div>`;
  box.classList.remove("hidden");
  $("rAcc").onchange = draw;
  $("rAddCond").onclick = () => { if (st.conds.length < 10) st.conds.push({ field: "subject", op: "contains", value: "" }); draw(); };
  $("rAddAct").onclick = () => { if (st.acts.length < 5) st.acts.push({ type: "forward", to: "" }); draw(); };
  $("rClose").onclick = () => { box.classList.add("hidden"); box.innerHTML = ""; };
  $("rSave").onclick = async () => {
    $("rErr").textContent = "";
    const body = { name: $("rName").value.trim(), account_id: Number($("rAcc").value) || null, match_all: $("rMatch").value === "1",
                   stop: $("rStop").checked, active: $("rActive").checked,
                   conditions: st.conds.map(c => c.field === "attachment" ? { field: "attachment" } : { field: c.field, op: c.op || "contains", value: (c.value || "").trim() }),
                   actions: st.acts };
    try {
      if (isNew) await api("rules", { method: "POST", body }); else await api(`rules/${r.id}`, { method: "PATCH", body });
      box.classList.add("hidden"); box.innerHTML = "";
      await loadRules();
    } catch (e) { $("rErr").textContent = e.message; }
  };
  if (!isNew) $("rDelete").onclick = async () => {
    if (!confirm(`Regel „${r.name}“ löschen?`)) return;
    try { await api(`rules/${r.id}`, { method: "DELETE" }); box.classList.add("hidden"); box.innerHTML = ""; await loadRules(); }
    catch (e) { $("rErr").textContent = e.message; }
  };
  draw();
  box.scrollIntoView({ behavior: "smooth", block: "start" });
  setTimeout(() => $("rName").focus(), 50);
}

$("ruleNew").onclick = () => openRuleForm(null);

// Aus einer geöffneten Mail: Regel mit Absender vorbefüllen
async function ruleFromMessage(m) {
  await showTab("accounts");
  await loadRules();
  const domain = (m.from_addr || "").split("@")[1];
  openRuleForm(null, { account_id: m.account_id,
                       conditions: [{ field: "from", op: "contains", value: m.from_addr || domain || "" }] });
  $("rName").value = m.from_name || m.from_addr || "";
}
