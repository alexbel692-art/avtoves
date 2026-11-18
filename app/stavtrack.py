"""Ставтрэк (WiaFleet): уровень топлива по ДУТ в момент взвешивания.

При каждом взвешивании запрашивается последняя позиция машины с рассчитанными значениями датчиков
и сохраняется, сколько литров топлива было в баке. Это убирает из сверки газа ошибку от того,
что машина уехала с полным баком, а вернулась с полупустым.

API (https://wiafleet.online/api): POST /session/token, GET /devices, GET /devices/{id}/sensors, GET /positions.
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
from datetime import datetime, timezone

log = logging.getLogger("stavtrack")

DEFAULTS = {"enabled": False, "url": "https://wiafleet.online/api", "email": "", "password": "",
            "fuel_density": 0.84, "max_age_min": 15,
            # литров топлива, которые уже есть в таре ТС из справочника (0 — тара без топлива)
            "fuel_in_tare_l": 0,
            # по каким словам в названии датчика узнать показания СУГ в цистерне
            "lpg_words": "СУГ, ППМ, газ, жидк, продукт, уровень, плотн"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS st_links (plate TEXT PRIMARY KEY, device_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS weigh_fuel (
    weighing_id INTEGER PRIMARY KEY,
    plate     TEXT NOT NULL,
    liters    REAL,            -- NULL: показание получить не удалось
    pos_time  TEXT,            -- время данных трекера (UTC)
    age_s     INTEGER,
    note      TEXT NOT NULL DEFAULT '',
    lpg       TEXT NOT NULL DEFAULT ''   -- показания датчиков СУГ (JSON)
);
-- Сколько СУГ осталось в цистерне сразу после выгрузки клиенту (датчик ППМ через Ставтрэк)
CREATE TABLE IF NOT EXISTS unload_lpg (
    unload_id TEXT PRIMARY KEY,
    pct       REAL,                      -- заполнение цистерны, %
    readings  TEXT NOT NULL DEFAULT '[]',
    note      TEXT NOT NULL DEFAULT '',
    taken     TEXT NOT NULL
);
"""
LOOKALIKE = str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ")


def plate_key(text) -> str:
    """Госномер без пробелов и региона; латиница и кириллица считаются одинаково."""
    clean = re.sub(r"[\s\-_]+", "", str(text)).upper().translate(LOOKALIKE)
    found = re.search(r"[А-Я]\d{3}[А-Я]{2}|[А-Я]{2}\d{4}", clean)      # машина или прицеп
    return found.group(0) if found else clean


def clean(v):
    """Число из датчика: округляем; явная ошибка расчёта (датчик отключён, значения 10^27) — «нет данных»."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        if v != v or abs(v) > 1e9:
            return None
        return round(v, 2) if isinstance(v, float) else v
    text = str(v).strip()
    return text[:40] if text else None


def lpg_pct(readings: list[dict], tanks: dict) -> float | None:
    """Заполнение цистерны в процентах по показаниям датчиков СУГ.
    Датчик в % — берётся как есть; в литрах — делится на объём цистерны из справочника ТС.
    Бочка (прицеп) важнее машины: газ везут в ней."""
    order = sorted(readings, key=lambda r: r.get("src") != "прицеп")
    for r in order:
        v, unit, name = r.get("value"), str(r.get("unit") or "").lower(), str(r.get("name") or "").lower()
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        if "%" in unit or ("%" in name and not unit):
            if 0 <= v <= 110:
                return round(float(v), 1)
        elif unit in ("л", "l", "литр", "литры", "литров") or "объ" in name:
            tank = tanks.get(r.get("src"))
            if tank and v >= 0:
                return round(float(v) / float(tank) * 100, 1)
    return None


class StError(Exception):
    pass


class Stavtrack:
    def __init__(self, db):
        self.db = db
        with db.lock:
            db.conn.executescript(SCHEMA)
            cols = {r[1] for r in db.conn.execute("PRAGMA table_info(weigh_fuel)")}
            if "lpg" not in cols:
                db.conn.execute("ALTER TABLE weigh_fuel ADD COLUMN lpg TEXT NOT NULL DEFAULT ''")
            db.conn.commit()
        self.lock = threading.Lock()
        self.token = None
        self.token_for = None
        self.devices_cache: tuple[float, list] = (0.0, [])
        self.sensors_cache: dict[int, tuple[float, list]] = {}
        self.positions_cache: tuple[float, dict] = (0.0, {})

    # ---- настройки (хранятся в базе; пароль наружу не отдаётся)
    def settings(self) -> dict:
        rows = self.db.q("SELECT value FROM kv WHERE key='stavtrack'")
        out = dict(DEFAULTS)
        if rows:
            out.update(json.loads(rows[0]["value"]))
        return out

    def save(self, new: dict):
        cur = self.settings()
        cur.update({k: v for k, v in new.items() if k in DEFAULTS})
        self.db.x("INSERT INTO kv(key, value) VALUES('stavtrack', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(cur, ensure_ascii=False),))
        with self.lock:
            self.token = None
            self.devices_cache = (0.0, [])
            self.sensors_cache = {}

    def ready(self) -> bool:
        s = self.settings()
        return bool(s["enabled"] and s["email"] and s["password"])

    # ---- запросы
    def _http(self, method: str, url: str, body=None, token: str | None = None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as ex:
            raise StError(f"Ставтрэк ответил ошибкой {ex.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise StError("Нет связи со Ставтрэком") from None

    def _login(self, s: dict) -> str:
        try:
            res = self._http("POST", s["url"].rstrip("/") + "/session/token",
                             {"email": s["email"], "password": s["password"]})
        except StError as ex:
            if "ошибкой 4" in str(ex):
                raise StError("Ставтрэк не принял логин или пароль") from None
            raise
        tok = ((res or {}).get("token") or {}).get("token")
        if not tok:
            raise StError("Ставтрэк не выдал токен")
        return tok

    def call(self, path: str, params: dict | None = None):
        s = self.settings()
        if not (s["email"] and s["password"]):
            raise StError("Не заданы логин и пароль Ставтрэка")
        url = s["url"].rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
        who = (s["url"], s["email"], s["password"])
        for attempt in (1, 2):
            with self.lock:
                if self.token is None or self.token_for != who:
                    self.token, self.token_for = self._login(s), who
                tok = self.token
            try:
                return self._http("GET", url, token=tok)
            except StError as ex:
                if "ошибкой 401" in str(ex) and attempt == 1:
                    with self.lock:
                        self.token = None       # токен истёк: входим заново
                    continue
                raise
        raise StError("Не удалось войти в Ставтрэк")

    def devices(self) -> list[dict]:
        ts, cached = self.devices_cache
        if time.time() - ts < 600 and cached:
            return cached
        got = [{"id": d["id"], "name": str(d.get("name") or "")} for d in (self.call("/devices") or [])]
        self.devices_cache = (time.time(), got)
        return got

    def sensors(self, device_id: int) -> list[dict]:
        """Все датчики устройства, как они настроены в Ставтрэке (ДУТ, уровень в бочке, зажигание и т. д.)."""
        ts, cached = self.sensors_cache.get(device_id, (0.0, None))
        if cached is not None and time.time() - ts < 600:
            return cached
        got = list(self.call(f"/devices/{device_id}/sensors") or [])
        self.sensors_cache[device_id] = (time.time(), got)
        return got

    def fuel_sensors(self, device_id: int) -> list[dict]:
        return [s for s in self.sensors(device_id) if s.get("type") == "fuel"]

    @staticmethod
    def _value(values: dict, sensor: dict):
        # значение датчика лежит под его названием; на случай другого формата пробуем id и параметр
        for key in (sensor.get("name"), str(sensor.get("id")), sensor.get("param")):
            v = values.get(key) if key is not None else None
            if isinstance(v, dict):
                v = v.get("value")
            if v is not None and not isinstance(v, (dict, list)):
                return key, v
        return None, None

    def readings(self, device_id: int, pos: dict | None) -> list[dict]:
        """Показания всех видимых датчиков устройства для карты: [{name, type, value, unit}]."""
        values = (pos or {}).get("sensorsAttributes") or {}
        out, used = [], set()
        for sensor in sorted(self.sensors(device_id), key=lambda x: (x.get("orderId") is None, x.get("orderId") or 0)):
            if sensor.get("visible") is False or sensor.get("type") == "driver_assignment":
                continue
            key, v = self._value(values, sensor)
            if key is not None:
                used.add(key)
            out.append({"name": str(sensor.get("name") or sensor.get("param") or "Датчик"), "type": sensor.get("type") or "",
                        "value": clean(v), "unit": sensor.get("unit") or ""})
        # датчики в Ставтрэке не описаны — показываем то, что прислал трекер
        if not used:
            for key, v in values.items():
                if not isinstance(v, (dict, list)) and v is not None:
                    out.append({"name": str(key), "type": "", "value": clean(v), "unit": ""})
        return out[:40]

    def positions(self) -> dict[int, dict]:
        """Последние позиции всех устройств учётной записи (для карты), не чаще раза в 30 секунд."""
        ts, cached = self.positions_cache
        if time.time() - ts < 30 and cached:
            return cached
        got = {p["deviceId"]: p for p in (self.call("/positions") or []) if p.get("deviceId") is not None}
        self.positions_cache = (time.time(), got)
        return got

    # ---- привязка машины к устройству
    def device_for(self, plate: str) -> dict | None:
        devs = self.devices()
        link = self.db.q("SELECT device_id FROM st_links WHERE plate=?", (plate,))
        if link:
            return next((d for d in devs if d["id"] == link[0]["device_id"]), None)
        taken = {r["device_id"] for r in self.db.q("SELECT device_id FROM st_links")}
        key = plate_key(plate)
        same = [d for d in devs if plate_key(d["name"]) == key and d["id"] not in taken]
        return same[0] if len(same) == 1 else None      # только однозначное совпадение по госномеру

    # ---- показание ДУТ
    def fuel(self, plate: str) -> dict:
        """{'liters': float|None, 'pos_time': str|None, 'age_s': int|None, 'note': str, 'raw': dict}"""
        s = self.settings()
        dev = self.device_for(plate)
        if not dev:
            return {"liters": None, "pos_time": None, "age_s": None, "raw": {},
                    "note": "машина не привязана к устройству Ставтрэка"}
        pos = self.call("/positions", {"deviceIds": dev["id"]}) or []
        pos = next((p for p in pos if p.get("deviceId") == dev["id"]), pos[0] if pos else None)
        return self.fuel_of(dev, pos)

    def fuel_of(self, dev: dict, pos: dict | None) -> dict:
        s = self.settings()
        sensors = self.fuel_sensors(dev["id"])
        if not pos:
            return {"liters": None, "pos_time": None, "age_s": None, "raw": {}, "note": "нет данных от трекера"}
        values = pos.get("sensorsAttributes") or {}
        when = pos.get("deviceTime") or pos.get("satTime")
        age = None
        if when:
            try:
                moment = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                age = int((datetime.now(timezone.utc) - moment).total_seconds())
            except ValueError:
                pass
        out = {"liters": None, "pos_time": when, "age_s": age, "raw": values, "device": dev["name"], "note": ""}
        if not sensors:
            out["note"] = "у устройства нет топливного датчика"
            return out
        total, found = 0.0, 0
        for sensor in sensors:
            _, v = self._value(values, sensor)
            v = clean(v)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total += float(v)
                found += 1
        if not found:
            out["note"] = "в данных трекера нет показания ДУТ"
            return out
        if found < len(sensors):
            out["note"] = f"показания есть только у {found} из {len(sensors)} ДУТ"
            return out
        out["liters"] = round(total, 1)
        if age is None or age > float(s["max_age_min"]) * 60:
            out["note"] = "данные трекера устарели" if age is not None else "неизвестно время данных трекера"
        return out

    # ---- показания датчиков СУГ (уровень, объём, масса в цистерне) машины и её бочки
    def lpg_readings(self, plate: str, trailer: str = "") -> list[dict]:
        words = [w.strip().lower() for w in str(self.settings().get("lpg_words") or "").split(",") if w.strip()]
        out = []
        for who, number in (("машина", plate), ("прицеп", trailer)):
            dev = self.device_for(number) if number else None
            if not dev:
                continue
            pos = self.call("/positions", {"deviceIds": dev["id"]}) or []
            pos = next((p for p in pos if p.get("deviceId") == dev["id"]), None)
            for r in self.readings(dev["id"], pos):
                if r["type"] in ("fuel", "ignition", "voltage", "odometer", "driver_assignment"):
                    continue
                if any(w in r["name"].lower() for w in words):
                    out.append({**r, "src": who})
        return out

    def tanks(self, plate: str, trailer: str = "") -> dict:
        out = {}
        for who, number in (("машина", plate), ("прицеп", trailer)):
            v = self.db.vehicle(number) if number else None
            if v and v.get("tank_l"):
                out[who] = v["tank_l"]
        return out

    # ---- вызывается после каждой выгрузки клиенту: сколько СУГ осталось в цистерне
    def on_unload(self, unload_id: str, plate: str, trailer: str, ts: str):
        if not plate or not self.ready():
            return

        def work():
            now = datetime.now()
            note, readings, pct = "", [], None
            try:
                moment = datetime.fromisoformat(ts)
            except ValueError:
                moment = now
            # Выгрузку отправили без связи и позже: нынешнее показание к ней уже не относится
            if (now - moment).total_seconds() > float(self.settings()["max_age_min"]) * 60:
                note = "выгрузка пришла позже — показание датчика не взято"
            else:
                try:
                    readings = self.lpg_readings(plate, trailer)
                    pct = lpg_pct(readings, self.tanks(plate, trailer))
                    if not readings:
                        note = "нет показаний датчиков СУГ"
                    elif pct is None:
                        note = "датчик не в % и не указан объём цистерны"
                except StError as ex:
                    note = str(ex)
                except Exception:  # noqa: BLE001 — выгрузка не должна зависеть от Ставтрэка
                    log.exception("Ошибка запроса датчиков СУГ при выгрузке")
                    note = "ошибка запроса к Ставтрэку"
            self.db.x("INSERT OR REPLACE INTO unload_lpg(unload_id, pct, readings, note, taken) VALUES(?,?,?,?,?)",
                      (unload_id, pct, json.dumps(readings, ensure_ascii=False), note,
                       now.isoformat(timespec="seconds")))
        threading.Thread(target=work, name="stavtrack-lpg", daemon=True).start()

    # ---- вызывается после каждого взвешивания
    def on_weighing(self, weighing_id: int, plate: str):
        if not plate or not self.ready():
            return

        def work():
            try:
                f = self.fuel(plate)
            except StError as ex:
                f = {"liters": None, "pos_time": None, "age_s": None, "note": str(ex)}
            except Exception:  # noqa: BLE001 — весовая не должна зависеть от Ставтрэка
                log.exception("Ошибка запроса ДУТ")
                return
            w = self.db.q("SELECT trailer, trip_id FROM weighings WHERE id=?", (weighing_id,))
            trailer, trip_id = (w[0]["trailer"] or "", w[0]["trip_id"]) if w else ("", None)
            try:
                lpg = self.lpg_readings(plate, trailer)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка запроса датчиков СУГ")
                lpg = []
            usable = f["liters"] if not f["note"] else None      # устаревшее показание в расчёт не идёт
            note = f["note"] + (f" ({f['liters']} л)" if f["note"] and f["liters"] is not None else "")
            self.db.x("INSERT OR REPLACE INTO weigh_fuel(weighing_id, plate, liters, pos_time, age_s, note, lpg) "
                      "VALUES(?,?,?,?,?,?,?)", (weighing_id, plate, usable, f["pos_time"], f["age_s"], note,
                                                json.dumps(lpg, ensure_ascii=False)))
            if trip_id and usable is not None:
                self.db.recalc_gas(trip_id)          # газ по весам — за вычетом топлива в баках
        threading.Thread(target=work, name="stavtrack", daemon=True).start()
