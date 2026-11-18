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
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__
from . import settings as st
from .plates import normalize as norm_plate
from .routing import PRECISE, RouteError, Routing, best_order
from .amocrm import ROLES as AMO_ROLES, AmoCRM, AmoError, clean_domain
from .stavtrack import Stavtrack, StError
from .weighing import DriverError

STATIC = Path(__file__).parent / "static"
MAX_LITERS = 200_000
DRIVER_DAYS = 365          # водитель входит один раз
LOGIST_DAYS = 14
BACKDATE_DAYS = 60
# Цвета водителей на доске заявок: заметные, различимые, с тёмным текстом на светлом фоне строки
DRIVER_COLORS = ["#1d5fbf", "#e8590c", "#2b8a3e", "#ae3ec9", "#c92a2a", "#0c8599", "#f08c00", "#5f3dc4",
                 "#a61e4d", "#5c940d", "#364fc7", "#862e9c"]


def driver_color(d: dict) -> str:
    c = (d.get("color") or "").strip()
    return c if re.fullmatch(r"#[0-9a-fA-F]{6}", c) else DRIVER_COLORS[(int(d["id"]) - 1) % len(DRIVER_COLORS)]

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

    def project_id(self) -> str:
        try:
            return str(json.loads(self.file.read_text(encoding="utf-8")).get("project_id") or "")
        except (OSError, ValueError):
            return ""

    def check(self):
        """Ключ рабочий, если Google выдаёт по нему доступ к отправке уведомлений."""
        self.creds = None
        self._token()

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
                             ("geo_precision", "TEXT NOT NULL DEFAULT ''"), ("geo_text", "TEXT NOT NULL DEFAULT ''"),
                             ("phone", "TEXT NOT NULL DEFAULT ''"), ("amo_contact_id", "INTEGER")]),
                ("orders", [("lat", "REAL"), ("lon", "REAL"), ("geo_precision", "TEXT NOT NULL DEFAULT ''"),
                            ("phone", "TEXT NOT NULL DEFAULT ''"), ("amo_lead_id", "INTEGER")]),
                ("unloads", [("order_id", "INTEGER"), ("paid", "TEXT NOT NULL DEFAULT ''"), ("paid_at", "TEXT"),
                             ("paid_by", "TEXT NOT NULL DEFAULT ''"),
                             ("seal", "TEXT NOT NULL DEFAULT ''"),        # номер пломбы на ёмкости клиента
                             ("warranty", "INTEGER")]),                   # гарантийный талон: 1 / 0 / NULL — не указано
                ("drivers", [("color", "TEXT NOT NULL DEFAULT ''")]),
                ("logists", [("role", "TEXT NOT NULL DEFAULT 'logist'")])):
            have = {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols:
                if col not in have:
                    db.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        db.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_drivers_login ON drivers(login) WHERE login<>''")
        # Права администратора — только у отдельного пользователя-администратора, не у логистов
        db.conn.execute("UPDATE logists SET admin=0 WHERE role<>'admin' AND admin<>0")
        db.conn.commit()


ADMIN_FILE = "admin-password.txt"


def ensure_admin(db, data_dir, reset: bool = False) -> tuple[str, str] | None:
    """Администратор кабинета создаётся сам при первом запуске: логин admin и случайный пароль.
    Пароль записывается в файл admin-password.txt в папке данных (его выводит установщик).
    reset=True — новый пароль для администратора (sudo bash deploy/admin-password.sh)."""
    rows = db.q("SELECT * FROM logists WHERE role='admin' AND active=1 ORDER BY id")
    if rows and not reset:
        return None
    password = secrets.token_urlsafe(9)
    if rows:
        login = rows[0]["login"]
        db.x("UPDATE logists SET pass_hash=? WHERE id=?", (st.hash_password(password), rows[0]["id"]))
    else:
        login, n = "admin", 1
        while db.q("SELECT 1 FROM logists WHERE login=?", (login,)):
            n += 1
            login = f"admin{n}"
        db.x("INSERT INTO logists(login,name,pass_hash,admin,role) VALUES(?,?,?,1,'admin')",
             (login, "Администратор", st.hash_password(password)))
    f = Path(data_dir) / ADMIN_FILE
    f.write_text(f"Кабинет АвтоВес (/logist), администратор\nЛогин: {login}\nПароль: {password}\n"
                 "Смените пароль после первого входа: «Пользователи → Логисты и менеджеры».\n", encoding="utf-8")
    try:
        f.chmod(0o600)
    except OSError:
        pass
    logging.getLogger("logist").warning("Администратор кабинета: логин %s, пароль в файле %s", login, f)
    return login, password


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
                    # Топливо уже вычтено из газа при каждом взвешивании (db.recalc_gas);
                    # здесь — сколько литров газа «съело» бы сожжённое топливо без этой поправки
                    burned_kg = (leg["fuel_out"] - leg["fuel_in"]) * fuel_density
                    leg["fuel_fix_l"] = round(burned_kg / density)
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
    ensure_admin(db, ctx.cfg["data_dir"])
    admin_file = Path(ctx.cfg["data_dir"]) / ADMIN_FILE
    tokens = Tokens(ctx.cfg["data_dir"])
    snap_dir = Path(ctx.cfg["data_dir"]) / "snapshots"
    tile_dir = Path(ctx.cfg["data_dir"]) / "tiles"
    pusher = getattr(ctx, "pusher", None) or Pusher(ctx.cfg["data_dir"])
    if getattr(ctx, "stavtrack", None) is None:
        ctx.stavtrack = Stavtrack(db)
        db.on_weighing = getattr(db, "on_weighing", []) + [ctx.stavtrack.on_weighing]
    stv = ctx.stavtrack
    if getattr(ctx, "routing", None) is None:
        ctx.routing = Routing(db)
    routing = ctx.routing
    if getattr(ctx, "amocrm", None) is None:
        ctx.amocrm = AmoCRM(db)
    amo = ctx.amocrm

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

    def worker(request: Request, token: str | None = None) -> dict:
        """Логист или менеджер — рабочие данные. Администратору они не нужны: у него пользователи и подключения."""
        u = staff(request, token)
        if u["role"] == "admin":
            raise Bad("Администратор не работает с заявками и выгрузками: войдите как логист", 403)
        return u

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
                "paid": r.get("paid") or "", "order_id": r.get("order_id"),
                "seal": r.get("seal") or "", "warranty": r.get("warranty")}

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
            "orders": db.q("SELECT o.id, o.client_id, c.name AS client, c.private, o.address, o.liters, o.day, o.note, o.phone, "
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
        seal = text(op.get("seal"), "Номер пломбы", 40, required=False)
        warranty = int(op["warranty"]) if isinstance(op.get("warranty"), bool) else None
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
        db.x("INSERT INTO unloads(id,driver_id,driver,plate,trailer,client_id,liters,ts,comment,created,order_id,paid,"
             "seal,warranty) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (uid, d["id"], d["name"], plate, trailer, cid, amount, ts, comment, now_iso(),
              order["id"] if order else None, paid, seal, warranty))
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

    # ---- приложение водителя в Google Play: сведения о компании и политика конфиденциальности
    def app_info() -> dict:
        db.x("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        rows = db.q("SELECT value FROM kv WHERE key='app_info'")
        out = {"company": "", "contact": ""}
        if rows:
            out.update(json.loads(rows[0]["value"]))
        return out

    async def privacy_page(_):
        """Политика конфиденциальности для страницы приложения в Google Play (ссылка /privacy)."""
        from html import escape
        info = app_info()
        company = escape(info["company"] or "Компания — владелец сервера АвтоВес")
        contact = escape(info["contact"] or "через своего логиста или руководителя")
        html = PRIVACY.replace("{company}", company).replace("{contact}", contact)
        return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})

    @handler
    async def appinfo_api(request: Request):
        admin_of(request)
        if request.method == "POST":
            b = await body(request)
            new = {"company": text(b.get("company"), "Название компании", 200, False),
                   "contact": text(b.get("contact"), "Контакт", 200, False)}
            app_info()
            db.x("INSERT INTO kv(key, value) VALUES('app_info', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps(new, ensure_ascii=False),))
        if request.method == "GET":
            app_info()
        drivers = db.q("SELECT COUNT(*) AS n FROM drivers WHERE active=1 AND COALESCE(login,'')<>''")[0]["n"]
        return JSONResponse({**app_info(), "push": pusher.ready(), "drivers": drivers,
                             "project": pusher.project_id() if hasattr(pusher, "project_id") else "",
                             "push_list": db.q("SELECT d.id, d.name, COUNT(p.token) AS phones FROM drivers d "
                                               "LEFT JOIN push_tokens p ON p.driver_id=d.id WHERE d.active=1 AND "
                                               "COALESCE(d.login,'')<>'' GROUP BY d.id ORDER BY d.name"),
                             "push_drivers": db.q("SELECT COUNT(DISTINCT driver_id) AS n FROM push_tokens")[0]["n"],
                             "host": request.headers.get("host", "")})

    @handler
    async def firebase_key(request: Request):
        """Ключ сервисного аккаунта Firebase для уведомлений водителям: загружается из кабинета администратора."""
        admin_of(request)
        f = Path(ctx.cfg["data_dir"]) / "firebase.json"
        if request.method == "DELETE":
            f.unlink(missing_ok=True)
            if hasattr(pusher, "creds"):
                pusher.creds = None
            return JSONResponse({"ok": True})
        b = await body(request)
        need = ("type", "project_id", "private_key", "client_email")
        if b.get("type") != "service_account" or not all(isinstance(b.get(k), str) and b[k] for k in need) \
                or "PRIVATE KEY" not in b["private_key"]:
            raise Bad("Это не ключ сервисного аккаунта Firebase. Нужен файл из «Настройки проекта → "
                      "Сервисные аккаунты → Создать закрытый ключ», а не google-services.json")
        old = f.read_bytes() if f.is_file() else None
        f.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        try:
            f.chmod(0o600)
        except OSError:
            pass
        if hasattr(pusher, "check"):
            try:
                await asyncio.get_running_loop().run_in_executor(None, pusher.check)
            except Exception as ex:  # noqa: BLE001 — нет пакета, нет связи или ключ отозван
                if old is None:
                    f.unlink(missing_ok=True)
                else:
                    f.write_bytes(old)
                pusher.creds = None
                raise Bad(f"Google не принял ключ: {str(ex)[:200]}") from ex
        return JSONResponse({"ok": True, "project": b["project_id"]})

    @handler
    async def test_push(request: Request):
        """Проверка: уведомление на телефоны выбранного водителя прямо сейчас."""
        admin_of(request)
        b = await body(request)
        rows = db.q("SELECT id, name FROM drivers WHERE id=?", (b.get("driver_id"),))
        if not rows:
            raise Bad("Выберите водителя")
        if not pusher.ready():
            raise Bad("Сначала загрузите ключ Firebase")
        toks = [r["token"] for r in db.q("SELECT token FROM push_tokens WHERE driver_id=?", (rows[0]["id"],))]
        if not toks:
            raise Bad(f"{rows[0]['name']} ещё не включил уведомления: пусть войдёт в новую версию приложения "
                      "из Google Play и разрешит уведомления")
        dead = await asyncio.get_running_loop().run_in_executor(
            None, pusher.send, toks, "Проверка уведомлений", "АвтоВес: уведомления о заказах работают")
        for t in dead:
            db.x("DELETE FROM push_tokens WHERE token=?", (t,))
        return JSONResponse({"ok": True, "phones": len(toks) - len(dead), "dead": len(dead)})

    @handler
    async def logist_status(_):
        # Где взять пароль администратора — подсказка только в локальной сети весовой
        hint = str(admin_file) if not public and admin_file.is_file() else ""
        return JSONResponse({"setup": False, "empty": False, "admin_file": hint, "version": __version__})

    def new_password(value) -> str:
        if not isinstance(value, str) or len(value) < 8:
            raise Bad("Пароль: не короче 8 символов")
        return st.hash_password(value)

    @handler
    async def logist_setup(request: Request):
        raise Bad("Администратор создаётся автоматически; пользователей добавляет он", 403)

    @handler
    async def logist_login(request: Request):
        row, token = await login_common(request, "l", "logists", LOGIST_DAYS)
        return JSONResponse({"token": token})

    @handler
    async def logist_me(request: Request):
        u = staff(request)
        return JSONResponse({"login": u["login"], "name": u["name"], "admin": u["role"] == "admin", "role": u["role"],
                             "notices": notice_count(own(u)) if u["role"] != "admin" else 0, "tolerance": lcfg(), "push": pusher.ready(),
                             "map": map_settings() if u["role"] == "logist" else None})

    def map_settings() -> dict:
        s_ = routing.settings()
        if s_["map_provider"] == "stavtrack" and s_.get("map_url"):
            return {"provider": "stavtrack", "url": s_["map_url"]}
        yandex = s_["map_provider"] == "yandex" and bool(s_["map_key"])
        # Ключ JavaScript API Яндекс Карт по своей природе виден в браузере; его ограничивают по адресу сайта
        return {"provider": "yandex" if yandex else "osm", "key": s_["map_key"] if yandex else ""}

    @handler
    async def dashboard(request: Request):
        """Сводка: продажи (литры) по дням выбранного месяца и по месяцам за год, сравнение с прошлым месяцем,
        клиенты, водители, менеджеры, типы клиентов. Менеджер видит только своих клиентов."""
        u = worker(request)
        owner = own(u)
        mine, margs = ("", ()) if owner is None else (" AND c.manager_id=?", (owner,))
        now = datetime.now()
        month = request.query_params.get("month") or now.strftime("%Y-%m")
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
            raise Bad("Некорректный месяц")
        y, m = int(month[:4]), int(month[5:])

        def shift(y_, m_, k):
            i = y_ * 12 + m_ - 1 + k
            return i // 12, i % 12 + 1

        def ym(y_, m_):
            return f"{y_:04d}-{m_:02d}"
        ny, nm = shift(y, m, 1)
        py, pm = shift(y, m, -1)
        days_in = (datetime(ny, nm, 1) - datetime(y, m, 1)).days
        current = month == now.strftime("%Y-%m")
        upto = now.day if current else days_in          # для сравнения берём столько же дней прошлого месяца
        base = "FROM unloads u JOIN clients c ON c.id=u.client_id WHERE u.deleted IS NULL"

        def total(start, end):
            r = db.q(f"SELECT COALESCE(SUM(u.liters),0) AS l, COUNT(*) AS n, COUNT(DISTINCT u.client_id) AS k "
                     f"{base} AND u.ts>=? AND u.ts<?" + mine, (start, end, *margs))[0]
            return {"liters": round(r["l"], 2), "count": r["n"], "clients": r["k"]}
        start, end = ym(y, m) + "-01", ym(ny, nm) + "-01"
        per_day = {r["d"]: r for r in db.q(
            f"SELECT substr(u.ts,1,10) AS d, SUM(u.liters) AS l, COUNT(*) AS n {base} AND u.ts>=? AND u.ts<?{mine} GROUP BY d",
            (start, end, *margs))}
        days = []
        for d in range(1, days_in + 1):
            key = f"{month}-{d:02d}"
            r = per_day.get(key)
            days.append({"day": key, "liters": round(r["l"], 2) if r else 0, "count": r["n"] if r else 0,
                         "future": current and d > now.day})
        prev_end = ym(py, pm) + f"-{min(upto, (datetime(y, m, 1) - datetime(py, pm, 1)).days):02d}T23:59:59"
        # 12 месяцев, заканчивая выбранным
        my, mm = shift(y, m, -11)
        per_month = {r["mo"]: r for r in db.q(
            f"SELECT substr(u.ts,1,7) AS mo, SUM(u.liters) AS l, COUNT(*) AS n {base} AND u.ts>=? AND u.ts<?{mine} GROUP BY mo",
            (ym(my, mm) + "-01", end, *margs))}
        months = []
        for k in range(12):
            yy, mo = shift(my, mm, k)
            r = per_month.get(ym(yy, mo))
            months.append({"month": ym(yy, mo), "liters": round(r["l"], 2) if r else 0, "count": r["n"] if r else 0})

        def top(col, limit=8, none="Не указан"):
            return [{"name": r["label"] or none, "liters": round(r["l"], 2), "count": r["n"]} for r in db.q(
                f"SELECT {col} AS label, SUM(u.liters) AS l, COUNT(*) AS n {base} AND u.ts>=? AND u.ts<?{mine} "
                f"GROUP BY {col} ORDER BY l DESC LIMIT {limit}", (start, end, *margs))]
        kinds = {r["k"]: r for r in db.q(
            f"SELECT CASE WHEN c.private=1 THEN 'private' WHEN c.legal=1 THEN 'legal' ELSE 'own' END AS k, "
            f"SUM(u.liters) AS l, COUNT(*) AS n {base} AND u.ts>=? AND u.ts<?{mine} GROUP BY k", (start, end, *margs))}
        split = [{"name": name, "liters": round(kinds[k]["l"], 2) if k in kinds else 0, "count": kinds[k]["n"] if k in kinds else 0}
                 for k, name in (("legal", "Юрлица"), ("private", "Частные лица"), ("own", "Свои точки"))]
        today = now.strftime("%Y-%m-%d")
        tday = total(today, (now + timedelta(days=1)).strftime("%Y-%m-%d"))
        out = {"month": month, "current": current, "days_in": days_in, "upto": upto,
               "days": days, "months": months, "total": total(start, end),
               "prev": {**total(ym(py, pm) + "-01", prev_end), "month": ym(py, pm)},
               "today": {**tday, "day": today}, "split": split,
               "clients": top("c.name"), "drivers": top("u.driver"), "managers": None,
               "pay": None, "invoices": db.q(f"SELECT COUNT(*) AS n {base} AND u.invoice='pending'" + mine, margs)[0]["n"],
               "unpaid": db.q(f"SELECT COUNT(*) AS n {base} AND u.paid='no'" + mine, margs)[0]["n"],
               "orders": None, "on_road": None}
        # оплата частных лиц за месяц: сколько выгрузок оплачено и нет
        pay = {r["p"]: r["n"] for r in db.q(
            f"SELECT COALESCE(u.paid,'') AS p, COUNT(*) AS n {base} AND c.private=1 AND u.ts>=? AND u.ts<?{mine} GROUP BY p",
            (start, end, *margs))}
        out["pay"] = {"yes": pay.get("yes", 0), "no": pay.get("no", 0)}
        if owner is None:
            out["managers"] = top("(SELECT name FROM logists WHERE id=c.manager_id)", none="Без менеджера")
            st_ = {r["status"]: r["n"] for r in db.q(
                "SELECT status, COUNT(*) AS n FROM orders WHERE day=? GROUP BY status", (today,))}
            out["orders"] = {"new": st_.get("new", 0), "done": st_.get("done", 0), "failed": st_.get("failed", 0)}
            c = lcfg()
            since = (now - timedelta(days=3)).isoformat(timespec="seconds")
            out["on_road"] = sum(1 for g in legs(db, density(), c["tolerance_pct"], c["tolerance_l"], since)
                                 if g["status"] == "open")
        return JSONResponse(out)

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
        return JSONResponse(report(request, own(worker(request))))

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
            "drivers": [r | {"color": driver_color(r)} for r in db.q(
                "SELECT id,name,login,active,plate,color,(pass_hash<>'') AS has_password FROM drivers ORDER BY name")],
            "clients": db.q("SELECT * FROM clients ORDER BY active DESC, name"),
            "vehicles": [{"plate": v["plate"], "kind": v.get("kind") or "", "tank_l": v.get("tank_l"),
                          "tare": v.get("tare")} for v in vs],
        })

    @handler
    async def logist_xlsx(request: Request):
        owner = own(worker(request))
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
        sh.append(["Дата и время", "Водитель", "Машина", "Прицеп", "Клиент", "Литры", "Счёт", "Комментарий", "Оплата",
                   "Номер пломбы", "Гарантийный талон"])
        for c in sh[1]:
            c.font = bold
        for u in data["unloads"]:
            sh.append([u["ts"].replace("T", " "), safe_cell(u["driver"]), u["plate"], u["trailer"], safe_cell(u["client"]),
                       u["liters"], {"pending": "нужно выставить", "done": "выставлен"}.get(u["invoice"], ""),
                       safe_cell(u["comment"]), {"yes": "оплачено", "no": "не оплачено"}.get(u["paid"], ""),
                       safe_cell(u["seal"]), {1: "выдан", 0: "не выдан"}.get(u["warranty"], "")])
        for col, w in zip("ABCDEFGHIJK", (20, 26, 14, 14, 30, 12, 18, 40, 14, 16, 18)):
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
            if "seal" in b:
                db.x("UPDATE unloads SET seal=? WHERE id=?", (text(b["seal"], "Номер пломбы", 40, False), uid))
            if "warranty" in b:
                w = b["warranty"]
                db.x("UPDATE unloads SET warranty=? WHERE id=?", (int(w) if isinstance(w, bool) else None, uid))
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
        admin_of(request)
        b = await body(request)
        name = text(b.get("name"), "ФИО", 120)
        login = text(b.get("login"), "Логин", 60).casefold()
        did = b.get("id")
        clash = db.q("SELECT id FROM drivers WHERE login=?", (login,))
        if clash and clash[0]["id"] != did:
            raise Bad("Такой логин уже есть", 409)
        active = int(b.get("active", True) is not False)
        color = str(b.get("color") or "").strip()
        if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise Bad("Цвет: выберите цвет из палитры")
        if did:
            if not db.driver(int(did)):
                raise Bad("Водитель не найден", 404)
            db.x("UPDATE drivers SET name=?, login=?, active=? WHERE id=?", (name, login, active, did))
            if color:
                db.x("UPDATE drivers SET color=? WHERE id=?", (color.lower(), did))
            if b.get("password"):
                db.x("UPDATE drivers SET pass_hash=? WHERE id=?", (new_password(b["password"]), did))
        else:
            # PIN для страницы в сети весовой не задан: вместо него случайная заглушка
            did = db.x("INSERT INTO drivers(name,login,pass_hash,active,pin_hash) VALUES(?,?,?,?,?)",
                       (name, login, new_password(b.get("password")), active, "nopin:" + secrets.token_hex(16)))
            # Свой цвет на доске заявок: первый, которого ещё нет у работающих водителей
            busy = {driver_color(r) for r in db.q("SELECT id, color FROM drivers WHERE active=1 AND id<>?", (did,))}
            free = [c for c in DRIVER_COLORS if c not in busy]
            db.x("UPDATE drivers SET color=? WHERE id=?", ((color or (free or DRIVER_COLORS)[0]).lower(), did))
        return JSONResponse({"ok": True, "id": did})

    # ---- логисты
    @handler
    async def logists_api(request: Request):
        u = staff(request)
        is_admin = u["role"] == "admin"
        if request.method == "GET":
            if not is_admin:
                return JSONResponse(db.q("SELECT id,login,name,admin,active,role FROM logists WHERE id=?", (u["id"],)))
            return JSONResponse(db.q("SELECT id,login,name,admin,active,role FROM logists "
                                     "ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'logist' THEN 1 ELSE 2 END, login"))
        b = await body(request)
        lid = b.get("id")
        if not is_admin and lid != u["id"]:
            raise Bad("Пользователей добавляет и меняет только администратор", 403)
        if lid:
            if not db.q("SELECT 1 FROM logists WHERE id=?", (lid,)):
                raise Bad("Логист не найден", 404)
            if "name" in b:
                db.x("UPDATE logists SET name=? WHERE id=?", (text(b["name"], "Имя", 120, False), lid))
            if b.get("password"):
                db.x("UPDATE logists SET pass_hash=? WHERE id=?", (new_password(b["password"]), lid))
            if "active" in b and is_admin and lid != u["id"]:
                db.x("UPDATE logists SET active=? WHERE id=?", (int(bool(b["active"])), lid))
            out = {"ok": True}
            if b.get("password") and admin_file.is_file() and \
                    db.q("SELECT 1 FROM logists WHERE id=? AND role='admin'", (lid,)):
                admin_file.unlink(missing_ok=True)     # пароль сменили — старый файл больше не нужен
            if lid == u["id"] and b.get("password"):
                row = db.q("SELECT * FROM logists WHERE id=?", (lid,))[0]
                out["token"] = tokens.make("l", lid, row["pass_hash"], LOGIST_DAYS)
            return JSONResponse(out)
        login = text(b.get("login"), "Логин", 60).casefold()
        if db.q("SELECT 1 FROM logists WHERE login=?", (login,)):
            raise Bad("Такой логин уже есть", 409)
        role = b.get("role") if b.get("role") in ("manager", "admin") else "logist"
        db.x("INSERT INTO logists(login,name,pass_hash,admin,role) VALUES(?,?,?,?,?)",
             (login, text(b.get("name"), "Имя", 120, False), new_password(b.get("password")), int(role == "admin"), role))
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
        u = worker(request)
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
        u = worker(request)
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
        args = (client[0], text(b.get("address"), "Адрес", 300, False), liters_value(b.get("liters")),
                driver[0]["id"], day, text(b.get("note"), "Примечание", 300, False), b.get("id"), u["login"])
        oid = await asyncio.get_running_loop().run_in_executor(None, lambda: save_order(*args))
        return JSONResponse({"ok": True, "id": oid})

    def geo_for_order(client: dict, address: str):
        """Свой адрес заказа ищем отдельно; если адрес как у клиента — берутся координаты клиента."""
        if address and address != client["address"]:
            return locate("", address, None)[:3]
        return (None, None, "")

    def save_order(client: dict, address: str, liters: float, driver_id: int, day: str, note: str,
                         oid, login: str, phone: str = "", amo_lead_id: int | None = None,
                         quiet: bool = False) -> int:
        """Новый заказ или изменение заказа водителю; водитель получает уведомление.
        Ищет координаты адреса, поэтому из обработчиков запросов вызывается в отдельном потоке."""
        f = (client["id"], address or client["address"], liters, driver_id, day, note)
        if oid:
            old = db.q("SELECT * FROM orders WHERE id=?", (oid,))
            if not old:
                raise Bad("Заказ не найден", 404)
            if old[0]["status"] == "done":
                raise Bad("Заказ уже выполнен")
            changed = any(old[0][k] != v for k, v in zip(("client_id", "address", "liters", "driver_id", "day", "note"), f)) \
                or old[0]["status"] != "new" or (old[0]["phone"] or "") != phone
            db.x("UPDATE orders SET client_id=?,address=?,liters=?,driver_id=?,day=?,note=?,status='new',reason='',"
                 "closed=NULL, phone=? WHERE id=?", (*f, phone, oid))
            if old[0]["driver_id"] != f[3] and old[0]["status"] == "new":
                push(old[0]["driver_id"], "Заказ снят", f"{client['name']}: заказ передан другому водителю")
            if changed or not quiet:
                push(f[3], "Заказ изменён" if old[0]["driver_id"] == f[3] and old[0]["status"] == "new" else "Новый заказ",
                     f"{client['name']}: {f[2]:g} л. {f[1]}".strip())
            moved = old[0]["address"] != f[1]
        else:
            pos = db.q("SELECT COALESCE(MAX(pos),0)+1 AS p FROM orders WHERE driver_id=? AND day=?", (f[3], day))[0]["p"]
            oid = db.x("INSERT INTO orders(client_id,address,liters,driver_id,day,note,pos,created,created_by,phone,amo_lead_id) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*f, pos, now_iso(), login, phone, amo_lead_id))
            push(f[3], "Новый заказ", f"{client['name']}: {f[2]:g} л. {f[1]}".strip())
            moved = True
        if moved:
            own_geo = geo_for_order(client, f[1])
            db.x("UPDATE orders SET lat=?, lon=?, geo_precision=? WHERE id=?", (*own_geo, oid))
        return oid

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
            new = {"mode": "driving" if b.get("mode") == "driving" else "truck", "has_trailer": bool(b.get("has_trailer")),
                   "map_provider": b.get("map_provider") if b.get("map_provider") in ("yandex", "stavtrack") else "osm"}
            if "map_url" in b:
                url = text(b.get("map_url"), "Ссылка на карту Ставтрэка", 500, False)
                if url and not re.fullmatch(r"https?://[^\s<>\"']+", url):
                    raise Bad("Ссылка на карту Ставтрэка должна начинаться с https://")
                new["map_url"] = url
            if new["map_provider"] == "stavtrack" and not (new.get("map_url") or routing.settings().get("map_url")):
                raise Bad("Вставьте гео-ссылку из Ставтрэка")
            for key in ("geocoder_key", "router_key", "map_key"):
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
                             "mode": s_["mode"], "has_trailer": s_["has_trailer"],
                             "map_provider": s_["map_provider"], "map_key_set": bool(s_["map_key"]),
                             "map_url": s_.get("map_url") or ""})

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

    # ---- заявки из amoCRM: логист выбирает водителя, заявка уходит заказом в приложение
    def amo_client(lead: dict) -> dict:
        """Клиент заявки: по контакту amoCRM, иначе по имени; новый клиент — частное лицо."""
        name = (lead["client"] or lead["name"] or f"Сделка {lead['lead_id']}")[:120]
        rows = db.q("SELECT * FROM clients WHERE amo_contact_id=?", (lead["contact_id"],)) if lead["contact_id"] else []
        if not rows:
            rows = [r for r in db.q("SELECT * FROM clients") if r["name"].casefold() == name.casefold()]
        mgr = db.q("SELECT id FROM logists WHERE role='manager' AND active=1 AND name=?",
                   (lead["manager"],)) if lead["manager"] else []
        mgr_id = mgr[0]["id"] if mgr else None
        if rows:
            cid = rows[0]["id"]
            db.x("UPDATE clients SET amo_contact_id=COALESCE(?, amo_contact_id), phone=CASE WHEN ?<>'' THEN ? ELSE phone END, "
                 "active=1, manager_id=COALESCE(manager_id, ?) WHERE id=?",
                 (lead["contact_id"], lead["phone"], lead["phone"], mgr_id, cid))
        else:
            cid = db.x("INSERT INTO clients(name, private, reviewed, manager, manager_id, phone, amo_contact_id) "
                       "VALUES(?,1,1,?,?,?,?)", (name, lead["manager"][:120], mgr_id, lead["phone"], lead["contact_id"]))
        return db.q("SELECT * FROM clients WHERE id=?", (cid,))[0]

    def amo_note(lead: dict) -> str:
        parts = (["Тел.: " + lead["phone"]] if lead["phone"] else []) + \
                ([lead["conditions"]] if lead["conditions"] else []) + \
                (["Менеджер: " + lead["manager"]] if lead["manager"] else [])
        note = ""
        for p in parts:
            p = p.strip().rstrip(".")
            note += ("" if not note else " " if note[-1] in "!?" else ". ") + p
        return note if len(note) <= 300 else note[:299] + "…"

    def amo_address(lead: dict) -> str:
        return lead["address"] or ", ".join(x for x in (lead["district"], lead["highway"]) if x)[:300]

    def amo_order(lead: dict) -> dict | None:
        rows = db.q("SELECT o.*, c.name AS client FROM orders o JOIN clients c ON c.id=o.client_id WHERE o.id=?",
                    (lead["order_id"],)) if lead["order_id"] else []
        return rows[0] if rows else None

    def amo_cancel(order: dict, why: str):
        db.x("UPDATE orders SET status='cancelled', closed=? WHERE id=?", (now_iso(), order["id"]))
        push(order["driver_id"], "Заказ отменён", f"{order['client']}: {why}")

    def amo_assign(lead_id: int, driver_id, login: str):
        rows = db.q("SELECT * FROM amo_leads WHERE lead_id=?", (lead_id,))
        if not rows:
            raise Bad("Заявка не найдена", 404)
        lead, order = rows[0], amo_order(rows[0])
        if order and order["status"] == "done":
            raise Bad("Заявка уже выполнена")
        if not driver_id:
            if order and order["status"] == "new":
                amo_cancel(order, "заказ снят логистом")
            return None
        if lead["lost"] or not lead["day"]:
            raise Bad("Сделка закрыта в amoCRM или в ней нет даты доставки")
        driver = db.q("SELECT * FROM drivers WHERE id=? AND active=1", (driver_id,))
        if not driver:
            raise Bad("Выберите водителя")
        if not driver[0]["login"]:
            raise Bad("У водителя нет входа в приложение: задайте логин и пароль во вкладке «Водители»")
        if not lead["liters"]:
            raise Bad("В сделке amoCRM не указаны литры")
        oid = save_order(amo_client(lead), amo_address(lead), liters_value(lead["liters"]), driver[0]["id"], lead["day"],
                         amo_note(lead), order["id"] if order else None, login, lead["phone"], lead_id)
        db.x("UPDATE amo_leads SET order_id=? WHERE lead_id=?", (oid, lead_id))
        return oid

    def amo_lead_changed(lead_id: int):
        """Сделку изменили в amoCRM: заказ у водителя обновляется, закрытая сделка снимает заказ."""
        rows = db.q("SELECT * FROM amo_leads WHERE lead_id=?", (lead_id,))
        order = amo_order(rows[0]) if rows else None
        if not order or order["status"] != "new":
            return
        lead = rows[0]
        if lead["lost"] or not lead["day"] or not lead["liters"]:
            amo_cancel(order, "сделка закрыта в amoCRM")
            return
        save_order(amo_client(lead), amo_address(lead), liters_value(lead["liters"]), order["driver_id"], lead["day"],
                   amo_note(lead), order["id"], "amoCRM", lead["phone"], lead_id, quiet=True)

    amo.on_lead = amo_lead_changed
    amo.start()

    @handler
    async def amo_leads(request: Request):
        logist_of(request)
        day = request.query_params.get("day") or datetime.now().strftime("%Y-%m-%d")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise Bad("Некорректная дата")
        rows = db.q("SELECT a.*, o.status AS order_status, o.driver_id, o.reason, d.name AS driver, d.color AS dcolor, "
                    "un.liters AS fact, "
                    "un.paid, un.seal, un.warranty, un.plate AS fact_plate FROM amo_leads a LEFT JOIN orders o ON o.id=a.order_id LEFT JOIN drivers d ON d.id=o.driver_id "
                    "LEFT JOIN unloads un ON un.id=o.unload_id WHERE a.day=? AND a.lost=0 "
                    "ORDER BY a.base, a.highway, a.district, a.lead_id", (day,))
        drivers = [{"id": d["id"], "name": d["name"], "plate": combo_of(d)[0], "color": driver_color(d)}
                   for d in db.q("SELECT * FROM drivers WHERE active=1 AND login<>'' ORDER BY name")]
        plates = {d["id"]: d["plate"] for d in drivers}
        s_ = amo.public()
        for r in rows:
            r["plate"] = (r.pop("fact_plate") or plates.get(r["driver_id"], "")) if r["order_status"] == "done" \
                else plates.get(r["driver_id"], "") if r["order_status"] == "new" else ""
            r.pop("fact_plate", None)
            dcolor = r.pop("dcolor", None)
            r["color"] = driver_color({"id": r["driver_id"], "color": dcolor}) \
                if r["driver_id"] and r["order_status"] in ("new", "done") else ""
            r["url"] = f"https://{s_['domain']}/leads/detail/{r['lead_id']}" if s_["domain"] else ""
        days = db.q("SELECT day, COUNT(*) AS n, SUM(CASE WHEN order_id IS NULL THEN 1 ELSE 0 END) AS free FROM amo_leads "
                    "WHERE lost=0 AND day BETWEEN date(?, '-7 day') AND date(?, '+14 day') GROUP BY day ORDER BY day",
                    (day, day))
        return JSONResponse({"day": day, "leads": rows, "drivers": drivers, "days": days,
                             "amo": {k: s_[k] for k in ("enabled", "ready", "domain", "last_sync", "last_error", "last_count")}})

    @handler
    async def amo_assign_api(request: Request):
        u = logist_of(request)
        b = await body(request)
        try:
            lead_id = int(b.get("lead_id"))
            driver_id = int(b["driver_id"]) if b.get("driver_id") not in (None, "", 0) else None
        except (TypeError, ValueError) as exc:
            raise Bad("Некорректная заявка или водитель") from exc
        oid = await asyncio.get_running_loop().run_in_executor(None, amo_assign, lead_id, driver_id, u["login"])
        return JSONResponse({"ok": True, "order_id": oid})

    @handler
    async def amo_sync_api(request: Request):
        logist_of(request)
        if not amo.ready():
            raise Bad("amoCRM не подключён: попросите администратора открыть «Подключения → amoCRM»")
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, amo.sync)
        except AmoError as ex:
            raise Bad(str(ex), 502) from ex
        return JSONResponse({"ok": True, "count": res["count"], "changed": res["changed"]})

    @handler
    async def amo_settings_api(request: Request):
        admin_of(request)
        if request.method == "POST":
            b = await body(request)
            cur = amo.settings()
            try:
                new = {"enabled": bool(b.get("enabled")), "domain": clean_domain(b.get("domain"))}
            except AmoError as ex:
                raise Bad(str(ex)) from ex
            if b.get("token"):
                new["token"] = text(b["token"], "Ключ доступа", 4000).replace(" ", "")
            for key, lo, hi in (("interval_min", 1, 120), ("days_back", 1, 365)):
                if b.get(key) not in (None, ""):
                    try:
                        val = int(str(b[key]).strip())
                    except ValueError as exc:
                        raise Bad("Укажите целое число") from exc
                    if not lo <= val <= hi:
                        raise Bad(f"Значение должно быть от {lo} до {hi}")
                    new[key] = val
            if "pipeline_id" in b:
                try:
                    new["pipeline_id"] = int(b.get("pipeline_id") or 0)
                except (TypeError, ValueError) as exc:
                    raise Bad("Некорректная воронка") from exc
            if isinstance(b.get("fields"), dict):
                fields = {}
                for role, _ in AMO_ROLES:
                    v = b["fields"].get(role)
                    if v not in (None, "", 0):
                        try:
                            fields[role] = int(v)
                        except (TypeError, ValueError) as exc:
                            raise Bad("Некорректное поле amoCRM") from exc
                if new["enabled"] and not fields.get("day"):
                    raise Bad("Выберите поле «Дата доставки»: без него заявки не разложить по дням")
                new["fields"] = fields
            amo.save(new)
            # Первое подключение: поля угадываются по названиям, их можно поправить
            if not amo.settings()["fields"] and amo.settings()["token"] and amo.settings()["domain"]:
                try:
                    meta = await asyncio.get_running_loop().run_in_executor(None, amo.meta)
                    amo.save({"fields": meta["guess"]})
                except AmoError:
                    pass
            if cur.get("token") != amo.settings()["token"] or cur.get("domain") != amo.settings()["domain"]:
                amo.save({"last_error": ""})
            if b.get("new_hook_secret"):
                amo.save({"hook_secret": ""})      # старый адрес вебхука перестанет работать
        return JSONResponse(amo.public() | {"hook_url": hook_url(request)})

    def hook_url(request: Request) -> str:
        """Адрес вебхука для amoCRM: тот же сайт, через который администратор открыл кабинет."""
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        return f"{proto}://{host}/api/amocrm/hook/{amo.hook_secret()}"

    @handler
    async def amo_hook(request: Request):
        """Вебхук amoCRM: сделка добавлена, изменена, сменила этап или удалена."""
        if not amo.check_secret(request.path_params["secret"]):
            logging.getLogger("amocrm").warning(
                "amoCRM вебхук отклонён: неверный адрес (секрет). Скопируйте адрес заново из «Подключения → amoCRM»")
            raise Bad("Не найдено", 404)
        raw = await request.body()
        if len(raw) > 5_000_000:
            raise Bad("Слишком большой запрос", 413)
        try:
            n = amo.hook(raw)
        except AmoError as ex:
            logging.getLogger("amocrm").warning("amoCRM вебхук отклонён: %s", ex)
            raise Bad(str(ex), 403) from ex
        return JSONResponse({"ok": True, "leads": n})

    @handler
    async def amo_meta_api(request: Request):
        admin_of(request)
        try:
            meta = await asyncio.get_running_loop().run_in_executor(None, amo.meta)
        except AmoError as ex:
            raise Bad(str(ex), 502) from ex
        return JSONResponse(meta)

    # ---- Ставтрэк: топливо по ДУТ
    def admin_of(request: Request) -> dict:
        u = staff(request)
        if u["role"] != "admin":
            raise Bad("Доступно только администратору", 403)
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
            for key, lo, hi in (("fuel_density", 0.6, 1.0), ("max_age_min", 1, 1440), ("fuel_in_tare_l", 0, 3000)):
                if key == "fuel_in_tare_l" and b.get(key) in (None, ""):
                    continue
                try:
                    val = float(str(b.get(key)).replace(",", "."))
                except ValueError as exc:
                    raise Bad("Укажите число") from exc
                if not lo <= val <= hi:
                    raise Bad(f"Значение должно быть от {lo} до {hi}")
                new[key] = val
            if "lpg_words" in b:
                new["lpg_words"] = text(b.get("lpg_words"), "Слова датчиков СУГ", 300, False)
            stv.save(new)
            if "fuel_in_tare_l" in new:            # пересчитать газ в рейсах с учтённым топливом
                for t in db.q("SELECT id FROM trips WHERE fuel1_l IS NOT NULL OR fuel2_l IS NOT NULL"):
                    db.recalc_gas(t["id"])
        s_ = stv.settings()
        return JSONResponse({k: v for k, v in s_.items() if k != "password"} | {"password_set": bool(s_["password"])})

    @handler
    async def stavtrack_check(request: Request):
        """Проверка связи: какие машины привязаны и что сейчас показывает ДУТ."""
        admin_of(request)
        loop = asyncio.get_running_loop()

        def work():
            devs = stv.devices()
            all_pos = stv.positions()
            rows = []
            for v in db.vehicles():
                f = stv.fuel(v["plate"])
                dev = stv.device_for(v["plate"])
                if v.get("kind") == "trailer" and not dev:
                    continue                 # бочки без своего трекера в проверке не показываем
                note = "" if v.get("kind") == "trailer" and "топливного" in f["note"] else f["note"]
                rows.append({"plate": v["plate"], "kind": v.get("kind") or "", "device": f.get("device") or "",
                             "device_id": dev["id"] if dev else None, "liters": f["liters"],
                             "age_s": f["age_s"], "note": note,
                             "readings": stv.readings(dev["id"], all_pos.get(dev["id"])) if dev else [],
                             "sensors": sorted(str(k) for k in (f.get("raw") or {}))[:30],
                             "linked": bool(db.q("SELECT 1 FROM st_links WHERE plate=?", (v["plate"],)))})
            return {"devices": devs, "vehicles": rows}
        try:
            return JSONResponse(await loop.run_in_executor(None, work))
        except StError as ex:
            raise Bad(str(ex), 502) from ex

    def plate_guess(name: str) -> str:
        clean = re.sub(r"[\s\-_]+", "", name).upper().translate(str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ"))
        found = re.search(r"[А-Я]\d{3}[А-Я]{2}\d{2,3}|[А-Я]{2}\d{6,7}", clean)
        return norm_plate(found.group(0))[0] if found else ""

    @handler
    async def stavtrack_import(request: Request):
        """Загрузка машин из Ставтрэка в справочник ТС."""
        admin_of(request)
        loop = asyncio.get_running_loop()
        try:
            devs = await loop.run_in_executor(None, stv.devices)
        except StError as ex:
            raise Bad(str(ex), 502) from ex
        if request.method == "GET":
            linked = {r["device_id"] for r in db.q("SELECT device_id FROM st_links")}
            auto = set()
            for v in db.vehicles():
                d = stv.device_for(v["plate"])
                if d:
                    auto.add(d["id"])
            return JSONResponse([{"device_id": d["id"], "name": d["name"], "plate": plate_guess(d["name"])}
                                 for d in devs if d["id"] not in linked and d["id"] not in auto])
        items = (await body(request)).get("items")
        if not isinstance(items, list) or not items:
            raise Bad("Отметьте хотя бы одну машину")
        known = {d["id"] for d in devs}
        added = 0
        for it in items:
            plate = norm_plate(str(it.get("plate") or ""))[0]
            if len(plate) < 6:
                raise Bad(f"Укажите госномер для «{it.get('name') or it.get('device_id')}»")
            if it.get("device_id") not in known:
                raise Bad("Устройство не найдено в Ставтрэке")
            if not db.vehicle(plate):        # существующую машину не трогаем, только привязываем
                db.save_vehicle({"plate": plate, "kind": it.get("kind") if it.get("kind") in ("tractor", "trailer") else "",
                                 "model": " ".join(str(it.get("name") or "").split())[:60]})
                added += 1
            db.x("INSERT INTO st_links(plate, device_id) VALUES(?,?) "
                 "ON CONFLICT(plate) DO UPDATE SET device_id=excluded.device_id", (plate, it["device_id"]))
        # машины, которые уже были в справочнике и совпали с устройством по госномеру, привязываем насовсем
        linked = len(items)
        for v in db.vehicles():
            if not db.q("SELECT 1 FROM st_links WHERE plate=?", (v["plate"],)):
                d = stv.device_for(v["plate"])
                if d:
                    db.x("INSERT INTO st_links(plate, device_id) VALUES(?,?)", (v["plate"], d["id"]))
                    linked += 1
        return JSONResponse({"ok": True, "added": added, "linked": linked})

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
                trailer = (last[0]["trailer"] or "") if last else ""
                trailer_sensors = []
                if trailer:                  # у бочки может быть свой трекер с датчиком уровня
                    tdev = stv.device_for(trailer)
                    if tdev and tdev["id"] != dev["id"]:
                        trailer_sensors = stv.readings(tdev["id"], all_pos.get(tdev["id"]))
                c = lcfg()
                road = legs(db, density(), c["tolerance_pct"], c["tolerance_l"], plate=v["plate"])
                road = road[0] if road and road[0]["status"] == "open" else None
                vehicles.append({
                    "plate": v["plate"], "device": dev["name"], "device_id": dev["id"],
                    "lat": pos["latitude"], "lon": pos["longitude"], "gps_valid": pos.get("valid"),
                    "sensors": stv.readings(dev["id"], pos), "trailer_sensors": trailer_sensors,
                    "speed": round(pos.get("speed") or 0), "course": pos.get("course"), "address": pos.get("address") or "",
                    "time": pos.get("deviceTime"), "age_s": f["age_s"],
                    "stale": f["age_s"] is None or f["age_s"] > 30 * 60, "fuel": f["liters"],
                    "driver": driver["name"] if driver else "", "trailer": trailer,
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
        rows = db.q("SELECT w.*, d.name AS driver, f.liters AS fuel, f.note AS fuel_note, f.lpg AS lpg, "
                    "t.w1_id, t.w2_id, t.gas_in, t.gas_out, t.fuel1_l, t.fuel2_l FROM weighings w "
                    "LEFT JOIN drivers d ON d.id=w.driver_id LEFT JOIN weigh_fuel f ON f.weighing_id=w.id "
                    "LEFT JOIN trips t ON t.id=w.trip_id "
                    "WHERE w.ts>=? AND w.ts<=? ORDER BY w.id DESC LIMIT 500", (start, end + "T23:59:59"))
        dens = density()

        def gas_of(w):
            """Газ в цистерне по весам при этом взвешивании (за вычетом тары и топлива по ДУТ)."""
            if w.get("w1_id") == w["id"]:
                kg, fuel_used = w.get("gas_in"), w.get("fuel1_l")
            elif w.get("w2_id") == w["id"]:
                kg, fuel_used = w.get("gas_out"), w.get("fuel2_l")
            else:
                return None
            if kg is None:
                return None
            return {"kg": kg, "l": round(kg / dens), "fuel_counted": fuel_used is not None}
        return JSONResponse([{
            "id": w["id"], "ts": w["ts"], "plate": w["plate"], "trailer": w.get("trailer") or "", "weight": w["weight"],
            "kind": w.get("kind") or "", "source": w.get("source") or "", "driver": w.get("driver") or "",
            "fuel": w.get("fuel"), "fuel_note": w.get("fuel_note") or "",
            "gas": gas_of(w), "lpg": json.loads(w["lpg"]) if w.get("lpg") else [],
            "photos": [f"/api/logist/photo/{x}?t={tok}" for x in (w["snapshots"] or "").split(";") if x],
        } for w in rows])

    # ---- карта OpenStreetMap через сервер: плитка скачивается один раз и раздаётся из кэша.
    # Браузеры пользователей к OpenStreetMap не ходят, поэтому их блокировки (старый Chrome, общий адрес) не мешают.
    tile_locks: dict[str, asyncio.Lock] = {}

    @handler
    async def map_tile(request: Request):
        staff(request, request.query_params.get("t", ""))
        try:
            z, x, y = (int(request.path_params[k]) for k in ("z", "x", "y"))
        except ValueError as exc:
            raise Bad("Не найдено", 404) from exc
        if not (0 <= z <= 19 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise Bad("Не найдено", 404)
        f = tile_dir / str(z) / str(x) / f"{y}.png"
        fresh = 14 * 86400                              # OpenStreetMap просит хранить плитки хотя бы неделю
        headers = {"Cache-Control": "private, max-age=604800"}
        if f.is_file() and time.time() - f.stat().st_mtime < fresh:
            return FileResponse(f, media_type="image/png", headers=headers)
        key = f"{z}/{x}/{y}"
        lock = tile_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not (f.is_file() and time.time() - f.stat().st_mtime < fresh):
                url = getattr(ctx, "tile_url", "https://tile.openstreetmap.org/{z}/{x}/{y}.png").format(z=z, x=x, y=y)
                host = request.headers.get("host", "")
                req = urllib.request.Request(url, headers={
                    "User-Agent": f"AvtoVes/{__version__} (+https://{host}/privacy)",
                    "Referer": f"https://{host}/"})

                def fetch():
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        return resp.read()
                try:
                    png = await asyncio.get_running_loop().run_in_executor(None, fetch)
                except (urllib.error.URLError, TimeoutError, OSError):
                    if f.is_file():                      # нет связи — отдаём старую плитку
                        return FileResponse(f, media_type="image/png", headers=headers)
                    raise Bad("Карта сейчас недоступна", 502)
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".tmp")
                tmp.write_bytes(png)
                tmp.replace(f)
        tile_locks.pop(key, None)
        return FileResponse(f, media_type="image/png", headers=headers)

    @handler
    async def logist_photo(request: Request):
        u = worker(request, request.query_params.get("t", ""))
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
        u = worker(request)
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
        Route("/privacy", privacy_page),
        Route("/api/logist/appinfo", appinfo_api, methods=["GET", "POST"]),
        Route("/api/logist/appinfo/firebase", firebase_key, methods=["POST", "DELETE"]),
        Route("/api/logist/appinfo/test-push", test_push, methods=["POST"]),
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
        Route("/api/logist/amo/leads", amo_leads),
        Route("/api/logist/amo/assign", amo_assign_api, methods=["POST"]),
        Route("/api/logist/amo/sync", amo_sync_api, methods=["POST"]),
        Route("/api/logist/amocrm", amo_settings_api, methods=["GET", "POST"]),
        Route("/api/logist/amocrm/meta", amo_meta_api),
        Route("/api/amocrm/hook/{secret}", amo_hook, methods=["POST"]),
        Route("/api/logist/weighings", weighings_api),
        Route("/api/logist/map", map_api),
        Route("/api/logist/stavtrack", stavtrack_api, methods=["GET", "POST"]),
        Route("/api/logist/stavtrack/check", stavtrack_check),
        Route("/api/logist/stavtrack/import", stavtrack_import, methods=["GET", "POST"]),
        Route("/api/logist/dashboard", dashboard),
        Route("/api/logist/tiles/{z}/{x}/{y}.png", map_tile),
        Route("/api/logist/stavtrack/link", stavtrack_link, methods=["POST"]),
        Route("/api/logist/photo/{path:path}", logist_photo),
        Route("/api/logist/invoices/{uid}/done", invoice_mark_as("done"), methods=["POST"]),
        Route("/api/logist/invoices/{uid}/undo", invoice_mark_as("undo"), methods=["POST"]),
        Route("/api/logist/legs/{tid:int}/done", leg_done, methods=["POST"]),
    ]


PRIVACY = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>АвтоВес Водитель — политика конфиденциальности</title>
<style>body{margin:0;background:#f4f6f8;color:#15202b;font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:28px 18px 60px}h1{font-size:26px;line-height:1.25}h2{font-size:18px;margin-top:28px}
li{margin:4px 0}</style></head><body><main>
<h1>Политика конфиденциальности приложения «АвтоВес Водитель»</h1>
<p>Приложение «АвтоВес Водитель» — рабочий инструмент водителей компании {company}. Оно работает только с сервером
этой компании; учётную запись водителю выдаёт логист, самостоятельная регистрация не предусмотрена.</p>
<h2>Какие данные обрабатываются</h2>
<ul>
<li>логин и имя водителя, которые завёл логист; пароль хранится на сервере только в зашифрованном (хешированном) виде;</li>
<li>госномера тягача и полуприцепа, результаты взвешиваний на весах компании и фото машины с камер весовой;</li>
<li>сведения о выгрузках: клиент, адрес, объём, время, отметка об оплате, комментарий водителя;</li>
<li>заказы, которые логист назначил водителю;</li>
<li>технический идентификатор устройства для уведомлений (токен Firebase Cloud Messaging).</li>
</ul>
<p>Приложение не запрашивает доступ к местоположению, контактам, камере, микрофону и файлам телефона.</p>
<h2>Зачем</h2>
<p>Для учёта рейсов, взвешиваний и выгрузок газа, расчёта сверки с весами, выставления счетов клиентам
и отправки водителю уведомлений о новых заказах.</p>
<h2>Где хранятся и кому передаются</h2>
<p>Данные передаются по защищённому соединению (HTTPS) и хранятся на сервере компании {company}.
Третьим лицам не продаются и не передаются, кроме сервиса Google Firebase Cloud Messaging, через который
доставляются уведомления (передаётся только текст уведомления и токен устройства).
При открытии маршрута приложение передаёт координаты точек доставки в приложение или на сайт Яндекс Карт.</p>
<h2>Сколько хранятся</h2>
<p>Записи о взвешиваниях и выгрузках хранятся, пока они нужны компании для учёта и отчётности.
После выхода из приложения данные входа на телефоне удаляются.</p>
<h2>Удаление данных и вопросы</h2>
<p>Чтобы удалить учётную запись и связанные с ней данные или задать вопрос, обратитесь: {contact}.</p>
</main></body></html>
"""
