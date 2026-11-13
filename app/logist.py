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
import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__
from . import settings as st
from .plates import normalize as norm_plate
from .routing import PRECISE, RouteError, Routing, best_order
from .stavtrack import Stavtrack, StError
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
CREATE TABLE IF NOT EXISTS orders (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    address   TEXT NOT NULL DEFAULT '',
    liters    REAL NOT NULL,                  -- сколько нужно слить
    driver_id INTEGER NOT NULL,
    day       TEXT NOT NULL,
    pos       INTEGER NOT NULL DEFAULT 0,
    note      TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'new',    -- new / done / failed / cancelled
    reason    TEXT NOT NULL DEFAULT '',
    created   TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    closed    TEXT,
    unload_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_driver ON orders(driver_id, status);
CREATE TABLE IF NOT EXISTS push_tokens (token TEXT PRIMARY KEY, driver_id INTEGER NOT NULL, updated TEXT NOT NULL);
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


class Pusher:
    """Уведомления водителям через Firebase. Работает, если в папке данных лежит firebase.json
    (ключ сервисного аккаунта) и установлен пакет google-auth; иначе тихо выключен."""

    def __init__(self, data_dir: str | Path):
        self.file = Path(data_dir) / "firebase.json"
        self.creds = None
        self.project = None
        self.log = logging.getLogger("push")

    def ready(self) -> bool:
        return self.file.is_file()

    def _token(self) -> str:
        from google.auth.transport.requests import Request as GRequest
        from google.oauth2 import service_account
        if self.creds is None:
            self.creds = service_account.Credentials.from_service_account_file(
                str(self.file), scopes=["https://www.googleapis.com/auth/firebase.messaging"])
            self.project = self.creds.project_id
        if not self.creds.valid:
            self.creds.refresh(GRequest())
        return self.creds.token

    def send(self, tokens: list[str], title: str, body: str) -> list[str]:
        """Отправляет уведомление; возвращает токены, которые больше не действуют."""
        if not tokens or not self.ready():
            return []
        import urllib.error
        import urllib.request
        dead = []
        try:
            access = self._token()
        except Exception as ex:  # noqa: BLE001
            self.log.warning("Firebase не настроен или недоступен: %s", ex)
            return []
        for tok in tokens:
            msg = {"message": {"token": tok, "notification": {"title": title, "body": body},
                               "android": {"priority": "HIGH", "notification": {"sound": "default"}}}}
            req = urllib.request.Request(
                f"https://fcm.googleapis.com/v1/projects/{self.project}/messages:send",
                data=json.dumps(msg).encode(), method="POST",
                headers={"Authorization": "Bearer " + access, "Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10).close()
            except urllib.error.HTTPError as ex:
                if ex.code in (400, 404):
                    dead.append(tok)
                else:
                    self.log.warning("Firebase ответил ошибкой %s", ex.code)
            except OSError as ex:
                self.log.warning("Нет связи с Firebase: %s", ex)
        return dead


def init_db(db):
    with db.lock:
        db.conn.executescript(SCHEMA)
        have = {r[1] for r in db.conn.execute("PRAGMA table_info(drivers)")}
        for col in ("login", "pass_hash"):
            if col not in have:
                db.conn.execute(f"ALTER TABLE drivers ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        for table, cols in (
                ("clients", [("address", "TEXT NOT NULL DEFAULT ''"), ("private", "INTEGER NOT NULL DEFAULT 0"),
                             ("manager_id", "INTEGER"), ("lat", "REAL"), ("lon", "REAL"),
                             ("geo_precision", "TEXT NOT NULL DEFAULT ''"), ("geo_text", "TEXT NOT NULL DEFAULT ''")]),
                ("orders", [("lat", "REAL"), ("lon", "REAL"), ("geo_precision", "TEXT NOT NULL DEFAULT ''")]),
                ("unloads", [("order_id", "INTEGER"), ("paid", "TEXT NOT NULL DEFAULT ''"), ("paid_at", "TEXT"),
                             ("paid_by", "TEXT NOT NULL DEFAULT ''")]),
                ("logists", [("role", "TEXT NOT NULL DEFAULT 'logist'")])):
            have = {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols:
                if col not in have:
                    db.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
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
    st = db.q("SELECT value FROM kv WHERE key='stavtrack'") if db.q(
        "SELECT 1 FROM sqlite_master WHERE name='weigh_fuel'") else []
    fuel_density = float(json.loads(st[0]["value"]).get("fuel_density", 0.84)) if st else 0.84

    def fuel_at(weighing_id):
        if weighing_id is None or not st:
            return None
        row = db.q("SELECT liters FROM weigh_fuel WHERE weighing_id=?", (weighing_id,))
        return row[0]["liters"] if row else None

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
               "status": "open", "left_l": round(out_l - by_driver),
               "fuel_out": fuel_at(t.get("w2_id")), "fuel_in": None, "fuel_fix_l": None}
        if nxt:
            same = (nxt.get("trailer") or "") == leg["trailer"]
            if nxt.get("gas_in") is None or not same:
                leg["status"] = "unknown"      # нет тары или вернулся с другим прицепом — сравнить нельзя
            else:
                leg["in_l"] = round(nxt["gas_in"] / density)
                leg["scale_l"] = leg["out_l"] - leg["in_l"]
                leg["fuel_in"] = fuel_at(nxt.get("w1_id"))
                if leg["fuel_out"] is not None and leg["fuel_in"] is not None:
                    # Сожжённое в рейсе топливо весы видят как «уехавший газ»: убираем его
                    burned_kg = (leg["fuel_out"] - leg["fuel_in"]) * fuel_density
                    leg["fuel_fix_l"] = round(burned_kg / density)
                    leg["scale_l"] -= leg["fuel_fix_l"]
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
    pusher = getattr(ctx, "pusher", None) or Pusher(ctx.cfg["data_dir"])
    if getattr(ctx, "stavtrack", None) is None:
        ctx.stavtrack = Stavtrack(db)
        db.on_weighing = getattr(db, "on_weighing", []) + [ctx.stavtrack.on_weighing]
    stv = ctx.stavtrack
    if getattr(ctx, "routing", None) is None:
        ctx.routing = Routing(db)
    routing = ctx.routing

    def parse_coords(value: str) -> tuple[float, float]:
        try:
            lat, lon = (float(x) for x in value.replace(";", ",").replace(" ", ",").split(",") if x)
        except ValueError as exc:
            raise Bad("Координаты: укажите широту и долготу через запятую, например 45.0428, 41.9734") from exc
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise Bad("Координаты: широта от −90 до 90, долгота от −180 до 180")
        return lat, lon

    def locate(coords: str, address: str, old: dict | None):
        """Координаты адреса: (lat, lon, точность, найденный адрес) или None, если менять не нужно."""
        if coords:
            return (*parse_coords(coords), "manual", "")
        if not address:
            return (None, None, "", "")
        if old and old.get("address") == address and old.get("lat") is not None and old.get("geo_precision") != "manual":
            return None
        try:
            g = routing.geocode(address)
            return (g["lat"], g["lon"], g["precision"], g["text"])
        except RouteError as ex:
            return (None, None, "error", str(ex))

    def push(driver_id: int, title: str, body_: str):
        toks = [r["token"] for r in db.q("SELECT token FROM push_tokens WHERE driver_id=?", (driver_id,))]
        if not toks or not pusher.ready():
            return

        def work():
            for dead in pusher.send(toks, title, body_):
                db.x("DELETE FROM push_tokens WHERE token=?", (dead,))
        threading.Thread(target=work, name="push", daemon=True).start()

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

    def staff(request: Request, token: str | None = None) -> dict:
        """Логист или менеджер. Менеджер видит только своих клиентов: оплаты и счета."""
        return current(request, "l", "logists", token)

    def logist_of(request: Request) -> dict:
        u = staff(request)
        if u["role"] != "logist":
            raise Bad("Доступно только логисту", 403)
        return u

    def own(u: dict) -> int | None:
        return u["id"] if u["role"] == "manager" else None

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
                "driver": r["driver"], "driver_id": r["driver_id"], "invoice": r["invoice"],
                "paid": r.get("paid") or "", "order_id": r.get("order_id")}

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
            "clients": db.q("SELECT id, name, private FROM clients WHERE active=1 ORDER BY name"),
            "orders": db.q("SELECT o.id, o.client_id, c.name AS client, c.private, o.address, o.liters, o.day, o.note, "
                           "COALESCE(o.lat, c.lat) AS lat, COALESCE(o.lon, c.lon) AS lon "
                           "FROM orders o JOIN clients c ON c.id=o.client_id "
                           "WHERE o.driver_id=? AND o.status='new' ORDER BY o.day, o.pos, o.id", (d["id"],)),
            "push": pusher.ready(),
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
        order = None
        if op.get("order_id") is not None:
            found = db.q("SELECT * FROM orders WHERE id=?", (op["order_id"],))
            if not found or found[0]["driver_id"] != d["id"]:
                raise Bad("Заказ не найден")
            order = found[0]
            if order["status"] == "cancelled":
                raise Bad("Заказ отменён логистом")
            if order["status"] == "done":
                raise Bad("Заказ уже выполнен")
        cid = order["client_id"] if order else client_id_for(op)
        client = db.q("SELECT * FROM clients WHERE id=?", (cid,))[0]
        paid = ""
        if isinstance(op.get("paid"), bool):
            paid = "yes" if op["paid"] else "no"
        if client["private"] and not paid:
            raise Bad("Укажите, оплатил ли клиент")
        if client["legal"] or (client["reviewed"] and not client["private"]):
            paid = ""          # юрлицу выставляют счёт, на своих объектах оплаты нет
        db.x("INSERT INTO unloads(id,driver_id,driver,plate,trailer,client_id,liters,ts,comment,created,order_id,paid) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             (uid, d["id"], d["name"], plate, trailer, cid, amount, ts, comment, now_iso(),
              order["id"] if order else None, paid))
        if order:
            db.x("UPDATE orders SET status='done', closed=?, unload_id=?, reason='' WHERE id=?", (ts, uid, order["id"]))
        refresh_invoice(uid)

    def apply_order_fail(d: dict, op: dict):
        found = db.q("SELECT * FROM orders WHERE id=?", (op.get("order_id"),))
        if not found or found[0]["driver_id"] != d["id"]:
            raise Bad("Заказ не найден")
        if found[0]["status"] != "new":
            return
        db.x("UPDATE orders SET status='failed', reason=?, closed=? WHERE id=?",
             (text(op.get("reason"), "Причина", 200), local_ts(op.get("at")), found[0]["id"]))

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
        if r.get("order_id"):
            db.x("UPDATE orders SET status='new', closed=NULL, unload_id=NULL WHERE id=? AND status='done'",
                 (r["order_id"],))
        refresh_invoice(r["id"])

    @handler
    async def app_push_token(request: Request):
        d = driver_of(request)
        tok = text((await body(request)).get("token"), "Токен", 4096)
        db.x("INSERT INTO push_tokens(token, driver_id, updated) VALUES(?,?,?) "
             "ON CONFLICT(token) DO UPDATE SET driver_id=excluded.driver_id, updated=excluded.updated",
             (tok, d["id"], now_iso()))
        return JSONResponse({"ok": True})

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
                kinds = {"unload": apply_unload, "delete": apply_delete, "order_fail": apply_order_fail}
                if not isinstance(op, dict) or op.get("type") not in kinds:
                    raise Bad("Неизвестный тип записи")
                kinds[op["type"]](d, op)
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
        u = staff(request)
        return JSONResponse({"login": u["login"], "name": u["name"], "admin": bool(u["admin"]), "role": u["role"],
                             "notices": notice_count(own(u)), "tolerance": lcfg(), "push": pusher.ready()})

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

    def report(request: Request, owner: int | None = None) -> dict:
        start, end = period(request)
        sql = ("SELECT u.*, c.name AS client FROM unloads u JOIN clients c ON c.id=u.client_id "
               "WHERE u.deleted IS NULL AND u.ts>=? AND u.ts<=?")
        args: list = [start, end + "T23:59:59"]
        if owner is not None:
            sql += " AND c.manager_id=?"; args.append(owner)
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
        return JSONResponse(report(request, own(staff(request))))

    @handler
    async def logist_refs(request: Request):
        u = staff(request)
        vs = db.vehicles()
        managers = db.q("SELECT id, name, login FROM logists WHERE role='manager' AND active=1 ORDER BY name")
        if u["role"] == "manager":
            return JSONResponse({
                "drivers": db.q("SELECT id, name FROM drivers ORDER BY name"),
                "clients": db.q("SELECT * FROM clients WHERE manager_id=? ORDER BY name", (u["id"],)),
                "vehicles": [{"plate": v["plate"]} for v in vs], "managers": []})
        return JSONResponse({
            "managers": managers,
            "drivers": db.q("SELECT id,name,login,active,plate,(pass_hash<>'') AS has_password FROM drivers ORDER BY name"),
            "clients": db.q("SELECT * FROM clients ORDER BY active DESC, name"),
            "vehicles": [{"plate": v["plate"], "kind": v.get("kind") or "", "tank_l": v.get("tank_l"),
                          "tare": v.get("tare")} for v in vs],
        })

    @handler
    async def logist_xlsx(request: Request):
        owner = own(staff(request))
        from openpyxl import Workbook
        from openpyxl.styles import Font
        data = report(request, owner)
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
        sh.append(["Дата и время", "Водитель", "Машина", "Прицеп", "Клиент", "Литры", "Счёт", "Комментарий", "Оплата"])
        for c in sh[1]:
            c.font = bold
        for u in data["unloads"]:
            sh.append([u["ts"].replace("T", " "), safe_cell(u["driver"]), u["plate"], u["trailer"], safe_cell(u["client"]),
                       u["liters"], {"pending": "нужно выставить", "done": "выставлен"}.get(u["invoice"], ""),
                       safe_cell(u["comment"]), {"yes": "оплачено", "no": "не оплачено"}.get(u["paid"], "")])
        for col, w in zip("ABCDEFGHI", (20, 26, 14, 14, 30, 12, 18, 40, 14)):
            sh.column_dimensions[col].width = w
        lg = wb.create_sheet("Сверка с весами")
        lg.append(["Выезд", "Возврат", "Машина", "Прицеп", "Водитель", "При выезде, л", "При возврате, л",
                   "По весам, л", "По водителю, л", "Разница, л", "Разница, %", "Итог", "Поправка на топливо, л"])
        for c in lg[1]:
            c.font = bold
        cfg_ = lcfg()
        names = {"ok": "сходится", "bad": "расхождение", "open": "в рейсе", "unknown": "нельзя сравнить"}
        for leg in [] if owner else legs(db, density(), cfg_["tolerance_pct"], cfg_["tolerance_l"], data["from"],
                                          data["to"] + "T23:59:59"):
            lg.append([leg["start"].replace("T", " "), (leg["end"] or "").replace("T", " "), leg["plate"],
                       leg["trailer"], safe_cell(leg["driver"]), leg["out_l"], leg["in_l"], leg["scale_l"],
                       leg["driver_l"], leg["diff_l"], leg["diff_pct"], names[leg["status"]], leg["fuel_fix_l"]])
        for col in "ABCDEFGHIJKLM":
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
        kind = b.get("kind") or ("legal" if b.get("legal") else "private" if b.get("private") else "own")
        if kind not in ("legal", "private", "own"):
            raise Bad("Некорректный тип клиента")
        manager_id = b.get("manager_id") or None
        if manager_id is not None and not db.q("SELECT 1 FROM logists WHERE id=? AND role='manager'", (manager_id,)):
            raise Bad("Менеджер не найден")
        f = {"name": text(b.get("name"), "Название", 120), "legal": int(kind == "legal"),
             "legal_name": text(b.get("legal_name"), "Юрлицо", 200, False),
             "manager": text(b.get("manager"), "Менеджер", 120, False), "active": int(b.get("active", True) is not False),
             "address": text(b.get("address"), "Адрес", 300, False), "private": int(kind == "private"),
             "manager_id": manager_id}
        other = [r for r in db.q("SELECT id, name FROM clients") if r["name"].casefold() == f["name"].casefold()]
        cid = b.get("id")
        if other and other[0]["id"] != cid:
            raise Bad("Такой клиент уже есть", 409)
        if cid:
            old = db.q("SELECT * FROM clients WHERE id=?", (cid,))
            if not old:
                raise Bad("Клиент не найден", 404)
            db.x("UPDATE clients SET name=?,legal=?,legal_name=?,manager=?,active=?,address=?,private=?,manager_id=?,"
                 "reviewed=1 WHERE id=?",
                 (*f.values(), cid))
            # Клиента добавил водитель: счёт нужен и по выгрузкам, записанным до проверки.
            if f["legal"] and not old[0]["reviewed"]:
                stale = db.q("SELECT id FROM unloads WHERE client_id=? AND invoice='' AND deleted IS NULL", (cid,))
            else:
                stale = db.q("SELECT id FROM unloads WHERE client_id=? AND invoice='pending'", (cid,))
            for r in stale:
                refresh_invoice(r["id"])
        else:
            cid = db.x("INSERT INTO clients(name,legal,legal_name,manager,active,address,private,manager_id) "
                       "VALUES(?,?,?,?,?,?,?,?)", tuple(f.values()))
            old = []
        loop = asyncio.get_running_loop()
        geo = await loop.run_in_executor(None, locate, text(b.get("coords"), "Координаты", 60, False),
                                         f["address"], old[0] if old else None)
        if geo is not None:
            db.x("UPDATE clients SET lat=?, lon=?, geo_precision=?, geo_text=? WHERE id=?", (*geo, cid))
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
        u = staff(request)
        if request.method == "GET":
            if u["role"] == "manager":
                return JSONResponse(db.q("SELECT id,login,name,admin,active,role FROM logists WHERE id=?", (u["id"],)))
            return JSONResponse(db.q("SELECT id,login,name,admin,active,role FROM logists ORDER BY role, login"))
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
        role = "manager" if b.get("role") == "manager" else "logist"
        db.x("INSERT INTO logists(login,name,pass_hash,admin,role) VALUES(?,?,?,?,?)",
             (login, text(b.get("name"), "Имя", 120, False), new_password(b.get("password")),
              int(bool(b.get("admin")) and role == "logist"), role))
        return JSONResponse({"ok": True})

    # ---- уведомления
    def bad_legs() -> list[dict]:
        c = lcfg()
        since = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        done = {r["ref"] for r in db.q("SELECT ref FROM notes_done WHERE kind='leg'")}
        return [leg for leg in legs(db, density(), c["tolerance_pct"], c["tolerance_l"], since)
                if leg["status"] == "bad" and str(leg["id"]) not in done]

    def failed_orders() -> list[dict]:
        since = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        return db.q("SELECT o.id, o.day, o.liters, o.address, o.reason, c.name AS client, d.name AS driver "
                    "FROM orders o JOIN clients c ON c.id=o.client_id JOIN drivers d ON d.id=o.driver_id "
                    "WHERE o.status='failed' AND o.day>=? AND CAST(o.id AS TEXT) NOT IN "
                    "(SELECT ref FROM notes_done WHERE kind='order') ORDER BY o.closed DESC", (since,))

    def notice_count(owner: int | None = None) -> int:
        mine, args = ("", ()) if owner is None else (" AND c.manager_id=?", (owner,))
        n = db.q("SELECT COUNT(*) AS n FROM unloads u JOIN clients c ON c.id=u.client_id WHERE u.deleted IS NULL "
                 "AND (u.invoice='pending' OR u.paid='no')" + mine, args)[0]["n"]
        if owner is not None:
            return n
        n += db.q("SELECT COUNT(*) AS n FROM clients WHERE reviewed=0 AND active=1")[0]["n"]
        return n + len(bad_legs()) + len(failed_orders())

    @handler
    async def notices(request: Request):
        u = staff(request)
        owner = own(u)
        mine, margs = ("", ()) if owner is None else (" AND c.manager_id=?", (owner,))
        status = "done" if request.query_params.get("invoices") == "done" else "pending"
        order = "u.invoiced_at DESC" if status == "done" else "u.ts"
        base = ("SELECT u.*, c.name AS client, c.legal_name, COALESCE(NULLIF(m.name,''), m.login, c.manager) AS manager "
                "FROM unloads u JOIN clients c ON c.id=u.client_id LEFT JOIN logists m ON m.id=c.manager_id "
                "WHERE u.deleted IS NULL")
        inv = db.q(f"{base} AND u.invoice=?{mine} ORDER BY {order} LIMIT 500", (status, *margs))
        unpaid = db.q(f"{base} AND u.paid='no'{mine} ORDER BY u.ts LIMIT 500", margs)

        def row(r):
            return {"id": r["id"], "ts": r["ts"], "client": r["legal_name"] or r["client"], "point": r["client"],
                    "manager": r["manager"] or "", "liters": r["liters"], "driver": r["driver"], "plate": r["plate"],
                    "invoiced_at": r["invoiced_at"], "invoiced_by": r["invoiced_by"]}
        return JSONResponse({
            "invoices": [row(r) for r in inv],
            "unpaid": [row(r) for r in unpaid],
            "legs": [] if owner else bad_legs(),
            "failed_orders": [] if owner else failed_orders(),
            "new_clients": [] if owner else db.q("SELECT id, name FROM clients WHERE reviewed=0 AND active=1 ORDER BY name"),
            "count": notice_count(owner),
        })

    def manager_owns(u: dict, uid: str):
        if u["role"] == "manager" and not db.q(
                "SELECT 1 FROM unloads u JOIN clients c ON c.id=u.client_id WHERE u.id=? AND c.manager_id=?",
                (uid, u["id"])):
            raise Bad("Это клиент другого менеджера", 403)

    @handler
    async def paid_mark(request: Request):
        u = staff(request)
        uid = request.path_params["uid"]
        manager_owns(u, uid)
        with db.lock:
            changed = db.conn.execute(
                "UPDATE unloads SET paid='yes', paid_at=?, paid_by=? WHERE id=? AND paid='no' AND deleted IS NULL",
                (now_iso(), u["name"] or u["login"], uid)).rowcount
            db.conn.commit()
        if not changed:
            raise Bad("Оплата уже отмечена", 409)
        return JSONResponse({"ok": True})

    @handler
    async def order_note_done(request: Request):
        logist_of(request)
        db.x("INSERT OR IGNORE INTO notes_done(kind, ref) VALUES('order', ?)", (str(int(request.path_params["oid"])),))
        return JSONResponse({"ok": True})

    # ---- заказы водителям
    @handler
    async def orders_api(request: Request):
        u = logist_of(request)
        if request.method == "GET":
            start, end = period(request)
            return JSONResponse(db.q(
                "SELECT o.*, c.name AS client, d.name AS driver, un.liters AS fact, un.paid FROM orders o "
                "JOIN clients c ON c.id=o.client_id JOIN drivers d ON d.id=o.driver_id "
                "LEFT JOIN unloads un ON un.id=o.unload_id WHERE o.day BETWEEN ? AND ? "
                "ORDER BY o.day, d.name, o.pos, o.id", (start, end)))
        b = await body(request)
        client = db.q("SELECT * FROM clients WHERE id=?", (b.get("client_id"),))
        driver = db.q("SELECT * FROM drivers WHERE id=? AND active=1", (b.get("driver_id"),))
        if not client:
            raise Bad("Выберите клиента")
        if not driver:
            raise Bad("Выберите водителя")
        day = str(b.get("day") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise Bad("Укажите дату")
        f = (client[0]["id"], text(b.get("address"), "Адрес", 300, False) or client[0]["address"],
             liters_value(b.get("liters")), driver[0]["id"], day, text(b.get("note"), "Примечание", 300, False))
        oid = b.get("id")
        if oid:
            old = db.q("SELECT * FROM orders WHERE id=?", (oid,))
            if not old:
                raise Bad("Заказ не найден", 404)
            if old[0]["status"] == "done":
                raise Bad("Заказ уже выполнен")
            db.x("UPDATE orders SET client_id=?,address=?,liters=?,driver_id=?,day=?,note=?,status='new',reason='',"
                 "closed=NULL WHERE id=?", (*f, oid))
            if old[0]["driver_id"] != f[3] and old[0]["status"] == "new":
                push(old[0]["driver_id"], "Заказ снят", f"{client[0]['name']}: заказ передан другому водителю")
            push(f[3], "Заказ изменён" if old[0]["driver_id"] == f[3] else "Новый заказ",
                 f"{client[0]['name']}: {f[2]:g} л. {f[1]}".strip())
        else:
            pos = db.q("SELECT COALESCE(MAX(pos),0)+1 AS p FROM orders WHERE driver_id=? AND day=?", (f[3], day))[0]["p"]
            oid = db.x("INSERT INTO orders(client_id,address,liters,driver_id,day,note,pos,created,created_by) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (*f, pos, now_iso(), u["login"]))
            push(f[3], "Новый заказ", f"{client[0]['name']}: {f[2]:g} л. {f[1]}".strip())
        # Свой адрес заказа ищем отдельно; если адрес как у клиента — берутся координаты клиента
        own_geo = (None, None, "")
        if f[1] and f[1] != client[0]["address"]:
            loop = asyncio.get_running_loop()
            own_geo = (await loop.run_in_executor(None, locate, "", f[1], None))[:3]
        db.x("UPDATE orders SET lat=?, lon=?, geo_precision=? WHERE id=?", (*own_geo, oid))
        return JSONResponse({"ok": True, "id": oid})

    @handler
    async def orders_route(request: Request):
        """Лучший порядок объезда заказов водителя за день: с базы и обратно, по времени в пути с пробками."""
        logist_of(request)
        b = await body(request)
        day = str(b.get("day") or "")
        rows = db.q("SELECT o.id, o.driver_id, c.name AS client, COALESCE(o.lat, c.lat) AS lat, "
                    "COALESCE(o.lon, c.lon) AS lon FROM orders o JOIN clients c ON c.id=o.client_id "
                    "WHERE o.driver_id=? AND o.day=? AND o.status='new' ORDER BY o.pos, o.id", (b.get("driver_id"), day))
        if not rows:
            raise Bad("У этого водителя нет невыполненных заказов на эту дату")
        missing = [r["client"] for r in rows if r["lat"] is None]
        if missing:
            raise Bad("Нет координат адреса: " + ", ".join(missing) + ". Исправьте адрес клиента или укажите координаты.")
        s_ = routing.settings()
        if s_["base_lat"] is None:
            raise Bad("Укажите адрес базы на вкладке «Маршруты»")
        points = [(s_["base_lat"], s_["base_lon"])] + [(r["lat"], r["lon"]) for r in rows]
        loop = asyncio.get_running_loop()
        try:
            dur, dist = await loop.run_in_executor(None, routing.matrix, points)
        except RouteError as ex:
            raise Bad(str(ex), 502) from ex
        order = best_order(dur)
        path = [0] + order + [0]
        before = [0] + list(range(1, len(points))) + [0]
        total = lambda p, m: sum(m[a][c] for a, c in zip(p, p[1:]))      # noqa: E731
        for pos, idx in enumerate(order, 1):
            db.x("UPDATE orders SET pos=? WHERE id=?", (pos, rows[idx - 1]["id"]))
        if order != list(range(1, len(points))):
            push(rows[0]["driver_id"], "Маршрут изменён", "Порядок заказов обновлён: " +
                 " → ".join(rows[i - 1]["client"] for i in order))
        return JSONResponse({"ok": True, "order": [rows[i - 1]["client"] for i in order],
                             "km": round(total(path, dist) / 1000, 1), "minutes": round(total(path, dur) / 60),
                             "saved_minutes": round((total(before, dur) - total(path, dur)) / 60)})

    @handler
    async def routing_api(request: Request):
        admin_of(request)
        if request.method == "POST":
            b = await body(request)
            new = {"mode": "driving" if b.get("mode") == "driving" else "truck", "has_trailer": bool(b.get("has_trailer"))}
            for key in ("geocoder_key", "router_key"):
                if b.get(key):
                    new[key] = text(b[key], "Ключ", 200)
            routing.save(new)
            address = text(b.get("base_address"), "Адрес базы", 300, False)
            coords = text(b.get("base_coords"), "Координаты базы", 60, False)
            loop = asyncio.get_running_loop()
            old = routing.settings()
            geo = await loop.run_in_executor(None, locate, coords, address,
                                             {"address": old["base_address"], "lat": old["base_lat"],
                                              "geo_precision": old.get("base_precision", "")})
            new = {"base_address": address}
            if geo is not None:
                if geo[2] == "error":
                    raise Bad("Адрес базы: " + geo[3])
                new.update({"base_lat": geo[0], "base_lon": geo[1]})
                routing.db.x("INSERT INTO kv(key, value) VALUES('yandex_base', ?) ON CONFLICT(key) DO UPDATE SET "
                             "value=excluded.value", (json.dumps({"precision": geo[2], "text": geo[3]}),))
            routing.save(new)
        s_ = routing.settings()
        base = db.q("SELECT value FROM kv WHERE key='yandex_base'")
        base = json.loads(base[0]["value"]) if base else {"precision": "", "text": ""}
        return JSONResponse({"geocoder_key_set": bool(s_["geocoder_key"]), "router_key_set": bool(s_["router_key"]),
                             "base_address": s_["base_address"], "base_lat": s_["base_lat"], "base_lon": s_["base_lon"],
                             "base_precision": base["precision"], "base_text": base["text"],
                             "mode": s_["mode"], "has_trailer": s_["has_trailer"]})

    @handler
    async def order_cancel(request: Request):
        logist_of(request)
        rows = db.q("SELECT o.*, c.name AS client FROM orders o JOIN clients c ON c.id=o.client_id WHERE o.id=?",
                    (request.path_params["oid"],))
        if not rows:
            raise Bad("Заказ не найден", 404)
        if rows[0]["status"] == "done":
            raise Bad("Заказ уже выполнен")
        db.x("UPDATE orders SET status='cancelled', closed=? WHERE id=?", (now_iso(), rows[0]["id"]))
        if rows[0]["status"] == "new":
            push(rows[0]["driver_id"], "Заказ отменён", f"{rows[0]['client']}: {rows[0]['liters']:g} л")
        return JSONResponse({"ok": True})

    # ---- Ставтрэк: топливо по ДУТ
    def admin_of(request: Request) -> dict:
        u = logist_of(request)
        if not u["admin"]:
            raise Bad("Доступно только логисту-администратору", 403)
        return u

    @handler
    async def stavtrack_api(request: Request):
        admin_of(request)
        if request.method == "POST":
            b = await body(request)
            new = {"enabled": bool(b.get("enabled")), "email": text(b.get("email"), "Логин", 200, False)}
            url = text(b.get("url"), "Адрес API", 300)
            if not re.match(r"^https?://[^\s/]+", url):
                raise Bad("Адрес API: укажите полный адрес, например https://wiafleet.online/api")
            new["url"] = url
            if b.get("password"):
                new["password"] = text(b["password"], "Пароль", 200)
            for key, lo, hi in (("fuel_density", 0.6, 1.0), ("max_age_min", 1, 1440)):
                try:
                    val = float(str(b.get(key)).replace(",", "."))
                except ValueError as exc:
                    raise Bad("Укажите число") from exc
                if not lo <= val <= hi:
                    raise Bad(f"Значение должно быть от {lo} до {hi}")
                new[key] = val
            stv.save(new)
        s_ = stv.settings()
        return JSONResponse({k: v for k, v in s_.items() if k != "password"} | {"password_set": bool(s_["password"])})

    @handler
    async def stavtrack_check(request: Request):
        """Проверка связи: какие машины привязаны и что сейчас показывает ДУТ."""
        admin_of(request)
        loop = asyncio.get_running_loop()

        def work():
            devs = stv.devices()
            rows = []
            for v in db.vehicles():
                if v.get("kind") == "trailer":
                    continue
                f = stv.fuel(v["plate"])
                rows.append({"plate": v["plate"], "device": f.get("device") or "", "liters": f["liters"],
                             "age_s": f["age_s"], "note": f["note"],
                             "sensors": sorted(str(k) for k in (f.get("raw") or {}))[:30],
                             "linked": bool(db.q("SELECT 1 FROM st_links WHERE plate=?", (v["plate"],)))})
            return {"devices": devs, "vehicles": rows}
        try:
            return JSONResponse(await loop.run_in_executor(None, work))
        except StError as ex:
            raise Bad(str(ex), 502) from ex

    @handler
    async def stavtrack_link(request: Request):
        admin_of(request)
        b = await body(request)
        plate = text(b.get("plate"), "Машина", 20)
        if b.get("device_id"):
            db.x("INSERT INTO st_links(plate, device_id) VALUES(?,?) "
                 "ON CONFLICT(plate) DO UPDATE SET device_id=excluded.device_id", (plate, int(b["device_id"])))
        else:
            db.x("DELETE FROM st_links WHERE plate=?", (plate,))
        return JSONResponse({"ok": True})

    # ---- карта: где сейчас машины
    def places():
        s_ = routing.settings()
        return {"clients": db.q("SELECT name, address, lat, lon, geo_precision FROM clients "
                                "WHERE active=1 AND lat IS NOT NULL"),
                "base": {"lat": s_["base_lat"], "lon": s_["base_lon"]} if s_["base_lat"] is not None else None}

    @handler
    async def map_api(request: Request):
        logist_of(request)
        loop = asyncio.get_running_loop()
        today = datetime.now().strftime("%Y-%m-%dT00:00:00")

        def work():
            all_pos = stv.positions()
            vehicles, unlinked = [], []
            for v in db.vehicles():
                if v.get("kind") == "trailer":
                    continue
                dev = stv.device_for(v["plate"])
                pos = all_pos.get(dev["id"]) if dev else None
                if not dev or not pos or pos.get("latitude") is None:
                    unlinked.append(v["plate"])
                    continue
                f = stv.fuel_of(dev, pos)
                # Кто на машине: водитель последнего взвешивания или последней выгрузки
                last = db.q("SELECT driver_id, trailer, ts FROM (SELECT driver_id, trailer, ts FROM weighings WHERE plate=? "
                            "AND driver_id IS NOT NULL UNION ALL SELECT driver_id, trailer, ts FROM unloads WHERE plate=? "
                            "AND deleted IS NULL) ORDER BY ts DESC LIMIT 1", (v["plate"], v["plate"]))
                driver = db.driver(last[0]["driver_id"]) if last else None
                done = db.q("SELECT COALESCE(SUM(liters),0) AS l, COUNT(*) AS n FROM unloads WHERE plate=? "
                            "AND deleted IS NULL AND ts>=?", (v["plate"], today))[0]
                orders = db.q("SELECT c.name AS client, o.address, o.liters FROM orders o JOIN clients c ON c.id=o.client_id "
                              "WHERE o.driver_id=? AND o.status='new' ORDER BY o.day, o.pos, o.id",
                              (driver["id"],)) if driver else []
                c = lcfg()
                road = legs(db, density(), c["tolerance_pct"], c["tolerance_l"], plate=v["plate"])
                road = road[0] if road and road[0]["status"] == "open" else None
                vehicles.append({
                    "plate": v["plate"], "device": dev["name"], "lat": pos["latitude"], "lon": pos["longitude"],
                    "speed": round(pos.get("speed") or 0), "course": pos.get("course"), "address": pos.get("address") or "",
                    "time": pos.get("deviceTime"), "age_s": f["age_s"],
                    "stale": f["age_s"] is None or f["age_s"] > 30 * 60, "fuel": f["liters"],
                    "driver": driver["name"] if driver else "", "trailer": (last[0]["trailer"] or "") if last else "",
                    "today_l": round(done["l"], 2), "today_n": done["n"], "orders": orders,
                    "gas_left_l": road["left_l"] if road else None})
            return {"configured": True, "vehicles": vehicles, "unlinked": unlinked, "error": None}

        if not stv.ready():
            return JSONResponse({"configured": False, "vehicles": [], "unlinked": [], "error": None, **places()})
        try:
            return JSONResponse({**(await loop.run_in_executor(None, work)), **places()})
        except StError as ex:
            return JSONResponse({"configured": True, "vehicles": [], "unlinked": [], "error": str(ex), **places()})

    # ---- взвешивания с фото
    @handler
    async def weighings_api(request: Request):
        logist_of(request)
        start, end = period(request)
        tok = bearer(request)
        rows = db.q("SELECT w.*, d.name AS driver, f.liters AS fuel, f.note AS fuel_note FROM weighings w "
                    "LEFT JOIN drivers d ON d.id=w.driver_id LEFT JOIN weigh_fuel f ON f.weighing_id=w.id "
                    "WHERE w.ts>=? AND w.ts<=? ORDER BY w.id DESC LIMIT 500", (start, end + "T23:59:59"))
        return JSONResponse([{
            "id": w["id"], "ts": w["ts"], "plate": w["plate"], "trailer": w.get("trailer") or "", "weight": w["weight"],
            "kind": w.get("kind") or "", "source": w.get("source") or "", "driver": w.get("driver") or "",
            "fuel": w.get("fuel"), "fuel_note": w.get("fuel_note") or "",
            "photos": [f"/api/logist/photo/{x}?t={tok}" for x in (w["snapshots"] or "").split(";") if x],
        } for w in rows])

    @handler
    async def logist_photo(request: Request):
        u = staff(request, request.query_params.get("t", ""))
        if u["role"] != "logist":
            raise Bad("Доступно только логисту", 403)
        rel = request.path_params["path"]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}/[\w.\-]+\.jpg", rel) or not (snap_dir / rel).is_file():
            raise Bad("Не найдено", 404)
        return FileResponse(snap_dir / rel)

    def invoice_mark_as(action: str):
        @handler
        async def mark(request: Request):
            return await invoice_mark(request, action)
        return mark

    async def invoice_mark(request: Request, action: str):
        u = staff(request)
        uid = request.path_params["uid"]
        manager_owns(u, uid)
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
        Route("/api/app/push-token", app_push_token, methods=["POST"]),
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
        Route("/api/logist/unloads/{uid}/paid", paid_mark, methods=["POST"]),
        Route("/api/logist/orders", orders_api, methods=["GET", "POST"]),
        Route("/api/logist/orders/{oid:int}/cancel", order_cancel, methods=["POST"]),
        Route("/api/logist/orders/route", orders_route, methods=["POST"]),
        Route("/api/logist/routing", routing_api, methods=["GET", "POST"]),
        Route("/api/logist/orders/{oid:int}/seen", order_note_done, methods=["POST"]),
        Route("/api/logist/weighings", weighings_api),
        Route("/api/logist/map", map_api),
        Route("/api/logist/stavtrack", stavtrack_api, methods=["GET", "POST"]),
        Route("/api/logist/stavtrack/check", stavtrack_check),
        Route("/api/logist/stavtrack/link", stavtrack_link, methods=["POST"]),
        Route("/api/logist/photo/{path:path}", logist_photo),
        Route("/api/logist/invoices/{uid}/done", invoice_mark_as("done"), methods=["POST"]),
        Route("/api/logist/invoices/{uid}/undo", invoice_mark_as("undo"), methods=["POST"]),
        Route("/api/logist/legs/{tid:int}/done", leg_done, methods=["POST"]),
    ]
