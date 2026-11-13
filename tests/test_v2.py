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
from datetime import datetime, timedelta, timezone
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


class FakePusher:
    """Вместо Firebase: запоминает, что и кому отправлено."""

    def __init__(self):
        self.sent = []

    def ready(self):
        return True

    def send(self, tokens, title, body):
        self.sent.append((tuple(tokens), title, body))
        return []


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
        self.pusher = FakePusher()


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

        # Логист присылает заказ частному лицу: он появляется в приложении, водитель отмечает оплату
        call(public, "/api/logist/clients", "POST", {"name": "Петров И. И.", "kind": "private", "address": "с. Южное, ул. Мира, 5"}, logist)
        call(public, "/api/logist/orders", "POST", {"client_id": 4, "liters": 500, "driver_id": 1, "day": datetime.now().strftime("%Y-%m-%d")}, logist)
        page.evaluate("window.dispatchEvent(new Event('online'))")
        expect(page.locator("#orders")).to_contain_text("с. Южное, ул. Мира, 5")
        expect(page.locator(".toast")).to_contain_text("Новый заказ")
        page.screenshot(path=SHOTS / "orders.png", full_page=True)
        page.click("#orders .order-done")
        assert page.input_value("#liters") == "500"
        page.click("#save-unload")
        expect(page.locator("#unload-error")).to_contain_text("оплатил ли клиент")
        page.click("#paid-no")
        page.click("#save-unload")
        page.click("#dialog-yes")       # больше остатка по весам: подтверждаем
        expect(page.locator("#orders")).to_have_count(0)
        expect(page.locator("#state")).to_have_text("Все записи отправлены")
        assert ctx.db.q("SELECT paid FROM unloads WHERE order_id IS NOT NULL")[0]["paid"] == "no"
        assert errors == []

        # Кабинет логиста на внешнем порту
        desk = browser.new_page(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        desk.on("pageerror", lambda e: errors.append(str(e)))
        desk.goto(public + "/logist")
        desk.fill("#l-login", "olga")
        desk.fill("#l-pass", "parol-logista")
        desk.click("#l-enter")
        # Сводка: показатели и диаграммы
        expect(desk.locator("main h1")).to_have_text("Сводка")
        expect(desk.locator("#tiles")).to_contain_text("Ждут счёта")
        expect(desk.locator("#chart-days path.bar")).to_have_count(14)
        expect(desk.locator("#chart-clients .fill")).to_have_count(3)
        expect(desk.locator("#chart-drivers .name")).to_have_text(["Иванов Сергей"])
        desk.locator("#chart-days .hit").last.hover()
        expect(desk.locator(".tip")).to_contain_text("выгрузок: 3")
        desk.screenshot(path=SHOTS / "home.png")
        desk.click("#tiles button:has-text('Ждут счёта')")       # плитка ведёт в нужный раздел
        expect(desk.locator("main h1")).to_have_text("Уведомления")
        expect(desk.locator("nav [data-tab=notices]")).to_have_text("Уведомления (3)")
        expect(desk.locator("#unpaid")).to_contain_text("Петров И. И.")
        expect(desk.locator("#new-clients")).to_contain_text("ИП Смирнов")
        expect(desk.locator("#invoices tbody tr")).to_have_count(1)
        desk.screenshot(path=SHOTS / "notices.png")
        desk.click("#new-clients button:has-text('ИП Смирнов')")
        desk.select_option("dialog select[name=kind]", "legal")
        desk.click("dialog button[type=submit]")
        expect(desk.locator("#invoices tbody tr")).to_have_count(2)
        desk.locator("#invoices tr:has-text('ООО «Север»') button").click()
        expect(desk.locator("#invoices tbody tr")).to_have_count(1)
        desk.click("nav [data-tab=orders]")
        expect(desk.locator("#orders")).to_contain_text("не оплачено")
        desk.click("#add-order")
        desk.select_option("dialog select[name=client_id]", label="АГЗС Север")
        desk.fill("dialog input[name=liters]", "7000")
        desk.select_option("dialog select[name=driver_id]", label="Петров Пётр")
        desk.click("dialog button[type=submit]")
        expect(desk.locator("#orders tbody tr")).to_have_count(2)
        desk.screenshot(path=SHOTS / "orders-desk.png")
        desk.click("nav [data-tab=weighings]")
        expect(desk.locator("#weighings tbody tr")).to_have_count(2)
        desk.click("nav [data-tab=report]")
        expect(desk.locator("#r-total")).to_have_text(f"25{nb}500,5")
        with desk.expect_download() as dl:
            desk.click("#xlsx")
        assert dl.value.suggested_filename.endswith(".xlsx")
        desk.click("nav [data-tab=legs]")
        expect(desk.locator("#legs")).to_contain_text("В рейсе")
        desk.screenshot(path=SHOTS / "legs.png")
        desk.click("nav [data-tab=drivers]")
        expect(desk.locator("#drivers tbody tr")).to_have_count(2)
        desk.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
        desk.click("nav [data-tab=map]")
        expect(desk.locator("#fleet")).to_contain_text("Ставтрэк не подключён")
        desk.click("nav [data-tab=routing]")
        expect(desk.locator("#routing-state")).to_contain_text("Не задан")
        desk.click("nav [data-tab=orders]")
        desk.click("#calc-route")
        desk.click("dialog button[type=submit]")
        expect(desk.locator("dialog .error")).to_contain_text("Нет координат адреса")
        desk.click("dialog button:has-text('Отмена')")
        desk.click("nav [data-tab=stavtrack]")
        expect(desk.locator("#st-state")).to_have_text("Выключено")
        desk.click("#st-check")
        expect(desk.locator("#st-error")).to_contain_text("логин и пароль")
        desk.click("#st-setup")
        expect(desk.locator("dialog input[name=url]")).to_have_value("https://wiafleet.online/api")
        desk.click("dialog button:has-text('Отмена')")
        desk.click("nav [data-tab=legs]")
        expect(desk.locator("#legs thead")).to_contain_text("Топливо, л")
        desk.click("nav [data-tab=logists]")
        expect(desk.locator("#logists")).to_contain_text("Логист-администратор")
        small = browser.new_page(viewport={"width": 390, "height": 800})
        small.goto(public + "/logist")
        assert small.evaluate("document.documentElement.scrollWidth") <= 390
        assert errors == []
        browser.close()


def wait_push(ctx, n):
    for _ in range(100):
        if len(ctx.pusher.sent) >= n:
            return ctx.pusher.sent
        time.sleep(0.02)
    raise AssertionError(ctx.pusher.sent)


def test_orders_payment_and_manager_cabinet(env):
    ctx, local, public, logist = env
    token = driver_token(public)
    other = driver_token(public, "petrov")
    day = datetime.now().strftime("%Y-%m-%d")
    # Менеджеры и клиенты: частное лицо у Анны, юрлицо у Бориса
    for name, login in (("Анна", "anna"), ("Борис", "boris")):
        assert call(public, "/api/logist/logists", "POST", {"name": name, "login": login, "password": "parol-menedzhera", "role": "manager"}, logist)[0] == 200
    managers = {m["login"]: m["id"] for m in call(public, "/api/logist/refs", token=logist)[1]["managers"]}
    call(public, "/api/logist/clients", "POST", {"name": "Петров И. И.", "kind": "private", "address": "с. Южное, ул. Мира, 5", "manager_id": managers["anna"]}, logist)
    call(public, "/api/logist/clients", "POST", {"id": 1, "name": "АГЗС Север", "kind": "legal", "legal_name": "ООО «Север»", "manager_id": managers["boris"]}, logist)
    private_id = [c["id"] for c in call(public, "/api/logist/refs", token=logist)[1]["clients"] if c["name"] == "Петров И. И."][0]

    # Заказ водителю: уведомление уходит на его телефон, адрес берётся у клиента
    call(public, "/api/app/push-token", "POST", {"token": "phone-ivanov"}, token)
    code, res = call(public, "/api/logist/orders", "POST", {"client_id": private_id, "liters": 500, "driver_id": 1, "day": day, "note": "Позвонить заранее"}, logist)
    assert code == 200
    order_id = res["id"]
    call(public, "/api/logist/orders", "POST", {"client_id": 1, "liters": 12000, "driver_id": 1, "day": day}, logist)
    sent = wait_push(ctx, 2)
    assert sent[0] == (("phone-ivanov",), "Новый заказ", "Петров И. И.: 500 л. с. Южное, ул. Мира, 5")
    me = call(public, "/api/app/me", token=token)[1]
    assert [(o["client"], o["liters"], o["address"], bool(o["private"])) for o in me["orders"]] == [
        ("Петров И. И.", 500.0, "с. Южное, ул. Мира, 5", True), ("АГЗС Север", 12000.0, "", False)]
    assert call(public, "/api/app/me", token=other)[1]["orders"] == []

    def sync(tok, op):
        return call(public, "/api/app/sync", "POST", {"ops": [{"op_id": "x", **op}]}, tok)[1]["results"][0]

    base = {"type": "unload", "plate": "Т100АА26", "trailer": "АВ123477", "liters": 480}
    # Чужой заказ выполнить нельзя; у частного лица нужно отметить оплату
    assert not sync(other, {**base, "id": "z1", "order_id": order_id, "paid": True})["ok"]
    res = sync(token, {**base, "id": "p1", "order_id": order_id})
    assert not res["ok"] and "оплатил" in res["error"]
    assert sync(token, {**base, "id": "p1", "order_id": order_id, "paid": False})["ok"]
    assert sync(token, {**base, "id": "p1", "order_id": order_id, "paid": False})["ok"]
    # Второй заказ не получился
    second = me["orders"][1]["id"]
    assert sync(token, {"type": "order_fail", "order_id": second, "reason": "Закрыто"})["ok"]
    assert call(public, "/api/app/me", token=token)[1]["orders"] == []
    orders = call(public, f"/api/logist/orders?from={day}&to={day}", token=logist)[1]
    assert [(o["status"], o["fact"], o["paid"], o["reason"]) for o in orders] == [("done", 480.0, "no", ""), ("failed", None, None, "Закрыто")]

    # Логист видит неоплату и невыполненный заказ; заказ можно отправить заново, выполненный менять нельзя
    notices = call(public, "/api/logist/notices", token=logist)[1]
    assert [u["id"] for u in notices["unpaid"]] == ["p1"] and notices["unpaid"][0]["manager"] == "Анна"
    assert [o["reason"] for o in notices["failed_orders"]] == ["Закрыто"]
    assert call(public, "/api/logist/orders", "POST", {"id": order_id, "client_id": private_id, "liters": 1, "driver_id": 1, "day": day}, logist)[0] == 400
    assert call(public, f"/api/logist/orders/{order_id}/cancel", "POST", token=logist)[0] == 400
    assert call(public, "/api/logist/orders", "POST", {"id": second, "client_id": 1, "liters": 12000, "driver_id": 2, "day": day}, logist)[0] == 200
    assert len(call(public, "/api/app/me", token=other)[1]["orders"]) == 1
    assert call(public, f"/api/logist/orders/{second}/cancel", "POST", token=logist)[0] == 200
    assert call(public, "/api/app/me", token=other)[1]["orders"] == []

    # Кабинет менеджера: только свои клиенты, только оплаты и счета
    anna = call(public, "/api/logist/login", "POST", {"login": "anna", "password": "parol-menedzhera"})[1]["token"]
    boris = call(public, "/api/logist/login", "POST", {"login": "boris", "password": "parol-menedzhera"})[1]["token"]
    sync(token, {**base, "id": "l1", "client_id": 1, "liters": 9000})
    me_a = call(public, "/api/logist/me", token=anna)[1]
    assert me_a["role"] == "manager" and me_a["notices"] == 1
    n_a = call(public, "/api/logist/notices", token=anna)[1]
    assert [u["id"] for u in n_a["unpaid"]] == ["p1"] and n_a["invoices"] == [] and n_a["legs"] == []
    n_b = call(public, "/api/logist/notices", token=boris)[1]
    assert n_b["unpaid"] == [] and [i["id"] for i in n_b["invoices"]] == ["l1"]
    assert {u["id"] for u in call(public, "/api/logist/report", token=anna)[1]["unloads"]} == {"p1"}
    assert [c["name"] for c in call(public, "/api/logist/refs", token=anna)[1]["clients"]] == ["Петров И. И."]
    for path, method, body in (("/api/logist/orders", "GET", None), ("/api/logist/legs", "GET", None),
                               ("/api/logist/weighings", "GET", None), ("/api/logist/unloads/p1", "DELETE", None),
                               ("/api/logist/clients", "POST", {"name": "x"}), ("/api/logist/drivers", "POST", {"name": "x", "login": "x", "password": "12345678"}),
                               ("/api/logist/logists", "POST", {"login": "hack", "password": "12345678"})):
        assert call(public, path, method, body, anna)[0] == 403, path
    assert len(call(public, "/api/logist/logists", token=anna)[1]) == 1
    # Чужую оплату и чужой счёт отметить нельзя, свои можно
    assert call(public, "/api/logist/unloads/p1/paid", "POST", token=boris)[0] == 403
    assert call(public, "/api/logist/invoices/l1/done", "POST", token=anna)[0] == 403
    assert call(public, "/api/logist/unloads/p1/paid", "POST", token=anna)[0] == 200
    assert call(public, "/api/logist/unloads/p1/paid", "POST", token=anna)[0] == 409
    assert call(public, "/api/logist/invoices/l1/done", "POST", token=boris)[0] == 200
    assert call(public, "/api/logist/me", token=anna)[1]["notices"] == 0
    row = ctx.db.q("SELECT paid, paid_by FROM unloads WHERE id='p1'")[0]
    assert (row["paid"], row["paid_by"]) == ("yes", "Анна")


def test_weighing_photos_in_cabinet(env):
    ctx, local, public, logist = env
    token = driver_token(public)
    day = datetime.now().strftime("%Y-%m-%d")
    folder = Path(ctx.cfg["data_dir"]) / "snapshots" / day
    folder.mkdir()
    for name in ("120000_cam1.jpg", "120000_cam2.jpg"):
        (folder / name).write_bytes(b"\xff\xd8jpeg")
    weigh(ctx, public, token, "arrival", 15000, datetime.now())
    ctx.db.x("UPDATE weighings SET snapshots=?", (f"{day}/120000_cam1.jpg;{day}/120000_cam2.jpg",))
    [w] = call(public, f"/api/logist/weighings?from={day}&to={day}", token=logist)[1]
    assert w["kind"] == "arrival" and w["driver"] == "Иванов Сергей" and len(w["photos"]) == 2
    code, data = call(public, w["photos"][1], raw=True)
    assert code == 200 and data == b"\xff\xd8jpeg"
    assert call(public, w["photos"][0].split("?")[0] + "?t=" + token)[0] == 401
    assert call(public, f"/api/logist/photo/{day}/../../t.db?t={logist}")[0] in (400, 404)


class FakeWiaFleet:
    """Вместо Ставтрэка: те же адреса и формат ответов, что в его описании API."""

    def __init__(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route
        self.fuel = 400.0
        self.age_min = 0
        self.expire_once = False
        self.logins = 0

        def authed(request):
            if self.expire_once:
                self.expire_once = False
                return False
            return request.headers.get("authorization") == f"Bearer tok{self.logins}"

        async def token(request):
            b = await request.json()
            if b != {"email": "ves@example.ru", "password": "st-parol"}:
                return Response("bad", 401)
            self.logins += 1
            return JSONResponse({"token": {"token": f"tok{self.logins}", "refreshToken": "r", "exp": "2030-01-01T00:00:00Z"}})

        async def devices(request):
            if not authed(request):
                return Response("", 401)
            return JSONResponse([{"id": 11, "name": "Вольво T100AA26"}, {"id": 12, "name": "Легковая"}])

        async def sensors(request):
            if not authed(request):
                return Response("", 401)
            if request.path_params["d"] != 11:
                return JSONResponse([])
            return JSONResponse([{"id": 5, "deviceId": 11, "name": "ДУТ бак", "type": "fuel", "param": "fuel1"},
                                 {"id": 6, "deviceId": 11, "name": "Зажигание", "type": "ignition", "param": "ign"}])

        async def positions(request):
            if not authed(request):
                return Response("", 401)
            when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=self.age_min)
            mine = {"deviceId": 11, "deviceTime": when.isoformat(timespec="seconds") + "Z", "latitude": 45.04,
                    "longitude": 41.97, "speed": 62.4, "address": "Ставрополь, ул. Ленина",
                    "sensorsAttributes": {"ДУТ бак": self.fuel, "Зажигание": True}}
            other = {"deviceId": 12, "deviceTime": "2026-01-01T00:00:00Z", "latitude": 45.3, "longitude": 42.5,
                     "sensorsAttributes": {}}
            if "deviceIds" not in request.query_params:
                return JSONResponse([mine, other])
            if request.query_params["deviceIds"] != "11":
                return JSONResponse([other])
            when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=self.age_min)
            return JSONResponse([{"deviceId": 11, "deviceTime": when.isoformat(timespec="seconds") + "Z",
                                  "latitude": 45.0, "longitude": 42.0,
                                  "sensorsAttributes": {"ДУТ бак": self.fuel, "Зажигание": True}}])

        app = Starlette(routes=[Route("/api/session/token", token, methods=["POST"]), Route("/api/devices", devices),
                                Route("/api/devices/{d:int}/sensors", sensors), Route("/api/positions", positions)])
        self.server, base = serve(app)
        self.url = base + "/api"


def wait_fuel(ctx, n):
    for _ in range(150):
        rows = ctx.db.q("SELECT * FROM weigh_fuel ORDER BY weighing_id")
        if len(rows) >= n:
            return rows
        time.sleep(0.02)
    raise AssertionError(rows)


def test_fuel_sensor_from_stavtrack_corrects_reconciliation(env):
    ctx, local, public, logist = env
    fake = FakeWiaFleet()
    token = driver_token(public)
    cfg = {"enabled": True, "url": fake.url, "email": "ves@example.ru", "password": "wrong", "fuel_density": "0,84", "max_age_min": 15}
    # Неверный пароль: понятная ошибка; пароль обратно не отдаётся; менеджеру и водителю настройки закрыты
    res = call(public, "/api/logist/stavtrack", "POST", cfg, logist)[1]
    assert res["password_set"] is True and "password" not in res
    code, res = call(public, "/api/logist/stavtrack/check", token=logist)
    assert code == 502 and "логин или пароль" in res["error"]
    assert call(public, "/api/logist/stavtrack", token=token)[0] == 401
    call(public, "/api/logist/stavtrack", "POST", {**cfg, "password": "st-parol"}, logist)
    check = call(public, "/api/logist/stavtrack/check", token=logist)[1]
    assert "vehicles" in check, check
    assert check["vehicles"] == [{"plate": "Т100АА26", "device": "Вольво T100AA26", "liters": 400.0, "age_s": check["vehicles"][0]["age_s"],
                                  "note": "", "sensors": ["ДУТ бак", "Зажигание"], "linked": False}]

    t0 = datetime.now() - timedelta(hours=6)
    weigh(ctx, public, token, "arrival", 15000, t0)
    fake.expire_once = True                      # токен истёк: программа входит заново сама
    weigh(ctx, public, token, "departure", 28500, t0 + timedelta(hours=1))      # 25 000 л газа, в баке 400 л
    assert [r["liters"] for r in wait_fuel(ctx, 2)] == [400.0, 400.0]
    unload(public, token, "f1", 24000, t0 + timedelta(hours=2), {"client_id": 2})
    # За рейс сожгли 100 л солярки (84 кг): без поправки весы приписали бы это газу
    fake.fuel = 300.0
    weigh(ctx, public, token, "arrival", 15456 - 84, t0 + timedelta(hours=4))
    wait_fuel(ctx, 3)
    q = f"?from={t0:%Y-%m-%d}&to={datetime.now():%Y-%m-%d}"
    [leg] = call(public, "/api/logist/legs" + q, token=logist)[1]
    assert (leg["fuel_out"], leg["fuel_in"], leg["fuel_fix_l"]) == (400.0, 300.0, 156)
    assert (leg["out_l"], leg["in_l"], leg["scale_l"], leg["diff_l"], leg["status"]) == (25000, 689, 24155, 155, "ok")
    ws = call(public, "/api/logist/weighings" + q, token=logist)[1]
    assert [w["fuel"] for w in ws] == [300.0, 400.0, 400.0]

    # Карта: машина, её водитель, прицеп, заказы и топливо
    call(public, "/api/logist/orders", "POST", {"client_id": 1, "liters": 5000, "driver_id": 1, "day": f"{datetime.now():%Y-%m-%d}"}, logist)
    m = call(public, "/api/logist/map", token=logist)[1]
    assert m["error"] is None and m["unlinked"] == []
    [car] = m["vehicles"]
    assert (car["plate"], car["lat"], car["lon"], car["speed"], car["fuel"], car["stale"]) == ("Т100АА26", 45.04, 41.97, 62, 300.0, False)
    assert (car["driver"], car["trailer"], car["today_l"], car["address"]) == ("Иванов Сергей", "АВ123477", 24000.0, "Ставрополь, ул. Ленина")
    assert [(o["client"], o["liters"]) for o in car["orders"]] == [("АГЗС Север", 5000.0)]
    assert call(public, "/api/logist/map", token=token)[0] == 401

    # Сводка для диаграмм; загрузка машин из Ставтрэка в справочник
    d = call(public, "/api/logist/dashboard", token=logist)[1]
    assert len(d["days"]) == 14 and d["today"]["liters"] == 24000.0 and d["period_count"] == 1
    assert d["clients"][0]["liters"] == 24000.0 and d["drivers"] == [{"name": "Иванов Сергей", "liters": 24000.0, "count": 1}]
    assert d["orders"] == {"new": 1, "done": 0, "failed": 0}
    assert call(public, "/api/logist/dashboard", token=token)[0] == 401
    assert call(public, "/api/logist/stavtrack/import", token=token)[0] == 401
    assert call(public, "/api/logist/stavtrack/import", token=logist)[1] == [{"device_id": 12, "name": "Легковая", "plate": ""}]
    code, res = call(public, "/api/logist/stavtrack/import", "POST", {"items": [{"device_id": 12, "name": "Легковая", "plate": ""}]}, logist)
    assert code == 400 and "госномер" in res["error"]
    res = call(public, "/api/logist/stavtrack/import", "POST", {"items": [{"device_id": 12, "name": "Легковая", "plate": "в222вв26", "kind": ""}]}, logist)[1]
    assert res == {"ok": True, "added": 1, "linked": 1}
    car2 = ctx.db.vehicle("В222ВВ26")
    assert (car2["model"], car2["kind"]) == ("Легковая", "") and ctx.stavtrack.device_for("В222ВВ26")["id"] == 12
    assert call(public, "/api/logist/stavtrack/import", token=logist)[1] == []
    ctx.db.x("DELETE FROM st_links WHERE plate='В222ВВ26'")
    ctx.db.x("DELETE FROM vehicles WHERE plate='В222ВВ26'")

    # Устаревшие данные трекера в расчёт не идут, но видны с пометкой
    fake.age_min = 60
    weigh(ctx, public, token, "departure", 28500, t0 + timedelta(hours=5))
    last = wait_fuel(ctx, 4)[-1]
    assert last["liters"] is None and "устарели" in last["note"] and "300" in last["note"]
    # Ручная привязка к другому устройству: у него нет ДУТ
    call(public, "/api/logist/stavtrack/link", "POST", {"plate": "Т100АА26", "device_id": 12}, logist)
    v = call(public, "/api/logist/stavtrack/check", token=logist)[1]["vehicles"][0]
    assert v["device"] == "Легковая" and v["liters"] is None and v["linked"] is True
    # Ставтрэк недоступен: взвешивание всё равно проходит
    fake.server.should_exit = True
    time.sleep(0.3)
    ctx.stavtrack.save({})
    weigh(ctx, public, token, "arrival", 15500, t0 + timedelta(hours=5, minutes=30))
    assert "Ставтрэк" in wait_fuel(ctx, 5)[-1]["note"]


def test_map_in_browser(env):
    from playwright.sync_api import expect, sync_playwright
    ctx, local, public, logist = env
    fake = FakeWiaFleet()
    token = driver_token(public)
    call(public, "/api/logist/stavtrack", "POST", {"enabled": True, "url": fake.url, "email": "ves@example.ru", "password": "st-parol", "fuel_density": 0.84, "max_age_min": 15}, logist)
    weigh(ctx, public, token, "arrival", 15000, datetime.now() - timedelta(hours=2))
    call(public, "/api/logist/orders", "POST", {"client_id": 1, "liters": 5000, "driver_id": 1, "day": f"{datetime.now():%Y-%m-%d}", "address": "с. Южное"}, logist)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
        page.goto(public + "/logist")
        page.fill("#l-login", "olga")
        page.fill("#l-pass", "parol-logista")
        page.click("#l-enter")
        page.click("nav [data-tab=map]")
        expect(page.locator(".car-mark")).to_have_count(1)
        page.click("#fleet button:has-text('Т100АА26')")
        popup = page.locator(".leaflet-popup-content")
        expect(popup).to_contain_text("Иванов Сергей")
        expect(popup).to_contain_text("АГЗС Север")
        expect(popup).to_contain_text("топливо: 400 л")
        SHOTS.mkdir(exist_ok=True)
        page.screenshot(path=SHOTS / "map.png")
        page.click("nav [data-tab=report]")       # уход с карты не оставляет ошибок
        page.click("nav [data-tab=map]")
        expect(page.locator(".car-mark")).to_have_count(1)

        # Загрузка машин из Ставтрэка: окно со списком устройств
        page.click("nav [data-tab=stavtrack]")
        page.click("#st-import")
        expect(page.locator("#st-import-table tbody tr")).to_have_count(1)
        page.fill("#st-import-table input[type=text]", "В222ВВ26")
        page.check("#st-import-table input[type=checkbox]")
        page.screenshot(path=SHOTS / "import.png")
        page.click("dialog button[type=submit]")
        expect(page.locator("#st-info")).to_contain_text("Загружено машин: 1")
        assert ctx.db.vehicle("В222ВВ26")["model"] == "Легковая"

        # Яндекс Карты вместо OpenStreetMap: ключ задан — карта рисуется через их программу
        res = call(public, "/api/logist/routing", "POST", {"map_provider": "yandex", "map_key": "map-key", "mode": "truck", "has_trailer": True}, logist)[1]
        assert res["map_provider"] == "yandex" and res["map_key_set"] is True and "map_key" not in res
        assert call(public, "/api/logist/me", token=logist)[1]["map"] == {"provider": "yandex", "key": "map-key"}
        asked = []

        def ymaps_stub(route):
            asked.append(route.request.url)
            route.fulfill(content_type="application/javascript", body=YMAPS_STUB)
        page.route("https://api-maps.yandex.ru/**", ymaps_stub)
        page.reload()
        page.click("nav [data-tab=map]")
        expect(page.locator("#map-state")).to_contain_text("Яндекс Карты")
        expect(page.locator("#map .ym-mark")).to_have_count(2)       # две машины
        assert asked == ["https://api-maps.yandex.ru/2.1/?apikey=map-key&lang=ru_RU"]
        page.click("#fleet button:has-text('Т100АА26')")
        expect(page.locator("#map .ym-open")).to_contain_text("Иванов Сергей")
        page.click("nav [data-tab=report]")
        page.click("nav [data-tab=map]")
        expect(page.locator("#map .ym-mark")).to_have_count(2)
        assert errors == []
        browser.close()


# Заглушка программы Яндекс Карт (API 2.1): только то, чем пользуется кабинет
YMAPS_STUB = """
window.ymaps = {
  ready(fn) { if (fn) fn(); return Promise.resolve(); },
  Map: function (id) {
    const box = document.getElementById(id);
    const self = this;
    this.zoom = 7;
    this.geoObjects = {
      add(pm) { const n = document.createElement('div'); n.className = 'ym-mark'; n.textContent = pm.props.iconContent || pm.props.hintContent; box.append(n); pm.node = n; pm.box = box; },
      removeAll() { box.replaceChildren(); },
    };
    this.getZoom = () => self.zoom;
    this.setCenter = (c, z) => { self.zoom = z; };
    this.setBounds = () => {};
    this.destroy = () => box.replaceChildren();
  },
  Placemark: function (point, props, options) {
    const pm = this;
    this.props = props;
    this.opened = false;
    this.balloon = {
      isOpen: () => pm.opened,
      open() { pm.opened = true; const n = document.createElement('div'); n.className = 'ym-open'; n.innerHTML = props.balloonContent; pm.box.append(n); },
    };
  },
};
"""


class FakeYandex:
    """Вместо Яндекса: геокодер и матрица расстояний в формате из их документации."""
    PLACES = {  # адрес → (широта, долгота, точность)
        "база": (45.00, 42.00, "exact"), "с. Южное, ул. Мира, 5": (45.00, 42.30, "exact"),
        "г. Северск, АГЗС": (45.00, 42.10, "street"), "х. Дальний": (45.00, 42.20, "other")}

    def __init__(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        self.matrix_calls = []

        async def geocode(request):
            q = request.query_params
            if q.get("apikey") != "geo-key":
                return JSONResponse({"statusCode": 403}, 403)
            assert q["lang"] == "ru_RU" and q["format"] == "json"
            place = self.PLACES.get(q["geocode"])
            if not place:
                return JSONResponse({"response": {"GeoObjectCollection": {"featureMember": []}}})
            return JSONResponse({"response": {"GeoObjectCollection": {"featureMember": [{"GeoObject": {
                "Point": {"pos": f"{place[1]} {place[0]}"},
                "metaDataProperty": {"GeocoderMetaData": {"precision": place[2], "text": "Россия, " + q["geocode"]}}}}]}}})

        async def matrix(request):
            q = request.query_params
            if q.get("apikey") != "route-key":
                return JSONResponse({"errors": ["Key not found"]}, 401)
            origins = [tuple(map(float, p.split(","))) for p in q["origins"].split("|")]
            dests = [tuple(map(float, p.split(","))) for p in q["destinations"].split("|")]
            assert len(origins) * len(dests) <= 100
            self.matrix_calls.append((len(origins), len(dests), q["mode"], q.get("has_trailer")))
            # все точки на одной дороге: время пропорционально разнице долготы, 1 км = 1 минута
            rows = [{"elements": [{"status": "OK", "distance": {"value": round(abs(a[1] - b[1]) * 100000)},
                                   "duration": {"value": round(abs(a[1] - b[1]) * 6000)}} for b in dests]} for a in origins]
            return JSONResponse({"rows": rows})

        self.server, self.url = serve(Starlette(routes=[Route("/v1", geocode), Route("/v2/distancematrix", matrix)]))


def test_addresses_and_route_from_yandex(env):
    ctx, local, public, logist = env
    fake = FakeYandex()
    token = driver_token(public)
    day = f"{datetime.now():%Y-%m-%d}"
    ctx.routing.save({"geocoder_url": fake.url + "/v1", "router_url": fake.url + "/v2/distancematrix"})

    def client(body):
        code, res = call(public, "/api/logist/clients", "POST", body, logist)
        assert code == 200, res
        return next(c for c in call(public, "/api/logist/refs", token=logist)[1]["clients"] if c["id"] == res["id"])

    # Без ключа клиент сохраняется, но без координат
    c = client({"name": "Южное", "kind": "own", "address": "с. Южное, ул. Мира, 5"})
    assert c["lat"] is None and c["geo_precision"] == "error" and "ключ" in c["geo_text"]
    # Ключи и база: ключи обратно не отдаются; неверный адрес базы — понятная ошибка
    code, res = call(public, "/api/logist/routing", "POST", {"geocoder_key": "geo-key", "router_key": "route-key", "base_address": "нет такого", "mode": "truck", "has_trailer": True}, logist)
    assert code == 400 and "Адрес не найден" in res["error"]
    res = call(public, "/api/logist/routing", "POST", {"base_address": "база", "mode": "truck", "has_trailer": True}, logist)[1]
    assert (res["base_lat"], res["base_lon"], res["base_precision"]) == (45.0, 42.0, "exact")
    assert res["geocoder_key_set"] and res["router_key_set"] and "geo-key" not in json.dumps(res) and "route-key" not in json.dumps(res)
    assert call(public, "/api/logist/routing", token=token)[0] == 401

    # Адреса: точный, неточный, ручные координаты, ненайденный
    far = client({"id": c["id"], "name": "Южное", "kind": "own", "address": "с. Южное, ул. Мира, 5"})
    assert (far["lat"], far["lon"], far["geo_precision"]) == (45.0, 42.3, "exact")
    near = client({"name": "Северск", "kind": "own", "address": "г. Северск, АГЗС"})
    assert near["geo_precision"] == "street" and near["lon"] == 42.1
    mid = client({"name": "Дальний", "kind": "own", "address": "х. Дальний", "coords": "45.0, 42.2"})
    assert (mid["lat"], mid["lon"], mid["geo_precision"]) == (45.0, 42.2, "manual")
    lost = client({"name": "Потерянный", "kind": "own", "address": "д. Несуществующая"})
    assert lost["lat"] is None and "не найден" in lost["geo_text"]
    assert call(public, "/api/logist/clients", "POST", {"name": "Кривой", "kind": "own", "coords": "сорок пять"}, logist)[0] == 400

    # Заказы в неудобном порядке: дальний, ближний, средний
    for cid, liters in ((far["id"], 100), (near["id"], 200), (mid["id"], 300)):
        call(public, "/api/logist/orders", "POST", {"client_id": cid, "liters": liters, "driver_id": 1, "day": day}, logist)
    call(public, "/api/app/push-token", "POST", {"token": "phone"}, token)
    before = len(ctx.pusher.sent)
    code, res = call(public, "/api/logist/orders/route", "POST", {"driver_id": 1, "day": day}, logist)
    assert code == 200, res
    assert res["order"] in (["Северск", "Дальний", "Южное"], ["Южное", "Дальний", "Северск"])
    assert (res["km"], res["minutes"], res["saved_minutes"]) == (60.0, 60, 20)
    assert fake.matrix_calls[-1] == (4, 4, "truck", "true")
    mine = call(public, "/api/app/me", token=token)[1]["orders"]
    assert [o["client"] for o in mine] == res["order"] and mine[0]["lat"] == 45.0
    assert "Маршрут изменён" in [p[1] for p in wait_push(ctx, before + 1)]

    # В приложении водителя: весь маршрут одной кнопкой в Яндекс Картах, точки в рассчитанном порядке
    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.add_init_script("window.Android = { routes: [], notify() {}, requestPush() {}, openRoute(p) { this.routes.push(p); } };")
        page.goto(APP_HTML.as_uri())
        page.fill("#server", public)
        page.fill("#login", "ivanov")
        page.fill("#password", PASSWORD)
        page.click("#enter")
        expect(page.locator("#route-all")).to_contain_text("3 точ.")
        page.click("#route-all")
        page.locator("#orders article .nav").first.click()
        coords = {o["client"]: f'{o["lat"]:g},{o["lon"]:g}' for o in mine}
        assert page.evaluate("window.Android.routes") == ["~".join(coords[n] for n in res["order"]), coords[res["order"][0]]]
        browser.close()

    # Заказ с адресом без координат: расчёт объясняет, что исправить
    call(public, "/api/logist/orders", "POST", {"client_id": lost["id"], "liters": 50, "driver_id": 1, "day": day}, logist)
    code, res = call(public, "/api/logist/orders/route", "POST", {"driver_id": 1, "day": day}, logist)
    assert code == 400 and "Потерянный" in res["error"]
    # Свой адрес заказа ищется отдельно от адреса клиента
    oid = call(public, f"/api/logist/orders?from={day}&to={day}", token=logist)[1][-1]["id"]
    call(public, "/api/logist/orders", "POST", {"id": oid, "client_id": lost["id"], "liters": 50, "driver_id": 1, "day": day, "address": "г. Северск, АГЗС"}, logist)
    assert call(public, "/api/logist/orders/route", "POST", {"driver_id": 1, "day": day}, logist)[0] == 200
    # Больше точек, чем помещается в один запрос Яндекса (100 ячеек): матрица собирается частями
    for i in range(8):
        call(public, "/api/logist/orders", "POST", {"client_id": mid["id"], "liters": 10 + i, "driver_id": 1, "day": day}, logist)
    fake.matrix_calls.clear()
    assert call(public, "/api/logist/orders/route", "POST", {"driver_id": 1, "day": day}, logist)[0] == 200
    assert [c[:2] for c in fake.matrix_calls] == [(7, 13), (6, 13)]
    # Неверный ключ: понятная ошибка без самого ключа
    ctx.routing.save({"router_key": "bad-key"})
    code, res = call(public, "/api/logist/orders/route", "POST", {"driver_id": 1, "day": day}, logist)
    assert code == 502 and "ключ API не принят" in res["error"] and "bad-key" not in res["error"]
    # Карта показывает клиентов с координатами и базу
    m = call(public, "/api/logist/map", token=logist)[1]
    assert m["base"] == {"lat": 45.0, "lon": 42.0} and {c["name"] for c in m["clients"]} == {"Южное", "Северск", "Дальний"}
