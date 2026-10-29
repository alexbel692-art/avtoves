"""Работающая система: весы, реле, камеры, распознавание, цикл взвешивания.

Настройки из программы применяются на лету: оборудование останавливается и создаётся
заново с новыми параметрами (база и веб-сервер продолжают работать).
"""
from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path

from .anpr import ANPRService, Camera, SimRecognizer
from .config import load_config
from .db import DB
from .io_control import IOController
from .plates import PlateTracker
from .scale import Scale
from .weighing import WeighingController

log = logging.getLogger("runtime")

SIM_SCALE_PORT, SIM_IO_PORT = 14001, 15020


class Runtime:
    def __init__(self, cfg: dict, force_simulate: bool = False):
        self.force_simulate = force_simulate
        self.config_path = cfg["_path"]
        self.db = DB(Path(cfg["data_dir"]) / "avtoves.db")
        self.tracker = PlateTracker()
        self.sim = None
        self._sim_started = False
        self.lock = threading.RLock()
        self.cfg: dict = {}
        self.scale = self.io = self.anpr = self.ctrl = None
        self.cameras: list[Camera] = []
        self._start(cfg)

    # ------------------------------------------------------------------
    def _sim_world(self):
        if not self._sim_started:
            from .sim import SimWorld, start_modbus_server, start_scale_server
            self.sim = SimWorld()
            start_scale_server(self.sim, SIM_SCALE_PORT)
            start_modbus_server(self.sim, SIM_IO_PORT)
            self._sim_started = True
        return self.sim

    def _start(self, cfg: dict):
        cfg = copy.deepcopy(cfg)
        if self.force_simulate:
            cfg["simulate"] = True
        sim = None
        if cfg["simulate"]:
            sim = self._sim_world()
            cfg["scale"]["connection"] = f"tcp://127.0.0.1:{SIM_SCALE_PORT}"
            cfg["io"].update(enabled=True, host="127.0.0.1", port=SIM_IO_PORT, coil_base=0, di_base=0)
            cfg["io"]["relays"] = {"a_red": 0, "a_green": 1, "b_red": 2, "b_green": 3,
                                   "barrier_a": None, "barrier_b": None}
            cfg["io"]["inputs"] = {"ir_a": None, "ir_b": None}
            if not cfg["cameras"]:
                cfg["cameras"] = [{"id": "cam_a", "name": "Въезд А", "side": "a"},
                                  {"id": "cam_b", "name": "Въезд Б", "side": "b"}]
        self.sim = sim
        scale = Scale(cfg["scale"])
        io = IOController(cfg["io"])
        cameras = [Camera(c, source=sim.camera_source(c.get("side"), c["id"]) if sim else None)
                   for c in cfg["cameras"]]
        anpr = ANPRService(cfg["anpr"], cameras, self.tracker, SimRecognizer(sim) if sim else None)
        ctrl = WeighingController(cfg, scale, io, self.tracker, cameras, self.db)
        if self.ctrl is not None:
            ctrl.events.extend(self.ctrl.events)    # журнал событий не теряем
        self.cfg, self.scale, self.io, self.cameras, self.anpr, self.ctrl = cfg, scale, io, cameras, anpr, ctrl
        for part in (scale, io, *cameras, anpr, ctrl):
            part.start()

    def _stop(self):
        for part in (self.ctrl, self.anpr, *self.cameras, self.io, self.scale):
            try:
                part and part.stop()
            except Exception:  # noqa: BLE001
                log.exception("остановка %s", part)

    def reload(self) -> dict:
        """Перечитать config.yaml и перезапустить оборудование."""
        with self.lock:
            new = load_config(self.config_path)
            old_port = int(self.cfg["server"]["port"])
            self._stop()
            self._start(new)
            self.ctrl.event("Настройки применены")
            log.info("Настройки применены")
            return {"restart_needed": int(new["server"]["port"]) != old_port}
