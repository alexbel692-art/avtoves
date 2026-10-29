"""АвтоВес — автомобильные весы с распознаванием номеров и светофорами.

Запуск:  python main.py            (настройки в config.yaml)
         python main.py --simulate (без оборудования, для проверки)
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent
# Модели распознавания, вложенные в установщик (работа без интернета).
# Библиотеки ищут их в «домашней папке» — подставляем папку models только для этого процесса.
_MODELS = BASE_DIR / "models"
if _MODELS.is_dir():
    os.environ["USERPROFILE"] = os.environ["HOME"] = str(_MODELS)

import uvicorn  # noqa: E402

from app import __version__  # noqa: E402

from app.anpr import ANPRService, Camera, SimRecognizer  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db import DB  # noqa: E402
from app.io_control import IOController  # noqa: E402
from app.plates import PlateTracker  # noqa: E402
from app.scale import Scale  # noqa: E402
from app.weighing import WeighingController  # noqa: E402
from app.web import build_app  # noqa: E402


def setup_logging(data_dir: str):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.TimedRotatingFileHandler(Path(data_dir) / "avtoves.log", when="midnight",
                                                   backupCount=30, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def build(cfg: dict) -> SimpleNamespace:
    sim = None
    if cfg["simulate"]:
        from app.sim import SimWorld, start_modbus_server, start_scale_server
        sim = SimWorld()
        start_scale_server(sim, 14001)
        start_modbus_server(sim, 15020)
        cfg["scale"]["connection"] = "tcp://127.0.0.1:14001"
        cfg["io"].update(enabled=True, host="127.0.0.1", port=15020)
        cfg["io"]["relays"] = {"a_red": 0, "a_green": 1, "b_red": 2, "b_green": 3,
                               "barrier_a": None, "barrier_b": None}
        if not cfg["cameras"]:
            cfg["cameras"] = [{"id": "cam_a", "name": "Въезд А", "side": "a"},
                              {"id": "cam_b", "name": "Въезд Б", "side": "b"}]

    db = DB(Path(cfg["data_dir"]) / "avtoves.db")
    scale = Scale(cfg["scale"])
    io = IOController(cfg["io"])
    tracker = PlateTracker()
    cameras = [Camera(c, source=sim.camera_source(c.get("side"), c["id"]) if sim else None)
               for c in cfg["cameras"]]
    anpr = ANPRService(cfg["anpr"], cameras, tracker, SimRecognizer(sim) if sim else None)
    ctrl = WeighingController(cfg, scale, io, tracker, cameras, db)
    ctx = SimpleNamespace(cfg=cfg, db=db, scale=scale, io=io, tracker=tracker, cameras=cameras,
                          anpr=anpr, ctrl=ctrl, sim=sim)
    for part in (scale, io, *cameras, anpr, ctrl):
        part.start()
    return ctx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.simulate:
        cfg["simulate"] = True
    setup_logging(cfg["data_dir"])
    ctx = build(cfg)
    logging.info("АвтоВес %s запущен: http://%s:%s (настройки: %s)", __version__,
                 cfg["server"]["host"], cfg["server"]["port"], cfg.get("_path"))
    uvicorn.run(build_app(ctx), host=cfg["server"]["host"], port=int(cfg["server"]["port"]),
                log_level="warning")


if __name__ == "__main__":
    main()
