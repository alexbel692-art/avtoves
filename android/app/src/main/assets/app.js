'use strict';

const view = document.getElementById('view');
const overlay = document.getElementById('overlay');

// ----- хранение -----
function load(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch (e) {
    return fallback;
  }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* память недоступна */ }
}

let session = load('av.session', null);
let lastServer = load('av.server', '');
let lastLogin = load('av.login', '');
let data = null;
let screen = 'main';
let selectedDate = localDate();
let syncing = false;
let online = true;
let notice = '';
let weighResult = null;
let weighBusy = false;
let pollTimer = null;

function emptyData() {
  return { queue: [], rejected: [], me: null, plate: '', trailer: '', picked: false };
}
function dataKey() { return 'av.d.' + session.driver.id; }
function saveData() { save(dataKey(), data); }

// ----- общее -----
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false) {
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
  }
  return node;
}
function show(...parts) {
  view.replaceChildren(el('div', null, ...parts));
}
function uuid() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}
function localDate(moment) {
  const d = moment || new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
function fmt(value) {
  return Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 });
}
function plate(number) {
  return el('div', { class: 'plate' }, el('span', null, number), el('i', null, 'RUS'));
}
function toast(text) {
  const node = el('div', { class: 'toast', role: 'status' }, text);
  document.body.append(node);
  setTimeout(() => node.remove(), 2600);
}
function ask(text, yes, onYes) {
  const close = () => { overlay.replaceChildren(); };
  overlay.replaceChildren(el('div', { class: 'dialog', role: 'dialog', 'aria-modal': 'true' },
    el('div', null,
      el('p', null, text),
      el('div', { class: 'twin' },
        el('button', { class: 'quiet', onclick: close }, 'Отмена'),
        el('button', { class: 'main', id: 'dialog-yes', onclick: () => { close(); onYes(); } }, yes)))));
}
const me = () => data.me || { plates: [], trailers: [], kinds: {}, tanks: {}, clients: [], unloads: [], weighings: [], scale: {}, road: null };
function tankOf() {
  const tanks = me().tanks || {};
  return tanks[data.trailer] || tanks[data.plate] || null;
}
function needsTrailer() {
  return (me().kinds || {})[data.plate] === 'tractor';
}

// ----- сеть -----
async function api(path, options) {
  const opts = options || {};
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeout || 15000);
  try {
    const resp = await fetch(opts.server + path, {
      method: opts.method || 'GET',
      headers: Object.assign({ 'Content-Type': 'application/json' },
        opts.token ? { Authorization: 'Bearer ' + opts.token } : {}),
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: ctrl.signal,
    });
    let payload = {};
    try { payload = await resp.json(); } catch (e) { /* ответ не JSON */ }
    if (!resp.ok) {
      const err = new Error(payload.error || 'Сервер ответил ошибкой ' + resp.status);
      err.status = resp.status;
      throw err;
    }
    return payload;
  } finally {
    clearTimeout(timer);
  }
}
function authed(path, options) {
  return api(path, Object.assign({ server: session.server, token: session.token }, options));
}
function normalizeServer(raw) {
  let url = raw.trim().replace(/\/+$/, '');
  if (url && !/^https?:\/\//i.test(url)) url = 'https://' + url;
  return url;
}

function sessionExpired() {
  session = null;
  save('av.session', null);
  notice = 'Войдите заново. Неотправленные выгрузки останутся в телефоне.';
  render();
}

async function sync(quiet) {
  if (!session || syncing) return;
  syncing = true;
  try {
    const sent = data.queue.slice(0, 200);
    if (sent.length) {
      const { results } = await authed('/api/app/sync', { method: 'POST', body: { ops: sent } });
      const done = new Set();
      for (const result of results) {
        done.add(result.op_id);
        const op = sent.find((item) => item.op_id === result.op_id);
        if (!result.ok) {
          data.rejected.push({ error: result.error, op });
        } else if (op && op.type === 'unload' && data.me && !data.me.unloads.some((u) => u.id === op.id)) {
          // Сервер принял выгрузку: она остаётся в итогах, даже если связь пропадёт до обновления списка.
          data.me.unloads.push(unloadFromOp(op));
        } else if (op && op.type === 'delete' && data.me) {
          data.me.unloads = data.me.unloads.filter((u) => u.id !== op.unload_id);
        }
      }
      data.queue = data.queue.filter((item) => !done.has(item.op_id));
      saveData();
    }
    data.me = await authed('/api/app/me');
    if (!data.picked) {
      data.plate = data.me.plate || '';
      data.trailer = data.me.trailer || '';
    }
    saveData();
    online = true;
    if (data.queue.length) setTimeout(sync, 200);
  } catch (err) {
    if (err.status === 401) {
      syncing = false;
      sessionExpired();
      return;
    }
    online = false;
  }
  syncing = false;
  if (quiet) updateLive();
  else if (screen === 'main' || screen === 'vehicle' || (screen === 'weigh' && !weighBusy && !weighResult)) render();
}

function enqueue(op) {
  op.op_id = uuid();
  data.queue.push(op);
  saveData();
}

// ----- данные дня -----
function unloadFromOp(op) {
  return { id: op.id, client: op.client_label, liters: op.liters, ts: op.at, day: op.day, plate: op.plate, trailer: op.trailer, comment: op.comment };
}
function dayOf(u) {
  return u.day || localDate(new Date(u.ts));
}
function unloadsFor(day) {
  const removed = new Set(data.queue.filter((op) => op.type === 'delete').map((op) => op.unload_id));
  const known = new Set(me().unloads.map((u) => u.id));
  const pending = data.queue.filter((op) => op.type === 'unload' && !known.has(op.id))
    .map((op) => Object.assign(unloadFromOp(op), { pending: true }));
  return pending.concat(me().unloads)
    .filter((u) => dayOf(u) === day && !removed.has(u.id))
    .sort((a, b) => (new Date(a.ts) < new Date(b.ts) ? 1 : -1));
}
function leftInTank() {
  const road = me().road;
  if (!road) return null;
  const queued = data.queue.filter((op) => op.type === 'unload' && op.plate === data.plate)
    .reduce((sum, op) => sum + op.liters, 0);
  return Math.round(road.left_l - queued);
}

// ----- экраны -----
function render() {
  clearInterval(pollTimer);
  pollTimer = null;
  if (!session) { renderLogin(); return; }
  if (screen === 'vehicle') renderVehicle();
  else if (screen === 'weigh') renderWeigh();
  else if (screen === 'unload') renderUnloadForm();
  else renderMain();
}

function renderLogin() {
  const error = el('div', { class: 'error', role: 'alert' }, notice);
  const server = el('input', { id: 'server', value: lastServer, autocapitalize: 'off', autocomplete: 'off', inputmode: 'url', placeholder: 'например, ves.example.ru' });
  const login = el('input', { id: 'login', value: lastLogin, autocapitalize: 'off', autocomplete: 'username' });
  const password = el('input', { id: 'password', type: 'password', autocomplete: 'current-password' });
  const submit = el('button', { class: 'main', type: 'submit', id: 'enter' }, 'Войти');
  const form = el('form', {
    onsubmit: async (event) => {
      event.preventDefault();
      const url = normalizeServer(server.value);
      if (!url || !login.value.trim() || !password.value) {
        error.textContent = 'Заполните адрес сервера, логин и пароль.';
        return;
      }
      submit.disabled = true;
      error.textContent = '';
      try {
        const res = await api('/api/app/login', { server: url, method: 'POST', body: { login: login.value.trim(), password: password.value } });
        session = { server: url, token: res.token, driver: res.driver };
        save('av.session', session);
        lastServer = url; lastLogin = login.value.trim();
        save('av.server', lastServer); save('av.login', lastLogin);
        data = load(dataKey(), emptyData());
        notice = '';
        screen = 'main';
        selectedDate = localDate();
        render();
        sync();
      } catch (err) {
        submit.disabled = false;
        error.textContent = err.status ? err.message : 'Нет связи с сервером. Проверьте адрес и интернет.';
      }
    },
  },
  el('label', { for: 'server' }, 'Адрес сервера'), server,
  el('label', { for: 'login' }, 'Логин'), login,
  el('label', { for: 'password' }, 'Пароль'), password,
  el('p', null), submit, error);
  show(el('h1', null, 'АвтоВес Водитель'), form);
}

function vehicleBlock() {
  return el('section', { class: 'hitch' },
    data.plate ? el('div', { class: 'plates' }, plate(data.plate), data.trailer ? plate(data.trailer) : null)
      : el('div', { class: 'none', id: 'no-vehicle' }, 'Машина не выбрана'),
    data.plate && needsTrailer() && !data.trailer ? el('div', { class: 'none' }, 'Выберите прицеп') : null,
    el('p', null),
    el('button', { id: 'change-vehicle', onclick: () => { screen = 'vehicle'; render(); } }, data.plate ? 'Сменить машину или прицеп' : 'Выбрать машину'));
}
function vehicleReady() {
  if (!data.plate || (needsTrailer() && !data.trailer)) {
    screen = 'vehicle';
    render();
    return false;
  }
  return true;
}

function renderMain() {
  const today = localDate();
  const list = unloadsFor(selectedDate);
  const total = list.reduce((sum, u) => sum + Math.round(u.liters * 100), 0) / 100;
  const byClient = new Map();
  for (const u of list) {
    const entry = byClient.get(u.client) || { cl: 0, n: 0 };
    entry.cl += Math.round(u.liters * 100);
    entry.n += 1;
    byClient.set(u.client, entry);
  }
  const unsent = data.queue.length;
  const stateText = unsent ? `Не отправлено записей: ${unsent}. Отправятся, когда появится связь.`
    : (online ? 'Все записи отправлены' : 'Нет связи. Выгрузки сохранятся в телефоне.');
  const left = leftInTank();

  show(
    el('div', { class: 'bar' },
      el('b', null, session.driver.name),
      el('button', { class: 'small quiet', id: 'logout', onclick: logout }, 'Выйти')),
    data.rejected.length ? el('div', { class: 'notice', id: 'rejected' },
      el('b', null, 'Сервер не принял записи:'),
      data.rejected.map((r) => el('p', null, describeOp(r.op) + ': ' + r.error)),
      el('button', { class: 'small', onclick: () => { data.rejected = []; saveData(); render(); } }, 'Понятно')) : null,
    el('div', { class: 'state' + (unsent || !online ? ' wait' : ''), id: 'state' }, stateText),
    vehicleBlock(),
    left !== null ? el('div', { class: 'info', id: 'left' }, 'В цистерне по весам: ', el('b', null, fmt(left) + ' л')) : null,
    el('div', { class: 'twin' },
      el('button', { id: 'go-weigh', onclick: () => { if (vehicleReady()) { weighResult = null; screen = 'weigh'; render(); } } }, 'Взвеситься'),
      el('button', { class: 'main', id: 'new-unload', onclick: () => { if (vehicleReady()) { screen = 'unload'; render(); } } }, 'Записать выгрузку')),
    el('h2', null, selectedDate === today ? 'Слито сегодня' : 'Слито за день'),
    el('input', {
      type: 'date', id: 'day', value: selectedDate, max: today, 'aria-label': 'День',
      onchange: (event) => { if (event.target.value) { selectedDate = event.target.value; render(); } },
    }),
    el('div', { class: 'sum' }, el('strong', { id: 'total' }, fmt(total)), el('span', null, 'л, выгрузок: ' + list.length)),
    Array.from(byClient.entries()).sort((a, b) => b[1].cl - a[1].cl).map(([name, entry]) =>
      el('div', { class: 'row point-row' }, el('span', null, name + ' (' + entry.n + ')'), el('b', null, fmt(entry.cl / 100) + ' л'))),
    el('h2', null, 'Выгрузки'),
    list.length ? list.map(renderUnload) : el('p', { class: 'empty' }, 'За этот день выгрузок нет.'),
  );
}

function renderUnload(u) {
  const canDelete = dayOf(u) === localDate() && u.invoice !== 'done';
  return el('article', { class: 'trip' + (u.pending ? ' pending' : '') },
    el('div', { class: 'top' }, el('span', null, u.client), el('b', null, fmt(u.liters) + ' л')),
    el('div', { class: 'meta' }, u.plate + (u.trailer ? ' + ' + u.trailer : '') + (u.comment ? '. ' + u.comment : '')),
    el('div', { class: 'act' },
      el('span', { class: 'meta' }, u.pending ? 'Ждёт связи' : ''),
      canDelete ? el('button', {
        class: 'small quiet',
        onclick: () => ask(`Удалить выгрузку ${u.client}, ${fmt(u.liters)} л?`, 'Удалить', () => deleteUnload(u)),
      }, 'Удалить') : null));
}

function deleteUnload(u) {
  if (u.pending) {
    data.queue = data.queue.filter((op) => !(op.type === 'unload' && op.id === u.id));
    saveData();
  } else {
    enqueue({ type: 'delete', unload_id: u.id, label: `${u.client}, ${fmt(u.liters)} л` });
  }
  toast('Выгрузка удалена');
  render();
  sync();
}

function describeOp(op) {
  if (!op) return 'Запись';
  if (op.type === 'unload') return `Выгрузка ${op.client_label}, ${fmt(op.liters)} л`;
  return `Удаление выгрузки ${op.label}`;
}

function logout() {
  const text = data.queue.length
    ? `Не отправлено записей: ${data.queue.length}. Они сохранятся в телефоне и уйдут после входа под этим логином. Выйти?`
    : 'Выйти из приложения?';
  ask(text, 'Выйти', () => {
    session = null;
    save('av.session', null);
    notice = '';
    render();
  });
}

function renderVehicle() {
  const m = me();
  const choose = (kind, value) => {
    data[kind] = value;
    if (kind === 'plate' && (m.kinds || {})[value] !== 'tractor') data.trailer = '';
    data.picked = true;
    saveData();
    render();
  };
  const list = (kind, items) => items.map((p) => el('button', {
    class: 'pick' + (data[kind] === p ? ' on' : ''), onclick: () => choose(kind, p),
  }, plate(p), (m.tanks || {})[p] ? el('span', null, fmt(m.tanks[p]) + ' л') : null));
  show(
    el('button', { class: 'quiet back', id: 'back', onclick: () => { screen = 'main'; render(); } }, data.plate && !(needsTrailer() && !data.trailer) ? 'Готово' : 'Назад'),
    el('h1', null, 'На какой машине работаете?'),
    el('p', null),
    m.plates.length ? list('plate', m.plates)
      : el('p', { class: 'empty' }, 'Списка машин пока нет. Нужна связь с сервером, а машины добавляет весовщик в справочник ТС.'),
    data.plate && m.trailers.length ? [el('h2', null, needsTrailer() ? 'Какой прицеп подцепили?' : 'Прицеп, если есть'),
      list('trailer', m.trailers),
      data.trailer ? el('button', { class: 'quiet', id: 'no-trailer', onclick: () => choose('trailer', '') }, 'Без прицепа') : null] : null,
  );
}

// ----- взвешивание -----
function updateLive() {
  const node = document.getElementById('live-weight');
  if (!node) return;
  const sc = me().scale || {};
  node.textContent = !online ? 'нет связи' : (!sc.connected ? 'весы не на связи' : fmt(sc.weight || 0) + ' кг');
  const hint = document.getElementById('live-hint');
  if (hint) hint.textContent = online && sc.connected ? (sc.stable ? 'Вес стабилен' : 'Вес меняется') : '';
}

function renderWeigh() {
  const back = el('button', { class: 'quiet back', id: 'back', onclick: () => { screen = 'main'; weighResult = null; render(); sync(); } }, 'Назад');
  if (weighResult) {
    const r = weighResult;
    show(back,
      el('h1', null, r.direction === 'arrival' ? 'Приезд записан' : 'Выезд записан'),
      el('div', { class: 'weight', id: 'fixed-weight' }, fmt(r.weight) + ' кг'),
      el('div', { class: 'plates' }, plate(r.plate), r.trailer ? plate(r.trailer) : null),
      r.gas ? el('div', { class: 'info' }, 'Газа в машине по весам: ', el('b', null, fmt(r.gas.liters) + ' л')) : null,
      r.photo ? el('img', { class: 'photo', alt: 'Фото машины на весах', src: session.server + r.photo }) : null,
      el('p', null, 'Можно съезжать с весов.'));
    return;
  }
  const error = el('div', { class: 'error', role: 'alert', id: 'weigh-error' });
  const buttons = [];
  const weigh = async (direction) => {
    if (weighBusy) return;
    weighBusy = true;
    buttons.forEach((b) => { b.disabled = true; });
    error.textContent = 'Ждём, пока вес успокоится…';
    try {
      weighResult = await authed('/api/app/weigh', {
        method: 'POST', timeout: 45000, body: { plate: data.plate, trailer: data.trailer, direction },
      });
      weighBusy = false;
      render();
      sync(true);
    } catch (err) {
      weighBusy = false;
      buttons.forEach((b) => { b.disabled = false; });
      if (err.status === 401) { sessionExpired(); return; }
      error.textContent = err.status ? err.message : 'Нет связи с сервером. Взвешивание работает только при связи.';
    }
  };
  buttons.push(
    el('button', { class: 'main', id: 'arrival', onclick: () => weigh('arrival') }, 'Приехал на базу'),
    el('button', { class: 'main', id: 'departure', onclick: () => weigh('departure') }, 'Уехал с базы'));
  show(back,
    el('h1', null, 'Взвешивание'),
    el('p', null, 'Заедьте на весы, остановитесь и нажмите кнопку. Вес записывается с весов на базе.'),
    el('div', { class: 'plates' }, plate(data.plate), data.trailer ? plate(data.trailer) : null),
    el('div', { class: 'weight', id: 'live-weight' }, ''),
    el('div', { class: 'meta', id: 'live-hint' }, ''),
    el('p', null), buttons[0], el('p', null), buttons[1], error);
  updateLive();
  sync(true);
  pollTimer = setInterval(() => { if (!weighBusy) sync(true); }, 3000);
}

// ----- выгрузка -----
function renderUnloadForm() {
  const NEW = '__new';
  const m = me();
  const tank = tankOf();
  const left = leftInTank();
  const recent = [];
  for (const u of m.unloads) {
    if (!recent.includes(u.client)) recent.push(u.client);
  }
  const clients = m.clients.slice().sort((a, b) => {
    const ra = recent.indexOf(a.name); const rb = recent.indexOf(b.name);
    if (ra !== rb && (ra < 0 || rb < 0)) return ra < 0 ? 1 : -1;
    if (ra !== rb) return ra - rb;
    return a.name.localeCompare(b.name, 'ru');
  });
  const error = el('div', { class: 'error', role: 'alert', id: 'unload-error' });
  const newName = el('input', { id: 'client-new', maxlength: 120 });
  const newWrap = el('div', { hidden: true }, el('label', { for: 'client-new' }, 'Название клиента'), newName);
  const select = el('select', {
    id: 'client',
    onchange: () => { newWrap.hidden = select.value !== NEW; if (!newWrap.hidden) newName.focus(); },
  },
  el('option', { value: '' }, 'Выберите клиента'),
  clients.map((c) => el('option', { value: c.id }, c.name)),
  el('option', { value: NEW }, 'Другой клиент'));
  const amount = el('input', { id: 'liters', inputmode: 'decimal', autocomplete: 'off' });
  const comment = el('input', { id: 'comment', maxlength: 200 });

  const store = (litersValue, clientId, label) => {
    const now = new Date();
    const op = {
      type: 'unload', id: uuid(), plate: data.plate, trailer: data.trailer, liters: litersValue,
      at: now.toISOString(), day: localDate(now), comment: comment.value.trim(), client_label: label,
    };
    if (clientId) op.client_id = clientId; else op.client_name = label;
    enqueue(op);
    selectedDate = op.day;
    screen = 'main';
    toast('Выгрузка записана');
    render();
    sync();
  };
  const form = el('form', {
    onsubmit: (event) => {
      event.preventDefault();
      error.textContent = '';
      let clientId = null;
      let label;
      if (select.value === NEW) {
        label = newName.value.trim();
        if (!label) { error.textContent = 'Укажите название клиента.'; return; }
      } else if (select.value) {
        const client = m.clients.find((c) => String(c.id) === select.value);
        clientId = client.id; label = client.name;
      } else {
        error.textContent = 'Выберите, кому слили газ.';
        return;
      }
      const value = Number(amount.value.replace(',', '.').trim());
      if (!Number.isFinite(value) || value <= 0) { error.textContent = 'Укажите, сколько литров слили.'; return; }
      if (value > 200000) { error.textContent = 'Слишком большое значение: не больше 200 000 л.'; return; }
      const rounded = Math.round(value * 100) / 100;
      const limit = left !== null ? left : tank;
      if (limit !== null && rounded > limit) {
        const what = left !== null ? `остатка в цистерне по весам (${fmt(left)} л)` : `объёма цистерны (${fmt(tank)} л)`;
        ask(`${fmt(rounded)} л больше ${what}. Записать всё равно?`, 'Записать', () => store(rounded, clientId, label));
        return;
      }
      store(rounded, clientId, label);
    },
  },
  el('label', { for: 'client' }, 'Кому слили'), select, newWrap,
  el('label', { for: 'liters' }, 'Сколько слили, литров'), amount,
  left !== null && left > 0 ? [el('p', null), el('button', { type: 'button', class: 'quiet', id: 'all-left', onclick: () => { amount.value = String(left); } }, 'Весь остаток: ' + fmt(left) + ' л')] : null,
  el('label', { for: 'comment' }, 'Комментарий, если нужен'), comment,
  el('p', null),
  el('button', { class: 'main', type: 'submit', id: 'save-unload' }, 'Записать выгрузку'),
  error);
  show(
    el('button', { class: 'quiet back', id: 'back', onclick: () => { screen = 'main'; render(); } }, 'Назад'),
    el('h1', null, 'Новая выгрузка'), el('p', null),
    el('div', { class: 'plates' }, plate(data.plate), data.trailer ? plate(data.trailer) : null), form);
}

// ----- запуск -----
if (session) data = load(dataKey(), emptyData());
render();
if (session) sync();
window.addEventListener('online', () => sync());
setInterval(() => { if (session && screen === 'main' && !overlay.childElementCount) sync(); }, 60000);
// Кнопка «Назад» Android вызывает эту функцию из MainActivity.
window.avtovesBack = function () {
  if (overlay.childElementCount) { overlay.replaceChildren(); return true; }
  if (session && screen !== 'main' && !weighBusy) { screen = 'main'; weighResult = null; render(); return true; }
  return false;
};
