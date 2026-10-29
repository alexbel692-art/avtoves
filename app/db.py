"""База данных SQLite: взвешивания, рейсы, справочник ТС."""
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
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

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
                     auto: bool, snapshots: str, trip_hours: int) -> dict:
        """Сохраняет взвешивание и связывает его с рейсом (первое/второе взвешивание)."""
        ts = now_iso()
        wid = self.x(
            "INSERT INTO weighings(ts,plate,plate_conf,weight,side_in,auto,snapshots) VALUES(?,?,?,?,?,?,?)",
            (ts, plate, conf, weight, side_in, int(auto), snapshots))
        trip = None
        if plate:
            since = (datetime.now() - timedelta(hours=trip_hours)).isoformat(timespec="seconds")
            rows = self.q("SELECT * FROM trips WHERE plate=? AND status='open' AND w1_ts>=? "
                          "ORDER BY id DESC LIMIT 1", (plate, since))
            trip = rows[0] if rows else None
        if trip:
            w1 = trip["w1"]
            gross, tare = max(w1, weight), min(w1, weight)
            self.x("UPDATE trips SET w2_id=?,w2_ts=?,w2=?,gross=?,tare=?,net=?,direction=?,"
                   "status='closed',updated=? WHERE id=?",
                   (wid, ts, weight, gross, tare, gross - tare, "in" if w1 >= weight else "out",
                    now_iso(), trip["id"]))
            tid = trip["id"]
        else:
            veh = self.vehicle(plate) if plate else None
            tid = self.x("INSERT INTO trips(plate,w1_id,w1_ts,w1,counterparty,driver,updated) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (plate, wid, ts, weight, (veh or {}).get("counterparty", ""),
                          (veh or {}).get("driver", ""), now_iso()))
        self.x("UPDATE weighings SET trip_id=? WHERE id=?", (tid, wid))
        return {"weighing_id": wid, "trip": self.trip(tid)}

    def trip(self, tid: int) -> dict | None:
        r = self.q("SELECT * FROM trips WHERE id=?", (tid,))
        return r[0] if r else None

    def update_trip(self, tid: int, fields: dict) -> dict | None:
        allowed = {"plate", "cargo", "counterparty", "driver", "comment", "status"}
        f = {k: v for k, v in fields.items() if k in allowed}
        if not f:
            return self.trip(tid)
        f["updated"] = now_iso()
        f["exported_1c"] = 0          # изменённый рейс уйдёт в 1С повторно
        sets = ",".join(f"{k}=?" for k in f)
        self.x(f"UPDATE trips SET {sets} WHERE id=?", (*f.values(), tid))
        if "plate" in f:
            self.x("UPDATE weighings SET plate=? WHERE trip_id=?", (f["plate"], tid))
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
        return self.trip(tid)

    def trips(self, date_from: str | None = None, date_to: str | None = None,
              plate: str | None = None, status: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM trips WHERE 1=1", []
        if date_from:
            sql += " AND w1_ts>=?"; args.append(date_from)
        if date_to:
            sql += " AND w1_ts<?"; args.append(date_to)
        if plate:
            sql += " AND plate LIKE ?"; args.append(f"%{plate.upper()}%")
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
        self.x("INSERT INTO vehicles(plate,model,counterparty,driver,tare,blocked,comment) "
               "VALUES(:plate,:model,:counterparty,:driver,:tare,:blocked,:comment) "
               "ON CONFLICT(plate) DO UPDATE SET model=excluded.model,counterparty=excluded.counterparty,"
               "driver=excluded.driver,tare=excluded.tare,blocked=excluded.blocked,comment=excluded.comment",
               {"model": "", "counterparty": "", "driver": "", "tare": None, "blocked": 0, "comment": "", **v})

    def delete_vehicle(self, plate: str):
        self.x("DELETE FROM vehicles WHERE plate=?", (plate,))
