"""Логика проезда через весы: светофоры, фиксация веса, привязка номера, рейсы."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from .db import DB
from .io_control import LIGHT_GREEN, LIGHT_RED, IOController
from .plates import PlateTracker, normalize
from .scale import Scale

log = logging.getLogger("weighing")

FREE, ARRIVING, FIXED = "free", "arriving", "fixed"
STATE_TEXT = {
    FREE: "Весы свободны — проезд разрешён",
    ARRIVING: "Машина на весах — ожидание стабилизации",
    FIXED: "Вес зафиксирован — выезд разрешён",
}


class WeighingController:
    def __init__(self, cfg: dict, scale: Scale, io: IOController, tracker: PlateTracker,
                 cameras: list, db: DB):
        self.cfg = cfg
        self.wcfg = cfg["weighing"]
        self.scfg = cfg["scale"]
        self.scale, self.io, self.tracker, self.cameras, self.db = scale, io, tracker, cameras, db
        self.snap_dir = Path(cfg["data_dir"]) / "snapshots"
        self.state = FREE
        self.state_since = time.time()
        self.arrive_ts = 0.0
        self.empty_since: float | None = None
        self.side_in: str | None = None
        self.plate_override: str | None = None   # номер, введённый оператором
        self.last_result: dict | None = None
        self.fixed_log: deque[tuple[str, float]] = deque(maxlen=20)   # (номер, время фиксации)
        self.fixed_cand: dict | None = None
        self.free_since = time.time()
        self.events: deque[dict] = deque(maxlen=50)
        self.warning = ""
        self._fix_request = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, name="weighing", daemon=True)

    def start(self):
        self._enter_free()
        self._thr.start()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------ действия оператора
    def request_fix(self):
        self._fix_request = True

    def set_plate(self, text: str):
        plate, _ = normalize(text)
        self.plate_override = plate or None
        self.event(f"Оператор указал номер: {plate or '—'}")

    def reset(self):
        """Сбросить цикл (например, машина уехала с весов не по правилам)."""
        with self._lock:
            self.event("Цикл сброшен оператором")
            self._enter_free()

    # ------------------------------------------------------------ основной цикл
    def _run(self):
        while not self._stop.wait(0.2):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в цикле взвешивания")

    def _tick(self):
        s = self.scale.snapshot()
        now = time.time()
        with self._lock:
            if not s["connected"]:
                self.warning = "Нет связи с весовым индикатором"
                if self.state == FREE:
                    self._lights_all(LIGHT_RED)   # без весов не пускаем
                return
            w = s["weight"] or 0.0
            self.warning = "Перегруз весов!" if s["overload"] else ""
            occupied = w >= self.scfg["min_weight"]
            empty = w < self.scfg["empty_weight"]

            if self.state == FREE:
                self._lights_free()
                if occupied:
                    self._set_state(ARRIVING)
                    self.arrive_ts = now
                    self.side_in = None
                    self._lights_all(LIGHT_RED)
                    self.event(f"Заезд на весы, {w:.0f} кг")

            elif self.state == ARRIVING:
                cand = self.current_plate()
                if cand and cand.get("side") and not self.side_in:
                    self.side_in = cand["side"]
                if empty:
                    if self._empty_for(now):
                        self.event("Машина уехала без фиксации веса")
                        self._enter_free()
                    return
                self.empty_since = None
                if self.io.ir_blocked():
                    self.warning = "Машина стоит не полностью на платформе (ИК-барьер)"
                    return
                if self._fix_request or (self.wcfg["auto_fix"] and s["stable"] and occupied):
                    if not s["stable"] and not self._fix_request:
                        return
                    self._fix_request = False
                    self._fix(w, auto=self.wcfg["auto_fix"] and s["stable"])

            elif self.state == FIXED:
                if empty and self._empty_for(now):
                    self.event("Весы освобождены")
                    self._enter_free()
                elif not empty:
                    self.empty_since = None
            self._fix_request = False if self.state != ARRIVING else self._fix_request

    def _empty_for(self, now: float) -> bool:
        if self.empty_since is None:
            self.empty_since = now
        return now - self.empty_since >= float(self.wcfg["clear_time"])

    # ------------------------------------------------------------ фиксация
    def current_plate(self) -> dict | None:
        if self.plate_override:
            return {"plate": self.plate_override, "conf": 1.0, "votes": 0, "valid": True,
                    "side": self.side_in, "camera": "оператор", "crop": None, "alternatives": []}
        if self.state == FREE:
            return None
        if self.state == FIXED:
            return self.fixed_cand
        since = self.arrive_ts - float(self.wcfg["plate_window_sec"])
        # номера машин, взвешенных недавно, не могут принадлежать новой машине
        # но если такая машина снова подъехала после освобождения весов — это новый заезд
        ignore = {p: self.free_since for p, t in self.fixed_log if t >= since and p}
        return self.tracker.best(since, arrive_ts=self.arrive_ts, ignore=ignore,
                                 seen_after=self.free_since - 1)

    def _fix(self, weight: float, auto: bool):
        cand = self.current_plate() or {}
        plate = cand.get("plate", "")
        snaps = self._save_snapshots(cand.get("crop"))
        res = self.db.add_weighing(plate, cand.get("conf"), weight, self.side_in or cand.get("side"),
                                   auto, ";".join(snaps), int(self.wcfg["trip_max_hours"]))
        trip = res["trip"]
        kind = "второе" if trip["status"] == "closed" else "первое"
        msg = f"Зафиксировано {weight:.0f} кг, номер {plate or 'НЕ РАСПОЗНАН'} ({kind} взвешивание)"
        if trip["status"] == "closed":
            msg += f", нетто {trip['net']:.0f} кг"
        self.event(msg)
        veh = self.db.vehicle(plate) if plate else None
        if veh and veh.get("blocked"):
            self.event(f"ВНИМАНИЕ: ТС {plate} в стоп-листе")
        self.fixed_log.append((plate, time.time()))
        self.fixed_cand = {**cand, "plate": plate} if plate else None
        self.last_result = {**res, "plate": plate, "weight": weight, "snapshots": snaps,
                            "kind": kind, "ts": datetime.now().isoformat(timespec="seconds")}
        self._set_state(FIXED)
        self._lights_exit()

    def _save_snapshots(self, crop: bytes | None) -> list[str]:
        day = datetime.now().strftime("%Y-%m-%d")
        stamp = datetime.now().strftime("%H%M%S")
        d = self.snap_dir / day
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for cam in self.cameras:
            jpg = cam.jpeg(width=1600, quality=85)
            if jpg:
                p = d / f"{stamp}_{cam.id}.jpg"
                p.write_bytes(jpg)
                out.append(f"{day}/{p.name}")
        if crop:
            p = d / f"{stamp}_plate.jpg"
            p.write_bytes(crop)
            out.append(f"{day}/{p.name}")
        return out

    # ------------------------------------------------------------ светофоры
    def _lights_all(self, state: str):
        for side in ("a", "b"):
            self.io.set_light(side, state)

    def _lights_free(self):
        if self.wcfg["mode"] == "reverse":
            self.io.set_light("a", LIGHT_GREEN)
            self.io.set_light("b", LIGHT_RED)
        else:
            self._lights_all(LIGHT_GREEN)

    def _lights_exit(self):
        if self.wcfg["mode"] == "reverse":
            exit_side = "a"
        else:
            exit_side = {"a": "b", "b": "a"}.get(self.side_in or "")
        if exit_side is None:          # сторона заезда неизвестна — открываем обе
            self._lights_all(LIGHT_GREEN)
            for s in ("a", "b"):
                self.io.pulse_barrier(s)
            return
        self.io.set_light(exit_side, LIGHT_GREEN)
        self.io.set_light("b" if exit_side == "a" else "a", LIGHT_RED)
        self.io.pulse_barrier(exit_side)

    # ------------------------------------------------------------ служебное
    def _enter_free(self):
        self._set_state(FREE)
        self.free_since = time.time()
        self.plate_override = None
        self.side_in = None
        self.empty_since = None
        self._fix_request = False
        self._lights_free()

    def _set_state(self, st: str):
        self.state, self.state_since = st, time.time()

    def event(self, text: str):
        log.info(text)
        self.events.appendleft({"ts": datetime.now().strftime("%H:%M:%S"), "text": text})

    def snapshot(self) -> dict:
        cand = self.current_plate()
        return {
            "state": self.state, "state_text": STATE_TEXT[self.state],
            "state_for": round(time.time() - self.state_since),
            "warning": self.warning, "side_in": self.side_in,
            "auto_fix": self.wcfg["auto_fix"], "mode": self.wcfg["mode"],
            "plate": {k: v for k, v in (cand or {}).items() if k != "crop"} if cand else None,
            "plate_override": self.plate_override,
            "last": {k: v for k, v in (self.last_result or {}).items()} if self.last_result else None,
            "events": list(self.events)[:20],
        }
