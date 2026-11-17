"""Заявки из amoCRM в кабинете логиста: обмен, доска по дням и базам, назначение водителя, заказ в приложении.

amoCRM заменён имитацией API v4 со сделками как в таблице логиста; база, сервер и интерфейсы — настоящие.
"""
import io
import json
import time
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

from test_v2 import SHOTS, call, driver_token, env, wait_push  # noqa: F401

TOKEN = "dolgosrochnyi-token"
F_DAY, F_DISTRICT, F_HIGHWAY, F_BASE, F_LITERS, F_COND, F_DRIVER = 101, 102, 103, 104, 105, 106, 107


class FakeAmo:
    """Имитация amoCRM API v4: сделки, контакты, пользователи, поля, воронки."""

    def __init__(self, day: str):
        self.calls = []
        stamp = int(datetime.strptime(day, "%Y-%m-%d").replace(hour=12).timestamp())
        now = int(time.time())
        self.leads = {
            31865331: self.lead(31865331, stamp, "Новая Москва", "Калужское шоссе", "Онуфриево", "4000", "", 501, 11, now),
            31611863: self.lead(31611863, stamp, "Солнечногорский", "Ленинградское шоссе", "Онуфриево", "2000",
                                "Шланг 50м, желательна первая половина дня!", 502, 12, now),
            31603247: self.lead(31603247, stamp, "Раменский", "Рязанское шоссе", "", "3 500 л", "", 503, 11, now),
            31573605: self.lead(31573605, stamp, "Можайский", "Можайское шоссе", "Онуфриево", "5000", "", 504, 11, now,
                                status=143),
            31613683: self.lead(31613683, None, "Истринский", "Новорижское шоссе", "Онуфриево", "2800", "", 505, 11, now),
        }
        self.contacts = {
            501: ("[Д] Новая Москва  -  Виталий", ["+79671239000"]),
            502: ("[Д] Солнечногорский  -  Ирина", ["+79166814775", "89166814775"]),
            503: ("[Д] Раменский  -  Олег", ["+79260000003"]),
            504: ("[Д] Можайский  -  Денис", ["89264111586"]),
            505: ("[Д] Истринский  -  Ольга", ["89150007189"]),
        }

    @staticmethod
    def lead(lid, day_ts, district, highway, base, liters, cond, contact, user, updated, status=142000):
        values = [(F_DISTRICT, district), (F_HIGHWAY, highway), (F_BASE, base), (F_LITERS, liters), (F_COND, cond)]
        cfv = [{"field_id": fid, "field_name": "x", "values": [{"value": v}]} for fid, v in values if v]
        if day_ts:
            cfv.append({"field_id": F_DAY, "field_name": "Дата доставки", "field_type": "date", "values": [{"value": day_ts}]})
        return {"id": lid, "name": f"Сделка #{lid}", "status_id": status, "pipeline_id": 9, "responsible_user_id": user,
                "updated_at": updated, "custom_fields_values": cfv,
                "_embedded": {"contacts": [{"id": contact, "is_main": True}]}}

    def update(self, lid, **kw):
        lead = self.leads[lid]
        by = {cf["field_id"]: cf for cf in lead["custom_fields_values"]}
        for fid, v in kw.pop("fields", {}).items():
            if fid in by:
                by[fid]["values"] = [{"value": v}]
            else:
                lead["custom_fields_values"].append({"field_id": fid, "values": [{"value": v}]})
        lead.update(kw)
        lead["updated_at"] = int(time.time()) + 1

    def page(self, key, items, page, limit):
        chunk = items[(page - 1) * limit: page * limit]
        if not chunk:
            return None
        out = {"_embedded": {key: chunk}, "_links": {}}
        if page * limit < len(items):
            out["_links"]["next"] = {"href": "next"}
        return out

    def __call__(self, req, timeout=None):
        url = urllib.parse.urlparse(req.full_url)
        q = urllib.parse.parse_qs(url.query)
        self.calls.append(url.path)
        assert url.netloc == "rosavtonomg.amocrm.ru", url.netloc
        if req.get_header("Authorization") != "Bearer " + TOKEN:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
        page, limit = int(q.get("page", ["1"])[0]), int(q.get("limit", ["250"])[0])
        path = url.path.removeprefix("/api/v4/")
        if path == "leads/custom_fields":
            data = self.page("custom_fields", [
                {"id": F_DAY, "name": "Дата доставки", "type": "date"}, {"id": F_DISTRICT, "name": "Район", "type": "select"},
                {"id": F_HIGHWAY, "name": "Направление", "type": "select"}, {"id": F_BASE, "name": "База", "type": "select"},
                {"id": F_LITERS, "name": "Литры план", "type": "numeric"}, {"id": F_COND, "name": "Особые условия", "type": "textarea"},
                {"id": F_DRIVER, "name": "Водитель", "type": "text"}], page, limit)
        elif path == "leads/pipelines":
            data = self.page("pipelines", [{"id": 9, "name": "Доставка газа", "_embedded": {"statuses": [
                {"id": 142000, "name": "Заявка принята"}, {"id": 143, "name": "Закрыто и не реализовано"}]}}], page, limit)
        elif path == "users":
            data = self.page("users", [{"id": 11, "name": "Красиков Иван Геннадьевич"},
                                       {"id": 12, "name": "Захарченко Никита Алексеевич"}], page, limit)
        elif path == "contacts":
            ids = [int(x) for x in q.get("filter[id][]", [])]
            data = self.page("contacts", [{"id": i, "name": self.contacts[i][0], "custom_fields_values": [
                {"field_code": "PHONE", "values": [{"value": p} for p in self.contacts[i][1]]}]} for i in ids], page, limit)
        elif path == "leads":
            since = int(q.get("filter[updated_at][from]", ["0"])[0])
            items = sorted((x for x in self.leads.values() if x["updated_at"] >= since), key=lambda x: x["updated_at"])
            data = self.page("leads", json.loads(json.dumps(items)), page, limit)
        else:
            raise urllib.error.HTTPError(req.full_url, 404, "Not found", {}, io.BytesIO(b""))
        if data is None:
            raise urllib.error.HTTPError(req.full_url, 204, "No Content", {}, io.BytesIO(b""))
        return Resp(json.dumps(data).encode())


class Resp:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def board(public, logist, day):
    code, res = call(public, f"/api/logist/amo/leads?day={day}", token=logist)
    assert code == 200, res
    return res


def test_amocrm_requests_to_driver_app(env):
    ctx, local, public, logist = env
    day = datetime.now().strftime("%Y-%m-%d")
    fake = FakeAmo(day)
    ctx.amocrm.opener = fake

    # Подключение: неверный ключ не принимается, верный — поля сделки определяются по названиям
    assert call(public, "/api/logist/amocrm", "POST", {"domain": "https://rosavtonomg.amocrm.ru/leads/pipeline/9",
                                                         "token": "wrong", "enabled": True}, logist)[0] == 200
    code, res = call(public, "/api/logist/amocrm/meta", token=logist)
    assert code == 502 and "ключ" in res["error"]
    code, s = call(public, "/api/logist/amocrm", "POST", {"domain": "rosavtonomg", "token": TOKEN, "enabled": True}, logist)
    assert code == 200 and s["domain"] == "rosavtonomg.amocrm.ru" and s["token_set"] and "token" not in s
    assert s["fields"] == {"day": F_DAY, "district": F_DISTRICT, "highway": F_HIGHWAY, "base": F_BASE,
                           "liters": F_LITERS, "conditions": F_COND}
    assert s["ready"]
    assert call(public, "/api/logist/amocrm", "POST", {"domain": "evil.example.com", "enabled": True}, logist)[0] == 400

    # Обмен: закрытая и сделка без даты в заявки не попадают; база по району, если в сделке пусто
    code, res = call(public, "/api/logist/amo/sync", "POST", token=logist)
    assert code == 200, res
    b = board(public, logist, day)
    assert b["amo"]["ready"] and b["amo"]["last_error"] == ""
    rows = [(l["base"], l["district"], l["highway"], l["liters"], l["client"], l["phone"], l["manager"]) for l in b["leads"]]
    assert rows == [
        ("Мотяково", "Раменский", "Рязанское шоссе", 3500.0, "[Д] Раменский - Олег", "+79260000003", "Красиков Иван Геннадьевич"),
        ("Онуфриево", "Новая Москва", "Калужское шоссе", 4000.0, "[Д] Новая Москва - Виталий", "+79671239000",
         "Красиков Иван Геннадьевич"),
        ("Онуфриево", "Солнечногорский", "Ленинградское шоссе", 2000.0, "[Д] Солнечногорский - Ирина",
         "+79166814775, 89166814775", "Захарченко Никита Алексеевич"),
    ]
    assert b["leads"][0]["url"] == "https://rosavtonomg.amocrm.ru/leads/detail/31603247"
    assert [d["name"] for d in b["drivers"]] == ["Иванов Сергей", "Петров Пётр"]
    assert b["days"] == [{"day": day, "n": 3, "free": 3}]

    # Логист выбирает водителя: заказ уходит в приложение с телефоном и условиями
    token, other = driver_token(public), driver_token(public, "petrov")
    call(public, "/api/app/push-token", "POST", {"token": "phone-ivanov"}, token)
    call(public, "/api/app/push-token", "POST", {"token": "phone-petrov"}, other)
    code, res = call(public, "/api/logist/amo/assign", "POST", {"lead_id": 31611863, "driver_id": 1}, logist)
    assert code == 200, res
    sent = wait_push(ctx, 1)
    assert sent[-1] == (("phone-ivanov",), "Новый заказ",
                        "[Д] Солнечногорский - Ирина: 2000 л. Солнечногорский, Ленинградское шоссе")
    me = call(public, "/api/app/me", token=token)[1]
    (o,) = me["orders"]
    assert (o["client"], o["liters"], o["day"], o["phone"], bool(o["private"])) == (
        "[Д] Солнечногорский - Ирина", 2000.0, day, "+79166814775, 89166814775", True)
    assert o["note"] == ("Тел.: +79166814775, 89166814775. Шланг 50м, желательна первая половина дня! "
                         "Менеджер: Захарченко Никита Алексеевич")
    lead = [l for l in board(public, logist, day)["leads"] if l["lead_id"] == 31611863][0]
    assert (lead["order_status"], lead["driver"]) == ("new", "Иванов Сергей")

    # Передать другому водителю: первому — «Заказ снят», второму — новый заказ
    assert call(public, "/api/logist/amo/assign", "POST", {"lead_id": 31611863, "driver_id": 2}, logist)[0] == 200
    sent = wait_push(ctx, 3)
    assert sent[1][:2] == (("phone-ivanov",), "Заказ снят") and sent[2][:2] == (("phone-petrov",), "Новый заказ")
    assert call(public, "/api/app/me", token=token)[1]["orders"] == []
    assert len(call(public, "/api/app/me", token=other)[1]["orders"]) == 1
    clients = [c for c in call(public, "/api/logist/refs", token=logist)[1]["clients"] if c["name"].startswith("[Д]")]
    assert len(clients) == 1 and clients[0]["private"] == 1 and clients[0]["phone"] == "+79166814775, 89166814775"

    # Сделку поменяли в amoCRM: водитель видит новые литры; закрыли — заказ снимается
    fake.update(31611863, fields={F_LITERS: "2500"})
    assert call(public, "/api/logist/amo/sync", "POST", token=logist)[0] == 200
    assert call(public, "/api/app/me", token=other)[1]["orders"][0]["liters"] == 2500.0
    assert wait_push(ctx, 4)[3][1] == "Заказ изменён"
    fake.update(31611863, status_id=143)
    assert call(public, "/api/logist/amo/sync", "POST", token=logist)[0] == 200
    assert call(public, "/api/app/me", token=other)[1]["orders"] == []
    assert wait_push(ctx, 5)[4] == (("phone-petrov",), "Заказ отменён", "[Д] Солнечногорский - Ирина: сделка закрыта в amoCRM")
    assert [l["lead_id"] for l in board(public, logist, day)["leads"]] == [31603247, 31865331]

    # Снять водителя с заявки; заявку без литров отправить нельзя
    assert call(public, "/api/logist/amo/assign", "POST", {"lead_id": 31865331, "driver_id": 1}, logist)[0] == 200
    assert len(call(public, "/api/app/me", token=token)[1]["orders"]) == 1
    assert call(public, "/api/logist/amo/assign", "POST", {"lead_id": 31865331, "driver_id": None}, logist)[0] == 200
    assert call(public, "/api/app/me", token=token)[1]["orders"] == []
    fake.update(31603247, fields={F_LITERS: ""})
    call(public, "/api/logist/amo/sync", "POST", token=logist)
    code, res = call(public, "/api/logist/amo/assign", "POST", {"lead_id": 31603247, "driver_id": 1}, logist)
    assert code == 400 and "литры" in res["error"]

    # Водитель выполнил заявку — на доске «Выполнена» с фактом
    assert call(public, "/api/logist/amo/assign", "POST", {"lead_id": 31865331, "driver_id": 1}, logist)[0] == 200
    oid = call(public, "/api/app/me", token=token)[1]["orders"][0]["id"]
    op = {"op_id": "a1", "type": "unload", "id": "amo-1", "plate": "Т100АА26", "trailer": "АВ123477", "liters": 3980,
          "order_id": oid, "paid": True, "at": datetime.now().astimezone().isoformat()}
    assert call(public, "/api/app/sync", "POST", {"ops": [op]}, token)[1]["results"][0]["ok"]
    lead = [l for l in board(public, logist, day)["leads"] if l["lead_id"] == 31865331][0]
    assert (lead["order_status"], lead["fact"], lead["paid"], lead["plate"]) == ("done", 3980.0, "yes", "Т100АА26")
    code, res = call(public, "/api/logist/amo/assign", "POST", {"lead_id": 31865331, "driver_id": 2}, logist)
    assert code == 400 and "выполнена" in res["error"]

    # Доступ: настройки — только администратору, доска — только логисту
    assert call(public, "/api/logist/logists", "POST", {"name": "Анна", "login": "anna", "password": "parol-menedzhera",
                                                          "role": "manager"}, logist)[0] == 200
    anna = call(public, "/api/logist/login", "POST", {"login": "anna", "password": "parol-menedzhera"})[1]["token"]
    for path in ("/api/logist/amo/leads", "/api/logist/amocrm"):
        assert call(public, path, token=anna)[0] == 403
    assert call(public, "/api/logist/amo/leads")[0] == 401


def test_amocrm_requests_in_browser(env):
    from playwright.sync_api import expect, sync_playwright
    ctx, local, public, logist = env
    day = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    fake = FakeAmo(day)
    fake.leads[31613683] = FakeAmo.lead(31613683, int(datetime.strptime(tomorrow, "%Y-%m-%d").replace(hour=12).timestamp()),
                                        "Истринский", "Новорижское шоссе", "Онуфриево", "2800", "", 505, 11, int(time.time()))
    ctx.amocrm.opener = fake
    SHOTS.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(public + "/logist")
        page.fill("#l-login", "olga")
        page.fill("#l-pass", "parol-logista")
        page.click("#l-enter")
        expect(page.locator("main h1")).to_have_text("Сводка")

        # Заявки до подключения — подсказка, где подключить
        page.click("nav button[data-tab='requests']")
        expect(page.locator("#req-off")).to_contain_text("Подключения → amoCRM")

        # Подключение amoCRM администратором
        page.click("nav button[data-tab='amocrm']")
        page.click("#amo-setup")
        page.fill("dialog input[name='domain']", "rosavtonomg.amocrm.ru")
        page.fill("dialog input[name='token']", TOKEN)
        page.select_option("dialog select[name='enabled']", "1")
        page.click("dialog button[type='submit']")
        expect(page.locator("#amo-info")).to_contain_text("заявки забираются каждые 5 мин")
        page.click("#amo-check")
        expect(page.locator("#amo-ok")).to_contain_text("полей сделки 7, воронок 1")
        expect(page.locator("#amo-info")).to_contain_text("Поле «Литры план»Литры план")
        page.screenshot(path=SHOTS / "amocrm-settings.png", full_page=True)

        # Доска заявок: по базам, назначение водителя из списка
        page.click("nav button[data-tab='requests']")
        page.click("#req-sync")
        expect(page.locator("#req-ok")).to_contain_text("Обновлено из amoCRM")
        expect(page.locator(".req-base h2")).to_have_text(["Мотяково", "Онуфриево"])
        expect(page.locator("#req-total")).to_have_text("9\xa0500")
        expect(page.locator(".req-days button")).to_have_count(2)
        row = page.locator("tr[data-lead='31865331']")
        expect(row).to_contain_text("Водитель не назначен")
        row.locator("select").select_option(label="Иванов Сергей")
        expect(page.locator("tr[data-lead='31865331']")).to_contain_text("У водителя в приложении")
        expect(page.locator("tr[data-lead='31865331'] select")).to_have_value("1")
        expect(page.locator("tr[data-lead='31865331']")).not_to_have_class("free")
        page.locator("tr[data-lead='31611863'] select").select_option(label="Петров Пётр")
        expect(page.locator("tr[data-lead='31611863']")).to_contain_text("У водителя в приложении")
        page.screenshot(path=SHOTS / "amocrm-requests.png", full_page=True)

        # Завтрашний день — одна заявка
        page.click(".req-days button >> nth=1")
        expect(page.locator("#requests tbody tr")).to_have_count(1)
        expect(page.locator("#requests")).to_contain_text("Истринский")
        browser.close()
    assert errors == []
    orders = ctx.db.q("SELECT driver_id, liters, amo_lead_id FROM orders ORDER BY id")
    assert [(o["driver_id"], o["liters"], o["amo_lead_id"]) for o in orders] == [(1, 4000.0, 31865331), (2, 2000.0, 31611863)]
