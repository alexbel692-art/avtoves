"""Загрузка настроек из config.yaml."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "simulate": False,
    # public_port — внешний порт для интернета (приложение водителя и кабинет логиста); 0 — выключен
    "server": {"host": "0.0.0.0", "port": 8780, "api_token": "", "public_port": 8790},
    # сверка выгрузок с весами: расхождение больше tolerance_pct % (но не меньше tolerance_l литров) — уведомление
    "logist": {"tolerance_pct": 2, "tolerance_l": 100},
    "data_dir": "data",
    "scale": {
        "connection": "tcp://127.0.0.1:4001", "parser": "cas", "data_len": 8,
        "timeout_sec": 3, "min_weight": 400, "empty_weight": 200,
        "stable_time": 3.0, "stable_delta": 50, "use_indicator_stable": True,
    },
    "io": {
        "enabled": True, "device": "moxa", "host": "127.0.0.1", "port": 502, "unit_id": 1,
        "coil_base": 0, "di_base": 0, "out_reg": 470, "out_state_reg": 468, "in_reg": 51,
        "relays": {}, "inputs": {},
        "barrier_pulse_sec": 1.0, "ir_active_level": 1,
    },
    "weighing": {
        "mode": "through", "auto_fix": True, "driver_mode": False, "clear_time": 3.0,
        "trip_max_hours": 24, "plate_window_sec": 60,
    },
    # газовозы: газ в машине = вес − тара пустой машины; литры = кг / плотность
    "gas": {"enabled": True, "name": "СУГ (пропан-бутан)", "density": 0.54, "show_liters": True},
    "anpr": {
        "enabled": True, "engine": "fast_alpr", "detector_model": "yolo-v9-t-384-license-plate-end2end",
        "ocr_model": "cct-xs-v1-global-model", "interval_ms": 300,
        "min_confidence": 0.5, "country": "ru",
    },
    "cameras": [],
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.environ.get("AVTOVES_CONFIG", BASE_DIR / "config.yaml"))
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, raw)
    data_dir = Path(cfg["data_dir"])
    if not data_dir.is_absolute():
        data_dir = path.parent / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "snapshots").mkdir(exist_ok=True)
    cfg["data_dir"] = str(data_dir)
    cfg["_path"] = str(path)
    return cfg
