"""Заявки из amoCRM в кабинете логиста: обмен, доска по дням и базам, назначение водителя, заказ в приложении.

amoCRM заменён имитацией API v4 со сделками как в таблице логиста; база, сервер и интерфейсы — настоящие.
"""
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from test_v2 import SHOTS, admin_password, call, driver_token, env, wait_push  # noqa: F401

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
            501: ("[Д] Новая Москва  -  Виталий", ["+79004445566"]),
            502: ("[Д] Солнечногорский  -  Ирина", ["+79001112233", "89001112233"]),
            503: ("[Д] Раменский  -  Олег", ["+79260000003"]),
            504: ("[Д] Можайский  -  Денис", ["89002223344"]),
            505: ("[Д] Истринский  -  Ольга", ["89007778899"]),
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
            ids = {int(x) for x in q.get("filter[id][]", [])}
            items = sorted((x for x in self.leads.values() if x["updated_at"] >= since and (not ids or x["id"] in ids)),
                           key=lambda x: x["updated_at"])
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
                                                         "token": "wrong", "enabled": True}, ctx.admin)[0] == 200
    code, res = call(public, "/api/logist/amocrm/meta", token=ctx.admin)
    assert code == 502 and "ключ" in res["error"]
    code, s = call(public, "/api/logist/amocrm", "POST", {"domain": "rosavtonomg", "token": TOKEN, "enabled": True}, ctx.admin)
    assert code == 200 and s["domain"] == "rosavtonomg.amocrm.ru" and s["token_set"] and "token" not in s
    assert s["fields"] == {"day": F_DAY, "district": F_DISTRICT, "highway": F_HIGHWAY, "base": F_BASE,
                           "liters": F_LITERS, "conditions": F_COND}
    assert s["ready"]
    assert call(public, "/api/logist/amocrm", "POST", {"domain": "evil.example.com", "enabled": True}, ctx.admin)[0] == 400
    assert call(public, "/api/logist/amocrm", "POST", {"domain": "rosavtonomg", "enabled": False}, logist)[0] == 403

    # Обмен: закрытая и сделка без даты в заявки не попадают; база по району, если в сделке пусто
    code, res = call(public, "/api/logist/amo/sync", "POST", token=logist)
    assert code == 200, res
    b = board(public, logist, day)
    assert b["amo"]["ready"] and b["amo"]["last_error"] == ""
    rows = [(l["base"], l["district"], l["highway"], l["liters"], l["client"], l["phone"], l["manager"]) for l in b["leads"]]
    assert rows == [
        ("Мотяково", "Раменский", "Рязанское шоссе", 3500.0, "[Д] Раменский - Олег", "+79260000003", "Красиков Иван Геннадьевич"),
        ("Онуфриево", "Новая Москва", "Калужское шоссе", 4000.0, "[Д] Новая Москва - Виталий", "+79004445566",
         "Красиков Иван Геннадьевич"),
        ("Онуфриево", "Солнечногорский", "Ленинградское шоссе", 2000.0, "[Д] Солнечногорский - Ирина",
         "+79001112233, 89001112233", "Захарченко Никита Алексеевич"),
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
        "[Д] Солнечногорский - Ирина", 2000.0, day, "+79001112233, 89001112233", True)
    assert o["note"] == ("Тел.: +79001112233, 89001112233. Шланг 50м, желательна первая половина дня! "
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
    assert len(clients) == 1 and clients[0]["private"] == 1 and clients[0]["phone"] == "+79001112233, 89001112233"

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
    assert (lead["seal"], lead["warranty"], lead["color"]) == ("", None, "#1d5fbf")     # старое приложение: не указано
    code, res = call(public, "/api/logist/amo/assign", "POST", {"lead_id": 31865331, "driver_id": 2}, logist)
    assert code == 400 and "выполнена" in res["error"]

    # Доступ: настройки — только администратору, доска — только логисту
    assert call(public, "/api/logist/logists", "POST", {"name": "Анна", "login": "anna", "password": "parol-menedzhera",
                                                          "role": "manager"}, ctx.admin)[0] == 200
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

        def login(user, password):
            page.fill("#l-login", user)
            page.fill("#l-pass", password)
            page.click("#l-enter")
        login("olga", "parol-logista")
        expect(page.locator("main h1")).to_have_text("Сводка")

        # Заявки до подключения — подсказка, кто подключает
        page.click("nav button[data-tab='requests']")
        expect(page.locator("#req-off")).to_contain_text("Подключает администратор")
        expect(page.locator("nav button[data-tab='amocrm']")).to_have_count(0)

        # Подключение amoCRM администратором
        page.click("#logout")
        login("admin", admin_password(ctx))
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

        # Доска заявок у логиста: по базам, назначение водителя из списка
        page.click("#logout")
        login("olga", "parol-logista")
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


def hook(public, secret, body: str):
    req = urllib.request.Request(f"{public}/api/amocrm/hook/{secret}", method="POST", data=body.encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def wait_for(check, what):
    for _ in range(100):
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(what)


def test_parse_amocrm_webhook():
    from app.amocrm import hook_leads, parse_form
    body = ("leads%5Bupdate%5D%5B0%5D%5Bid%5D=31865331&leads%5Bupdate%5D%5B0%5D%5Bstatus_id%5D=45097963"
            "&leads%5Bupdate%5D%5B0%5D%5Bcustom_fields%5D%5B0%5D%5Bid%5D=828019"
            "&leads%5Bupdate%5D%5B0%5D%5Bcustom_fields%5D%5B0%5D%5Bvalues%5D%5B0%5D%5Bvalue%5D=4500"
            "&leads%5Bstatus%5D%5B0%5D%5Bid%5D=31611863&leads%5Bdelete%5D%5B0%5D%5Bid%5D=31603247"
            "&account%5Bsubdomain%5D=rosavtonomg")
    p = parse_form(body.encode())
    assert p["leads"]["update"][0]["custom_fields"][0]["values"][0]["value"] == "4500"
    assert p["account"]["subdomain"] == "rosavtonomg"
    assert hook_leads(p) == ({31865331, 31611863}, {31603247})
    assert hook_leads(parse_form(b"contacts[update][0][id]=5")) == (set(), set())
    # Поля amoCRM компании (как в gaslk) узнаются по id, даже если названы иначе
    from app.amocrm import guess_fields
    g = guess_fields([{"id": 825487, "name": "Желаемая дата", "type": "date"}, {"id": 828019, "name": "Количество", "type": "numeric"},
                      {"id": 825393, "name": "Куда везти", "type": "text"}, {"id": 1, "name": "Район", "type": "select"}])
    assert g == {"day": 825487, "liters": 828019, "address": 825393, "district": 1}


def test_amocrm_webhook_updates_requests_at_once(env, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="amocrm")
    ctx, local, public, logist = env
    day = datetime.now().strftime("%Y-%m-%d")
    fake = FakeAmo(day)
    ctx.amocrm.opener = fake
    s = call(public, "/api/logist/amocrm", "POST", {"domain": "rosavtonomg", "token": TOKEN, "enabled": True}, ctx.admin)[1]
    url = s["hook_url"]
    assert url.startswith(public + "/api/amocrm/hook/") and "hook_secret" not in s
    secret = url.rsplit("/", 1)[1]
    assert call(public, "/api/logist/amocrm", token=logist)[0] == 403        # адрес с секретом — только администратору
    assert call(public, "/api/amo/sync", "POST", token=logist)[0] == 404
    assert call(public, "/api/logist/amo/sync", "POST", token=logist)[0] == 200
    call(public, "/api/app/push-token", "POST", {"token": "phone-ivanov"}, driver_token(public))
    assert call(public, "/api/logist/amo/assign", "POST", {"lead_id": 31865331, "driver_id": 1}, logist)[0] == 200

    # Чужой или пустой секрет и другой аккаунт amoCRM — отказ
    assert hook(public, "wrong", "leads[update][0][id]=31865331")[0] == 404
    assert hook(public, secret, "leads[update][0][id]=31865331&account[subdomain]=other")[0] == 403

    # Сделку изменили — вебхук, и заявка с заказом водителя обновляются без ожидания обмена
    fake.update(31865331, fields={F_LITERS: "4500"})
    code, res = hook(public, secret, "leads[update][0][id]=31865331&leads[update][0][status_id]=142000"
                                     "&account[subdomain]=rosavtonomg")
    assert (code, res) == (200, {"ok": True, "leads": 1})

    def lead(lid):
        return next((x for x in call(public, f"/api/logist/amo/leads?day={day}", token=logist)[1]["leads"]
                     if x["lead_id"] == lid), None)
    wait_for(lambda: lead(31865331)["liters"] == 4500.0, "литры из вебхука")
    wait_for(lambda: ctx.db.q("SELECT liters FROM orders")[0]["liters"] == 4500.0, "заказ водителя обновлён")
    wait_push(ctx, 2)
    assert ctx.pusher.sent[1][1] == "Заказ изменён"

    # Новая сделка — сразу на доске
    fake.leads[31999999] = FakeAmo.lead(31999999, int(datetime.strptime(day, "%Y-%m-%d").replace(hour=12).timestamp()),
                                        "Клинский", "Ленинградское шоссе", "", "6000", "Первый залив!", 501, 12,
                                        int(time.time()))
    assert hook(public, secret, "leads[add][0][id]=31999999&account[subdomain]=rosavtonomg")[0] == 200
    wait_for(lambda: lead(31999999) is not None, "новая сделка из вебхука")
    assert (lead(31999999)["base"], lead(31999999)["conditions"]) == ("Онуфриево", "Первый залив!")

    # Сделку удалили — заявка пропадает, заказ у водителя снимается
    del fake.leads[31865331]
    assert hook(public, secret, "leads[delete][0][id]=31865331&account[subdomain]=rosavtonomg")[0] == 200
    wait_for(lambda: lead(31865331) is None, "удалённая сделка")
    wait_push(ctx, 3)
    assert ctx.pusher.sent[2][1] == "Заказ отменён"
    st = call(public, "/api/logist/amocrm", token=ctx.admin)[1]
    assert st["hook_count"] == 3 and st["last_hook"]
    # Каждый вебхук виден в журнале сервера (journalctl -u avtoves)
    text = caplog.text
    assert "amoCRM вебхук отклонён: неверный адрес" in text and "amoCRM вебхук отклонён: Вебхук от другого аккаунта" in text
    assert "amoCRM вебхук принят: события update, изменены сделки 31865331, удалены —" in text
    assert "amoCRM вебхук принят: события add, изменены сделки 31999999" in text
    assert "amoCRM вебхук принят: события delete, изменены сделки —, удалены 31865331" in text
    assert "amoCRM вебхук обработан: сделок 1, изменилось заявок 1" in text
    assert "amoCRM обмен: получено сделок" in text

    # Новый адрес: старый перестаёт работать
    s2 = call(public, "/api/logist/amocrm", "POST", {"domain": "rosavtonomg", "enabled": True, "new_hook_secret": True},
              ctx.admin)[1]
    assert s2["hook_url"] != url
    assert hook(public, secret, "leads[update][0][id]=31611863")[0] == 404


def test_driver_calls_client_and_opens_yandex_navigator(env):
    """В заказе водителя: «Позвонить» (несколько номеров — выбор) и «Маршрут» в Яндекс Навигаторе или Картах."""
    from playwright.sync_api import expect, sync_playwright
    from test_v2 import APP_HTML, PASSWORD
    ctx, local, public, logist = env
    day = datetime.now().strftime("%Y-%m-%d")
    ctx.amocrm.opener = FakeAmo(day)
    call(public, "/api/logist/amocrm", "POST", {"domain": "rosavtonomg", "token": TOKEN, "enabled": True}, ctx.admin)
    call(public, "/api/logist/amo/sync", "POST", token=logist)
    for lid in (31611863, 31603247):               # два номера / один номер; адрес без координат
        assert call(public, "/api/logist/amo/assign", "POST", {"lead_id": lid, "driver_id": 1}, logist)[0] == 200
    ctx.db.x("UPDATE orders SET lat=55.75, lon=37.62 WHERE amo_lead_id=31603247")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        page.add_init_script("window.Android = { calls: [], routes: [], notify() {}, requestPush() {},"
                             " dial(p) { this.calls.push(p); }, route(a, p, t) { this.routes.push([a, p, t]); } };")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(APP_HTML.as_uri())
        page.fill("#server", public)
        page.fill("#login", "ivanov")
        page.fill("#password", PASSWORD)
        page.click("#enter")
        first, second = page.locator("#orders article").nth(0), page.locator("#orders article").nth(1)
        expect(first).to_contain_text("[Д] Солнечногорский - Ирина")
        expect(first.locator("button.call")).to_have_text("Позвонить")
        expect(first.locator("button.nav")).to_have_text("Маршрут")
        page.screenshot(path=SHOTS / "app-call-route.png")
        # Снимок для страницы в Google Play (1080×2160): заказ с кнопками звонка и маршрута
        store = browser.new_page(viewport={"width": 360, "height": 720}, device_scale_factor=3, locale="ru-RU")
        store.add_init_script("window.Android = { notify() {}, requestPush() {} };")
        store.goto(APP_HTML.as_uri())
        store.fill("#server", public)
        store.fill("#login", "ivanov")
        store.fill("#password", PASSWORD)
        store.click("#enter")
        expect(store.locator("#orders article").first.locator("button.call")).to_be_visible()
        store.evaluate("window.scrollTo(0, document.querySelector('#orders').getBoundingClientRect().top + scrollY - 70)")
        (SHOTS / "play").mkdir(exist_ok=True)
        store.screenshot(path=SHOTS / "play" / "4-zvonok-marshrut.png")

        # Два номера — выбор, один номер — сразу набор
        first.locator("button.call").click()
        expect(page.locator(".sheet .pick-app")).to_have_text(["+79001112233", "89001112233"])
        page.screenshot(path=SHOTS / "app-call-pick.png")
        page.click("#call-1")
        second.locator("button.call").click()
        assert page.evaluate("window.Android.calls") == ["89001112233", "+79260000003"]

        # Маршрут: без координат — по адресу, с координатами — в точку; Навигатор или Карты на выбор
        first.locator("button.nav").click()
        expect(page.locator(".sheet .pick-app")).to_have_text(["Яндекс Навигатор", "Яндекс Карты"])
        page.screenshot(path=SHOTS / "app-route-pick.png")
        page.click("#route-navi")
        second.locator("button.nav").click()
        page.click("#route-maps")
        assert page.evaluate("window.Android.routes") == [
            ["navi", "", "Солнечногорский, Ленинградское шоссе"], ["maps", "55.75,37.62", ""]]
        browser.close()
    assert errors == []


def test_driver_colors_seal_and_warranty(env):
    """Свой цвет у каждого водителя на доске заявок; пломба и гарантийный талон в выгрузке."""
    from playwright.sync_api import expect, sync_playwright
    from test_v2 import APP_HTML, PASSWORD
    ctx, local, public, logist = env
    day = datetime.now().strftime("%Y-%m-%d")
    ctx.amocrm.opener = FakeAmo(day)
    call(public, "/api/logist/amocrm", "POST", {"domain": "rosavtonomg", "token": TOKEN, "enabled": True}, ctx.admin)
    call(public, "/api/logist/amo/sync", "POST", token=logist)
    # Цвета разные сразу; администратор может поменять
    colors = {d["name"]: d["color"] for d in call(public, "/api/logist/refs", token=ctx.admin)[1]["drivers"]}
    assert colors == {"Иванов Сергей": "#1d5fbf", "Петров Пётр": "#e8590c"}
    assert call(public, "/api/logist/drivers", "POST", {"name": "Новый Водитель", "login": "novyi", "password": PASSWORD},
                ctx.admin)[0] == 200
    assert {d["color"] for d in call(public, "/api/logist/refs", token=ctx.admin)[1]["drivers"]} == {"#1d5fbf", "#e8590c", "#2b8a3e"}
    assert call(public, "/api/logist/drivers", "POST", {"id": 2, "name": "Петров Пётр", "login": "petrov", "color": "red"},
                ctx.admin)[0] == 400
    assert call(public, "/api/logist/drivers", "POST", {"id": 2, "name": "Петров Пётр", "login": "petrov", "color": "#AE3EC9"},
                ctx.admin)[0] == 200
    for lid, did in ((31865331, 1), (31611863, 2), (31603247, 1)):
        assert call(public, "/api/logist/amo/assign", "POST", {"lead_id": lid, "driver_id": did}, logist)[0] == 200
    b = call(public, f"/api/logist/amo/leads?day={day}", token=logist)[1]
    assert {l["lead_id"]: l["color"] for l in b["leads"]} == {31603247: "#1d5fbf", 31865331: "#1d5fbf", 31611863: "#ae3ec9"}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Приложение водителя: номер пломбы и галочка «Гарантийный талон выдан»
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        page.add_init_script("window.Android = { notify() {}, requestPush() {} };")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(APP_HTML.as_uri())
        page.fill("#server", public)
        page.fill("#login", "ivanov")
        page.fill("#password", PASSWORD)
        page.click("#enter")
        page.click("#change-vehicle")
        page.click("button.pick:has-text('Т100АА26')")
        page.click("button.pick:has-text('АВ123477')")
        if page.locator("#vehicle-done").count():
            page.click("#vehicle-done")
        page.locator("#orders article", has_text="Новая Москва").locator("button.order-done").click()
        page.click("#paid-yes")
        page.fill("#seal", "ГП 004512")
        page.check("#warranty")
        page.screenshot(path=SHOTS / "app-seal.png")
        page.click("#save-unload")
        expect(page.locator(".toast")).to_contain_text("Выгрузка записана")

        def stored():
            rows = ctx.db.q("SELECT seal, warranty, liters FROM unloads")
            return rows[0] if rows else None
        for _ in range(100):
            if stored():
                break
            time.sleep(0.05)
        assert dict(stored()) == {"seal": "ГП 004512", "warranty": 1, "liters": 4000.0}

        # Доска логиста: строки водителей в их цветах, легенда, у выполненной — пломба и талон
        desk = browser.new_page(viewport={"width": 1366, "height": 900})
        desk.on("pageerror", lambda e: errors.append(str(e)))
        desk.goto(public + "/logist")
        desk.fill("#l-login", "olga")
        desk.fill("#l-pass", "parol-logista")
        desk.click("#l-enter")
        desk.click("nav button[data-tab='requests']")
        expect(desk.locator("tr[data-lead='31611863']")).to_have_class("drv")
        assert desk.locator("tr[data-lead='31611863']").get_attribute("style") == "--drv:#ae3ec9"
        expect(desk.locator("#drv-legend button[data-driver]")).to_have_count(2)
        expect(desk.locator("#drv-legend button[data-driver]").first).to_contain_text("Иванов Сергей")
        expect(desk.locator("nav button[data-tab='clients']")).to_have_count(0)      # раздел «Клиенты» скрыт
        # Клик по водителю — только его заявки на день; ещё клик или «Все водители» — снова все
        desk.click("#drv-legend button[data-driver='1']")
        expect(desk.locator("#requests tbody tr")).to_have_count(2)
        expect(desk.locator("tr[data-lead='31611863']")).to_have_count(0)
        expect(desk.locator("#drv-picked")).to_contain_text("Иванов Сергей: заявок на")
        expect(desk.locator("#drv-picked")).to_contain_text("осталось сделать: 1")
        expect(desk.locator("#drv-legend button[data-driver='1']")).to_have_attribute("aria-pressed", "true")
        desk.screenshot(path=SHOTS / "amocrm-driver-filter.png", full_page=True)
        desk.click("#drv-all")
        expect(desk.locator("#requests tbody tr")).to_have_count(3)
        desk.click("#drv-legend button[data-driver='2']")
        expect(desk.locator("#requests tbody tr")).to_have_count(1)
        desk.click("#drv-legend button[data-driver='2']")
        expect(desk.locator("#drv-picked")).to_have_count(0)
        expect(desk.locator("tr[data-lead='31865331']")).to_contain_text("пломба № ГП 004512, талон выдан")
        desk.screenshot(path=SHOTS / "amocrm-colors.png", full_page=True)
        # В отчёте выгрузок и в Excel
        desk.click("nav button[data-tab='report']")
        expect(desk.locator("#unloads")).to_contain_text("пломба № ГП 004512, талон выдан")
        browser.close()
    assert errors == []
    code, raw = call(public, f"/api/logist/report.xlsx?from={day}&to={day}", token=logist, raw=True)
    import io as _io
    from openpyxl import load_workbook
    sh = load_workbook(_io.BytesIO(raw))["Выгрузки"]
    assert [c.value for c in sh[1]][-2:] == ["Номер пломбы", "Гарантийный талон"]
    assert [c.value for c in sh[2]][-2:] == ["ГП 004512", "выдан"]
    # Логист может исправить пломбу и талон
    uid = ctx.db.q("SELECT id FROM unloads")[0]["id"]
    assert call(public, f"/api/logist/unloads/{uid}", "PATCH", {"seal": "ГП 004513", "warranty": False}, logist)[0] == 200
    assert dict(ctx.db.q("SELECT seal, warranty FROM unloads")[0]) == {"seal": "ГП 004513", "warranty": 0}
