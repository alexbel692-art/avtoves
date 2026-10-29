"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (url, body, method) => {
  const r = await fetch(url, body === undefined && !method ? {} : {
    method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
};
const kg = v => v === null || v === undefined ? "—" : Math.round(v).toLocaleString("ru-RU");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pretty = p => { const m = /^(\D)(\d{3})(\D{2})(\d{2,3})$/.exec(p || ""); return m ? m.slice(1).join(" ") : (p || ""); };
const dt = s => s ? s.replace("T", " ").slice(0, 16) : "—";

// ---------------- вкладки
$$("nav button").forEach(b => b.onclick = () => {
  $$("nav button").forEach(x => x.classList.toggle("active", x === b));
  $$(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
  if (b.dataset.tab === "journal") loadJournal();
  if (b.dataset.tab === "vehicles") loadVehicles();
});

// ---------------- оператор
let camsBuilt = false;
function buildCams(cams) {
  $("#cams").innerHTML = cams.map(c => `
    <div class="cam" data-cam="${esc(c.id)}">
      <img alt="">
      <div class="cap">${esc(c.name)}${c.side ? " · сторона " + c.side.toUpperCase() : ""}</div>
      <div class="err" hidden></div>
    </div>`).join("");
  camsBuilt = true;
}

function refreshCams() {
  $$(".cam").forEach(el => {
    const img = $("img", el);
    const next = new Image();
    next.onload = () => { img.src = next.src; };
    next.src = `/api/cam/${el.dataset.cam}.jpg?t=${Date.now()}`;
  });
}

let lastCropKey = "";
function render(s) {
  $("#clock").textContent = s.time;
  $("#ver").textContent = "v" + s.version;
  const sc = s.scale, io = s.io, c = s.ctrl;
  $("#hdrStatus").innerHTML =
    `<span class="${sc.connected ? "on" : "off"}">Весы</span>` +
    `<span class="${!io.enabled || io.connected ? "on" : "off"}">Светофоры</span>` +
    s.cameras.map(k => `<span class="${k.connected ? "on" : "off"}">${esc(k.name)}</span>`).join("") +
    (s.simulate ? `<span class="on">ИМИТАЦИЯ</span>` : "");

  const w = $("#weight");
  w.textContent = sc.connected ? kg(sc.weight) : "- - -";
  w.classList.toggle("stable", !!sc.stable);
  $("#fStable").classList.toggle("lit", !!sc.stable);
  $("#fConn").classList.toggle("lit", !!sc.connected);
  $("#fOver").classList.toggle("lit", !!sc.overload);

  $("#stateCard").className = "card state-card " + c.state;
  $("#stateText").textContent = c.state_text;
  $("#warning").textContent = c.warning || sc.error || "";

  const p = c.plate;
  const plateEl = $("#plate");
  plateEl.textContent = p ? pretty(p.plate) : (c.state === "free" ? "—" : "не распознан");
  plateEl.classList.toggle("empty", !p);
  $("#plateMeta").textContent = p ? [
    p.camera === "оператор" ? "введён оператором" : `камера ${p.camera}, уверенность ${(p.conf * 100).toFixed(0)}%, кадров ${p.votes}`,
    p.valid ? "" : "формат не РФ",
    p.alternatives?.length ? "варианты: " + p.alternatives.map(a => a[0]).join(", ") : "",
  ].filter(Boolean).join(" · ") : "";
  const crop = $("#plateCrop");
  const key = p ? p.plate + p.votes : "";
  if (p && p.camera !== "оператор") {
    if (key !== lastCropKey) crop.src = "/api/plate_crop.jpg?t=" + Date.now();
    crop.style.display = "block";
  } else crop.style.display = "none";
  lastCropKey = key;

  $("#btnFix").disabled = c.state !== "arriving";

  for (const side of ["a", "b"]) {
    $(`.light-box[data-side=${side}] .tl`).className = "tl " + io.lights[side];
    $(`[data-barrier=${side}]`).hidden = !io.barriers.includes(side);
  }
  $("#manual").checked = io.manual;

  if (c.last) {
    const t = c.last.trip;
    $("#lastCard").hidden = false;
    $("#lastBody").innerHTML = `
      <div><span class="big">${kg(c.last.weight)} кг</span> · ${esc(pretty(c.last.plate) || "без номера")} · ${esc(c.last.kind)} взвешивание</div>
      ${t.status === "closed" ? `<div>Брутто ${kg(t.gross)} · Тара ${kg(t.tare)} · <b>Нетто ${kg(t.net)} кг</b></div>` : ""}
      <div><a href="/ticket/${t.id}" target="_blank">Талон №${t.id}</a> · <a href="#" data-edit="${t.id}">Изменить</a></div>`;
  }

  $("#events").innerHTML = c.events.map(e => `<li><b>${e.ts}</b>${esc(e.text)}</li>`).join("");
  $("#reads").innerHTML = s.reads.map(r =>
    `<li class="${r.valid ? "" : "bad"}"><b>${r.ts}</b>${esc(pretty(r.plate))} · ${esc(r.camera)} · ${(r.conf * 100).toFixed(0)}%</li>`).join("");

  // диагностика
  $("#diagHw").innerHTML = [
    `Весы: ${sc.connected ? "на связи" : "нет связи — " + esc(sc.error)}`,
    `Модуль светофоров: ${!io.enabled ? "выключен в настройках" : io.connected ? "на связи" : "нет связи — " + esc(io.error)}`,
    ...s.cameras.map(k => `Камера ${esc(k.name)}: ${k.connected ? "есть видео" : "нет видео " + esc(k.error)}`),
    `Распознавание: ${s.anpr_error ? esc(s.anpr_error) : "работает"}`,
    `ИК-барьеры: ${Object.keys(io.inputs).length ? Object.entries(io.inputs).map(([k, v]) => k + "=" + (v ? "перекрыт" : "свободен")).join(", ") : "не подключены"}`,
  ].map(x => `<div>${x}</div>`).join("");
  $("#diagRaw").textContent = sc.raw.map(l => JSON.stringify(l)).join("\n") || "нет данных";
  $("#simCard").hidden = !s.simulate;
}

async function poll() {
  try {
    const s = await api("/api/state");
    if (!camsBuilt) buildCams(s.cameras);
    render(s);
  } catch (e) {
    $("#stateText").textContent = "Нет связи с сервером АвтоВес";
  }
  setTimeout(poll, 400);
}
poll();
setInterval(refreshCams, 1000);

$("#btnFix").onclick = () => api("/api/fix");
$("#btnReset").onclick = () => confirm("Сбросить текущий цикл взвешивания?") && api("/api/reset");
$("#btnPlate").onclick = () => { api("/api/plate", { plate: $("#plateInput").value }); $("#plateInput").value = ""; };
$("#plateInput").onkeydown = e => e.key === "Enter" && $("#btnPlate").click();
$$("[data-light]").forEach(b => b.onclick = () => {
  const [side, state] = b.dataset.light.split(":");
  api("/api/light", { side, state });
});
$$("[data-barrier]").forEach(b => b.onclick = () => api("/api/barrier", { side: b.dataset.barrier }));
$("#manual").onchange = e => api("/api/manual", { on: e.target.checked });
$("#sDrive").onclick = () => api("/api/sim/drive", { plate: $("#sPlate").value, weight: +$("#sWeight").value, side: $("#sSide").value })
  .then(r => r.busy && alert("Имитатор занят — дождитесь окончания проезда"));

// ---------------- журнал
const today = new Date().toISOString().slice(0, 10);
$("#jFrom").value = today; $("#jTo").value = today;
const jQuery = () => new URLSearchParams({ from: $("#jFrom").value, to: $("#jTo").value, plate: $("#jPlate").value, status: $("#jStatus").value });
let journal = [];
async function loadJournal() {
  journal = await api("/api/trips?" + jQuery());
  const stText = { open: "открыт", closed: "закрыт", cancelled: "отменён" };
  $("#jTable tbody").innerHTML = journal.map(t => `
    <tr>
      <td>${t.id}</td><td><b>${esc(pretty(t.plate) || "—")}</b></td>
      <td>${dt(t.w1_ts)}</td><td class="num">${kg(t.w1)}</td>
      <td>${dt(t.w2_ts)}</td><td class="num">${kg(t.w2)}</td>
      <td class="num"><b>${kg(t.net)}</b></td><td>${esc(t.cargo)}</td><td>${esc(t.counterparty)}</td>
      <td class="st-${t.status}">${stText[t.status]}</td><td>${t.exported_1c ? "✓" : ""}</td>
      <td><button data-edit="${t.id}">Изменить</button> <a href="/ticket/${t.id}" target="_blank">Талон</a></td>
    </tr>`).join("");
  const net = journal.filter(t => t.status === "closed").reduce((a, t) => a + (t.net || 0), 0);
  $("#jTotal").textContent = `Рейсов: ${journal.length} · Нетто по закрытым: ${kg(net)} кг`;
}
$("#jLoad").onclick = loadJournal;
$("#jXlsx").onclick = () => location.href = "/api/trips.xlsx?" + jQuery();

// ---------------- редактирование рейса
document.addEventListener("click", async e => {
  const id = e.target.dataset?.edit;
  if (!id) return;
  e.preventDefault();
  const t = journal.find(x => x.id == id) || (await api("/api/trips?status=")).find(x => x.id == id);
  const f = $("#dlgForm");
  $("#dlgTitle").textContent = `Рейс №${t.id}`;
  for (const k of ["plate", "cargo", "counterparty", "driver", "comment", "status"]) f.elements[k].value = t[k] ?? "";
  $("#dlgClose1").hidden = t.status !== "open";
  const ws = await api(`/api/trips/${t.id}/weighings`);
  $("#dlgSnaps").innerHTML = ws.flatMap(w => w.snapshots.split(";").filter(Boolean))
    .map(s => `<a href="/snapshots/${s}" target="_blank"><img src="/snapshots/${s}"></a>`).join("");
  const dlg = $("#dlg");
  dlg.onclose = async () => {
    if (dlg.returnValue === "save") {
      const body = Object.fromEntries(["plate", "cargo", "counterparty", "driver", "comment", "status"].map(k => [k, f.elements[k].value]));
      await api(`/api/trips/${t.id}`, body, "PATCH");
    } else if (dlg.returnValue === "close1") {
      const r = await api(`/api/trips/${t.id}/close`);
      if (r.status === "open") alert("В справочнике ТС нет тары для этого номера");
    }
    if ($("#tab-journal").classList.contains("active")) loadJournal();
  };
  dlg.showModal();
});

// ---------------- справочник ТС
async function loadVehicles() {
  const vs = await api("/api/vehicles");
  $("#vTable tbody").innerHTML = vs.map(v => `
    <tr><td><b>${esc(pretty(v.plate))}</b></td><td>${esc(v.model)}</td><td>${esc(v.counterparty)}</td>
    <td>${esc(v.driver)}</td><td class="num">${kg(v.tare)}</td><td>${v.blocked ? "⛔" : ""}</td><td>${esc(v.comment)}</td>
    <td><button data-vedit='${esc(JSON.stringify(v))}'>Изменить</button> <button data-vdel="${esc(v.plate)}">Удалить</button></td></tr>`).join("");
  $$("[data-vedit]").forEach(b => b.onclick = () => {
    const v = JSON.parse(b.dataset.vedit), f = $("#vForm");
    for (const k of ["plate", "model", "counterparty", "driver", "tare", "comment"]) f.elements[k].value = v[k] ?? "";
    f.elements.blocked.checked = !!v.blocked;
  });
  $$("[data-vdel]").forEach(b => b.onclick = async () => {
    if (confirm("Удалить " + b.dataset.vdel + "?")) { await api("/api/vehicles/" + encodeURIComponent(b.dataset.vdel), {}, "DELETE"); loadVehicles(); }
  });
}
$("#vForm").onsubmit = async e => {
  e.preventDefault();
  const f = e.target;
  const v = Object.fromEntries(new FormData(f));
  v.blocked = f.elements.blocked.checked ? 1 : 0;
  v.tare = v.tare ? +v.tare : null;
  await api("/api/vehicles", v);
  f.reset();
  loadVehicles();
};

// ================================================================ НАСТРОЙКИ
const Set = (() => {
  let token = null;
  try { token = sessionStorage.getItem("avtoves.settings") } catch (e) {}
  let loaded = null;
  const form = $("#setForm");

  const sapi = async (url, body) => {
    const h = { "Content-Type": "application/json" };
    if (token) h["X-Settings-Token"] = token;
    const r = await fetch(url, body === undefined ? { headers: h } : { method: "POST", headers: h, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401 && j.need_login) { showLogin(); throw new Error("login"); }
    j._status = r.status;
    return j;
  };
  const saveToken = t => { token = t; try { t ? sessionStorage.setItem("avtoves.settings", t) : sessionStorage.removeItem("avtoves.settings") } catch (e) {} };

  const get = (o, path) => path.split(".").reduce((a, k) => (a == null ? undefined : a[k]), o);
  const put = (o, path, v) => { const ks = path.split("."); let c = o; ks.slice(0, -1).forEach(k => c = c[k] ??= {}); c[ks.at(-1)] = v; };

  function showLogin() {
    $("#setLogin").hidden = false; form.hidden = true;
    setTimeout(() => $("#setPw").focus(), 50);
  }

  async function open() {
    $("#setMsg").textContent = "";
    let s;
    try { s = await sapi("/api/settings/status") } catch (e) { return }
    if (s.protected && !s.authorized) { saveToken(null); return showLogin(); }
    $("#setPath").textContent = "Файл: " + s.config_path;
    try { loaded = await sapi("/api/settings") } catch (e) { return }
    $("#setLogin").hidden = true; form.hidden = false;
    fill(loaded);
  }

  function fill(d) {
    $$("[name]", form).forEach(el => {
      if (el.name.startsWith("server.new_password") || el.name === "server.clear_password") { el.value = ""; el.checked = false; return; }
      const v = get(d, el.name);
      if (el.type === "checkbox") el.checked = !!v;
      else el.value = v === null || v === undefined ? "" : v;
    });
    $("#pwState").textContent = d.server.protected ? "Пароль на настройки установлен." : "Пароля нет — настройки может изменить любой, кто открыл АвтоВес.";
    $("#pwClearWrap").hidden = !d.server.protected;
    renderCams(d.cameras || []);
    renderRelayBtns();
    clearErrors();
    toggle();
  }

  function collect() {
    const d = {};
    $$("[name]", form).forEach(el => {
      if (el.closest(".cam-row")) return;
      let v = el.type === "checkbox" ? el.checked : el.value.trim();
      if (el.name.startsWith("io.relays.") || el.name.startsWith("io.inputs.")) v = v === "" ? null : +v;
      put(d, el.name, v);
    });
    d.cameras = $$(".cam-row").map(r => ({
      id: r.dataset.id, name: $("[data-f=name]", r).value, side: $("[data-f=side]", r).value || null,
      url: $("[data-f=url]", r).value.trim(), anpr: $("[data-f=anpr]", r).checked,
    }));
    return d;
  }

  function toggle() {
    $$("[data-show]", form).forEach(el => {
      const [path, val] = el.dataset.show.split("=");
      const input = form.elements[path];
      const cur = input.type === "checkbox" ? input.checked : input.value;
      el.hidden = val === undefined ? !cur : cur !== val;
    });
  }
  form.addEventListener("change", toggle);

  // ---------- камеры
  function renderCams(list) {
    $("#camList").innerHTML = "";
    list.forEach(addCam);
    if (!list.length) $("#camList").innerHTML = `<p class="muted" id="camEmpty">Камер нет — номера придётся вводить вручную.</p>`;
  }
  function addCam(c = {}) {
    $("#camEmpty")?.remove();
    const n = $$(".cam-row").length;
    const row = document.createElement("div");
    row.className = "cam-row";
    row.dataset.id = c.id || "cam" + Date.now().toString(36);
    row.innerHTML = `
      <label>Название <input data-f="name" value="${esc(c.name || "Камера " + (n + 1))}"></label>
      <label>Сторона <select data-f="side"><option value="a">въезд А</option><option value="b">въезд Б</option><option value="">обзорная</option></select></label>
      <label>RTSP-адрес <input data-f="url" value="${esc(c.url || "")}" placeholder="rtsp://admin:пароль@192.168.1.71:554/Streaming/Channels/101"></label>
      <div class="cam-actions">
        <label class="chk"><input type="checkbox" data-f="anpr" ${c.anpr !== false ? "checked" : ""}> номера</label>
        <button type="button" data-cam-test>Проверить</button>
        <button type="button" data-cam-del title="Удалить камеру">✕</button>
      </div>
      <div class="test-out"></div>`;
    $("[data-f=side]", row).value = c.side === undefined ? (n === 0 ? "a" : "b") : (c.side || "");
    $("[data-cam-del]", row).onclick = () => row.remove();
    $("[data-cam-test]", row).onclick = () => runTest("camera", { url: $("[data-f=url]", row).value }, $(".test-out", row));
    $("#camList").appendChild(row);
  }
  $("#camAdd").onclick = () => addCam({});

  // ---------- проверки связи
  async function runTest(kind, body, out) {
    out.innerHTML = `<span class="muted">проверяю…</span>`;
    try {
      const r = await sapi("/api/settings/test/" + kind, body);
      let h = `<span class="${r.ok ? "ok" : "bad"}">${r.ok ? "✓" : "✕"} ${esc(r.message || r.error || "")}</span>`;
      if (r.relays) h += `<pre>Реле R0–R5: ${r.relays.join(" · ")}${r.inputs?.length ? "\nВходы DI0–DI5: " + r.inputs.join(" · ") : ""}</pre>`;
      if (r.lines?.length) h += `<pre>${r.lines.map(l => esc(l.replace(/[\x00-\x1f\x7f-\x9f]/g, "·"))).join("\n")}</pre>`;
      if (r.image) h += `<img src="${r.image}" alt="">`;
      out.innerHTML = h;
    } catch (e) { if (e.message !== "login") out.innerHTML = `<span class="bad">✕ ${esc(e.message)}</span>`; }
  }
  const val = n => form.elements[n].value.trim();
  const ioBody = () => ({ host: val("io.host"), port: +val("io.port") || 502, unit_id: +val("io.unit_id") || 0,
                          coil_base: +val("io.coil_base") || 0, di_base: +val("io.di_base") || 0 });
  $$("[data-test]", form).forEach(b => b.onclick = () => {
    if (b.dataset.test === "scale")
      runTest("scale", { type: val("scale.type"), host: val("scale.host"), port: +val("scale.port") || 4001,
                         com: val("scale.com"), baud: +val("scale.baud"), parser: val("scale.parser") }, $("#tScale"));
    else runTest("io", ioBody(), $("#tIo"));
  });
  function renderRelayBtns() {
    $("#relayBtns").innerHTML = [0, 1, 2, 3, 4, 5].map(i => `<button type="button" data-relay="${i}">R${i}</button>`).join(" ");
    $$("[data-relay]").forEach(b => b.onclick = () => runTest("relay", { ...ioBody(), relay: +b.dataset.relay }, $("#tIo")));
  }

  // ---------- токен 1С
  $("#tokGen").onclick = () => {
    const a = new Uint8Array(24); crypto.getRandomValues(a);
    form.elements["server.api_token"].value = btoa(String.fromCharCode(...a)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  };
  $("#tokCopy").onclick = () => { const i = form.elements["server.api_token"]; i.select(); navigator.clipboard?.writeText(i.value).catch(() => document.execCommand("copy")); };

  // ---------- ошибки
  function clearErrors() { $$(".field-err", form).forEach(e => e.classList.remove("field-err")); $$(".err-text", form).forEach(e => e.remove()); }
  function showErrors(fields) {
    clearErrors();
    let first = null;
    for (const [k, msg] of Object.entries(fields)) {
      let el = form.elements[k];
      const m = /^cameras\.(\d+)\.(\w+)$/.exec(k);
      if (m) el = $(`[data-f=${m[2]}]`, $$(".cam-row")[+m[1]]);
      if (!el || el instanceof RadioNodeList) { $("#setMsg").textContent += " " + msg; continue; }
      el.classList.add("field-err");
      const t = document.createElement("div"); t.className = "err-text"; t.textContent = msg;
      el.insertAdjacentElement("afterend", t);
      first ??= el;
    }
    first?.scrollIntoView({ block: "center" }); first?.focus();
  }

  // ---------- сохранение
  form.onsubmit = async e => {
    e.preventDefault();
    const msg = $("#setMsg");
    msg.className = "set-msg"; msg.textContent = "Сохраняю…";
    let r;
    try { r = await sapi("/api/settings", collect()) } catch (er) { return }
    if (r._status === 422) { msg.className = "set-msg bad"; msg.textContent = "Проверьте выделенные поля."; return showErrors(r.fields || {}); }
    if (!r.ok) { msg.className = "set-msg bad"; msg.textContent = r.error || "Ошибка сохранения"; return; }
    if ("token" in r) saveToken(r.token);
    msg.className = "set-msg ok"; msg.textContent = "Сохранено и применено ✓";
    camsBuilt = false;
    loaded = await sapi("/api/settings"); fill(loaded);
    if (r.restart_needed && confirm("Порт АвтоВес изменён. Новый порт заработает после перезапуска службы.\nПерезапустить сейчас? (около 15 секунд)")) {
      await sapi("/api/settings/restart", {});
      msg.textContent = "Перезапуск… программа подключится заново сама.";
    }
  };
  $("#setReload").onclick = () => loaded && fill(loaded);

  // ---------- вход
  $("#setPwBtn").onclick = async () => {
    $("#setPwErr").textContent = "";
    const r = await fetch("/api/settings/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: $("#setPw").value }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { $("#setPwErr").textContent = j.error || "ошибка"; return; }
    saveToken(j.token); $("#setPw").value = ""; open();
  };
  $("#setPw").onkeydown = e => e.key === "Enter" && $("#setPwBtn").click();

  return { open };
})();

$$("nav button[data-tab=settings]").forEach(b => b.addEventListener("click", () => Set.open()));
if (location.hash === "#settings") $("nav button[data-tab=settings]").click();
