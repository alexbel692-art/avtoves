"""Светофоры, шлагбаумы и ИК-датчики через модуль ввода-вывода по Modbus TCP.

Поддерживаются:
  moxa — Moxa ioLogik E1214 / E1214-T: реле R0…R5 — «катушки» (функция 05), входы DI0…DI5 (функция 02);
  owen — ОВЕН МК210-301: выходы DO1…DO8 — битовая маска в регистре 470 (функция 06),
         входы DI1…DI6 — битовая маска в регистре 51 (функция 03).
Номера реле и входов в настройках — как подписаны на корпусе модуля.
"""
from __future__ import annotations

import logging
import threading
import time

from .modbus import ModbusTCP

log = logging.getLogger("io")

LIGHT_OFF, LIGHT_RED, LIGHT_GREEN = "off", "red", "green"

DEVICES = {
    "moxa": {"name": "Moxa ioLogik E1214", "first": 0, "outputs": 6, "inputs": 6,
             "relays": {"a_red": 0, "a_green": 1, "b_red": 2, "b_green": 3, "barrier_a": None, "barrier_b": None}},
    "owen": {"name": "ОВЕН МК210-301", "first": 1, "outputs": 8, "inputs": 6,
             "relays": {"a_red": 1, "a_green": 2, "b_red": 3, "b_green": 4, "barrier_a": None, "barrier_b": None}},
}


class MoxaBackend:
    """Каждое реле — отдельная «катушка»."""
    first = 0

    def __init__(self, client: ModbusTCP, cfg: dict):
        self.c, self.coil_base, self.di_base = client, int(cfg.get("coil_base", 0)), int(cfg.get("di_base", 0))

    def write(self, outputs: dict[int, bool]):
        for n, v in outputs.items():
            self.c.write_coil(self.coil_base + n, v)

    def read_inputs(self, numbers: list[int]) -> dict[int, bool]:
        bits = self.c.read_inputs(self.di_base, max(numbers) + 1)
        return {n: bits[n] for n in numbers}

    def read_outputs(self, count: int) -> list[bool]:
        return self.c.read_coils(self.coil_base, count)


class OwenBackend:
    """Все выходы — одна битовая маска (бит 0 = DO1). Храним маску у себя и пишем целиком."""
    first = 1

    def __init__(self, client: ModbusTCP, cfg: dict):
        self.c = client
        self.out_reg, self.out_state_reg = int(cfg.get("out_reg", 470)), int(cfg.get("out_state_reg", 468))
        self.in_reg = int(cfg.get("in_reg", 51))
        self.mask = 0
        self.lock = threading.Lock()

    def write(self, outputs: dict[int, bool]):
        with self.lock:
            for n, v in outputs.items():
                bit = 1 << (n - 1)
                self.mask = (self.mask | bit) if v else (self.mask & ~bit)
            self.c.write_register(self.out_reg, self.mask)

    def read_inputs(self, numbers: list[int]) -> dict[int, bool]:
        m = self.c.read_holding(self.in_reg, 1)[0]
        return {n: bool(m >> (n - 1) & 1) for n in numbers}

    def read_outputs(self, count: int) -> list[bool]:
        m = self.c.read_holding(self.out_state_reg, 1)[0]
        return [bool(m >> i & 1) for i in range(count)]


def make_backend(cfg: dict, client: ModbusTCP):
    return OwenBackend(client, cfg) if cfg.get("device") == "owen" else MoxaBackend(client, cfg)


class IOController:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled"))
        self.device = "owen" if cfg.get("device") == "owen" else "moxa"
        self.relays: dict = cfg.get("relays") or {}
        self.inputs_map: dict = cfg.get("inputs") or {}
        self.client = ModbusTCP(cfg["host"], int(cfg["port"]), int(cfg["unit_id"])) if self.enabled else None
        self.backend = make_backend(cfg, self.client) if self.enabled else None
        self.lights = {"a": LIGHT_RED, "b": LIGHT_RED}
        self.inputs: dict[str, bool] = {}
        self.connected = not self.enabled
        self.error = ""
        self.manual = False            # ручной режим: автоматика не трогает светофоры
        self._hold_until = 0.0         # пауза записи (проверка реле из настроек)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._poll, name="io", daemon=True)

    def start(self):
        self._thr.start()
        self.apply()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    def set_light(self, side: str, state: str, *, force: bool = False):
        if self.manual and not force:
            return
        with self._lock:
            if self.lights.get(side) == state:
                return
            self.lights[side] = state
        log.info("Светофор %s -> %s", side.upper(), state)
        self.apply()

    def _outputs(self, values: dict[str, bool]) -> dict[int, bool]:
        return {int(self.relays[k]): v for k, v in values.items() if self.relays.get(k) is not None}

    def hold(self, sec: float):
        """Не перезаписывать выходы sec секунд — чтобы проверка реле из настроек не сбрасывалась."""
        self._hold_until = time.time() + sec

    def apply(self):
        """Записать состояние светофоров в реле (повторяется при каждом опросе)."""
        if not self.enabled or time.time() < self._hold_until:
            return
        vals = {}
        for side in ("a", "b"):
            st = self.lights[side]
            vals[f"{side}_red"] = st == LIGHT_RED
            vals[f"{side}_green"] = st == LIGHT_GREEN
        try:
            self.backend.write(self._outputs(vals))
            self.connected, self.error = True, ""
        except Exception as e:  # noqa: BLE001
            self.connected, self.error = False, str(e)

    def pulse_barrier(self, side: str):
        name = f"barrier_{side}"
        if self.relays.get(name) is None or not self.enabled:
            return

        def _do():
            try:
                self.backend.write(self._outputs({name: True}))
                time.sleep(float(self.cfg.get("barrier_pulse_sec", 1.0)))
                self.backend.write(self._outputs({name: False}))
            except Exception as e:  # noqa: BLE001
                log.warning("Шлагбаум %s: %s", side, e)

        threading.Thread(target=_do, daemon=True).start()

    def ir_blocked(self) -> bool:
        """True, если хоть один ИК-барьер перекрыт (машина стоит не полностью)."""
        return any(self.inputs.get(k) for k in ("ir_a", "ir_b") if self.inputs_map.get(k) is not None)

    def _poll(self):
        active = int(self.cfg.get("ir_active_level", 1))
        used = {k: int(v) for k, v in self.inputs_map.items() if v is not None}
        while not self._stop.wait(0.5):
            if not self.enabled:
                continue
            try:
                if used:
                    bits = self.backend.read_inputs(list(used.values()))
                    self.inputs = {k: bits[n] == bool(active) for k, n in used.items()}
                self.apply()   # переподтверждаем состояние (на случай перезагрузки модуля)
            except Exception as e:  # noqa: BLE001
                self.connected, self.error = False, str(e)

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled, "connected": self.connected, "error": self.error, "device": self.device,
            "lights": dict(self.lights), "inputs": dict(self.inputs), "manual": self.manual,
            "barriers": [s for s in ("a", "b") if self.relays.get(f"barrier_{s}") is not None],
        }
