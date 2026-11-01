"""Веб-интерфейс оператора и API (включая обмен с 1С)."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import os
from datetime import datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, Response,
                                 StreamingResponse)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__
from . import settings as st
from .io_control import LIGHT_GREEN, LIGHT_OFF, LIGHT_RED
from .plates import normalize, pretty

STATIC = Path(__file__).parent / "static"


def build_app(ctx) -> Starlette:
    """ctx: Runtime — поля cfg, scale, io, cameras, tracker, anpr, ctrl, db, sim (меняются при применении настроек)."""
    cfg, db = ctx.cfg, ctx.db

    def gas_cfg() -> dict:
        g = ctx.cfg.get("gas") or {}
        return {"enabled": bool(g.get("enabled", True)), "name": g.get("name") or "газ",
                "density": float(g.get("density") or 0.54), "show_liters": bool(g.get("show_liters", True))}

    def gas_info(t: dict) -> dict | None:
        """Газ по рейсу: в машине при въезде/выезде, сколько слито (+) или залито (−), литры."""
        if t.get("gas_in") is None:
            return None
        g = gas_cfg()
        d = g["density"] or 0.54
        g_in, g_out = t["gas_in"], t.get("gas_out")
        delta = round(g_in - g_out, 1) if g_out is not None else None
        veh = db.vehicle(t["plate"]) if t.get("plate") else None
        tank = (veh or {}).get("tank_l")
        lit = lambda kg: round(kg / d) if kg is not None else None  # noqa: E731
        pct = lambda kg: round(kg / d / tank * 100) if (kg is not None and tank) else None  # noqa: E731
        return {"tare": t.get("gas_tare"), "in_kg": g_in, "out_kg": g_out, "delta_kg": delta,
                "in_l": lit(g_in), "out_l": lit(g_out), "delta_l": lit(delta),
                "in_pct": pct(g_in), "out_pct": pct(g_out), "tank_l": tank,
                "action": None if delta is None else ("слито" if delta >= 0 else "залито")}

    def ctrl_state() -> dict:
        c = ctx.ctrl.snapshot()
        last = c.get("last")
        if last and last.get("trip"):
            fresh = db.trip(last["trip"]["id"]) or last["trip"]     # тару могли указать после взвешивания
            last["trip"] = fresh
            last["gas"] = gas_info(fresh) if gas_cfg()["enabled"] else None
        return c

    async def index(_):
        return FileResponse(STATIC / "index.html")

    async def state(_):
        return JSONResponse({
            "version": __version__,
            "gas": gas_cfg(),
            "time": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "simulate": bool(ctx.sim),
            "scale": ctx.scale.snapshot(),
            "io": ctx.io.snapshot(),
            "cameras": [c.snapshot() for c in ctx.cameras],
            "anpr_error": ctx.anpr.error,
            "ctrl": ctrl_state(),
            "reads": [{"ts": datetime.fromtimestamp(r.ts).strftime("%H:%M:%S"), "camera": r.camera,
                       "plate": r.plate, "conf": round(r.conf, 2), "valid": r.valid}
                      for r in reversed(ctx.tracker.recent(12))],
        })

    async def cam_jpg(request: Request):
        cam = next((c for c in ctx.cameras if c.id == request.path_params["cid"]), None)
        jpg = cam.jpeg() if cam else None
        if not jpg:
            return Response(status_code=204)
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def plate_crop(_):
        cand = ctx.ctrl.current_plate()
        if not cand or not cand.get("crop"):
            return Response(status_code=204)
        return Response(cand["crop"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------- действия оператора
    async def fix(_):
        ctx.ctrl.request_fix()
        return JSONResponse({"ok": True})

    async def set_plate(request: Request):
        ctx.ctrl.set_plate((await request.json()).get("plate", ""))
        return JSONResponse({"ok": True})

    async def reset(_):
        ctx.ctrl.reset()
        return JSONResponse({"ok": True})

    async def light(request: Request):
        b = await request.json()
        if b.get("side") not in ("a", "b") or b.get("state") not in (LIGHT_RED, LIGHT_GREEN, LIGHT_OFF):
            return JSONResponse({"error": "bad request"}, 400)
        ctx.io.set_light(b["side"], b["state"], force=True)
        ctx.ctrl.event(f"Оператор: светофор {b['side'].upper()} -> {b['state']}")
        return JSONResponse({"ok": True})

    async def manual(request: Request):
        ctx.io.manual = bool((await request.json()).get("on"))
        ctx.ctrl.event("Ручное управление светофорами " + ("ВКЛ" if ctx.io.manual else "ВЫКЛ"))
        return JSONResponse({"ok": True})

    async def barrier(request: Request):
        side = (await request.json()).get("side")
        ctx.io.pulse_barrier(side)
        ctx.ctrl.event(f"Оператор: открыт шлагбаум {str(side).upper()}")
        return JSONResponse({"ok": True})

    # ---------------------------------------------------------- журнал
    def _filters(request: Request):
        q = request.query_params
        return dict(date_from=q.get("from") or None,
                    date_to=(q.get("to") + "T23:59:59") if q.get("to") else None,
                    plate=q.get("plate") or None, status=q.get("status") or None)

    async def trips(request: Request):
        rows = db.trips(**_filters(request))
        for r in rows:
            r["gas"] = gas_info(r)
        return JSONResponse(rows)

    async def trip_patch(request: Request):
        tid = int(request.path_params["tid"])
        body = await request.json()
        if "plate" in body:
            body["plate"], _ = normalize(body["plate"])
        return JSONResponse(db.update_trip(tid, body))

    async def trip_close(request: Request):
        return JSONResponse(db.close_trip_manually(int(request.path_params["tid"])))

    async def trip_weighings(request: Request):
        return JSONResponse(db.weighings_of_trip(int(request.path_params["tid"])))

    async def export_xlsx(request: Request):
        from openpyxl import Workbook
        from openpyxl.styles import Font
        rows = db.trips(**_filters(request), limit=100000)
        wb = Workbook()
        ws = wb.active
        ws.title = "Взвешивания"
        head = ["№", "Номер ТС", "1-е взвеш.", "Вес 1, кг", "2-е взвеш.", "Вес 2, кг", "Брутто, кг",
                "Тара, кг", "Нетто, кг", "Груз", "Контрагент", "Водитель", "Статус", "Комментарий"]
        gas_on = gas_cfg()["enabled"]
        if gas_on:
            head += ["Тара пустой, кг", "Газ при въезде, кг", "Газ при выезде, кг", "Слито(+)/залито(−), кг",
                     "Газ при въезде, л", "Газ при выезде, л", "Слито/залито, л"]
        ws.append(head)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            row = [r["id"], r["plate"], r["w1_ts"], r["w1"], r["w2_ts"], r["w2"], r["gross"],
                   r["tare"], r["net"], r["cargo"], r["counterparty"], r["driver"],
                   {"open": "открыт", "closed": "закрыт", "cancelled": "отменён"}.get(r["status"]),
                   r["comment"]]
            if gas_on:
                g = gas_info(r) or {}
                row += [g.get("tare"), g.get("in_kg"), g.get("out_kg"), g.get("delta_kg"),
                        g.get("in_l"), g.get("out_l"), g.get("delta_l")]
            ws.append(row)
        for col, wdt in zip("ABCDEFGHIJKLMNOPQRSTU", (6, 12, 20, 11, 20, 11, 11, 11, 11, 18, 24, 18, 10, 30,
                                                       14, 16, 16, 18, 14, 14, 14)):
            ws.column_dimensions[col].width = wdt
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        name = f"vzveshivaniya_{datetime.now():%Y%m%d_%H%M}.xlsx"
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def ticket(request: Request):
        t = db.trip(int(request.path_params["tid"]))
        if not t:
            return HTMLResponse("Рейс не найден", 404)
        e = lambda v: html.escape(str(v if v is not None else "—"))  # noqa: E731
        fmt = lambda v: f"{v:,.0f}".replace(",", " ") if isinstance(v, (int, float)) else "—"  # noqa: E731
        ws = db.weighings_of_trip(t["id"])
        imgs = "".join(f'<img src="/snapshots/{e(s)}">' for w in ws for s in w["snapshots"].split(";")
                       if s.endswith("_plate.jpg"))
        g = gas_info(t) if gas_cfg()["enabled"] else None
        gas_rows = ""
        if g:
            gc = gas_cfg()
            lt = (lambda v: f" ({fmt(v)} л)" if v is not None and gc["show_liters"] else "")  # noqa: E731
            gas_rows = (f"<tr><td colspan=2><b>{e(gc['name'])}</b> — тара пустой машины {fmt(g['tare'])} кг</td></tr>"
                        f"<tr><td>В машине при въезде</td><td>{fmt(g['in_kg'])} кг{lt(g['in_l'])}</td></tr>")
            if g["out_kg"] is not None:
                gas_rows += (f"<tr><td>В машине при выезде</td><td>{fmt(g['out_kg'])} кг{lt(g['out_l'])}</td></tr>"
                             f"<tr><td>{'Слито' if g['delta_kg'] >= 0 else 'Залито'}</td>"
                             f"<td class='n'>{fmt(abs(g['delta_kg']))} кг{lt(abs(g['delta_l']) if g['delta_l'] is not None else None)}</td></tr>")
        return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><title>Талон №{t['id']}</title>
<style>body{{font:14px Arial;margin:24px;max-width:700px}}td{{padding:4px 10px;border-bottom:1px solid #ccc}}
h2{{margin:0 0 12px}}img{{height:60px;margin:6px 6px 0 0}}.n{{font-size:22px;font-weight:bold}}
@media print{{button{{display:none}}}}</style>
<button onclick="print()">Печать</button>
<h2>Талон взвешивания №{t['id']}</h2>
<table>
<tr><td>Номер ТС</td><td class="n">{e(pretty(t['plate']))}</td></tr>
<tr><td>Контрагент</td><td>{e(t['counterparty'])}</td></tr>
<tr><td>Водитель</td><td>{e(t['driver'])}</td></tr>
<tr><td>Груз</td><td>{e(t['cargo'])}</td></tr>
<tr><td>1-е взвешивание</td><td>{e(t['w1_ts'])} — {fmt(t['w1'])} кг</td></tr>
<tr><td>2-е взвешивание</td><td>{e(t['w2_ts'])} — {fmt(t['w2'])} кг</td></tr>
<tr><td>Брутто</td><td>{fmt(t['gross'])} кг</td></tr>
<tr><td>Тара</td><td>{fmt(t['tare'])} кг</td></tr>
<tr><td>Нетто</td><td class="n">{fmt(t['net'])} кг</td></tr>
{gas_rows}
<tr><td>Комментарий</td><td>{e(t['comment'])}</td></tr>
</table>{imgs}
<p style="margin-top:40px">Весовщик ______________ &nbsp;&nbsp;&nbsp; Водитель ______________</p>""")

    # ---------------------------------------------------------- справочник ТС
    async def vehicles(request: Request):
        if request.method == "GET":
            return JSONResponse(db.vehicles())
        v = await request.json()
        v["plate"], _ = normalize(v.get("plate", ""))
        if not v["plate"]:
            return JSONResponse({"error": "не указан номер"}, 400)
        for k in ("tare", "tank_l"):
            if v.get(k) in ("", None):
                v[k] = None
            else:
                try:
                    v[k] = float(v[k])
                except (TypeError, ValueError):
                    return JSONResponse({"error": f"{k}: введите число"}, 400)
        db.save_vehicle(v)
        return JSONResponse({"ok": True})

    async def weighing_tare(request: Request):
        """«Пустая машина — запомнить тару» по взвешиванию."""
        try:
            veh = db.set_tare_from_weighing(int(request.path_params["wid"]))
        except ValueError as ex:
            return JSONResponse({"error": str(ex)}, 400)
        ctx.ctrl.event(f"Тара {veh['plate']}: {veh['tare']:.0f} кг")
        return JSONResponse({"ok": True, "vehicle": veh})

    async def vehicle_delete(request: Request):
        db.delete_vehicle(request.path_params["plate"])
        return JSONResponse({"ok": True})

    # ---------------------------------------------------------- обмен с 1С
    def _auth(request: Request) -> bool:
        token = ctx.cfg["server"].get("api_token", "")
        return bool(token) and (request.headers.get("x-api-token") == token
                                or request.query_params.get("token") == token)

    async def api_1c_trips(request: Request):
        if not _auth(request):
            return JSONResponse({"error": "unauthorized"}, 401)
        only_new = request.query_params.get("only_new", "1") != "0"
        rows = db.trips_for_1c(only_new, request.query_params.get("from"))
        return JSONResponse([{
            "id": r["id"], "plate": r["plate"], "date": r["w2_ts"] or r["w1_ts"],
            "w1_date": r["w1_ts"], "w1": r["w1"], "w2_date": r["w2_ts"], "w2": r["w2"],
            "gross": r["gross"], "tare": r["tare"], "net": r["net"],
            "direction": r["direction"], "cargo": r["cargo"], "counterparty": r["counterparty"],
            "driver": r["driver"], "comment": r["comment"],
            "gas": gas_info(r),
        } for r in rows])

    async def api_1c_ack(request: Request):
        if not _auth(request):
            return JSONResponse({"error": "unauthorized"}, 401)
        ids = [int(i) for i in (await request.json()).get("ids", [])]
        return JSONResponse({"ok": True, "count": db.ack_1c(ids)})

    # ---------------------------------------------------------- имитация
    async def sim_drive(request: Request):
        if not ctx.sim:
            return JSONResponse({"error": "режим имитации выключен"}, 400)
        b = await request.json()
        plate, _ = normalize(b.get("plate", "А123ВС77"))
        ok = ctx.sim.drive(plate, float(b.get("weight", 25000)), b.get("side", "a"))
        return JSONResponse({"ok": ok, "busy": not ok})

    # ---------------------------------------------------------- настройки
    sessions = st.Sessions()

    def _protected() -> bool:
        return bool(ctx.cfg["server"].get("settings_password"))

    def _settings_ok(request: Request) -> bool:
        return not _protected() or sessions.valid(request.headers.get("x-settings-token"))

    def _deny():
        return JSONResponse({"error": "нужен пароль настроек", "need_login": True}, 401)

    async def settings_status(request: Request):
        return JSONResponse({"protected": _protected(), "authorized": _settings_ok(request),
                             "config_path": ctx.config_path})

    async def settings_login(request: Request):
        pw = str((await request.json()).get("password", ""))
        stored = ctx.cfg["server"].get("settings_password", "")
        if stored and st.check_password(pw, stored):
            return JSONResponse({"token": sessions.new()})
        await asyncio.sleep(1.0)
        return JSONResponse({"error": "неверный пароль"}, 403)

    async def settings_get(request: Request):
        if not _settings_ok(request):
            return _deny()
        return JSONResponse(st.to_form(st.load_raw(ctx.config_path)))

    async def settings_save(request: Request):
        if not _settings_ok(request):
            return _deny()
        form = await request.json()
        raw = st.load_raw(ctx.config_path)
        try:
            new = st.apply_form(raw, form)
        except st.FormError as ex:
            return JSONResponse({"error": "проверьте поля", "fields": ex.errors}, 422)
        pw_changed = new["server"].get("settings_password") != raw.get("server", {}).get("settings_password")
        st.save_raw(ctx.config_path, new)
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, ctx.reload)
        logging.getLogger("settings").info("Настройки сохранены (%s)", request.client.host if request.client else "?")
        out = {"ok": True, **res}
        if pw_changed:
            sessions.clear()
            # тот, кто поставил пароль, остаётся в настройках; пароль снят — вход не нужен
            out["token"] = sessions.new() if new["server"].get("settings_password") else None
        return JSONResponse(out)

    async def _run(fn, *a, timeout=25):
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, fn, *a), timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "message": "нет ответа (таймаут)"}

    async def settings_test(request: Request):
        if not _settings_ok(request):
            return _deny()
        kind = request.path_params["kind"]
        b = await request.json()
        try:
            if kind == "scale":
                return JSONResponse(await _run(st.test_scale, b, b.get("parser", "cas")))
            if kind == "io":
                return JSONResponse(await _run(st.test_io, b))
            if kind == "relay":
                ctx.io.hold(3.5)        # работающая программа не перебьёт проверку
                return JSONResponse(await _run(st.pulse_relay, b, int(b["relay"])))
            if kind == "camera":
                return JSONResponse(await _run(st.test_camera, b["url"], timeout=30))
        except (KeyError, ValueError, TypeError) as ex:
            return JSONResponse({"ok": False, "message": f"не заполнено: {ex}"})
        return JSONResponse({"error": "unknown"}, 404)

    async def settings_restart(request: Request):
        """Перезапуск службы (нужен после смены порта): процесс завершается, служба Windows поднимает его заново."""
        if not _settings_ok(request):
            return _deny()
        logging.getLogger("settings").warning("Перезапуск по запросу из настроек")
        asyncio.get_running_loop().call_later(1.0, os._exit, 3)
        return JSONResponse({"ok": True})

    snap_dir = Path(cfg["data_dir"]) / "snapshots"
    return Starlette(routes=[
        Route("/", index),
        Route("/api/state", state),
        Route("/api/cam/{cid}.jpg", cam_jpg),
        Route("/api/plate_crop.jpg", plate_crop),
        Route("/api/fix", fix, methods=["POST"]),
        Route("/api/plate", set_plate, methods=["POST"]),
        Route("/api/reset", reset, methods=["POST"]),
        Route("/api/light", light, methods=["POST"]),
        Route("/api/manual", manual, methods=["POST"]),
        Route("/api/barrier", barrier, methods=["POST"]),
        Route("/api/trips", trips),
        Route("/api/trips.xlsx", export_xlsx),
        Route("/api/trips/{tid:int}", trip_patch, methods=["PATCH"]),
        Route("/api/trips/{tid:int}/close", trip_close, methods=["POST"]),
        Route("/api/trips/{tid:int}/weighings", trip_weighings),
        Route("/ticket/{tid:int}", ticket),
        Route("/api/vehicles", vehicles, methods=["GET", "POST"]),
        Route("/api/vehicles/{plate}", vehicle_delete, methods=["DELETE"]),
        Route("/api/weighings/{wid:int}/tare", weighing_tare, methods=["POST"]),
        Route("/api/1c/trips", api_1c_trips),
        Route("/api/1c/ack", api_1c_ack, methods=["POST"]),
        Route("/api/sim/drive", sim_drive, methods=["POST"]),
        Route("/api/settings/status", settings_status),
        Route("/api/settings/login", settings_login, methods=["POST"]),
        Route("/api/settings", settings_get),
        Route("/api/settings", settings_save, methods=["POST"]),
        Route("/api/settings/test/{kind}", settings_test, methods=["POST"]),
        Route("/api/settings/restart", settings_restart, methods=["POST"]),
        Mount("/snapshots", StaticFiles(directory=str(snap_dir)), name="snapshots"),
        Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
    ])
