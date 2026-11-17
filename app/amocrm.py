"""amoCRM: заявки на доставку газа для кабинета логиста.

Сервер сам забирает из amoCRM сделки с датой доставки — как лист «Сделки» в Google-таблице логиста —
и раскладывает их по дням и базам. Логист выбирает водителя, заявка становится заказом в приложении водителя.

Доступ — «долгосрочный токен» интеграции amoCRM (amoМаркет → Создать интеграцию → Ключи и доступы).
API v4: GET /api/v4/leads, /leads/custom_fields, /leads/pipelines, /contacts, /users.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger("amocrm")

LOST = 143        # «Закрыто и не реализовано» — такие сделки в заявки не попадают
ROLES = [
    ("day", "Дата доставки"),
    ("district", "Район"),
    ("highway", "Направление (шоссе)"),
    ("base", "База"),
    ("liters", "Литры план"),
    ("conditions", "Особые условия"),
    ("address", "Адрес"),
]
# По каким словам в названии поля amoCRM узнать его роль (первое совпадение)
GUESS = {
    "day": ["дата доставки", "дата заправки", "дата отгрузки", "дата заказа", "дата"],
    "district": ["район"],
    "highway": ["направлен", "шоссе"],
    "base": ["база", "базы"],
    "liters": ["литры план", "литры, план", "литров", "литры", "объём", "объем"],
    "conditions": ["особые услов", "услови", "комментарий логист", "примечан"],
    "address": ["адрес"],
}
# База по району, если в сделке она не указана (таблица «Лист2» логиста)
BASE_BY_DISTRICT = {
    "Балашиха г.о.": "Мотяково", "Богородский г.о.": "Мотяково", "Бронницы г.о.": "Мотяково",
    "Владимирская область": "Мотяково", "Волоколамский": "Онуфриево", "Воскресенский": "Мотяково",
    "Домодедовский": "Мотяково", "Егорьевский": "Мотяково", "Зарайский": "Мотяково", "Истринский": "Онуфриево",
    "Калужская область": "Онуфриево", "Каширский": "Мотяково", "Клинский": "Онуфриево", "Коломенский": "Мотяково",
    "Красногорский": "Онуфриево", "Ленинский": "Мотяково", "Лотошинский": "Онуфриево", "Луховицкий": "Мотяково",
    "Люберецкий": "Мотяково", "Можайский": "Онуфриево", "Мытищинский": "Мотяково", "Наро-Фоминский": "Онуфриево",
    "Ногинский": "Мотяково", "Одинцовский": "Онуфриево", "Озерский": "Мотяково", "Орехово-Зуевский": "Мотяково",
    "Павлово-Посадский": "Мотяково", "Подольский": "Мотяково", "Пушкинский": "Мотяково", "Раменский": "Мотяково",
    "Рузский": "Онуфриево", "Рязанская область": "Мотяково", "Сергиево-Посадский": "Мотяково",
    "Серебряно-Прудский": "Мотяково", "Серпуховский": "Мотяково", "Смоленская область": "Онуфриево",
    "Солнечногорский": "Онуфриево", "Ступинский": "Мотяково", "Талдомский": "Онуфриево",
    "Тульская область": "Мотяково", "Чеховский": "Мотяково", "Шатурский": "Мотяково", "Шаховской": "Онуфриево",
    "Щелковский": "Мотяково", "Ярославская область": "Мотяково",
}
DEFAULTS = {"enabled": False, "domain": "", "token": "", "pipeline_id": 0, "interval_min": 5, "days_back": 45,
            "fields": {}, "cursor": 0, "last_sync": "", "last_error": "", "last_count": 0}
SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS amo_leads (
    lead_id    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    pipeline_id INTEGER,
    status_id  INTEGER,
    status     TEXT NOT NULL DEFAULT '',
    lost       INTEGER NOT NULL DEFAULT 0,
    day        TEXT,                       -- дата доставки (ГГГГ-ММ-ДД); без даты сделка — ещё не заявка
    district   TEXT NOT NULL DEFAULT '',
    highway    TEXT NOT NULL DEFAULT '',
    base       TEXT NOT NULL DEFAULT '',
    liters     REAL,
    client     TEXT NOT NULL DEFAULT '',
    contact_id INTEGER,
    phone      TEXT NOT NULL DEFAULT '',
    manager    TEXT NOT NULL DEFAULT '',
    conditions TEXT NOT NULL DEFAULT '',
    address    TEXT NOT NULL DEFAULT '',
    updated    INTEGER NOT NULL DEFAULT 0, -- updated_at в amoCRM
    synced     TEXT NOT NULL,
    order_id   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_amo_leads_day ON amo_leads(day);
"""
LEAD_COLS = ("name", "pipeline_id", "status_id", "status", "lost", "day", "district", "highway", "base", "liters",
             "client", "contact_id", "phone", "manager", "conditions", "address", "updated")


class AmoError(Exception):
    pass


def clean_domain(value: str) -> str:
    """«https://rosavtonomg.amocrm.ru/leads/...» или «rosavtonomg» → «rosavtonomg.amocrm.ru»."""
    v = str(value or "").strip().lower()
    v = re.sub(r"^https?://", "", v).split("/")[0]
    if v and "." not in v:
        v += ".amocrm.ru"
    if v and not re.fullmatch(r"[a-z0-9-]+\.(amocrm\.(ru|com)|kommo\.com)", v):
        raise AmoError("Адрес amoCRM: укажите адрес вида company.amocrm.ru")
    return v


def as_day(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        return datetime.fromtimestamp(int(value)).date().isoformat()
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return None


def as_liters(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    found = re.search(r"\d[\d\s]*(?:[.,]\d+)?", str(value))
    if not found:
        return None
    num = float(re.sub(r"\s", "", found.group(0)).replace(",", "."))
    return num if num > 0 else None


def field_text(cf: dict | None) -> str:
    if not cf:
        return ""
    vals = [v.get("value") for v in cf.get("values") or [] if v.get("value") not in (None, "")]
    return ", ".join(" ".join(str(v).split()) for v in vals)


def guess_fields(fields: list[dict]) -> dict:
    out, used = {}, set()
    for role, words in GUESS.items():
        for w in words:
            hit = next((f for f in fields if f["id"] not in used and w in f["name"].casefold()
                        and (role != "day" or f["type"] in ("date", "date_time", "birthday"))), None)
            if hit:
                out[role] = hit["id"]
                used.add(hit["id"])
                break
    return out


class AmoCRM:
    def __init__(self, db, opener=None):
        self.db = db
        self.opener = opener or urllib.request.urlopen
        with db.lock:
            db.conn.executescript(SCHEMA)
            db.conn.commit()
        self.lock = threading.Lock()          # одна синхронизация за раз
        self.on_lead = None                   # вызывается для каждой изменившейся заявки с заказом
        self.thread = None
        self.wake = threading.Event()

    # ---- настройки (ключ доступа наружу не отдаётся)
    def settings(self) -> dict:
        rows = self.db.q("SELECT value FROM kv WHERE key='amocrm'")
        out = json.loads(json.dumps(DEFAULTS))
        if rows:
            out.update(json.loads(rows[0]["value"]))
        return out

    def save(self, new: dict):
        cur = self.settings()
        resync = any(k in new and new[k] != cur.get(k) for k in ("domain", "pipeline_id", "fields", "days_back"))
        cur.update({k: v for k, v in new.items() if k in DEFAULTS})
        if resync:
            cur["cursor"] = 0                 # поля или воронка другие — сделки перечитываются заново
        self.db.x("INSERT INTO kv(key, value) VALUES('amocrm', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(cur, ensure_ascii=False),))
        self.wake.set()

    def public(self) -> dict:
        s = self.settings()
        return ({k: v for k, v in s.items() if k not in ("token", "cursor")}
                | {"token_set": bool(s["token"]), "ready": self.ready(), "roles": ROLES})

    def ready(self) -> bool:
        s = self.settings()
        return bool(s["enabled"] and s["domain"] and s["token"] and s["fields"].get("day"))

    # ---- запросы
    def _get(self, path: str, params: list | None = None, s: dict | None = None):
        s = s or self.settings()
        if not s["domain"] or not s["token"]:
            raise AmoError("amoCRM не подключён: укажите адрес и ключ доступа")
        url = f"https://{s['domain']}/api/v4/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + s["token"],
                                                   "Accept": "application/json", "User-Agent": "AvtoVes"})
        for attempt in range(3):
            try:
                with self.opener(req, timeout=20) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as ex:
                if ex.code == 204:
                    return None
                if ex.code == 429 and attempt < 2:
                    time.sleep(1.5)
                    continue
                if ex.code in (401, 403):
                    raise AmoError("amoCRM не принял ключ доступа: проверьте долгосрочный токен и адрес") from None
                raise AmoError(f"amoCRM ответил ошибкой {ex.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                raise AmoError("Нет связи с amoCRM") from None
        raise AmoError("amoCRM ограничил число запросов, повторите позже")

    def _pages(self, path: str, key: str, params: list, s: dict, limit_pages: int = 200):
        page = 1
        while page <= limit_pages:
            data = self._get(path, params + [("limit", 250), ("page", page)], s)
            items = ((data or {}).get("_embedded") or {}).get(key) or []
            yield from items
            if not items or not ((data or {}).get("_links") or {}).get("next"):
                return
            page += 1

    def meta(self, s: dict | None = None) -> dict:
        """Поля сделок и воронки — для настройки, какое поле что значит."""
        s = s or self.settings()
        fields = [{"id": f["id"], "name": f.get("name") or "", "type": f.get("type") or ""}
                  for f in self._pages("leads/custom_fields", "custom_fields", [], s)]
        pipelines = [{"id": p["id"], "name": p.get("name") or "",
                      "statuses": {st["id"]: st.get("name") or "" for st in (p.get("_embedded") or {}).get("statuses") or []}}
                     for p in self._pages("leads/pipelines", "pipelines", [], s, 5)]
        return {"fields": fields, "pipelines": pipelines, "guess": guess_fields(fields)}

    # ---- синхронизация
    def sync(self, full: bool = False) -> dict:
        with self.lock:
            s = self.settings()
            started = int(time.time())
            try:
                res = self._sync(s, full)
            except AmoError as ex:
                self._mark(last_error=str(ex), last_sync=datetime.now().isoformat(timespec="seconds"))
                raise
            self._mark(last_error="", last_sync=datetime.now().isoformat(timespec="seconds"),
                       last_count=res["count"], cursor=max(res["cursor"], 0) or started - 60)
            return res

    def _mark(self, **kw):
        cur = self.settings()
        cur.update(kw)
        self.db.x("INSERT INTO kv(key, value) VALUES('amocrm', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(cur, ensure_ascii=False),))

    def _sync(self, s: dict, full: bool) -> dict:
        fmap = {k: int(v) for k, v in (s.get("fields") or {}).items() if v}
        if not fmap.get("day"):
            raise AmoError("Не выбрано поле «Дата доставки» в настройках amoCRM")
        since = 0 if full else int(s.get("cursor") or 0)
        if not since:
            since = int(time.time()) - int(s.get("days_back") or 45) * 86400
        params = [("with", "contacts"), ("filter[updated_at][from]", since), ("order[updated_at]", "asc")]
        if s.get("pipeline_id"):
            params.append(("filter[pipeline_id]", int(s["pipeline_id"])))
        leads = list(self._pages("leads", "leads", params, s))
        if not leads:
            return {"count": 0, "changed": 0, "cursor": since}
        statuses = {}
        for p in self.meta(s)["pipelines"]:
            statuses.update(p["statuses"])
        users = {u["id"]: u.get("name") or "" for u in self._pages("users", "users", [], s, 20)}
        contact_ids = []
        for lead in leads:
            cs = (lead.get("_embedded") or {}).get("contacts") or []
            main = next((c for c in cs if c.get("is_main")), cs[0] if cs else None)
            lead["_contact"] = main["id"] if main else None
            if main and main["id"] not in contact_ids:
                contact_ids.append(main["id"])
        contacts = {}
        for i in range(0, len(contact_ids), 50):
            chunk = contact_ids[i:i + 50]
            for c in self._pages("contacts", "contacts", [("filter[id][]", x) for x in chunk], s, 5):
                phones = []
                for cf in c.get("custom_fields_values") or []:
                    if cf.get("field_code") == "PHONE":
                        phones += [" ".join(str(v.get("value")).split()) for v in cf.get("values") or [] if v.get("value")]
                contacts[c["id"]] = {"name": " ".join(str(c.get("name") or "").split()), "phone": ", ".join(phones)}
        changed = 0
        cursor = since
        for lead in leads:
            cursor = max(cursor, int(lead.get("updated_at") or 0))
            if self._store(lead, fmap, statuses, users, contacts):
                changed += 1
        return {"count": len(leads), "changed": changed, "cursor": cursor}

    def _store(self, lead: dict, fmap: dict, statuses: dict, users: dict, contacts: dict) -> bool:
        by_id = {cf.get("field_id"): cf for cf in lead.get("custom_fields_values") or []}

        def val(role):
            return field_text(by_id.get(fmap.get(role)))

        day_cf = by_id.get(fmap["day"])
        day = as_day((day_cf.get("values") or [{}])[0].get("value")) if day_cf else None
        contact = contacts.get(lead.get("_contact")) or {}
        district = val("district")
        row = {
            "name": " ".join(str(lead.get("name") or "").split()),
            "pipeline_id": lead.get("pipeline_id"), "status_id": lead.get("status_id"),
            "status": statuses.get(lead.get("status_id"), ""),
            "lost": int(lead.get("status_id") == LOST or bool(lead.get("is_deleted"))),
            "day": day, "district": district, "highway": val("highway"),
            "base": val("base") or BASE_BY_DISTRICT.get(district, ""),
            "liters": as_liters(val("liters")),
            "client": contact.get("name") or " ".join(str(lead.get("name") or "").split()),
            "contact_id": lead.get("_contact"), "phone": contact.get("phone", ""),
            "manager": users.get(lead.get("responsible_user_id"), ""),
            "conditions": val("conditions")[:500], "address": val("address")[:300],
            "updated": int(lead.get("updated_at") or 0),
        }
        old = self.db.q("SELECT * FROM amo_leads WHERE lead_id=?", (lead["id"],))
        if not old and (row["day"] is None or row["lost"]):
            return False                       # не заявка и не была ею
        now = datetime.now().isoformat(timespec="seconds")
        if old:
            same = all(old[0][k] == row[k] for k in LEAD_COLS if k != "updated")
            self.db.x(f"UPDATE amo_leads SET {', '.join(k + '=?' for k in LEAD_COLS)}, synced=? WHERE lead_id=?",
                      (*row.values(), now, lead["id"]))
            if same:
                return False
            if old[0]["order_id"] and self.on_lead:
                try:
                    self.on_lead(lead["id"])
                except Exception:  # noqa: BLE001 — ошибка одной заявки не должна останавливать обмен
                    log.exception("Не удалось обновить заказ по сделке %s", lead["id"])
        else:
            self.db.x(f"INSERT INTO amo_leads(lead_id, {', '.join(LEAD_COLS)}, synced) VALUES(?{', ?' * len(LEAD_COLS)}, ?)",
                      (lead["id"], *row.values(), now))
        return True

    # ---- обмен по расписанию
    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._loop, name="amocrm", daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            s = self.settings()
            if self.ready():
                try:
                    self.sync()
                except AmoError as ex:
                    log.warning("amoCRM: %s", ex)
                except Exception:  # noqa: BLE001
                    log.exception("amoCRM: сбой обмена")
            self.wake.wait(max(1, int(s.get("interval_min") or 5)) * 60)
            self.wake.clear()
