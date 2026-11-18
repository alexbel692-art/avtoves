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
let lastServer = load('av.server', '') || window.AVTOVES_SERVER || '';
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
let orderForUnload = null;

function emptyData() {
  return { queue: [], rejected: [], me: null, plate: '', trailer: '', picked: false, seenOrders: [] };
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
const ICONS = {
  trip: 'M3 15V8h11v7M14 10h4l3 3v2h-7M6.5 18.5a1.8 1.8 0 1 0 0-.1M16.5 18.5a1.8 1.8 0 1 0 0-.1M8.5 17h6',
  scale: 'M4 19h16M6.5 19 8 12h8l1.5 7M12 12V6M8.5 6h7',
  day: 'M5 5h14v15H5zM5 9h14M9 3v4M15 3v4M8.5 13h3M8.5 16.5h6',
  route: 'M4 11 20 4l-7 16-2-7z',
  fuel: 'M4 20V5a1.5 1.5 0 0 1 1.5-1.5h7A1.5 1.5 0 0 1 14 5v15M3 20h12M6.5 8h5M14 9h2.5a1.5 1.5 0 0 1 1.5 1.5V16a1.5 1.5 0 0 0 3 0V8l-3-3',
  phone: 'M6.6 3.5h3l1.5 4-2 1.3a11 11 0 0 0 6.1 6.1l1.3-2 4 1.5v3A2.1 2.1 0 0 1 18.2 20 15.6 15.6 0 0 1 4 5.8a2.1 2.1 0 0 1 2.6-2.3z',
  back: 'M15 5l-7 7 7 7',
  check: 'M5 12.5 10 17 19 7',
  drop: 'M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z',
  swap: 'M7 7h11l-3-3M17 17H6l3 3',
};
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'i');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[name]);
  svg.append(path);
  return svg;
}
function stateLine() {
  const unsent = data.queue.length;
  const text = unsent ? `Не отправлено записей: ${unsent}. Отправятся, когда появится связь.`
    : (online ? 'Все записи отправлены' : 'Нет связи. Выгрузки сохранятся в телефоне.');
  return el('div', { class: 'state' + (unsent ? ' wait' : (online ? '' : ' off')), id: 'state' }, text);
}
function rigButton() {
  const missing = !data.plate ? 'Выбрать машину' : (needsTrailer() && !data.trailer ? 'Выбрать прицеп' : '');
  return el('button', { class: 'rig', id: 'change-vehicle', 'aria-label': 'Сменить машину или прицеп', onclick: () => { screen = 'vehicle'; render(); } },
    data.plate ? el('div', { class: 'plates' }, plate(data.plate), data.trailer ? plate(data.trailer) : null) : null,
    missing ? el('span', { class: 'none', id: data.plate ? 'no-trailer-hint' : 'no-vehicle' }, missing) : el('span', { class: 'change' }, icon('swap'), 'Сменить'));
}
function topBar(back) {
  if (back) {
    return el('div', { class: 'top' },
      el('button', { class: 'back', id: 'back', onclick: back }, icon('back'), 'Назад'),
      el('div', { class: 'who' }), data.plate ? el('div', { class: 'plates' }, plate(data.plate), data.trailer ? plate(data.trailer) : null) : null);
  }
  return [el('div', { class: 'top' }, el('div', { class: 'who' }, el('b', null, session.driver.name), stateLine())),
    el('div', { class: 'rig-wrap' }, rigButton())];
}
function tabs(active) {
  const tab = (id, name, label, go) => el('button', { id, 'aria-current': active === name || (active === 'weigh' && name === 'scale') || (active === 'main' && name === 'trip') ? 'page' : null, onclick: go }, icon(name), label);
  return el('nav', { class: 'tabs', 'aria-label': 'Разделы' }, el('div', null,
    tab('tab-main', 'trip', 'Рейс', () => { screen = 'main'; render(); }),
    tab('go-weigh', 'scale', 'Весы', () => { if (vehicleReady()) { weighResult = null; screen = 'weigh'; render(); } }),
    tab('tab-day', 'day', 'День', () => { selectedDate = localDate(); screen = 'day'; render(); })));
}
function dock(solo, ...children) {
  return el('div', { class: 'dock' + (solo ? ' solo' : '') }, el('div', { style: 'width:100%' }, children));
}
function page(cls, ...children) {
  return el('div', { class: 'page ' + (cls || '') }, children);
}
function plate(number) {
  return el('div', { class: 'plate' }, el('span', null, number), el('i', null, 'RUS'));
}
function toast(text) {
  document.querySelectorAll('.toast').forEach((t) => t.remove());
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
const me = () => data.me || { plates: [], trailers: [], kinds: {}, tanks: {}, clients: [], unloads: [], weighings: [], scale: {}, road: null, orders: [] };
// Мост в Android: системные уведомления и адрес устройства для уведомлений через Google
const native = window.Android || null;
function activeOrders() {
  const closed = new Set(data.queue.filter((op) => op.order_id).map((op) => op.order_id));
  return (me().orders || []).filter((o) => !closed.has(o.id));
}
function announceOrders() {
  const seen = new Set(data.seenOrders || []);
  const fresh = (me().orders || []).filter((o) => !seen.has(o.id));
  data.seenOrders = (me().orders || []).map((o) => o.id);
  if (!fresh.length) return;
  const text = fresh.length === 1 ? `${fresh[0].client}: ${fmt(fresh[0].liters)} л. ${fresh[0].address}` : `Новых заказов: ${fresh.length}`;
  toast('Новый заказ. ' + text);
  try { if (native && document.hidden) native.notify('Новый заказ', text); } catch (e) { /* нет моста */ }
}
window.avtovesPushToken = function (token) {
  if (!session || !token) return;
  authed('/api/app/push-token', { method: 'POST', body: { token } }).catch(() => {});
};
function askPush() {
  try { if (native) native.requestPush(); } catch (e) { /* нет моста */ }
}
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
        if (result.ok && op && op.order_id && data.me) {
          data.me.orders = (data.me.orders || []).filter((o) => o.id !== op.order_id);
        }
      }
      data.queue = data.queue.filter((item) => !done.has(item.op_id));
      saveData();
    }
    const first = !data.me;
    data.me = await authed('/api/app/me');
    if (first) data.seenOrders = (data.me.orders || []).map((o) => o.id);
    else announceOrders();
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
  else if (screen === 'main' || screen === 'day' || screen === 'vehicle' || (screen === 'weigh' && !weighBusy && !weighResult)) render();
}

function enqueue(op) {
  op.op_id = uuid();
  data.queue.push(op);
  saveData();
}

// ----- данные дня -----
function unloadFromOp(op) {
  return { id: op.id, client: op.client_label, liters: op.liters, ts: op.at, day: op.day, plate: op.plate, trailer: op.trailer, comment: op.comment, paid: op.paid === true ? 'yes' : (op.paid === false ? 'no' : '') };
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
  // газ, долитый в пути и ещё не отправленный, тоже уже в цистерне
  const refilled = data.queue.filter((op) => op.type === 'refuel' && op.plate === data.plate)
    .reduce((sum, op) => sum + op.liters, 0);
  return Math.round(road.left_l - queued + refilled);
}

// ----- экраны -----
function render() {
  clearInterval(pollTimer);
  pollTimer = null;
  if (!session) { renderLogin(); return; }
  if (screen === 'vehicle') renderVehicle();
  else if (screen === 'weigh') renderWeigh();
  else if (screen === 'day') renderDay();
  else if (screen === 'unload') renderUnloadForm();
  else if (screen === 'refuel') renderRefuelForm();
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
        askPush();
      } catch (err) {
        submit.disabled = false;
        error.textContent = err.status ? err.message : 'Нет связи с сервером. Проверьте адрес и интернет.';
      }
    },
  },
  el('label', { for: 'server' }, 'Адрес сервера компании'), server,
  el('label', { for: 'login' }, 'Логин'), login,
  el('label', { for: 'password' }, 'Пароль'), password,
  submit, error);
  const logo = icon('trip');
  show(el('div', { class: 'login' },
    el('div', { class: 'brand' }, el('div', { class: 'logo' }, logo),
      el('div', null, el('h1', null, 'АвтоВес Водитель'), el('p', null, 'Логин и пароль выдаёт логист'))),
    form));
}

function vehicleReady() {
  if (!data.plate || (needsTrailer() && !data.trailer)) {
    screen = 'vehicle';
    render();
    return false;
  }
  return true;
}

function dayTotals(day) {
  const list = unloadsFor(day);
  const total = list.reduce((sum, u) => sum + Math.round(u.liters * 100), 0) / 100;
  return { list, total };
}

function renderMain() {
  const left = leftInTank();
  const tank = tankOf();
  const orders = activeOrders();
  const today = dayTotals(localDate());
  show(
    topBar(),
    page('has-dock',
      data.rejected.length ? el('div', { class: 'notice', id: 'rejected' },
        el('b', null, 'Сервер не принял записи:'),
        data.rejected.map((r) => el('p', null, describeOp(r.op) + ': ' + r.error)),
        el('button', { class: 'small', onclick: () => { data.rejected = []; saveData(); render(); } }, 'Понятно')) : null,
      left !== null ? el('section', { class: 'tank', id: 'left' },
        el('div', { class: 'k' }, 'В цистерне по весам'),
        el('div', { class: 'v' }, fmt(Math.max(left, 0)), el('small', null, 'л')),
        tank ? [el('div', { class: 'gauge', role: 'img', 'aria-label': `Заполнено на ${Math.round(Math.max(0, Math.min(1, left / tank)) * 100)} %` },
          el('div', { style: `width:${Math.max(0, Math.min(100, left / tank * 100))}%` })),
          el('div', { class: 's' }, el('span', null, Math.round(Math.max(0, Math.min(1, left / tank)) * 100) + ' %'), el('span', null, 'объём ' + fmt(tank) + ' л'))] : null) : null,
      el('button', { class: 'daystrip', onclick: () => { selectedDate = localDate(); screen = 'day'; render(); } },
        el('span', null, 'Слито сегодня', el('small', null, 'выгрузок: ' + today.list.length + (refuelsFor(localDate()).total ? ' · долито газа ' + fmt(refuelsFor(localDate()).total) + ' л' : ''))), el('b', null, el('span', { id: 'total' }, fmt(today.total)), ' л')),
      orderList()),
    dock(false, el('div', { class: 'dock-row' },
      el('button', { class: orders.length ? '' : 'main', id: 'new-unload', onclick: () => { if (vehicleReady()) { orderForUnload = null; screen = 'unload'; render(); } } },
        icon('drop'), orders.length ? 'Выгрузка без заказа' : 'Записать выгрузку'),
      el('button', { id: 'new-refuel', 'aria-label': 'Дозаправка цистерны газом', onclick: () => { if (vehicleReady()) { screen = 'refuel'; render(); } } }, icon('fuel'), 'Дозаправка'))),
    tabs('main'));
}

function renderDay() {
  const today = localDate();
  const { list, total } = dayTotals(selectedDate);
  const byClient = new Map();
  for (const u of list) {
    const entry = byClient.get(u.client) || { cl: 0, n: 0 };
    entry.cl += Math.round(u.liters * 100);
    entry.n += 1;
    byClient.set(u.client, entry);
  }
  show(
    topBar(),
    page('',
      el('input', {
        type: 'date', id: 'day', value: selectedDate, max: today, 'aria-label': 'День',
        onchange: (event) => { if (event.target.value) { selectedDate = event.target.value; render(); } },
      }),
      el('div', { class: 'sum' }, el('strong', { id: 'total' }, fmt(total)), el('span', null, 'л ' + (selectedDate === today ? 'слито сегодня' : 'слито за день') + ', выгрузок: ' + list.length)),
      byClient.size ? el('div', { class: 'rows' }, Array.from(byClient.entries()).sort((a, b) => b[1].cl - a[1].cl).map(([name, entry]) =>
        el('div', { class: 'row point-row' }, el('span', null, name + ' (' + entry.n + ')'), el('b', null, fmt(entry.cl / 100) + ' л')))) : null,
      el('h2', null, 'Выгрузки'),
      list.length ? list.map(renderUnload) : el('p', { class: 'empty' }, 'За этот день выгрузок нет.'),
      refuelsFor(selectedDate).list.length ? [el('h2', null, 'Дозаправки газом: ', el('span', { id: 'refuel-total' }, fmt(refuelsFor(selectedDate).total)), ' л'),
        refuelsFor(selectedDate).list.map((r) => el('article', { class: 'trip' + (r.pending ? ' pending' : '') },
          el('div', { class: 'top2' }, el('span', null, r.station), el('b', null, fmt(r.liters) + ' л')),
          el('div', { class: 'meta' }, r.plate + ' · ' + new Date(r.ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) + (r.comment ? '. ' + r.comment : '') + (r.pending ? '. Ждёт связи' : ''))))] : null,
      el('h2', null, 'Учётная запись'),
      el('button', { class: 'quiet', id: 'logout', onclick: logout }, 'Выйти из приложения')),
    tabs('day'));
}

function orderList() {
  const orders = activeOrders();
  if (!orders.length) {
    return el('div', { class: 'empty-card' }, el('b', null, 'Заказов нет'), 'Когда логист назначит заказ, придёт уведомление.');
  }
  const today = localDate();
  const onMap = orders.filter((o) => o.lat !== null && o.lat !== undefined);
  return el('section', { id: 'orders' }, el('h2', null, 'Заказы: ' + orders.length),
    onMap.length > 1 ? el('button', { class: 'nav', id: 'route-all', onclick: () => chooseNavigator(onMap) }, icon('route'), `Весь маршрут (${onMap.length} точ.)`) : null,
    orders.map((o, i) => el('article', { class: 'order' + (i === 0 ? ' first' : '') },
      el('div', { class: 'num' }, i === 0 ? 'Следующий' : (i + 1) + '-й по маршруту'),
      el('div', { class: 'head' }, el('span', { class: 'client' }, o.client), el('span', { class: 'lit' }, fmt(o.liters) + ' л')),
      o.address ? el('div', { class: 'addr' }, o.address) : null,
      (o.day !== today || orderNote(o)) ? el('div', { class: 'meta' }, (o.day !== today ? 'На ' + o.day.split('-').reverse().join('.') + '. ' : '') + orderNote(o)) : null,
      o.phone ? el('div', { class: 'phone-text' }, o.phone) : null,
      o.private ? el('div', { class: 'pay' }, 'Частное лицо: возьмите оплату') : null,
      (phonesOf(o).length || canRoute(o)) ? el('div', { class: 'acts go' },
        phonesOf(o).length ? el('button', { class: 'call', onclick: () => callClient(o) }, icon('phone'), 'Позвонить') : null,
        canRoute(o) ? el('button', { class: 'nav', onclick: () => chooseNavigator([o]) }, icon('route'), 'Маршрут') : null) : null,
      el('div', { class: 'acts' },
        el('button', { class: 'quiet fail', onclick: () => failOrder(o) }, 'Не получилось'),
        el('button', { class: 'main order-done', onclick: () => { if (vehicleReady()) { orderForUnload = o; screen = 'unload'; render(); } } }, icon('check'), 'Слил газ')))));
}
// Телефон заявки из amoCRM показывается кнопкой звонка; в примечании он только для старых версий приложения
function orderNote(o) {
  const note = o.note || '';
  return o.phone && note.startsWith('Тел.: ' + o.phone) ? note.slice(('Тел.: ' + o.phone).length).replace(/^\.\s*/, '') : note;
}
// ---- звонок клиенту и маршрут в Яндекс Навигаторе или Яндекс Картах
const hasPoint = (o) => o.lat !== null && o.lat !== undefined && o.lon !== null && o.lon !== undefined;
const canRoute = (o) => hasPoint(o) || !!(o.address || '').trim();
function phonesOf(o) {
  return (o.phone || '').split(',').map((x) => x.trim()).filter((x) => x.replace(/\D/g, '').length >= 5);
}
function dial(number) {
  const tel = number.replace(/[^\d+]/g, '');
  try { if (native && native.dial) { native.dial(tel); return; } } catch (e) { /* нет моста */ }
  location.href = 'tel:' + tel;
}
function sheet(title, options) {
  const close = () => overlay.replaceChildren();
  overlay.replaceChildren(el('div', { class: 'dialog', role: 'dialog', 'aria-modal': 'true', onclick: (e) => { if (e.target === e.currentTarget) close(); } },
    el('div', { class: 'sheet' }, el('p', null, title),
      options.map(([id, label, act]) => el('button', { id, class: 'pick-app', onclick: () => { close(); act(); } }, label)),
      el('button', { class: 'quiet', onclick: close }, 'Отмена'))));
}
function callClient(o) {
  const list = phonesOf(o);
  if (list.length === 1) { dial(list[0]); return; }
  sheet('Позвонить: ' + o.client, list.map((x, i) => ['call-' + i, x, () => dial(x)]));
}
function chooseNavigator(list) {
  const title = list.length === 1 ? 'Маршрут: ' + list[0].client : `Маршрут через ${list.length} точ.`;
  sheet(title, [['route-navi', 'Яндекс Навигатор', () => openNavigator(list, 'navi')],
    ['route-maps', 'Яндекс Карты', () => openNavigator(list, 'maps')]]);
}
function openNavigator(list, app) {
  // Точки в порядке объезда, который рассчитал логист; маршрут строится от текущего места водителя.
  // Без координат — по адресу: Навигатор и Карты сами найдут его.
  const withPoints = list.filter(hasPoint);
  const points = withPoints.slice(0, 50).map((o) => `${o.lat},${o.lon}`).join('~');
  const text = withPoints.length ? '' : (list[0].address || '').trim().slice(0, 300);
  try {
    if (native && native.route) { native.route(app || 'maps', points, text); return; }
    if (points && native && native.openRoute) { native.openRoute(points); return; }
  } catch (e) { /* нет моста */ }
  const to = points || encodeURIComponent(text);
  window.open(`https://yandex.ru/maps/?rtext=~${to}&rtt=auto`, '_blank');
}
function failOrder(o) {
  const reason = el('input', { id: 'fail-reason', maxlength: 200 });
  const error = el('div', { class: 'error', role: 'alert' });
  const close = () => overlay.replaceChildren();
  overlay.replaceChildren(el('div', { class: 'dialog', role: 'dialog', 'aria-modal': 'true' }, el('div', null,
    el('p', null, `Почему не получилось слить газ: ${o.client}?`),
    el('label', { for: 'fail-reason' }, 'Причина'), reason, error,
    el('div', { class: 'twin' },
      el('button', { class: 'quiet', onclick: close }, 'Отмена'),
      el('button', { class: 'main', id: 'fail-send', onclick: () => {
        if (!reason.value.trim()) { error.textContent = 'Напишите причину: её увидит логист.'; return; }
        enqueue({ type: 'order_fail', order_id: o.id, reason: reason.value.trim(), at: new Date().toISOString(), label: o.client });
        close(); toast('Логист увидит, что заказ не выполнен'); render(); sync();
      } }, 'Отправить логисту')))));
  reason.focus();
}

function renderUnload(u) {
  const canDelete = dayOf(u) === localDate() && u.invoice !== 'done';
  return el('article', { class: 'trip' + (u.pending ? ' pending' : '') },
    el('div', { class: 'top2' }, el('span', null, u.client), el('b', null, fmt(u.liters) + ' л')),
    el('div', { class: 'meta' }, u.plate + (u.trailer ? ' + ' + u.trailer : '') + (u.paid === 'yes' ? '. Оплачено' : (u.paid === 'no' ? '. Не оплачено' : '')) + (u.comment ? '. ' + u.comment : '')),
    u.lpg_pct !== null && u.lpg_pct !== undefined ? el('div', { class: 'meta lpg' }, 'В цистерне после выгрузки: ' + fmt(u.lpg_pct) + ' % (датчик)') : null,
    el('div', { class: 'act' },
      el('span', { class: 'meta' }, u.pending ? 'Ждёт связи' : ''),
      canDelete ? el('button', {
        class: 'small quiet',
        onclick: () => ask(`Удалить выгрузку ${u.client}, ${fmt(u.liters)} л?`, 'Удалить', () => deleteUnload(u)),
      }, 'Удалить') : null));
}

// ----- дозаправка цистерны газом в пути -----
function refuelsFor(day) {
  const known = new Set((me().refuels || []).map((r) => r.id));
  const pending = data.queue.filter((op) => op.type === 'refuel' && !known.has(op.id))
    .map((op) => ({ id: op.id, plate: op.plate, station: op.station_label, liters: op.liters, ts: op.at, comment: op.comment, pending: true }));
  const list = pending.concat(me().refuels || []).filter((r) => localDate(new Date(r.ts)) === day)
    .sort((a, b) => (new Date(a.ts) < new Date(b.ts) ? 1 : -1));
  return { list, total: list.reduce((sum, r) => sum + Math.round(r.liters * 100), 0) / 100 };
}
function renderRefuelForm() {
  const stations = me().stations || [];
  const error = el('div', { class: 'error', role: 'alert', id: 'refuel-error' });
  const station = el('select', { id: 'station' }, el('option', { value: '' }, 'Где дозаправились'),
    stations.map((st) => el('option', { value: st.id }, st.name + (st.address ? ' — ' + st.address : ''))));
  if (stations.length === 1) station.value = String(stations[0].id);
  const liters = el('input', { id: 'refuel-liters', inputmode: 'decimal', autocomplete: 'off' });
  const comment = el('input', { id: 'refuel-comment', maxlength: 200 });
  const form = el('form', {
    id: 'refuel-form',
    onsubmit: (event) => {
      event.preventDefault();
      const st = stations.find((x) => String(x.id) === station.value);
      if (!st) { error.textContent = 'Выберите точку заправки.'; return; }
      const value = Number(liters.value.replace(',', '.').trim());
      if (!Number.isFinite(value) || value <= 0) { error.textContent = 'Укажите, сколько литров газа долили.'; return; }
      if (value > 200000) { error.textContent = 'Слишком большое значение: не больше 200 000 л.'; return; }
      const now = new Date();
      enqueue({ type: 'refuel', id: uuid(), plate: data.plate, station_id: st.id, station_label: st.name,
        liters: Math.round(value * 100) / 100, at: now.toISOString(), comment: comment.value.trim() });
      screen = 'main';
      toast('Дозаправка записана');
      render();
      sync();
    },
  },
  el('div', { class: 'target' }, el('b', null, 'Газ в цистерну'), el('div', null, data.plate + (data.trailer ? ' + ' + data.trailer : ''))),
  stations.length ? [el('label', { for: 'station' }, 'Точка заправки'), station] : el('p', { class: 'empty' }, 'Логист ещё не добавил точки заправки газом.'),
  el('label', { for: 'refuel-liters' }, 'Сколько газа долили в цистерну, литров'), liters,
  el('label', { for: 'refuel-comment' }, 'Комментарий, если нужен'), comment,
  error);
  show(
    topBar(() => { screen = 'main'; render(); }),
    page('solo', el('h1', null, 'Дозаправка газом'), form),
    stations.length ? dock(true, el('button', { class: 'main', type: 'submit', form: 'refuel-form', id: 'save-refuel' }, icon('check'), 'Записать дозаправку')) : null);
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
  if (op.type === 'order_fail') return `Отказ от заказа ${op.label}`;
  if (op.type === 'refuel') return `Дозаправка газом ${op.station_label}, ${fmt(op.liters)} л`;
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
  }, plate(p), (m.tanks || {})[p] ? el('span', { class: 'cap' }, fmt(m.tanks[p]) + ' л') : null, data[kind] === p ? icon('check') : null));
  const ready = data.plate && !(needsTrailer() && !data.trailer);
  show(
    topBar(() => { screen = 'main'; render(); }),
    page('solo',
      el('h1', null, 'На какой машине работаете?'),
      el('p', null),
      m.plates.length ? list('plate', m.plates)
        : el('p', { class: 'empty' }, 'Списка машин пока нет. Нужна связь с сервером, а машины добавляет весовщик в справочник ТС.'),
      data.plate && m.trailers.length ? [el('h2', null, needsTrailer() ? 'Какой прицеп подцепили?' : 'Прицеп, если есть'),
        list('trailer', m.trailers),
        data.trailer ? el('button', { class: 'quiet', id: 'no-trailer', onclick: () => choose('trailer', '') }, 'Без прицепа') : null] : null),
    ready ? dock(true, el('button', { class: 'main', id: 'vehicle-done', onclick: () => { screen = 'main'; render(); } }, icon('check'), 'Готово')) : null,
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
  const leave = () => { screen = 'main'; weighResult = null; render(); sync(); };
  if (weighResult) {
    const r = weighResult;
    show(topBar(leave),
      page('has-dock',
        el('div', { class: 'done-mark' }, icon('check'), r.direction === 'arrival' ? 'Приезд записан' : 'Выезд записан'),
        el('div', { class: 'scale' }, el('div', { class: 'weight', id: 'fixed-weight' }, fmt(r.weight) + ' кг'),
          el('div', { class: 'meta' }, 'Можно съезжать с весов')),
        r.gas ? el('div', { class: 'info' }, 'Газа в машине по весам: ', el('b', null, fmt(r.gas.liters) + ' л')) : null,
        r.photo ? el('img', { class: 'photo', alt: 'Фото машины на весах', src: session.server + r.photo }) : null),
      dock(false, el('button', { class: 'main', id: 'weigh-done', onclick: leave }, 'Готово')),
      tabs('weigh'));
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
    el('button', { class: 'go', id: 'departure', onclick: () => weigh('departure') }, 'Уехал с базы'));
  show(topBar(leave),
    page('has-dock tall',
      el('h1', null, 'Взвешивание'),
      el('div', { class: 'scale' },
        el('div', { class: 'weight', id: 'live-weight' }, ''),
        el('div', { class: 'meta', id: 'live-hint' }, '')),
      el('p', { class: 'hint' }, 'Заедьте на весы, остановитесь и нажмите кнопку. Вес записывается с весов базы.'),
      error),
    dock(false, el('div', { class: 'weigh-btns' }, buttons)),
    tabs('weigh'));
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
    onchange: () => { newWrap.hidden = select.value !== NEW; payWrap.hidden = !isPrivate(); if (!newWrap.hidden) newName.focus(); },
  },
  el('option', { value: '' }, 'Выберите клиента'),
  clients.map((c) => el('option', { value: c.id }, c.name)),
  el('option', { value: NEW }, 'Другой клиент'));
  const order = orderForUnload;
  const amount = el('input', { id: 'liters', inputmode: 'decimal', autocomplete: 'off', value: order ? String(order.liters) : '' });
  const comment = el('input', { id: 'comment', maxlength: 200 });
  // Пломба на ёмкости клиента и гарантийный талон — видит логист в отчёте и в заявке
  const seal = el('input', { id: 'seal', maxlength: 40, autocomplete: 'off', autocapitalize: 'characters' });
  const warranty = el('input', { type: 'checkbox', id: 'warranty' });
  // Частное лицо платит на месте: водитель отмечает, получил ли оплату
  let paid = null;
  const payButtons = [
    el('button', { type: 'button', id: 'paid-yes', onclick: () => setPaid(true) }, 'Оплатил'),
    el('button', { type: 'button', id: 'paid-no', onclick: () => setPaid(false) }, 'Не оплатил')];
  function setPaid(value) {
    paid = value;
    payButtons[0].className = value ? 'on-choice' : '';
    payButtons[1].className = value ? '' : 'on-choice';
  }
  const payWrap = el('div', { id: 'pay', hidden: true }, el('label', null, 'Оплата'), el('div', { class: 'twin' }, payButtons));
  const isPrivate = () => {
    if (order) return !!order.private;
    if (select.value === NEW) return true;
    const c = m.clients.find((x) => String(x.id) === select.value);
    return !!(c && c.private);
  };

  const store = (litersValue, clientId, label) => {
    const now = new Date();
    const op = {
      type: 'unload', id: uuid(), plate: data.plate, trailer: data.trailer, liters: litersValue,
      at: now.toISOString(), day: localDate(now), comment: comment.value.trim(), client_label: label,
      seal: seal.value.trim(), warranty: warranty.checked,
    };
    if (clientId) op.client_id = clientId; else op.client_name = label;
    if (order) op.order_id = order.id;
    if (isPrivate() && paid !== null) op.paid = paid;
    enqueue(op);
    orderForUnload = null;
    selectedDate = op.day;
    screen = 'main';
    toast('Выгрузка записана');
    render();
    sync();
  };
  const form = el('form', {
    id: 'unload-form',
    onsubmit: (event) => {
      event.preventDefault();
      error.textContent = '';
      let clientId = null;
      let label;
      if (order) {
        clientId = order.client_id; label = order.client;
      } else if (select.value === NEW) {
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
      if (isPrivate() && paid === null && (order || select.value !== NEW)) { error.textContent = 'Отметьте, оплатил ли клиент.'; return; }
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
  order ? el('div', { class: 'target' }, el('b', null, order.client), order.address ? el('div', null, order.address) : null, el('div', { class: 'lit' }, 'По заказу: ' + fmt(order.liters) + ' л'))
    : [el('label', { for: 'client' }, 'Кому слили'), select, newWrap],
  el('label', { for: 'liters' }, 'Сколько слили, литров'), amount,
  el('div', { class: 'chips' },
    order ? el('button', { type: 'button', onclick: () => { amount.value = String(order.liters); } }, 'Как в заказе: ' + fmt(order.liters) + ' л') : null,
    left !== null && left > 0 ? el('button', { type: 'button', id: 'all-left', onclick: () => { amount.value = String(left); } }, 'Весь остаток: ' + fmt(left) + ' л') : null),
  payWrap,
  el('label', { for: 'seal' }, 'Номер пломбы'), seal,
  el('label', { class: 'check-row', for: 'warranty' }, warranty, el('span', null, 'Гарантийный талон выдан')),
  el('label', { for: 'comment' }, 'Комментарий, если нужен'), comment,
  error);
  payWrap.hidden = !isPrivate();
  show(
    topBar(() => { screen = 'main'; render(); }),
    page('solo', el('h1', null, order ? 'Выгрузка по заказу' : 'Новая выгрузка'), form),
    dock(true, el('button', { class: 'main', type: 'submit', form: 'unload-form', id: 'save-unload' }, icon('check'), 'Записать выгрузку')));
}

// ----- запуск -----
if (session) data = load(dataKey(), emptyData());
render();
if (session) { sync(); askPush(); }
window.addEventListener('online', () => sync());
document.addEventListener('visibilitychange', () => { if (!document.hidden && session && (screen === 'main' || screen === 'day')) sync(); });
setInterval(() => { if (session && (screen === 'main' || screen === 'day') && !overlay.childElementCount) sync(); }, 60000);
// Кнопка «Назад» Android вызывает эту функцию из MainActivity.
window.avtovesBack = function () {
  if (overlay.childElementCount) { overlay.replaceChildren(); return true; }
  if (session && screen !== 'main' && !weighBusy) { screen = 'main'; weighResult = null; render(); return true; }
  return false;
};
