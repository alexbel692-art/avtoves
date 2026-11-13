"""База данных SQLite: взвешивания, рейсы, справочник ТС, водители."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS weighings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    plate       TEXT NOT NULL DEFAULT '',
    plate_conf  REAL,
    weight      REAL NOT NULL,
    side_in     TEXT,
    auto        INTEGER NOT NULL DEFAULT 1,
    trip_id     INTEGER,
    snapshots   TEXT NOT NULL DEFAULT '',
    operator    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS trips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plate       TEXT NOT NULL DEFAULT '',
    w1_id       INTEGER,
    w1_ts       TEXT,
    w1          REAL,
    w2_id       INTEGER,
    w2_ts       TEXT,
    w2          REAL,
    gross       REAL,
    tare        REAL,
    net         REAL,
    direction   TEXT NOT NULL DEFAULT '',   -- in: ввоз (первое брутто), out: вывоз (первая тара)
    cargo       TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '',
    driver      TEXT NOT NULL DEFAULT '',
    comment     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',  -- open / closed / cancelled
    exported_1c INTEGER NOT NULL DEFAULT 0,
    updated     TEXT
);
CREATE INDEX IF NOT EXISTS ix_trips_plate ON trips(plate, status);
CREATE INDEX IF NOT EXISTS ix_trips_ts ON trips(w1_ts);
CREATE TABLE IF NOT EXISTS vehicles (
    plate        TEXT PRIMARY KEY,
    model        TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '',
    driver       TEXT NOT NULL DEFAULT '',
    tare         REAL,              -- известная тара (необязательно)
    blocked      INTEGER NOT NULL DEFAULT 0,
    comment      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS drivers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    phone        TEXT NOT NULL DEFAULT '',
    pin_hash     TEXT NOT NULL UNIQUE,      -- HMAC от PIN (сам PIN не хранится)
    plate        TEXT NOT NULL DEFAULT '',  -- машина по умолчанию
    active       INTEGER NOT NULL DEFAULT 1,
    comment      TEXT NOT NULL DEFAULT '',
    last_login   TEXT
);
"""


# Колонки, добавленные после первой версии (старые базы дополняются при запуске)
MIGRATIONS = {
    "trips": {"gas_tare": "REAL", "gas_in": "REAL", "gas_out": "REAL", "driver_id": "INTEGER",
              "trailer": "TEXT NOT NULL DEFAULT ''"},
    # source: auto / operator / driver;  kind: arrival (приехал) / departure (уехал) / ''
    "weighings": {"source": "TEXT NOT NULL DEFAULT ''", "kind": "TEXT NOT NULL DEFAULT ''",
                  "driver_id": "INTEGER", "plate_seen": "TEXT NOT NULL DEFAULT ''",
                  "trailer": "TEXT NOT NULL DEFAULT ''"},
    # kind: '' — одиночка (в т.ч. с бочками на борту), tractor — тягач (ездит с прицепом), trailer — прицеп/полуприцеп
    "vehicles": {"tank_l": "REAL", "kind": "TEXT NOT NULL DEFAULT ''"},
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.commit()
        self.lock = threading.RLock()

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    # ---------------------------------------------------------- взвешивание
    def add_weighing(self, plate: str, conf: float | None, weight: float, side_in: str | None,
                     auto: bool, snapshots: str, trip_hours: int, *, kind: str = "", source: str = "",
                     driver: dict | None = None, plate_seen: str = "", trailer: str = "") -> dict:
        """Сохраняет взвешивание и связывает его с рейсом.
        kind='' — первое/второе определяется само (есть открытый рейс этой машины → закрываем);
        kind='arrival' — «приехал на базу»: всегда новый рейс;
        kind='departure' — «уехал с базы»: закрываем открытый рейс машины."""
        ts = now_iso()
        wid = self.x(
            "INSERT INTO weighings(ts,plate,plate_conf,weight,side_in,auto,snapshots,source,kind,driver_id,"
            "plate_seen,operator,trailer) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, plate, conf, weight, side_in, int(auto), snapshots, source, kind,
             (driver or {}).get("id"), plate_seen, (driver or {}).get("name", ""), trailer))
        trip = None
        if plate and kind != "arrival":
            since = (datetime.now() - timedelta(hours=trip_hours)).isoformat(timespec="seconds")
            rows = self.q("SELECT * FROM trips WHERE plate=? AND status='open' AND w1_ts>=? "
                          "ORDER BY id DESC LIMIT 1", (plate, since))
            trip = rows[0] if rows else None
        if trip:
            w1 = trip["w1"]
            gross, tare = max(w1, weight), min(w1, weight)
            self.x("UPDATE trips SET w2_id=?,w2_ts=?,w2=?,gross=?,tare=?,net=?,direction=?,"
                   "status='closed',updated=?,trailer=CASE WHEN trailer='' THEN ? ELSE trailer END WHERE id=?",
                   (wid, ts, weight, gross, tare, gross - tare, "in" if w1 >= weight else "out",
                    now_iso(), trailer, trip["id"]))
            tid = trip["id"]
        else:
            veh = self.vehicle(plate) if plate else None
            drv_name = (driver or {}).get("name") or (veh or {}).get("driver", "")
            comment = "убытие без отметки приезда" if kind == "departure" else ""
            tid = self.x("INSERT INTO trips(plate,trailer,w1_id,w1_ts,w1,counterparty,driver,driver_id,comment,updated) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (plate, trailer, wid, ts, weight, (veh or {}).get("counterparty", ""),
                          drv_name, (driver or {}).get("id"), comment, now_iso()))
        self.x("UPDATE weighings SET trip_id=? WHERE id=?", (tid, wid))
        self.recalc_gas(tid)
        # Подписчики на взвешивание (например, запрос уровня топлива): их сбой не должен мешать весовой
        for hook in getattr(self, "on_weighing", []):
            try:
                hook(wid, plate)
            except Exception:  # noqa: BLE001
                pass
        return {"weighing_id": wid, "trip": self.trip(tid)}

    # ---------------------------------------------------------- газ (газовозы)
    def recalc_gas(self, tid: int, force: bool = False):
        """Газ в машине = вес − тара пустой машины (из справочника ТС).
        Тара запоминается в рейсе при первом расчёте, чтобы история не менялась
        при последующей правке справочника (force=True — пересчитать по текущей таре)."""
        t = self.trip(tid)
        if not t:
            return
        tare = t.get("gas_tare")
        if tare is None or force:
            tare = self.combo_tare(t["plate"], t.get("trailer") or "")
        if not tare:
            self.x("UPDATE trips SET gas_tare=NULL,gas_in=NULL,gas_out=NULL WHERE id=?", (tid,))
            return
        g_in = round(t["w1"] - tare, 1) if t["w1"] is not None else None
        g_out = round(t["w2"] - tare, 1) if t["w2"] is not None else None
        self.x("UPDATE trips SET gas_tare=?,gas_in=?,gas_out=? WHERE id=?", (tare, g_in, g_out, tid))

    def combo_tare(self, plate: str, trailer: str = "") -> float | None:
        """Тара пустой сцепки: машина (тягач) + прицеп. Нет тары хоть одного — газ не считается."""
        veh = self.vehicle(plate) if plate else None
        tare = (veh or {}).get("tare")
        if not tare:
            return None
        if trailer:
            tr = (self.vehicle(trailer) or {}).get("tare")
            if not tr:
                return None
            tare += tr
        return tare

    def tank_volume(self, plate: str, trailer: str = "") -> float | None:
        """Объём цистерны: у прицепа (полуприцеп-цистерна) или у одиночной машины."""
        for p in ([trailer] if trailer else []) + [plate]:
            v = (self.vehicle(p) or {}).get("tank_l") if p else None
            if v:
                return v
        return None

    def set_tare_from_weighing(self, wid: int) -> dict:
        """«Это пустая машина»: вес взвешивания становится тарой в справочнике.
        Со сцепкой: если известна тара тягача — запоминаем тару прицепа (и наоборот)."""
        w = self.q("SELECT * FROM weighings WHERE id=?", (wid,))
        if not w or not w[0]["plate"]:
            raise ValueError("у взвешивания нет номера ТС")
        w = w[0]
        trailer = w.get("trailer") or (self.trip(w["trip_id"]) or {}).get("trailer", "") if w["trip_id"] else ""
        if trailer:
            truck_tare = (self.vehicle(w["plate"]) or {}).get("tare")
            trailer_tare = (self.vehicle(trailer) or {}).get("tare")
            if truck_tare:
                target, value = trailer, w["weight"] - truck_tare
            elif trailer_tare:
                target, value = w["plate"], w["weight"] - trailer_tare
            else:
                raise ValueError(f"это взвешивание сцепки {w['plate']} + {trailer}: укажите в справочнике тару "
                                 "тягача или прицепа — вторую программа посчитает сама")
            if value <= 0:
                raise ValueError("получается отрицательная тара — проверьте тару в справочнике")
            veh = self.vehicle(target) or {"plate": target, "kind": "trailer" if target == trailer else ""}
            veh["tare"] = round(value, 1)
        else:
            target = w["plate"]
            veh = self.vehicle(target) or {"plate": target}
            veh["tare"] = w["weight"]
        self.save_vehicle(veh)
        # рейсы без расчёта газа — досчитываем (и тот, по которому нажали)
        for t in self.q("SELECT id FROM trips WHERE (plate=? OR trailer=?) AND (gas_tare IS NULL OR id=?)",
                        (target, target, w["trip_id"])):
            self.recalc_gas(t["id"], force=True)
        return self.vehicle(target)

    def trip(self, tid: int) -> dict | None:
        r = self.q("SELECT * FROM trips WHERE id=?", (tid,))
        return r[0] if r else None

    def update_trip(self, tid: int, fields: dict) -> dict | None:
        allowed = {"plate", "trailer", "cargo", "counterparty", "driver", "comment", "status"}
        f = {k: v for k, v in fields.items() if k in allowed}
        if not f:
            return self.trip(tid)
        f["updated"] = now_iso()
        f["exported_1c"] = 0          # изменённый рейс уйдёт в 1С повторно
        sets = ",".join(f"{k}=?" for k in f)
        self.x(f"UPDATE trips SET {sets} WHERE id=?", (*f.values(), tid))
        if "plate" in f:
            self.x("UPDATE weighings SET plate=? WHERE trip_id=?", (f["plate"], tid))
        if "trailer" in f:
            self.x("UPDATE weighings SET trailer=? WHERE trip_id=?", (f["trailer"], tid))
        if "plate" in f or "trailer" in f:
            self.recalc_gas(tid, force=True)
        return self.trip(tid)

    def close_trip_manually(self, tid: int) -> dict | None:
        """Закрыть рейс с одним взвешиванием, взяв тару из справочника ТС."""
        t = self.trip(tid)
        if not t or t["status"] != "open":
            return t
        veh = self.vehicle(t["plate"]) or {}
        tare = veh.get("tare")
        if tare:
            self.x("UPDATE trips SET gross=?,tare=?,net=?,direction='in',status='closed',updated=? WHERE id=?",
                   (t["w1"], tare, t["w1"] - tare, now_iso(), tid))
            self.recalc_gas(tid)
        return self.trip(tid)

    def trips(self, date_from: str | None = None, date_to: str | None = None,
              plate: str | None = None, status: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM trips WHERE 1=1", []
        if date_from:
            sql += " AND w1_ts>=?"; args.append(date_from)
        if date_to:
            sql += " AND w1_ts<?"; args.append(date_to)
        if plate:
            sql += " AND (plate LIKE ? OR trailer LIKE ?)"; args += [f"%{plate.upper()}%"] * 2
        if status:
            sql += " AND status=?"; args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        return self.q(sql, args)

    def weighings_of_trip(self, tid: int) -> list[dict]:
        return self.q("SELECT * FROM weighings WHERE trip_id=? ORDER BY id", (tid,))

    # ---------------------------------------------------------- 1С
    def trips_for_1c(self, only_new: bool = True, date_from: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trips WHERE status='closed'"
        args: list = []
        if only_new:
            sql += " AND exported_1c=0"
        if date_from:
            sql += " AND w1_ts>=?"; args.append(date_from)
        return self.q(sql + " ORDER BY id LIMIT 1000", args)

    def ack_1c(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        self.x(f"UPDATE trips SET exported_1c=1 WHERE id IN ({marks})", ids)
        return len(ids)

    # ---------------------------------------------------------- справочник ТС
    def vehicle(self, plate: str) -> dict | None:
        r = self.q("SELECT * FROM vehicles WHERE plate=?", (plate,))
        return r[0] if r else None

    def vehicles(self) -> list[dict]:
        return self.q("SELECT * FROM vehicles ORDER BY plate")

    def save_vehicle(self, v: dict):
        v = {"model": "", "counterparty": "", "driver": "", "tare": None, "tank_l": None, "blocked": 0,
             "comment": "", "kind": "", **v}
        v["kind"] = v.get("kind") if v.get("kind") in ("tractor", "trailer") else ""
        old = self.vehicle(v["plate"])
        self.x("INSERT INTO vehicles(plate,model,counterparty,driver,tare,tank_l,blocked,comment,kind) "
               "VALUES(:plate,:model,:counterparty,:driver,:tare,:tank_l,:blocked,:comment,:kind) "
               "ON CONFLICT(plate) DO UPDATE SET model=excluded.model,counterparty=excluded.counterparty,"
               "driver=excluded.driver,tare=excluded.tare,tank_l=excluded.tank_l,blocked=excluded.blocked,"
               "comment=excluded.comment,kind=excluded.kind",
               {k: v[k] for k in ("plate", "model", "counterparty", "driver", "tare", "tank_l", "blocked", "comment",
                                  "kind")})
        # тару указали впервые — досчитываем газ по рейсам с этой машиной или прицепом, где его ещё нет
        if v["tare"] and (not old or not old.get("tare")):
            for t in self.q("SELECT id FROM trips WHERE (plate=? OR trailer=?) AND gas_tare IS NULL",
                            (v["plate"], v["plate"])):
                self.recalc_gas(t["id"])

    # ---------------------------------------------------------- водители
    def drivers(self) -> list[dict]:
        rows = self.q("SELECT id,name,phone,plate,active,comment,last_login FROM drivers ORDER BY name")
        return rows

    def driver(self, did: int) -> dict | None:
        r = self.q("SELECT id,name,phone,plate,active,comment,last_login FROM drivers WHERE id=?", (did,))
        return r[0] if r else None

    def driver_by_pin_hash(self, h: str) -> dict | None:
        r = self.q("SELECT id,name,phone,plate,active,comment,last_login FROM drivers WHERE pin_hash=?", (h,))
        return r[0] if r else None

    def save_driver(self, d: dict, pin_hash: str | None) -> int:
        f = {k: d.get(k, "") for k in ("name", "phone", "plate", "comment")}
        f["active"] = 1 if d.get("active", True) else 0
        if d.get("id"):
            sets = ",".join(f"{k}=?" for k in f)
            args = list(f.values())
            if pin_hash:
                sets += ",pin_hash=?"
                args.append(pin_hash)
            self.x(f"UPDATE drivers SET {sets} WHERE id=?", (*args, int(d["id"])))
            return int(d["id"])
        return self.x("INSERT INTO drivers(name,phone,plate,comment,active,pin_hash) VALUES(?,?,?,?,?,?)",
                      (f["name"], f["phone"], f["plate"], f["comment"], f["active"], pin_hash))

    def delete_driver(self, did: int):
        self.x("DELETE FROM drivers WHERE id=?", (did,))

    def driver_login(self, did: int):
        self.x("UPDATE drivers SET last_login=? WHERE id=?", (now_iso(), did))

    def driver_weighings(self, did: int, since: str) -> list[dict]:
        return self.q("SELECT w.*, t.net, t.status AS trip_status FROM weighings w LEFT JOIN trips t ON t.id=w.trip_id "
                      "WHERE w.driver_id=? AND w.ts>=? ORDER BY w.id DESC LIMIT 50", (did, since))

    def open_trip(self, plate: str, trip_hours: int) -> dict | None:
        since = (datetime.now() - timedelta(hours=trip_hours)).isoformat(timespec="seconds")
        r = self.q("SELECT * FROM trips WHERE plate=? AND status='open' AND w1_ts>=? ORDER BY id DESC LIMIT 1",
                   (plate, since))
        return r[0] if r else None

    def delete_vehicle(self, plate: str):
        self.x("DELETE FROM vehicles WHERE plate=?", (plate,))
