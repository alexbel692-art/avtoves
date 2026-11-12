"""АвтоВес 2: приложение водителя, кабинет логиста, сверка с весами, внешний порт.

Весы и светофоры заменены заглушкой; база, серверный код и интерфейсы — настоящие.
Запуск: python -m pytest tests
"""
import asyncio
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import DB  # noqa: E402
from app.web import build_app, build_public_app  # noqa: E402
from app.weighing import DriverError  # noqa: E402

APP_HTML = ROOT / "android/app/src/main/assets/app.html"
SHOTS = Path(__file__).resolve().parent / "shots"
PASSWORD = "parol-voditelya"


class FakeScale:
    def __init__(self):
        self.weight = 0.0

    def snapshot(self):
        return {"connected": True, "weight": self.weight, "stable": True, "indicator_stable": True,
                "overload": False, "raw": []}


class FakeCtrl:
    """Вместо контроллера весовой: записывает взвешивание так же, как настоящий fix_by_driver."""

    def __init__(self, db, scale):
        self.db, self.scale, self.events = db, scale, []

    def event(self, text):
        self.events.append(text)

    def snapshot(self):
        return {"state": "idle", "state_text": "Весы свободны", "warning": "", "plate": None, "last": None,
                "driver_mode": True}

    def fix_by_driver(self, driver, plate, direction, wait_sec=25, trailer=""):
        if self.scale.weight < 400:
            raise DriverError("на весах нет машины")
        res = self.db.add_weighing(plate, None, self.scale.weight, None, False, "", 24, kind=direction,
                                   source="driver", driver=driver, trailer=trailer)
        return {**res, "plate": plate, "trailer": trailer, "weight": self.scale.weight, "snapshots": [],
                "ts": datetime.now().isoformat(timespec="seconds")}


class Ctx:
    sim = None
    cameras: list = []
    config_path = ""

    def __init__(self, tmp_path):
        (tmp_path / "snapshots").mkdir()
        self.cfg = {"data_dir": str(tmp_path), "server": {"port": 0, "api_token": "", "operator_password": "x$y$z"},
                    "gas": {"enabled": True, "density": 0.54}, "weighing": {"driver_mode": True},
                    "logist": {"tolerance_pct": 2, "tolerance_l": 100}}
        self.db = DB(tmp_path / "t.db")
        self.scale = FakeScale()
        self.ctrl = FakeCtrl(self.db, self.scale)


def serve(app):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    return server, f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"


def call(base, path, method="GET", body=None, token=None, raw=False):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            return resp.status, (data if raw else json.loads(data))
    except urllib.error.HTTPError as err:
        data = err.read()
        try:
            return err.code, json.loads(data)
        except ValueError:
            return err.code, data


@pytest.fixture
def env(tmp_path):
    ctx = Ctx(tmp_path)
    local_srv, local = serve(build_app(ctx))
    public_srv, public = serve(build_public_app(ctx))
    db = ctx.db
    db.save_vehicle({"plate": "Т100АА26", "kind": "tractor", "tare": 8000})
    db.save_vehicle({"plate": "АВ123477", "kind": "trailer", "tare": 7000, "tank_l": 45000})
    db.save_vehicle({"plate": "ВЕ567877", "kind": "trailer", "tare": 7500, "tank_l": 28000})
    code, res = call(local, "/api/logist/setup", "POST", {"login": "Olga", "password": "parol-logista", "name": "Ольга"})
    assert code == 200
    logist = res["token"]
    for name, login in (("Иванов Сергей", "ivanov"), ("Петров Пётр", "petrov")):
        assert call(public, "/api/logist/drivers", "POST", {"name": name, "login": login, "password": PASSWORD}, logist)[0] == 200
    call(public, "/api/logist/clients", "POST", {"name": "АГЗС Север", "legal": True, "legal_name": "ООО «Север»", "manager": "Анна"}, logist)
    call(public, "/api/logist/clients", "POST", {"name": "Свой склад", "legal": False}, logist)
    yield ctx, local, public, logist
    local_srv.should_exit = public_srv.should_exit = True


def driver_token(public, login="ivanov"):
    code, res = call(public, "/api/app/login", "POST", {"login": login, "password": PASSWORD})
    assert code == 200, res
    return res["token"]


def weigh(ctx, public, token, direction, weight, moment, trailer="АВ123477"):
    ctx.scale.weight = weight
    code, res = call(public, "/api/app/weigh", "POST",
                     {"plate": "Т100АА26", "trailer": trailer, "direction": direction}, token)
    assert code == 200, res
    # Взвешивания в тесте идут подряд; расставляем их по времени, как в жизни
    ts = moment.isoformat(timespec="seconds")
    trip = ctx.db.q("SELECT * FROM trips ORDER BY updated DESC, id DESC LIMIT 1")[0]
    column = "w2_ts" if trip["w2_ts"] else "w1_ts"
    ctx.db.x(f"UPDATE trips SET {column}=? WHERE id=?", (ts, trip["id"]))
    return res


def unload(public, token, uid, liters, moment, client=None, **extra):
    op = {"op_id": "o" + uid, "type": "unload", "id": uid, "plate": "Т100АА26", "trailer": "АВ123477",
          "liters": liters, "at": moment.astimezone().isoformat(), **(client or {"client_id": 1}), **extra}
    code, res = call(public, "/api/app/sync", "POST", {"ops": [op]}, token)
    assert code == 200
    return res["results"][0]


def test_public_port_exposes_only_driver_app_and_logist(env):
    ctx, local, public, logist = env
    # Светофоры, настройки, журнал и рабочее место оператора наружу не выходят вообще
    for path, method in (("/api/light", "POST"), ("/api/barrier", "POST"), ("/api/manual", "POST"), ("/api/fix", "POST"),
                         ("/api/settings", "GET"), ("/api/settings", "POST"), ("/api/state", "GET"), ("/api/trips", "GET"),
                         ("/api/vehicles", "GET"), ("/api/drivers", "GET"), ("/api/driver/login", "POST"),
                         ("/api/1c/trips", "GET"), ("/api/sim/drive", "POST"), ("/driver", "GET"),
                         ("/snapshots/x.jpg", "GET"), ("/login", "GET")):
        code, _ = call(public, path, method, {} if method == "POST" else None)
        assert code in (404, 405), (path, code)
    # Первого логиста нельзя создать из интернета
    assert call(public, "/api/logist/status")[1]["setup"] is False
    assert call(public, "/api/logist/setup", "POST", {"login": "x", "password": "12345678"})[0] == 403
    # Всё остальное требует входа
    for path in ("/api/app/me", "/api/logist/me", "/api/logist/report", "/api/logist/notices", "/api/logist/refs"):
        assert call(public, path)[0] == 401
    # В локальной сети рабочее место оператора по-прежнему закрыто паролем оператора
    assert call(local, "/api/state")[0] == 401
    assert call(local, "/api/light", "POST", {"side": "a", "state": "green"})[0] == 401


def test_logins_lockout_and_password_change(env):
    ctx, local, public, logist = env
    token = driver_token(public)
    assert call(public, "/api/app/me", token=token)[1]["driver"]["name"] == "Иванов Сергей"
    # Токен водителя не открывает кабинет логиста и наоборот
    assert call(public, "/api/logist/me", token=token)[0] == 401
    assert call(public, "/api/app/me", token=logist)[0] == 401
    for _ in range(5):
        assert call(public, "/api/app/login", "POST", {"login": "petrov", "password": "wrong"})[0] == 401
    assert call(public, "/api/app/login", "POST", {"login": "petrov", "password": PASSWORD})[0] == 429
    # Смена пароля и отключение водителя отзывают вход
    call(public, "/api/logist/drivers", "POST", {"id": 1, "name": "Иванов Сергей", "login": "ivanov", "password": "novyi-parol"}, logist)
    assert call(public, "/api/app/me", token=token)[0] == 401
    assert call(public, "/api/app/login", "POST", {"login": "ivanov", "password": "novyi-parol"})[0] == 200
    call(public, "/api/logist/drivers", "POST", {"id": 1, "name": "Иванов Сергей", "login": "ivanov", "active": False}, logist)
    assert call(public, "/api/app/login", "POST", {"login": "ivanov", "password": "novyi-parol"})[0] == 401
    # Короткий пароль не принимается; пароли в ответах не отдаются
    assert call(public, "/api/logist/drivers", "POST", {"name": "Новый", "login": "n", "password": "123"}, logist)[0] == 400
    assert "pass_hash" not in json.dumps(call(public, "/api/logist/refs", token=logist)[1])


def test_weighing_unloads_and_reconciliation(env):
    ctx, local, public, logist = env
    token = driver_token(public)
    t0 = datetime.now() - timedelta(hours=10)
    # Приехал пустой (15 000 кг = тара сцепки), залился, уехал с 13 500 кг газа = 25 000 л
    weigh(ctx, public, token, "arrival", 15000, t0)
    res = weigh(ctx, public, token, "departure", 28500, t0 + timedelta(hours=1))
    assert res["gas"]["liters"] == 25000
    me = call(public, "/api/app/me", token=token)[1]
    assert me["plate"] == "Т100АА26" and me["trailer"] == "АВ123477"
    assert me["road"]["left_l"] == 25000 and me["tanks"]["АВ123477"] == 45000

    assert unload(public, token, "u1", "15000,5", t0 + timedelta(hours=2))["ok"]
    assert unload(public, token, "u1", "15000,5", t0 + timedelta(hours=2))["ok"]       # повтор не задваивает
    assert unload(public, token, "u2", 9799.5, t0 + timedelta(hours=3), {"client_name": "ИП Смирнов"})["ok"]
    assert not unload(public, token, "u3", -5, t0 + timedelta(hours=3))["ok"]
    assert not unload(public, driver_token(public, "petrov"), "u1", 5, t0 + timedelta(hours=3))["ok"]
    assert call(public, "/api/app/me", token=token)[1]["road"]["left_l"] == 200

    # Вернулся с 54 кг газа (100 л): по весам отвёз 24 900 л, водитель записал 24 800 — в допуске
    weigh(ctx, public, token, "arrival", 15054, t0 + timedelta(hours=5))
    assert call(public, "/api/app/me", token=token)[1]["road"] is None
    day = t0.strftime("%Y-%m-%d")
    q = f"?from={day}&to={datetime.now():%Y-%m-%d}"
    [leg] = call(public, "/api/logist/legs" + q, token=logist)[1]
    assert (leg["out_l"], leg["in_l"], leg["scale_l"], leg["driver_l"], leg["diff_l"], leg["status"]) == \
        (25000, 100, 24900, 24800.0, 100, "ok")

    # Вторая поездка: по весам отвёз 24 900 л, а записал только 20 000 — расхождение
    weigh(ctx, public, token, "departure", 28554, t0 + timedelta(hours=6))
    unload(public, token, "u4", 20000, t0 + timedelta(hours=7))
    weigh(ctx, public, token, "arrival", 15108, t0 + timedelta(hours=8))
    legs = call(public, "/api/logist/legs" + q, token=logist)[1]
    assert [g["status"] for g in legs] == ["bad", "ok"] and legs[0]["diff_l"] == 4900

    notices = call(public, "/api/logist/notices", token=logist)[1]
    assert [g["diff_l"] for g in notices["legs"]] == [4900]
    assert [n["name"] for n in notices["new_clients"]] == ["ИП Смирнов"]
    assert {i["id"]: i["client"] for i in notices["invoices"]} == {"u1": "ООО «Север»", "u4": "ООО «Север»"}
    assert notices["count"] == 4 and call(public, "/api/logist/me", token=logist)[1]["notices"] == 4

    # Счёт: отметить, повтор отклоняется, вернуть
    assert call(public, "/api/logist/invoices/u1/done", "POST", token=logist)[0] == 200
    assert call(public, "/api/logist/invoices/u1/done", "POST", token=logist)[0] == 409
    done = call(public, "/api/logist/notices?invoices=done", token=logist)[1]["invoices"]
    assert done[0]["id"] == "u1" and done[0]["invoiced_by"] == "Ольга"
    # Новый клиент оказался юрлицом: счёт появляется и по уже записанной выгрузке
    call(public, "/api/logist/clients", "POST", {"id": 3, "name": "ИП Смирнов", "legal": True, "manager": "Пётр"}, logist)
    notices = call(public, "/api/logist/notices", token=logist)[1]
    assert {i["id"] for i in notices["invoices"]} == {"u2", "u4"} and notices["new_clients"] == []
    # Расхождение разобрано
    call(public, f"/api/logist/legs/{legs[0]['id']}/done", "POST", token=logist)
    assert call(public, "/api/logist/notices", token=logist)[1]["legs"] == []

    # Отчёт и Excel
    rep = call(public, "/api/logist/report" + q, token=logist)[1]
    assert rep["total"] == 44800.0 and rep["count"] == 3
    assert {r["name"]: r["liters"] for r in rep["by_client"]} == {"АГЗС Север": 35000.5, "ИП Смирнов": 9799.5}
    from openpyxl import load_workbook
    code, data = call(public, "/api/logist/report.xlsx" + q, token=logist, raw=True)
    book = load_workbook(io.BytesIO(data))
    assert book["Итого"]["B2"].value == 44800 and book["Сверка с весами"]["L2"].value == "расхождение"

    # Логист исправил выгрузку: расхождение уходит; водитель не может удалить выгрузку со счётом
    assert call(public, "/api/logist/unloads/u4", "PATCH", {"liters": 24850}, logist)[0] == 200
    assert call(public, "/api/logist/legs" + q, token=logist)[1][0]["status"] == "ok"
    res = call(public, "/api/app/sync", "POST", {"ops": [{"op_id": "d", "type": "delete", "unload_id": "u1"}]}, token)[1]
    assert not res["results"][0]["ok"] and "счёт" in res["results"][0]["error"]


def test_weigh_errors_and_other_trailer(env):
    ctx, local, public, logist = env
    token = driver_token(public)
    ctx.scale.weight = 0
    code, res = call(public, "/api/app/weigh", "POST", {"plate": "Т100АА26", "trailer": "АВ123477", "direction": "arrival"}, token)
    assert code == 409 and "нет машины" in res["error"]
    assert call(public, "/api/app/weigh", "POST", {"plate": "Т100АА26", "direction": "sideways"}, token)[0] == 400
    assert call(public, "/api/app/photo/../../t.db?t=" + token)[0] in (400, 404)
    assert call(public, "/api/app/photo/2026-01-01/x.jpg?t=bad")[0] == 401
    # Вернулся с другим прицепом: сравнивать нельзя, расхождением это не считается
    t0 = datetime.now() - timedelta(hours=5)
    weigh(ctx, public, token, "arrival", 15000, t0)
    weigh(ctx, public, token, "departure", 28500, t0 + timedelta(hours=1))
    weigh(ctx, public, token, "arrival", 15500, t0 + timedelta(hours=3), trailer="ВЕ567877")
    legs = call(public, f"/api/logist/legs?from={t0:%Y-%m-%d}&to={datetime.now():%Y-%m-%d}", token=logist)[1]
    assert legs[-1]["status"] == "unknown"
    assert call(public, "/api/logist/notices", token=logist)[1]["legs"] == []


def test_driver_app_and_logist_cabinet_in_browser(env):
    from playwright.sync_api import expect, sync_playwright
    ctx, local, public, logist = env
    SHOTS.mkdir(exist_ok=True)
    nb = " "
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        phone = browser.new_context(viewport={"width": 390, "height": 844})
        page = phone.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(APP_HTML.as_uri())
        page.fill("#server", public)
        page.fill("#login", "ivanov")
        page.fill("#password", "wrong-password")
        page.click("#enter")
        expect(page.locator(".error")).to_contain_text("Неверный логин")
        page.fill("#password", PASSWORD)
        page.click("#enter")
        expect(page.locator("#no-vehicle")).to_be_visible()

        # Выбор тягача и прицепа, затем взвешивание через интернет
        page.click("#go-weigh")
        page.click("button.pick:has-text('Т100АА26')")
        page.click("button.pick:has-text('АВ123477')")
        page.click("#back")
        ctx.scale.weight = 15000
        page.click("#go-weigh")
        expect(page.locator("#live-weight")).to_have_text(f"15{nb}000 кг")
        page.screenshot(path=SHOTS / "weigh.png")
        page.click("#arrival")
        expect(page.locator("#fixed-weight")).to_have_text(f"15{nb}000 кг")
        page.click("#back")
        ctx.scale.weight = 28500
        page.click("#go-weigh")
        page.click("#departure")
        expect(page.locator(".info")).to_contain_text(f"25{nb}000 л")
        page.click("#back")
        expect(page.locator("#left")).to_contain_text(f"25{nb}000 л")

        # Выгрузка; без связи остаётся в телефоне и считается в остатке
        page.click("#new-unload")
        page.select_option("#client", label="АГЗС Север")
        page.fill("#liters", "15000,5")
        page.click("#save-unload")
        expect(page.locator("#total")).to_have_text(f"15{nb}000,5")
        expect(page.locator("#state")).to_have_text("Все записи отправлены")
        expect(page.locator("#left")).to_contain_text(f"10{nb}000 л")
        phone.route("**/api/**", lambda route: route.abort())
        page.click("#new-unload")
        page.select_option("#client", "__new")
        page.fill("#client-new", "ИП Смирнов")
        page.click("#all-left")
        page.click("#save-unload")
        expect(page.locator("#state")).to_contain_text("Не отправлено записей: 1")
        expect(page.locator("#left")).to_contain_text("0 л")
        page.screenshot(path=SHOTS / "app.png", full_page=True)
        page.reload()
        expect(page.locator("#total")).to_have_text(f"25{nb}000,5")
        phone.unroute("**/api/**")
        page.reload()
        expect(page.locator("#state")).to_have_text("Все записи отправлены")
        assert len(ctx.db.q("SELECT 1 FROM unloads")) == 2
        assert errors == []

        # Кабинет логиста на внешнем порту
        desk = browser.new_page(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        desk.on("pageerror", lambda e: errors.append(str(e)))
        desk.goto(public + "/logist")
        desk.fill("#l-login", "olga")
        desk.fill("#l-pass", "parol-logista")
        desk.click("#l-enter")
        expect(desk.locator("nav [data-tab=notices]")).to_have_text("Уведомления (2)")
        expect(desk.locator("#new-clients")).to_contain_text("ИП Смирнов")
        expect(desk.locator("#invoices tbody tr")).to_have_count(1)
        desk.screenshot(path=SHOTS / "notices.png")
        desk.click("#new-clients button:has-text('ИП Смирнов')")
        desk.select_option("dialog select[name=legal]", "1")
        desk.fill("dialog input[name=manager]", "Пётр")
        desk.click("dialog button[type=submit]")
        expect(desk.locator("#invoices tbody tr")).to_have_count(2)
        desk.locator("#invoices tr:has-text('ООО «Север»') button").click()
        expect(desk.locator("#invoices tbody tr")).to_have_count(1)
        desk.click("nav [data-tab=report]")
        expect(desk.locator("#r-total")).to_have_text(f"25{nb}000,5")
        with desk.expect_download() as dl:
            desk.click("#xlsx")
        assert dl.value.suggested_filename.endswith(".xlsx")
        desk.click("nav [data-tab=legs]")
        expect(desk.locator("#legs")).to_contain_text("В рейсе")
        desk.screenshot(path=SHOTS / "legs.png")
        desk.click("nav [data-tab=drivers]")
        expect(desk.locator("#drivers tbody tr")).to_have_count(2)
        desk.click("nav [data-tab=logists]")
        expect(desk.locator("#logists")).to_contain_text("Администратор")
        small = browser.new_page(viewport={"width": 390, "height": 800})
        small.goto(public + "/logist")
        assert small.evaluate("document.documentElement.scrollWidth") <= 390
        assert errors == []
        browser.close()
