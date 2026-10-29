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

BASE_DIR = Path(__file__).resolve().parent
# Модели распознавания, вложенные в установщик (работа без интернета).
# Библиотеки ищут их в «домашней папке» — подставляем папку models только для этого процесса.
_MODELS = BASE_DIR / "models"
if _MODELS.is_dir():
    os.environ["USERPROFILE"] = os.environ["HOME"] = str(_MODELS)

import uvicorn  # noqa: E402

from app import __version__  # noqa: E402

from app.config import load_config  # noqa: E402
from app.runtime import Runtime  # noqa: E402
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg["data_dir"])
    ctx = Runtime(cfg, force_simulate=args.simulate)
    logging.info("АвтоВес %s запущен: http://%s:%s (настройки: %s)", __version__,
                 cfg["server"]["host"], cfg["server"]["port"], cfg.get("_path"))
    uvicorn.run(build_app(ctx), host=cfg["server"]["host"], port=int(cfg["server"]["port"]),
                log_level="warning")


if __name__ == "__main__":
    main()
