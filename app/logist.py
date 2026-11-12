"""АвтоВес 2: выгрузки у клиентов, кабинет логиста, приложение водителя, уведомления.

Эти адреса (/api/app/*, /api/logist/*, /logist) — единственное, что открывается в интернет
через отдельный «внешний» порт. Светофоры, настройки и рабочее место оператора остаются
только на основном порту в локальной сети.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__
from . import settings as st
from .plates import normalize as norm_plate
from .weighing import DriverError

STATIC = Path(__file__).parent / "static"
MAX_LITERS = 200_000
DRIVER_DAYS = 365          # водитель входит один раз
LOGIST_DAYS = 14
BACKDATE_DAYS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL UNIQUE,
    legal     INTEGER NOT NULL DEFAULT 0,     -- юрлицо: после выгрузки нужен счёт
    legal_name TEXT NOT NULL DEFAULT '',
    manager   TEXT NOT NULL DEFAULT '',
    reviewed  INTEGER NOT NULL DEFAULT 1,     -- 0: добавил водитель, логист ещё не проверил
    active    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS unloads (
    id        TEXT PRIMARY KEY,               -- код с телефона: повторная отправка не задваивает
    driver_id INTEGER NOT NULL,
    driver    TEXT NOT NULL DEFAULT '',
    plate     TEXT NOT NULL DEFAULT '',
    trailer   TEXT NOT NULL DEFAULT '',
    client_id INTEGER NOT NULL,
    liters    REAL NOT NULL,
    ts        TEXT NOT NULL,                  -- когда слили (время сервера)
    comment   TEXT NOT NULL DEFAULT '',
    created   TEXT NOT NULL,
    deleted   TEXT,
    invoice   TEXT NOT NULL DEFAULT '',       -- '' / pending / done
    invoiced_at TEXT,
    invoiced_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_unloads_ts ON unloads(ts);
CREATE INDEX IF NOT EXISTS ix_unloads_plate ON unloads(plate, ts);
CREATE TABLE IF NOT EXISTS logists (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    login     TEXT NOT NULL UNIQUE,
    name      TEXT NOT NULL DEFAULT '',
    pass_hash TEXT NOT NULL,
    admin     INTEGER NOT NULL DEFAULT 0,
    active    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS notes_done (kind TEXT NOT NULL, ref TEXT NOT NULL, PRIMARY KEY(kind, ref));
"""


class Bad(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def text(value, field: str, max_len: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise Bad(f"{field}: укажите текст")
    value = " ".join(value.split())
    if required and not value:
        raise Bad(f"{field}: заполните поле")
    if len(value) > max_len:
        raise Bad(f"{field}: не больше {max_len} символов")
    return value


def liters_value(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise Bad("Литры: укажите число")
    try:
        amount = float(str(value).replace(",", ".").strip())
    except ValueError as exc:
        raise Bad("Литры: укажите число") from exc
    if not (amount == amount) or amount in (float("inf"), float("-inf")) or amount <= 0:
        raise Bad("Литры: значение должно быть больше нуля")
    if amount > MAX_LITERS:
        raise Bad(f"Литры: не больше {MAX_LITERS} л")
    return round(amount, 2)


def local_ts(value) -> str:
    """Время с телефона (ISO, обычно UTC) → время сервера, как во всей базе АвтоВес."""
    if not value:
        return now_iso()
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise Bad("Некорректное время") from exc
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    now = datetime.now()
    if moment > now + timedelta(minutes=10):
        moment = now
    if moment < now - timedelta(days=BACKDATE_DAYS):
        raise Bad("Выгрузка слишком старая, обратитесь к логисту")
    return moment.isoformat(timespec="seconds")


def safe_cell(value):
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


class Tokens:
    """Подписанные токены входа. В подпись входит отпечаток пароля: смена пароля отзывает все входы."""

    def __init__(self, data_dir: str | Path):
        f = Path(data_dir) / "logist.key"
        if not f.exists():
            f.write_text(secrets.token_hex(32), encoding="ascii")
        self.key = bytes.fromhex(f.read_text(encoding="ascii").strip())
        self.fails: dict[str, list[float]] = {}

    @staticmethod
    def pwver(stored: str) -> str:
        return hashlib.sha256(stored.encode()).hexdigest()[:12]

    def make(self, kind: str, uid: int, stored: str, days: int) -> str:
        body = f"{kind}.{uid}.{int(time.time()) + days * 86400}.{self.pwver(stored)}"
        return body + "." + hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()[:32]

    def parse(self, token: str | None, kind: str) -> tuple[int, str] | None:
        try:
            k, uid, exp, ver, sig = (token or "").split(".")
            body = f"{k}.{uid}.{exp}.{ver}"
            good = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()[:32]
            if k == kind and hmac.compare_digest(sig, good) and int(exp) > time.time():
                return int(uid), ver
        except ValueError:
            pass
        return None

    # Перебор пароля: 5 ошибок за 5 минут на один логин — блокировка (адрес за прокси у всех общий)
    def blocked(self, key: str) -> bool:
        now = time.time()
        self.fails[key] = [t for t in self.fails.get(key, []) if now - t < 300]
        return len(self.fails[key]) >= 5

    def failed(self, key: str):
        self.fails.setdefault(key, []).append(time.time())
        if len(self.fails) > 5000:
            self.fails.clear()

    def ok(self, key: str):
        self.fails.pop(key, None)


def init_db(db):
    with db.lock:
        db.conn.executescript(SCHEMA)
        have = {r[1] for r in db.conn.execute("PRAGMA table_info(drivers)")}
        for col in ("login", "pass_hash"):
            if col not in have:
                db.conn.execute(f"ALTER TABLE drivers ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        db.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_drivers_login ON drivers(login) WHERE login<>''")
        db.conn.commit()


# ---------------------------------------------------------------- сверка с весами
def legs(db, density: float, tol_pct: float, tol_l: float, date_from: str | None = None,
         date_to: str | None = None, plate: str | None = None) -> list[dict]:
    """Поездки по клиентам: от взвешивания «уехал с базы» до следующего «приехал».
    По весам отвезено = газ при выезде − газ при возвращении; по водителю — сумма его выгрузок."""
    sql, args = "SELECT * FROM trips WHERE status<>'cancelled' AND plate<>'' AND w2_ts IS NOT NULL", []
    if plate:
        sql += " AND plate=?"; args.append(plate)
    trips = db.q(sql + " ORDER BY plate, w2_ts", args)
    out = []
    for t in trips:
        if t.get("gas_out") is None:
            continue
        start = t["w2_ts"]
        if date_from and start < date_from:
            continue
        if date_to and start > date_to:
            continue
        nxt = db.q("SELECT * FROM trips WHERE plate=? AND status<>'cancelled' AND w1_ts>? ORDER BY w1_ts LIMIT 1",
                   (t["plate"], start))
        nxt = nxt[0] if nxt else None
        end = nxt["w1_ts"] if nxt else None
        rows = db.q("SELECT u.*, c.name AS client FROM unloads u JOIN clients c ON c.id=u.client_id "
                    "WHERE u.plate=? AND u.deleted IS NULL AND u.ts>=? AND u.ts<? ORDER BY u.ts",
                    (t["plate"], start, end or "9999"))
        by_driver = round(sum(r["liters"] for r in rows), 2)
        out_l = round(t["gas_out"] / density)
        leg = {"id": t["id"], "plate": t["plate"], "trailer": t.get("trailer") or "", "driver": t.get("driver") or "",
               "start": start, "end": end, "out_l": out_l, "in_l": None, "scale_l": None,
               "driver_l": by_driver, "unloads": len(rows), "diff_l": None, "diff_pct": None,
               "status": "open", "left_l": round(out_l - by_driver)}
        if nxt:
            same = (nxt.get("trailer") or "") == leg["trailer"]
            if nxt.get("gas_in") is None or not same:
                leg["status"] = "unknown"      # нет тары или вернулся с другим прицепом — сравнить нельзя
            else:
                leg["in_l"] = round(nxt["gas_in"] / density)
                leg["scale_l"] = leg["out_l"] - leg["in_l"]
                leg["diff_l"] = round(leg["scale_l"] - by_driver)
                leg["diff_pct"] = round(leg["diff_l"] / leg["scale_l"] * 100, 1) if leg["scale_l"] > 0 else None
                limit = max(tol_l, abs(leg["scale_l"]) * tol_pct / 100)
                leg["status"] = "bad" if abs(leg["diff_l"]) > limit else "ok"
        out.append(leg)
    out.sort(key=lambda x: x["start"], reverse=True)
    return out


def build_routes(ctx, public: bool) -> list[Route]:
    db = ctx.db
    init_db(db)
    tokens = Tokens(ctx.cfg["data_dir"])
    snap_dir = Path(ctx.cfg["data_dir"]) / "snapshots"

    def lcfg() -> dict:
        c = ctx.cfg.get("logist") or {}
        return {"tolerance_pct": float(c.get("tolerance_pct", 2)), "tolerance_l": float(c.get("tolerance_l", 100))}

    def density() -> float:
        return float((ctx.cfg.get("gas") or {}).get("density") or 0.54)

    def handler(fn):
        async def wrapped(request: Request):
            try:
                return await fn(request)
            except Bad as ex:
                return JSONResponse({"error": ex.message}, ex.status)
        return wrapped

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError as exc:
            raise Bad("Ожидается JSON") from exc
        if not isinstance(data, dict):
            raise Bad("Ожидается JSON-объект")
        return data

    def bearer(request: Request) -> str:
        h = request.headers.get("authorization", "")
        return h[7:] if h.startswith("Bearer ") else ""

    # ------------------------------------------------------------ входы
    async def login_common(request: Request, kind: str, table: str, days: int):
        b = await body(request)
        login = text(b.get("login"), "Логин", 60).casefold()
        password = b.get("password") if isinstance(b.get("password"), str) else ""
        key = f"{kind}:{login}"
        if tokens.blocked(key):
            raise Bad("Слишком много попыток. Повторите через 5 минут.", 429)
        rows = db.q(f"SELECT * FROM {table} WHERE login=? AND active=1", (login,))
        row = rows[0] if rows else None
        loop = asyncio.get_running_loop()
        stored = row["pass_hash"] if row and row["pass_hash"] else "pbkdf2$00$00"
        good = await loop.run_in_executor(None, st.check_password, password, stored)
        if not row or not good:
            tokens.failed(key)
            raise Bad("Неверный логин или пароль", 401)
        tokens.ok(key)
        return row, tokens.make(kind, row["id"], row["pass_hash"], days)

    def current(request: Request, kind: str, table: str, token: str | None = None) -> dict:
        parsed = tokens.parse(token if token is not None else bearer(request), kind)
        if parsed:
            rows = db.q(f"SELECT * FROM {table} WHERE id=? AND active=1", (parsed[0],))
            if rows and rows[0]["pass_hash"] and tokens.pwver(rows[0]["pass_hash"]) == parsed[1]:
                return rows[0]
        raise Bad("Войдите заново", 401)

    def driver_of(request: Request, token: str | None = None) -> dict:
        return current(request, "d", "drivers", token)

    def logist_of(request: Request) -> dict:
        return current(request, "l", "logists")

    # ------------------------------------------------------------ приложение водителя
    @handler
    async def app_login(request: Request):
        row, token = await login_common(request, "d", "drivers", DRIVER_DAYS)
        db.driver_login(row["id"])
        ctx.ctrl.event(f"Водитель {row['name']} вошёл в приложение")
        return JSONResponse({"token": token, "driver": {"id": row["id"], "name": row["name"]}})

    def photo_url(snaps: str, token: str) -> str | None:
        files = [x for x in (snaps or "").split(";") if x]
        cam = [x for x in files if not x.endswith("_plate.jpg")]
        pick = (cam or files or [None])[0]
        return f"/api/app/photo/{pick}?t={token}" if pick else None

    def unload_json(r: dict) -> dict:
        return {"id": r["id"], "client_id": r["client_id"], "client": r["client"], "liters": r["liters"],
                "ts": r["ts"], "plate": r["plate"], "trailer": r["trailer"], "comment": r["comment"],
                "driver": r["driver"], "driver_id": r["driver_id"], "invoice": r["invoice"]}

    def combo_of(driver: dict) -> tuple[str, str]:
        last = db.q("SELECT plate, trailer FROM weighings WHERE driver_id=? ORDER BY id DESC LIMIT 1", (driver["id"],))
        if last:
            return last[0]["plate"], last[0].get("trailer") or ""
        return driver.get("plate") or "", ""

    def road_info(plate: str) -> dict | None:
        """Сколько газа в машине по весам с учётом выгрузок после выезда с базы."""
        if not plate:
            return None
        c = lcfg()
        found = legs(db, density(), c["tolerance_pct"], c["tolerance_l"], plate=plate)
        leg = found[0] if found else None
        if not leg or leg["status"] != "open":
            return None
        return {"start": leg["start"], "out_l": leg["out_l"], "driver_l": leg["driver_l"], "left_l": leg["left_l"]}

    @handler
    async def app_me(request: Request):
        d = driver_of(request)
        sc, c = ctx.scale.snapshot(), ctx.ctrl.snapshot()
        token = bearer(request)
        vs = db.vehicles()
        plate, trailer = combo_of(d)
        since = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
        today = datetime.now().strftime("%Y-%m-%dT00:00:00")
        mine = db.q("SELECT u.*, c.name AS client FROM unloads u JOIN clients c ON c.id=u.client_id "
                    "WHERE u.driver_id=? AND u.deleted IS NULL AND u.ts>=? ORDER BY u.ts DESC", (d["id"], since))
        return JSONResponse({
            "version": __version__,
            "driver": {"id": d["id"], "name": d["name"]},
            "plates": sorted({v["plate"] for v in vs if v.get("kind") != "trailer" and not v.get("blocked")}
                             | ({d["plate"]} if d["plate"] else set())),
            "trailers": sorted(v["plate"] for v in vs if v.get("kind") == "trailer"),
            "kinds": {v["plate"]: v.get("kind") or "" for v in vs},
            "tanks": {v["plate"]: v.get("tank_l") for v in vs if v.get("tank_l")},
            "plate": plate, "trailer": trailer,
            "scale": {"connected": sc["connected"], "weight": sc["weight"], "stable": sc["stable"]},
            "state_text": c["state_text"], "warning": c["warning"],
            "clients": db.q("SELECT id, name FROM clients WHERE active=1 ORDER BY name"),
            "unloads": [unload_json(r) for r in mine],
            "road": road_info(plate),
            "weighings": [{"id": w["id"], "ts": w["ts"], "plate": w["plate"], "trailer": w.get("trailer", ""),
                           "weight": w["weight"], "kind": w["kind"], "photo": photo_url(w["snapshots"], token)}
                          for w in db.driver_weighings(d["id"], today)],
        })

    @handler
    async def app_weigh(request: Request):
        d = driver_of(request)
        b = await body(request)
        plate, _ = norm_plate(str(b.get("plate", "")))
        direction = b.get("direction")
        if direction not in ("arrival", "departure"):
            raise Bad("Выберите: приехал или уехал")
        if len(plate) < 6:
            raise Bad("Выберите номер вашей машины")
        trailer = norm_plate(str(b.get("trailer") or ""))[0] if b.get("trailer") else ""
        if trailer and trailer == plate:
            raise Bad("Прицеп не может совпадать с машиной")
        veh = db.vehicle(plate)
        if veh and veh.get("blocked"):
            raise Bad("Машина в стоп-листе — обратитесь к весовщику", 403)
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(
                None, lambda: ctx.ctrl.fix_by_driver(dict(d), plate, direction, trailer=trailer))
        except DriverError as ex:
            raise Bad(str(ex), 409) from ex
        t = db.trip(res["trip"]["id"])
        gas = None
        if t.get("gas_in") is not None:
            kg = t["gas_out"] if t.get("gas_out") is not None else t["gas_in"]
            gas = {"kg": kg, "liters": round(kg / density())}
        return JSONResponse({"ok": True, "weight": res["weight"], "ts": res["ts"], "plate": res["plate"],
                             "trailer": trailer, "direction": direction, "gas": gas,
                             "photo": photo_url(";".join(res.get("snapshots") or []), bearer(request))})

    @handler
    async def app_photo(request: Request):
        driver_of(request, request.query_params.get("t", ""))
        rel = request.path_params["path"]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}/[\w.\-]+\.jpg", rel):
            raise Bad("Не найдено", 404)
        f = snap_dir / rel
        if not f.is_file():
            raise Bad("Не найдено", 404)
        return FileResponse(f)

    def client_id_for(op: dict) -> int:
        if op.get("client_id") is not None:
            rows = db.q("SELECT id FROM clients WHERE id=?", (op["client_id"],))
            if not rows:
                raise Bad("Клиент не найден")
            return rows[0]["id"]
        name = text(op.get("client_name"), "Клиент", 120)
        for row in db.q("SELECT id, name FROM clients"):
            if row["name"].casefold() == name.casefold():
                return row["id"]
        return db.x("INSERT INTO clients(name, reviewed) VALUES(?, 0)", (name,))

    def refresh_invoice(uid: str):
        rows = db.q("SELECT u.invoice, u.deleted, c.legal FROM unloads u JOIN clients c ON c.id=u.client_id "
                    "WHERE u.id=?", (uid,))
        if not rows:
            return
        r = rows[0]
        need = bool(r["legal"]) and not r["deleted"]
        if r["invoice"] == "" and need:
            db.x("UPDATE unloads SET invoice='pending' WHERE id=?", (uid,))
        elif r["invoice"] == "pending" and not need:
            db.x("UPDATE unloads SET invoice='' WHERE id=?", (uid,))

    def apply_unload(d: dict, op: dict):
        uid = text(op.get("id"), "Код выгрузки", 64)
        have = db.q("SELECT driver_id FROM unloads WHERE id=?", (uid,))
        if have:
            if have[0]["driver_id"] != d["id"]:
                raise Bad("Код выгрузки уже занят")
            return
        plate = norm_plate(str(op.get("plate") or ""))[0]
        if len(plate) < 6:
            raise Bad("Не выбрана машина")
        trailer = norm_plate(str(op.get("trailer") or ""))[0] if op.get("trailer") else ""
        amount = liters_value(op.get("liters"))
        ts = local_ts(op.get("at"))
        comment = text(op.get("comment"), "Комментарий", 200, required=False)
        cid = client_id_for(op)
        db.x("INSERT INTO unloads(id,driver_id,driver,plate,trailer,client_id,liters,ts,comment,created) "
             "VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, d["id"], d["name"], plate, trailer, cid, amount, ts, comment, now_iso()))
        refresh_invoice(uid)

    def apply_delete(d: dict, op: dict):
        rows = db.q("SELECT * FROM unloads WHERE id=?", (str(op.get("unload_id")),))
        if not rows or rows[0]["deleted"]:
            return
        r = rows[0]
        if r["driver_id"] != d["id"]:
            raise Bad("Это выгрузка другого водителя")
        if r["created"] < (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds"):
            raise Bad("Выгрузка старше суток: удалить может только логист")
        if r["invoice"] == "done":
            raise Bad("По выгрузке уже выставлен счёт: обратитесь к логисту")
        db.x("UPDATE unloads SET deleted=? WHERE id=?", (now_iso(), r["id"]))
        refresh_invoice(r["id"])

    @handler
    async def app_sync(request: Request):
        d = driver_of(request)
        ops = (await body(request)).get("ops")
        if not isinstance(ops, list) or len(ops) > 500:
            raise Bad("Некорректный список записей")
        results = []
        for op in ops:
            key = op.get("op_id") if isinstance(op, dict) else None
            try:
                if not isinstance(op, dict) or op.get("type") not in ("unload", "delete"):
                    raise Bad("Неизвестный тип записи")
                (apply_unload if op["type"] == "unload" else apply_delete)(d, op)
                results.append({"op_id": key, "ok": True})
            except Bad as ex:
                results.append({"op_id": key, "ok": False, "error": ex.message})
        return JSONResponse({"results": results})

    # ------------------------------------------------------------ кабинет логиста
    async def logist_page(_):
        return FileResponse(STATIC / "logist.html", headers={"Cache-Control": "no-cache"})

    @handler
    async def logist_status(_):
        # Первого логиста можно создать только из локальной сети весовой
        first = not db.q("SELECT 1 FROM logists LIMIT 1")
        return JSONResponse({"setup": first and not public, "empty": first, "version": __version__})

    def new_password(value) -> str:
        if not isinstance(value, str) or len(value) < 8:
            raise Bad("Пароль: не короче 8 символов")
        return st.hash_password(value)

    @handler
    async def logist_setup(request: Request):
        if public or db.q("SELECT 1 FROM logists LIMIT 1"):
            raise Bad("Первый логист уже создан", 403)
        b = await body(request)
        login = text(b.get("login"), "Логин", 60).casefold()
        lid = db.x("INSERT INTO logists(login,name,pass_hash,admin) VALUES(?,?,?,1)",
                   (login, text(b.get("name"), "Имя", 120, required=False), new_password(b.get("password"))))
        row = db.q("SELECT * FROM logists WHERE id=?", (lid,))[0]
        return JSONResponse({"token": tokens.make("l", lid, row["pass_hash"], LOGIST_DAYS)})

    @handler
    async def logist_login(request: Request):
        row, token = await login_common(request, "l", "logists", LOGIST_DAYS)
        return JSONResponse({"token": token})

    @handler
    async def logist_me(request: Request):
        u = logist_of(request)
        return JSONResponse({"login": u["login"], "name": u["name"], "admin": bool(u["admin"]),
                             "notices": notice_count(), "tolerance": lcfg()})

    def period(request: Request) -> tuple[str, str]:
        q = request.query_params
        today = datetime.now().strftime("%Y-%m-%d")
        start, end = q.get("from") or today, q.get("to") or today
        for v in (start, end):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise Bad("Некорректная дата")
        if start > end:
            raise Bad("Начало периода позже конца")
        return start, end

    def report(request: Request) -> dict:
        start, end = period(request)
        sql = ("SELECT u.*, c.name AS client FROM unloads u JOIN clients c ON c.id=u.client_id "
               "WHERE u.deleted IS NULL AND u.ts>=? AND u.ts<=?")
        args: list = [start, end + "T23:59:59"]
        q = request.query_params
        for name, col in (("driver_id", "u.driver_id"), ("client_id", "u.client_id")):
            if q.get(name):
                if not q[name].isdigit():
                    raise Bad("Некорректный фильтр")
                sql += f" AND {col}=?"; args.append(int(q[name]))
        if q.get("plate"):
            sql += " AND (u.plate=? OR u.trailer=?)"; args += [q["plate"]] * 2
        rows = db.q(sql + " ORDER BY u.ts DESC", args)

        def group(key):
            totals: dict[str, list] = {}
            for r in rows:
                e = totals.setdefault(r[key] or "Не указан", [0.0, 0])
                e[0] += r["liters"]; e[1] += 1
            return [{"name": k, "liters": round(v[0], 2), "count": v[1]}
                    for k, v in sorted(totals.items(), key=lambda kv: -kv[1][0])]

        return {"from": start, "to": end, "total": round(sum(r["liters"] for r in rows), 2), "count": len(rows),
                "by_client": group("client"), "by_driver": group("driver"), "by_plate": group("plate"),
                "by_trailer": group("trailer"), "unloads": [unload_json(r) for r in rows]}

    @handler
    async def logist_report(request: Request):
        logist_of(request)
        return JSONResponse(report(request))

    @handler
    async def logist_refs(request: Request):
        logist_of(request)
        vs = db.vehicles()
        return JSONResponse({
            "drivers": db.q("SELECT id,name,login,active,plate,(pass_hash<>'') AS has_password FROM drivers ORDER BY name"),
            "clients": db.q("SELECT * FROM clients ORDER BY active DESC, name"),
            "vehicles": [{"plate": v["plate"], "kind": v.get("kind") or "", "tank_l": v.get("tank_l"),
                          "tare": v.get("tare")} for v in vs],
        })

    @handler
    async def logist_xlsx(request: Request):
        logist_of(request)
        from openpyxl import Workbook
        from openpyxl.styles import Font
        data = report(request)
        wb = Workbook()
        ws = wb.active
        ws.title = "Итого"
        bold = Font(bold=True)
        ws.append([f"Период: {data['from']} — {data['to']}"])
        ws.append(["Всего, л", data["total"]])
        ws.append(["Выгрузок", data["count"]])
        for title, key in (("Клиент", "by_client"), ("Водитель", "by_driver"), ("Машина", "by_plate"),
                           ("Прицеп", "by_trailer")):
            ws.append([])
            ws.append([title, "Литры", "Выгрузок"])
            for c in ws[ws.max_row]:
                c.font = bold
            for item in data[key]:
                ws.append([safe_cell(item["name"]), item["liters"], item["count"]])
        ws.column_dimensions["A"].width = 34
        sh = wb.create_sheet("Выгрузки")
        sh.append(["Дата и время", "Водитель", "Машина", "Прицеп", "Клиент", "Литры", "Счёт", "Комментарий"])
        for c in sh[1]:
            c.font = bold
        for u in data["unloads"]:
            sh.append([u["ts"].replace("T", " "), safe_cell(u["driver"]), u["plate"], u["trailer"], safe_cell(u["client"]),
                       u["liters"], {"pending": "нужно выставить", "done": "выставлен"}.get(u["invoice"], ""),
                       safe_cell(u["comment"])])
        for col, w in zip("ABCDEFGH", (20, 26, 14, 14, 30, 12, 18, 40)):
            sh.column_dimensions[col].width = w
        lg = wb.create_sheet("Сверка с весами")
        lg.append(["Выезд", "Возврат", "Машина", "Прицеп", "Водитель", "При выезде, л", "При возврате, л",
                   "По весам, л", "По водителю, л", "Разница, л", "Разница, %", "Итог"])
        for c in lg[1]:
            c.font = bold
        cfg_ = lcfg()
        names = {"ok": "сходится", "bad": "расхождение", "open": "в рейсе", "unknown": "нельзя сравнить"}
        for leg in legs(db, density(), cfg_["tolerance_pct"], cfg_["tolerance_l"], data["from"],
                        data["to"] + "T23:59:59"):
            lg.append([leg["start"].replace("T", " "), (leg["end"] or "").replace("T", " "), leg["plate"],
                       leg["trailer"], safe_cell(leg["driver"]), leg["out_l"], leg["in_l"], leg["scale_l"],
                       leg["driver_l"], leg["diff_l"], leg["diff_pct"], names[leg["status"]]])
        for col in "ABCDEFGHIJKL":
            lg.column_dimensions[col].width = 17
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return StreamingResponse(
            buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="vygruzki_{data["from"]}_{data["to"]}.xlsx"'})

    @handler
    async def logist_legs(request: Request):
        logist_of(request)
        start, end = period(request)
        c = lcfg()
        return JSONResponse(legs(db, density(), c["tolerance_pct"], c["tolerance_l"], start, end + "T23:59:59"))

    @handler
    async def unload_edit(request: Request):
        u = logist_of(request)
        uid = request.path_params["uid"]
        if not db.q("SELECT 1 FROM unloads WHERE id=? AND deleted IS NULL", (uid,)):
            raise Bad("Выгрузка не найдена", 404)
        if request.method == "DELETE":
            db.x("UPDATE unloads SET deleted=? WHERE id=?", (now_iso(), uid))
        else:
            b = await body(request)
            if "liters" in b:
                db.x("UPDATE unloads SET liters=? WHERE id=?", (liters_value(b["liters"]), uid))
            if "comment" in b:
                db.x("UPDATE unloads SET comment=? WHERE id=?", (text(b["comment"], "Комментарий", 200, False), uid))
            if b.get("client_id") is not None:
                db.x("UPDATE unloads SET client_id=? WHERE id=?", (client_id_for(b), uid))
        refresh_invoice(uid)
        ctx.ctrl.event(f"Логист {u['login']}: выгрузка {'удалена' if request.method == 'DELETE' else 'исправлена'}")
        return JSONResponse({"ok": True})

    # ---- клиенты
    @handler
    async def client_save(request: Request):
        logist_of(request)
        b = await body(request)
        f = {"name": text(b.get("name"), "Название", 120), "legal": int(bool(b.get("legal"))),
             "legal_name": text(b.get("legal_name"), "Юрлицо", 200, False),
             "manager": text(b.get("manager"), "Менеджер", 120, False), "active": int(b.get("active", True) is not False)}
        other = [r for r in db.q("SELECT id, name FROM clients") if r["name"].casefold() == f["name"].casefold()]
        cid = b.get("id")
        if other and other[0]["id"] != cid:
            raise Bad("Такой клиент уже есть", 409)
        if cid:
            old = db.q("SELECT * FROM clients WHERE id=?", (cid,))
            if not old:
                raise Bad("Клиент не найден", 404)
            db.x("UPDATE clients SET name=?,legal=?,legal_name=?,manager=?,active=?,reviewed=1 WHERE id=?",
                 (*f.values(), cid))
            # Клиента добавил водитель: счёт нужен и по выгрузкам, записанным до проверки.
            if f["legal"] and not old[0]["reviewed"]:
                stale = db.q("SELECT id FROM unloads WHERE client_id=? AND invoice='' AND deleted IS NULL", (cid,))
            else:
                stale = db.q("SELECT id FROM unloads WHERE client_id=? AND invoice='pending'", (cid,))
            for r in stale:
                refresh_invoice(r["id"])
        else:
            cid = db.x("INSERT INTO clients(name,legal,legal_name,manager,active) VALUES(?,?,?,?,?)", tuple(f.values()))
        return JSONResponse({"ok": True, "id": cid})

    # ---- водители: логин и пароль для приложения
    @handler
    async def driver_save(request: Request):
        logist_of(request)
        b = await body(request)
        name = text(b.get("name"), "ФИО", 120)
        login = text(b.get("login"), "Логин", 60).casefold()
        did = b.get("id")
        clash = db.q("SELECT id FROM drivers WHERE login=?", (login,))
        if clash and clash[0]["id"] != did:
            raise Bad("Такой логин уже есть", 409)
        active = int(b.get("active", True) is not False)
        if did:
            if not db.driver(int(did)):
                raise Bad("Водитель не найден", 404)
            db.x("UPDATE drivers SET name=?, login=?, active=? WHERE id=?", (name, login, active, did))
            if b.get("password"):
                db.x("UPDATE drivers SET pass_hash=? WHERE id=?", (new_password(b["password"]), did))
        else:
            # PIN для страницы в сети весовой не задан: вместо него случайная заглушка
            did = db.x("INSERT INTO drivers(name,login,pass_hash,active,pin_hash) VALUES(?,?,?,?,?)",
                       (name, login, new_password(b.get("password")), active, "nopin:" + secrets.token_hex(16)))
        return JSONResponse({"ok": True, "id": did})

    # ---- логисты
    @handler
    async def logists_api(request: Request):
        u = logist_of(request)
        if request.method == "GET":
            return JSONResponse(db.q("SELECT id,login,name,admin,active FROM logists ORDER BY login"))
        b = await body(request)
        lid = b.get("id")
        if not u["admin"] and lid != u["id"]:
            raise Bad("Других логистов может менять только администратор", 403)
        if lid:
            if not db.q("SELECT 1 FROM logists WHERE id=?", (lid,)):
                raise Bad("Логист не найден", 404)
            if "name" in b:
                db.x("UPDATE logists SET name=? WHERE id=?", (text(b["name"], "Имя", 120, False), lid))
            if b.get("password"):
                db.x("UPDATE logists SET pass_hash=? WHERE id=?", (new_password(b["password"]), lid))
            if "active" in b and u["admin"] and lid != u["id"]:
                db.x("UPDATE logists SET active=? WHERE id=?", (int(bool(b["active"])), lid))
            out = {"ok": True}
            if lid == u["id"] and b.get("password"):
                row = db.q("SELECT * FROM logists WHERE id=?", (lid,))[0]
                out["token"] = tokens.make("l", lid, row["pass_hash"], LOGIST_DAYS)
            return JSONResponse(out)
        login = text(b.get("login"), "Логин", 60).casefold()
        if db.q("SELECT 1 FROM logists WHERE login=?", (login,)):
            raise Bad("Такой логин уже есть", 409)
        db.x("INSERT INTO logists(login,name,pass_hash,admin) VALUES(?,?,?,?)",
             (login, text(b.get("name"), "Имя", 120, False), new_password(b.get("password")), int(bool(b.get("admin")))))
        return JSONResponse({"ok": True})

    # ---- уведомления
    def bad_legs() -> list[dict]:
        c = lcfg()
        since = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        done = {r["ref"] for r in db.q("SELECT ref FROM notes_done WHERE kind='leg'")}
        return [leg for leg in legs(db, density(), c["tolerance_pct"], c["tolerance_l"], since)
                if leg["status"] == "bad" and str(leg["id"]) not in done]

    def notice_count() -> int:
        n = db.q("SELECT COUNT(*) AS n FROM unloads WHERE invoice='pending' AND deleted IS NULL")[0]["n"]
        n += db.q("SELECT COUNT(*) AS n FROM clients WHERE reviewed=0 AND active=1")[0]["n"]
        return n + len(bad_legs())

    @handler
    async def notices(request: Request):
        logist_of(request)
        status = "done" if request.query_params.get("invoices") == "done" else "pending"
        order = "u.invoiced_at DESC" if status == "done" else "u.ts"
        inv = db.q("SELECT u.*, c.name AS client, c.legal_name, c.manager FROM unloads u "
                   "JOIN clients c ON c.id=u.client_id WHERE u.invoice=? AND u.deleted IS NULL "
                   f"ORDER BY {order} LIMIT 500", (status,))
        return JSONResponse({
            "invoices": [{"id": r["id"], "ts": r["ts"], "client": r["legal_name"] or r["client"], "point": r["client"],
                          "manager": r["manager"], "liters": r["liters"], "driver": r["driver"], "plate": r["plate"],
                          "invoiced_at": r["invoiced_at"], "invoiced_by": r["invoiced_by"]} for r in inv],
            "legs": bad_legs(),
            "new_clients": db.q("SELECT id, name FROM clients WHERE reviewed=0 AND active=1 ORDER BY name"),
            "count": notice_count(),
        })

    def invoice_mark_as(action: str):
        @handler
        async def mark(request: Request):
            return await invoice_mark(request, action)
        return mark

    async def invoice_mark(request: Request, action: str):
        u = logist_of(request)
        uid = request.path_params["uid"]
        if action == "done":
            sql = ("UPDATE unloads SET invoice='done', invoiced_at=?, invoiced_by=? "
                   "WHERE id=? AND invoice='pending' AND deleted IS NULL")
            args = (now_iso(), u["name"] or u["login"], uid)
        else:
            sql = ("UPDATE unloads SET invoice='pending', invoiced_at=NULL, invoiced_by='' "
                   "WHERE id=? AND invoice='done' AND deleted IS NULL")
            args = (uid,)
        with db.lock:
            changed = db.conn.execute(sql, args).rowcount
            db.conn.commit()
        if not changed:
            raise Bad("Статус счёта уже изменён", 409)
        return JSONResponse({"ok": True})

    @handler
    async def leg_done(request: Request):
        logist_of(request)
        db.x("INSERT OR IGNORE INTO notes_done(kind, ref) VALUES('leg', ?)", (str(int(request.path_params["tid"])),))
        return JSONResponse({"ok": True})

    return [
        Route("/api/app/login", app_login, methods=["POST"]),
        Route("/api/app/me", app_me),
        Route("/api/app/weigh", app_weigh, methods=["POST"]),
        Route("/api/app/sync", app_sync, methods=["POST"]),
        Route("/api/app/photo/{path:path}", app_photo),
        Route("/logist", logist_page),
        Route("/api/logist/status", logist_status),
        Route("/api/logist/setup", logist_setup, methods=["POST"]),
        Route("/api/logist/login", logist_login, methods=["POST"]),
        Route("/api/logist/me", logist_me),
        Route("/api/logist/refs", logist_refs),
        Route("/api/logist/report", logist_report),
        Route("/api/logist/report.xlsx", logist_xlsx),
        Route("/api/logist/legs", logist_legs),
        Route("/api/logist/unloads/{uid}", unload_edit, methods=["PATCH", "DELETE"]),
        Route("/api/logist/clients", client_save, methods=["POST"]),
        Route("/api/logist/drivers", driver_save, methods=["POST"]),
        Route("/api/logist/logists", logists_api, methods=["GET", "POST"]),
        Route("/api/logist/notices", notices),
        Route("/api/logist/invoices/{uid}/done", invoice_mark_as("done"), methods=["POST"]),
        Route("/api/logist/invoices/{uid}/undo", invoice_mark_as("undo"), methods=["POST"]),
        Route("/api/logist/legs/{tid:int}/done", leg_done, methods=["POST"]),
    ]
